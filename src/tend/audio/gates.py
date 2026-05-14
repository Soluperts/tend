"""Gates that govern the wake/sleep boundary on the audio path.

OpenWakeWordGate sits pre-STT; it drops audio while Brain is inactive (after
running each frame through the openWakeWord model), and forwards audio while
Brain is active. On wake-word detection it activates Brain via Hub and emits
a TTSSpeakFrame("Yes?") acknowledgement.

SleepPhraseGate sits post-STT (added in a later task).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import numpy as np
from loguru import logger
from pipecat.frames.frames import Frame, InputAudioRawFrame, TranscriptionFrame, TTSSpeakFrame, UserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from rapidfuzz import fuzz

# openWakeWord's expected window: 80 ms @ 16 kHz. Its hard minimum is 400 samples (25 ms);
# below that, predict() raises. Pipecat's transport emits much smaller chunks, so we buffer.
_OWW_CHUNK_SAMPLES = 1280

# After Brain deactivates, ignore audio for this long before resuming wake-word
# detection. The sleep phrase's trailing audio is still in flight when the
# deactivation lands (Pipecat ships 20 ms frames; STT + sleep-phrase fuzzy match
# add latency), and the openWakeWord model averages a rolling window of
# predictions, so leftover audio can re-fire the wake gate immediately.
_POST_SLEEP_COOLDOWN_S = 1.5


class OpenWakeWordGate(FrameProcessor):
    """Pre-STT audio gate driven by openWakeWord.

    While Brain is inactive, each InputAudioRawFrame is fed to the OWW model.
    If the score for the configured wake word exceeds *threshold*, the gate:
      1. Emits a TTSSpeakFrame(ack_text) acknowledgement downstream.
      2. Calls hub.activate_agent("brain") to bring Brain online.
    The audio frame itself is dropped (not forwarded) whether or not the wake
    word triggered.

    While Brain is active, audio is forwarded directly to STT and the OWW
    model is not invoked (saves CPU).

    All non-audio frames pass through unconditionally.
    """

    def __init__(
        self,
        *,
        model_name: str,
        threshold: float,
        hub,           # the parent agent that owns Brain
        brain,         # the brain agent (we read .active on it)
        ack_text: str = "Yes?",
    ):
        super().__init__()
        self._model_name = model_name
        self._threshold = threshold
        self._hub = hub
        self._brain = brain
        self._ack_text = ack_text
        self._model = self._build_model()
        self._buffer = np.empty(0, dtype=np.int16)
        self._was_active = brain.active
        self._deactivated_at: float | None = None

    def _build_model(self):
        from openwakeword import get_pretrained_model_paths
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        # tflite-runtime ships Linux wheels only — on macOS openwakeword runs
        # through onnxruntime instead, so we must ask for the .onnx variants
        # of the pretrained model paths.
        inference_framework = "onnx" if sys.platform == "darwin" else "tflite"
        all_paths = get_pretrained_model_paths(inference_framework=inference_framework)
        matched = [
            p for p in all_paths
            if os.path.basename(p).startswith(self._model_name)
        ]
        if not matched:
            raise ValueError(
                f"openWakeWord: no pretrained model found for '{self._model_name}'. "
                f"Available: {[os.path.basename(p) for p in all_paths]}"
            )
        path = matched[0]

        # openwakeword ships package code but downloads model weights lazily
        # from its GitHub releases. First run on a fresh install: fetch them.
        if not os.path.exists(path):
            logger.info(
                "[oww] model weights missing on disk; downloading openwakeword "
                "feature + wakeword models (one-time, ~50 MB)..."
            )
            download_models()

        if not os.path.exists(path):
            raise ValueError(
                f"openWakeWord: model file still missing after download: {path}. "
                f"Check network connectivity and disk space."
            )

        logger.info(f"[oww] loading model from {path}")
        try:
            return Model(
                wakeword_model_paths=[path],
                inference_framework=inference_framework,
            )
        except Exception as e:
            logger.error(f"openWakeWord model '{self._model_name}' failed to load: {e!r}")
            raise

    def _get_score(self, scores: dict) -> float:
        """Extract the score for our wake word from a predict() result dict.

        The predict() keys may include a version suffix (e.g. "hey_jarvis_v0.1")
        or match model_name exactly (e.g. in tests). We check for an exact match
        first, then fall back to any key that starts with model_name.
        """
        if self._model_name in scores:
            return scores[self._model_name]
        for key, value in scores.items():
            if key.startswith(self._model_name):
                return value
        return 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, InputAudioRawFrame):
            await self.push_frame(frame, direction)
            return

        # Detect active-state transitions. On any flip, drop our sample buffer
        # so we never feed stale audio across a sleep/wake boundary; on
        # deactivation, also reset openWakeWord's prediction history and stamp
        # the cooldown clock so trailing sleep-phrase audio can't re-trigger.
        if self._brain.active != self._was_active:
            self._was_active = self._brain.active
            self._buffer = np.empty(0, dtype=np.int16)
            if not self._brain.active:
                self._deactivated_at = time.monotonic()
                self._model.reset()

        if self._brain.active:
            # Brain is active — forward audio to STT downstream; skip OWW (saves CPU).
            await self.push_frame(frame, direction)
            return

        # Cooldown after deactivation: drop frames without running OWW.
        if (
            self._deactivated_at is not None
            and time.monotonic() - self._deactivated_at < _POST_SLEEP_COOLDOWN_S
        ):
            return  # frame dropped

        # Brain inactive: buffer audio and feed full chunks to OWW. Drop the frame regardless.
        samples = np.frombuffer(frame.audio, dtype=np.int16)
        self._buffer = np.concatenate([self._buffer, samples])
        while len(self._buffer) >= _OWW_CHUNK_SAMPLES:
            chunk = self._buffer[:_OWW_CHUNK_SAMPLES]
            self._buffer = self._buffer[_OWW_CHUNK_SAMPLES:]
            scores = self._model.predict(chunk)
            score = self._get_score(scores)
            if score >= self._threshold:
                logger.info(f"[wake] openWakeWord triggered (score={score:.3f})")
                await self.push_frame(TTSSpeakFrame(self._ack_text), direction)
                await self._hub.activate_agent("brain")
                self._buffer = np.empty(0, dtype=np.int16)
                break
        # frame dropped (no push_frame for the audio frame itself)


class SleepPhraseGate(FrameProcessor):
    """Post-STT text gate. Detects sleep phrase via fuzzy match; tracks silence timer."""

    def __init__(
        self,
        *,
        sleep_phrase: str,
        fuzz_ratio: float,
        timeout_s: float,
        hub,
        brain,
    ):
        super().__init__()
        self._sleep_phrase = sleep_phrase.lower()
        self._fuzz_ratio = fuzz_ratio
        self._timeout_s = timeout_s
        self._hub = hub
        self._brain = brain
        self._silence_task: asyncio.Task | None = None

    def _is_sleep(self, text: str) -> bool:
        ratio = fuzz.ratio(text.strip().lower(), self._sleep_phrase) / 100.0
        return ratio >= self._fuzz_ratio

    async def _silence_timer(self) -> None:
        try:
            await asyncio.sleep(self._timeout_s)
            if self._brain.active:
                logger.info(f"[sleep] silence timeout ({self._timeout_s}s) -> deactivating brain")
                await self._hub.deactivate_agent("brain")
                await self._hub.on_brain_deactivated()
        except asyncio.CancelledError:
            pass

    def _reset_silence_timer(self) -> None:
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = asyncio.create_task(self._silence_timer())

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not self._brain.active:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, UserStartedSpeakingFrame):
            self._reset_silence_timer()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame):
            if self._is_sleep(frame.text):
                logger.info(f"[sleep] phrase match: {frame.text!r}")
                if self._silence_task and not self._silence_task.done():
                    self._silence_task.cancel()
                await self._hub.deactivate_agent("brain")
                await self._hub.on_brain_deactivated()
                return  # swallow
            # Reset timer on any successful transcription too.
            self._reset_silence_timer()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

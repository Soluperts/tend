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

import numpy as np
from loguru import logger
from pipecat.frames.frames import Frame, InputAudioRawFrame, TranscriptionFrame, TTSSpeakFrame, UserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from rapidfuzz import fuzz


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

    def _build_model(self):
        from openwakeword import get_pretrained_model_paths
        from openwakeword.model import Model

        # Resolve model_name to a file path by searching pretrained models.
        # e.g. "hey_jarvis" matches "hey_jarvis_v0.1.onnx".
        all_paths = get_pretrained_model_paths()
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
        logger.info(f"[oww] loading model from {path}")
        try:
            return Model(wakeword_model_paths=[path])
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

        if self._brain.active:
            # Brain is active — forward audio to STT downstream; skip OWW (saves CPU).
            await self.push_frame(frame, direction)
            return

        # Brain inactive: feed audio to OWW model, drop the frame regardless.
        samples = np.frombuffer(frame.audio, dtype=np.int16)
        scores = self._model.predict(samples)
        score = self._get_score(scores)
        if score >= self._threshold:
            logger.info(f"[wake] openWakeWord triggered (score={score:.3f})")
            await self.push_frame(TTSSpeakFrame(self._ack_text), direction)
            await self._hub.activate_agent("brain")
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
                return  # swallow
            # Reset timer on any successful transcription too.
            self._reset_silence_timer()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

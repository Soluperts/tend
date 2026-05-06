"""Latency loggers — pass-through processors that timestamp key pipeline events.

Place InputLatencyLogger before STT (or shortly after). Place OutputLatencyLogger
after TTS. Both forward all frames; they only emit `[t]` log lines for debugging.
"""

import time

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


_state: dict[str, float | None] = {"start": None, "stop": None, "transcript": None}


class InputLatencyLogger(FrameProcessor):
    """Log timing markers for user speech / STT events. Pass-through."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.monotonic()
        if isinstance(frame, UserStartedSpeakingFrame):
            _state["start"] = now
            logger.debug("[t] user started speaking")
        elif isinstance(frame, UserStoppedSpeakingFrame):
            _state["stop"] = now
            duration = (now - _state["start"]) if _state["start"] else 0
            logger.debug(f"[t] user stopped speaking (utterance: {duration:.2f}s)")
        elif isinstance(frame, TranscriptionFrame):
            _state["transcript"] = now
            stt_lat = (now - _state["stop"]) if _state["stop"] else 0
            logger.debug(f"[t] transcript ready (stt: {stt_lat:.2f}s) -> {frame.text!r}")
        await self.push_frame(frame, direction)


class OutputLatencyLogger(FrameProcessor):
    """Log timing markers for TTS / bot speech events. Pass-through."""

    def __init__(self) -> None:
        super().__init__()
        self._first_audio_seen = True
        self._audio_frames = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.monotonic()
        if isinstance(frame, TTSStartedFrame):
            self._first_audio_seen = False
            self._audio_frames = 0
            logger.debug("[t] tts started")
        elif isinstance(frame, TTSStoppedFrame):
            logger.debug(f"[t] tts stopped ({self._audio_frames} audio frames)")
        elif isinstance(frame, BotStartedSpeakingFrame):
            logger.debug("[t] bot started speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            logger.debug("[t] bot stopped speaking")
        elif isinstance(frame, TTSAudioRawFrame):
            self._audio_frames += 1
            if not self._first_audio_seen:
                self._first_audio_seen = True
                t_transcript = _state["transcript"]
                t_stop = _state["stop"]
                tta = (now - t_transcript) if t_transcript else 0
                ete = (now - t_stop) if t_stop else 0
                logger.debug(
                    f"[t] first audio out (transcript→audio: {tta:.2f}s, "
                    f"end-to-end: {ete:.2f}s, sr={frame.sample_rate}, "
                    f"ch={frame.num_channels}, bytes={len(frame.audio)})"
                )
        await self.push_frame(frame, direction)

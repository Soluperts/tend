"""Listen on the configured input device, transcribe with Whisper, print text.

Usage: .venv/bin/python scripts/test_listen.py
Speak after you see "Listening...", press Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from hasat.config import settings


class Printer(FrameProcessor):
    _frames = 0
    _peak = 0

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            import struct
            samples = struct.unpack(f"{len(frame.audio) // 2}h", frame.audio)
            m = max(abs(x) for x in samples) if samples else 0
            self._frames += 1
            if m > self._peak:
                self._peak = m
            if self._frames % 50 == 0:
                print(f"[audio frames: {self._frames}, running peak: {self._peak}]", flush=True)
        elif isinstance(frame, UserStartedSpeakingFrame):
            print("[VAD: user started speaking]", flush=True)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            print("[VAD: user stopped speaking]", flush=True)
        elif isinstance(frame, TranscriptionFrame):
            print(f"\n>>> {frame.text!r}\n", flush=True)
        await self.push_frame(frame, direction)


async def main() -> None:
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=False,
            audio_in_sample_rate=settings.sample_rate,
            input_device_index=settings.input_device_index,
        )
    )
    pipeline = Pipeline([
        transport.input(),
        VADProcessor(vad_analyzer=SileroVADAnalyzer()),
        WhisperSTTService(
            settings=WhisperSTTService.Settings(model=settings.whisper_model),
            device="cpu",
            compute_type="int8",
        ),
        Printer(),
    ])
    task = PipelineTask(pipeline)
    print("Listening... speak into the mic. Ctrl+C to stop.", flush=True)
    await PipelineRunner().run(task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

"""Speak a sentence through the configured Piper voice and audio output.

Usage: .venv/bin/python scripts/test_speak.py "hello world"
"""
from __future__ import annotations

import asyncio
import sys

from pipecat.frames.frames import EndFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.services.piper.tts import PiperTTSService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from hasat.config import settings


async def main() -> None:
    text = " ".join(sys.argv[1:]) or "Hello from hasat. The pipeline is alive."

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=False,
            audio_out_enabled=True,
            audio_out_sample_rate=settings.sample_rate,
            output_device_index=settings.output_device_index,
        )
    )
    pipeline = Pipeline([
        PiperTTSService(settings=PiperTTSService.Settings(voice=settings.piper_voice)),
        transport.output(),
    ])
    task = PipelineTask(pipeline)
    await task.queue_frame(TTSSpeakFrame(text))
    await task.queue_frame(EndFrame())
    await PipelineRunner().run(task)


if __name__ == "__main__":
    asyncio.run(main())

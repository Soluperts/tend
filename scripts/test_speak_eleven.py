"""Speak a sentence through ElevenLabs TTS to verify the cloud path works.

Usage: .venv/bin/python scripts/test_speak_eleven.py "hello world"
"""
from __future__ import annotations

import asyncio
import sys

from loguru import logger
from pipecat.frames.frames import EndFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from hasat.config import settings

logger.remove()
logger.add(sys.stderr, level="DEBUG")


async def main() -> None:
    text = " ".join(sys.argv[1:]) or "Hello from eleven labs."
    assert settings.elevenlabs_api_key, "ELEVENLABS_API_KEY not set"

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=False,
            audio_out_enabled=True,
            audio_out_sample_rate=24000,
            output_device_index=settings.output_device_index,
        )
    )
    pipeline = Pipeline([
        ElevenLabsTTSService(
            api_key=settings.elevenlabs_api_key,
            sample_rate=24000,
            settings=ElevenLabsTTSService.Settings(
                voice=settings.elevenlabs_voice_id,
                model=settings.elevenlabs_model,
            ),
        ),
        transport.output(),
    ])
    task = PipelineTask(pipeline)
    await task.queue_frame(TTSSpeakFrame(text))
    await task.queue_frame(EndFrame())
    await PipelineRunner().run(task)


if __name__ == "__main__":
    asyncio.run(main())

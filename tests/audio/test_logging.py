# SPDX-License-Identifier: MIT
"""Tests for the latency loggers — they should pass frames through and not raise."""

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from tend.audio.logging import InputLatencyLogger, OutputLatencyLogger


@pytest.fixture
def captured_frames():
    return []


async def _drive(processor, frame, captured):
    async def fake_push(f, direction=FrameDirection.DOWNSTREAM):
        captured.append(f)
    processor.push_frame = fake_push
    await processor.process_frame(frame, FrameDirection.DOWNSTREAM)


async def test_input_latency_logger_passes_frames_through(captured_frames):
    log = InputLatencyLogger()
    frames = [
        UserStartedSpeakingFrame(),
        UserStoppedSpeakingFrame(),
        TranscriptionFrame(text="hello", user_id="u", timestamp=""),
    ]
    for f in frames:
        await _drive(log, f, captured_frames)
    assert len(captured_frames) == len(frames)


async def test_output_latency_logger_passes_frames_through(captured_frames):
    log = OutputLatencyLogger()
    frames = [
        TTSStartedFrame(),
        TTSAudioRawFrame(audio=b"\x00" * 320, sample_rate=24000, num_channels=1),
        TTSStoppedFrame(),
        BotStartedSpeakingFrame(),
        BotStoppedSpeakingFrame(),
    ]
    for f in frames:
        await _drive(log, f, captured_frames)
    assert len(captured_frames) == len(frames)

# SPDX-License-Identifier: MIT
"""Tests for StereoToMonoLeft — drops XVF3800 echo-reference channel."""

from __future__ import annotations

import struct

import pytest
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from tend.audio.channels import StereoToMonoLeft, _stereo_to_mono_left


def _stereo_pcm(*pairs: tuple[int, int]) -> bytes:
    """Build interleaved 16-bit stereo PCM from (L, R) tuples."""
    return b"".join(struct.pack("<hh", l, r) for l, r in pairs)


def _mono_pcm(*samples: int) -> bytes:
    return b"".join(struct.pack("<h", s) for s in samples)


def test_stereo_to_mono_left_keeps_only_left_samples():
    stereo = _stereo_pcm((100, -1000), (200, -2000), (300, -3000))
    mono = _stereo_to_mono_left(stereo)
    assert mono == _mono_pcm(100, 200, 300)


def test_stereo_to_mono_left_handles_empty_input():
    assert _stereo_to_mono_left(b"") == b""


async def _run(processor: StereoToMonoLeft, frame: Frame) -> list[Frame]:
    """Drive the processor for one frame and return the frames it pushed."""
    captured: list[Frame] = []

    async def fake_push(f, direction=FrameDirection.DOWNSTREAM):
        captured.append(f)

    processor.push_frame = fake_push  # type: ignore[assignment]
    await processor.process_frame(frame, FrameDirection.DOWNSTREAM)
    return captured


async def test_processor_rewrites_stereo_input_to_mono_left():
    processor = StereoToMonoLeft()
    stereo_frame = InputAudioRawFrame(
        audio=_stereo_pcm((1000, -32000), (2000, -32000)),
        sample_rate=16000,
        num_channels=2,
    )
    pushed = await _run(processor, stereo_frame)
    assert len(pushed) == 1
    out = pushed[0]
    assert isinstance(out, InputAudioRawFrame)
    assert out.num_channels == 1
    assert out.sample_rate == 16000
    assert out.audio == _mono_pcm(1000, 2000)


async def test_processor_passthrough_for_mono_input():
    processor = StereoToMonoLeft()
    mono_frame = InputAudioRawFrame(
        audio=_mono_pcm(1, 2, 3, 4),
        sample_rate=16000,
        num_channels=1,
    )
    pushed = await _run(processor, mono_frame)
    assert len(pushed) == 1
    assert pushed[0] is mono_frame  # exact same object, not a rewrite


async def test_processor_passthrough_for_non_input_audio_frames():
    processor = StereoToMonoLeft()
    text = TextFrame(text="hi")
    out_audio = OutputAudioRawFrame(
        audio=_mono_pcm(1, 2),
        sample_rate=16000,
        num_channels=1,
    )
    pushed = await _run(processor, text)
    assert pushed == [text]
    pushed = await _run(processor, out_audio)
    assert pushed == [out_audio]

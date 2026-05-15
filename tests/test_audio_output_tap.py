# SPDX-License-Identifier: MIT
"""Tests for tend.audio.output_tap.OutputAudioCapture."""

from __future__ import annotations

import pytest
from pipecat.frames.frames import OutputAudioRawFrame, TextFrame
from pipecat.processors.frame_processor import FrameDirection

from tend.audio.aec import ReferenceBuffer
from tend.audio.output_tap import OutputAudioCapture


class _Sink:
    """Captures pushed frames for assertion."""

    def __init__(self):
        self.frames = []

    async def queue_frame(self, frame, direction):
        self.frames.append((frame, direction))


@pytest.mark.asyncio
async def test_output_audio_capture_appends_audio_to_reference():
    rb = ReferenceBuffer(capacity_bytes=64)
    cap = OutputAudioCapture(reference=rb)
    sink = _Sink()
    # Patch push_frame to route frames to the sink, bypassing the pipecat
    # pipeline machinery (StartFrame / clock) that is not needed here.
    # All other FrameProcessor tests in this repo use this same pattern.
    async def _push(frame, direction=FrameDirection.DOWNSTREAM):
        await sink.queue_frame(frame, direction)

    cap.push_frame = _push  # type: ignore[method-assign]

    frame = OutputAudioRawFrame(
        audio=b"\x11" * 16, sample_rate=16000, num_channels=1,
    )
    await cap.process_frame(frame, FrameDirection.DOWNSTREAM)

    # The audio bytes should be in the buffer.
    assert rb.read(16) == b"\x11" * 16
    # And the frame should be passed through.
    assert len(sink.frames) == 1
    assert sink.frames[0][0] is frame


@pytest.mark.asyncio
async def test_output_audio_capture_passes_through_non_output_frames():
    rb = ReferenceBuffer(capacity_bytes=64)
    cap = OutputAudioCapture(reference=rb)
    sink = _Sink()
    async def _push(frame, direction=FrameDirection.DOWNSTREAM):
        await sink.queue_frame(frame, direction)

    cap.push_frame = _push  # type: ignore[method-assign]

    frame = TextFrame(text="hi")
    await cap.process_frame(frame, FrameDirection.DOWNSTREAM)

    # Reference buffer stays empty.
    assert rb.read(4) == b"\x00\x00\x00\x00"
    # Frame still passes through.
    assert len(sink.frames) == 1
    assert sink.frames[0][0] is frame

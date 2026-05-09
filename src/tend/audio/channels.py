"""Stereo→mono channel selector for the XVF3800's 2-channel input.

The reSpeaker XVF3800 exposes a 2-channel UAC2 stream:
  - LEFT  channel: AEC-processed audio (clean, suitable for STT).
  - RIGHT channel: speaker-side reference signal (raw echo).

Capturing both and downmixing — which is what PortAudio + ALSA `plughw`
does for a mono-requesting client — defeats the chip's AEC by averaging
the clean processed audio with the reference echo. This processor sits
just after `transport.input()` and converts each stereo
`InputAudioRawFrame` into a mono frame containing the left channel only.

Frames that are not stereo `InputAudioRawFrame`s pass through unchanged.
"""

from __future__ import annotations

import numpy as np
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


def _stereo_to_mono_left(stereo_pcm16: bytes) -> bytes:
    """Drop the right channel from interleaved 16-bit stereo PCM."""
    samples = np.frombuffer(stereo_pcm16, dtype=np.int16).reshape(-1, 2)
    return samples[:, 0].tobytes()


class StereoToMonoLeft(FrameProcessor):
    """Pass-through except for 2-channel `InputAudioRawFrame`s, which it
    rewrites to mono using only the left channel's samples."""

    async def process_frame(
        self, frame: Frame, direction: FrameDirection,
    ) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and frame.num_channels == 2:
            mono = InputAudioRawFrame(
                audio=_stereo_to_mono_left(frame.audio),
                sample_rate=frame.sample_rate,
                num_channels=1,
            )
            await self.push_frame(mono, direction)
            return
        await self.push_frame(frame, direction)

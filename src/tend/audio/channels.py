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

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from tend.config import Settings


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


@dataclass(frozen=True)
class AudioPath:
    """Result of platform-aware audio-path selection."""
    in_channels: int
    pre_vad_processors: tuple[FrameProcessor, ...]
    aec_filter: Optional[BaseAudioFilter]


def select_audio_path(
    settings: Settings,
    *,
    aec_filter_factory: Callable[..., Optional[BaseAudioFilter]],
) -> AudioPath:
    """Return the audio-path tuple appropriate for this platform.

    `aec_filter_factory` is injected so this function can be unit-tested
    without pulling in the AEC engine; production callers pass
    `tend.audio.aec.make_aec_filter`.
    """
    import sys as _sys

    explicit = settings.mic_channels
    if explicit == 2 or (explicit is None and _sys.platform != "darwin"):
        return AudioPath(
            in_channels=2,
            pre_vad_processors=(StereoToMonoLeft(),),
            aec_filter=aec_filter_factory(settings) if _sys.platform == "darwin" else None,
        )
    return AudioPath(
        in_channels=1,
        pre_vad_processors=(),
        aec_filter=aec_filter_factory(settings),
    )

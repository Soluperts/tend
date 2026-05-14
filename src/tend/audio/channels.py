"""Audio path selection for the Hub pipeline.

This module exposes two things:

  - `StereoToMonoLeft`: a frame processor that converts the
    reSpeaker XVF3800's 2-channel UAC2 stream into mono by
    keeping the left (AEC-processed) channel and dropping the
    right (echo reference).

  - `AudioPath` + `select_audio_path()`: platform-aware dispatch
    that decides the transport's input channel count, the pre-VAD
    processor chain, and whether an AEC filter is installed. The
    Hub consumes whatever this returns instead of hardcoding
    XVF3800 assumptions.
"""

from __future__ import annotations

import sys
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


def _default_aec_filter_factory(settings: Settings) -> Optional[BaseAudioFilter]:
    """Build the AEC filter using the production engine resolver.

    Production callers don't pass this explicitly — `select_audio_path`
    defaults to it. Tests pass a stub factory instead so they don't
    pull in pyaec/webrtc.
    """
    from tend.audio.aec import ReferenceBuffer, make_aec_filter, resolve_aec_engine

    engine = resolve_aec_engine(settings)
    if engine == "off":
        return None
    # Each Hub gets its own buffer; OutputAudioCapture writes into it.
    reference = ReferenceBuffer()
    f = make_aec_filter(engine, reference=reference)
    # Attach the buffer so the Hub can find it to construct OutputAudioCapture.
    f._tend_reference = reference  # type: ignore[attr-defined]
    return f


def select_audio_path(
    settings: Settings,
    *,
    aec_filter_factory: Callable[[Settings], Optional[BaseAudioFilter]] = _default_aec_filter_factory,
) -> AudioPath:
    """Return the audio-path tuple appropriate for this platform.

    `aec_filter_factory` is injected so this function can be unit-tested
    without pulling in the AEC engine; production callers don't pass it
    (the default wires up the real AEC engine). Tests pass a stub factory
    instead so they don't pull in pyaec/webrtc.
    """
    explicit = settings.mic_channels
    if explicit == 2 or (explicit is None and sys.platform != "darwin"):
        return AudioPath(
            in_channels=2,
            pre_vad_processors=(StereoToMonoLeft(),),
            aec_filter=aec_filter_factory(settings),
        )
    return AudioPath(
        in_channels=1,
        pre_vad_processors=(),
        aec_filter=aec_filter_factory(settings),
    )

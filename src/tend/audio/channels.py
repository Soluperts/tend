"""Audio path selection for the Hub pipeline.

This module exposes:

  - `StereoToMonoLeft`: a frame processor that converts the
    reSpeaker XVF3800's 2-channel UAC2 stream into mono by keeping
    the left (AEC-processed) channel and dropping the right (echo
    reference).

  - `AECPair`: a filter + ReferenceBuffer pair returned by the AEC
    factory. The Hub uses the filter on the input side and feeds
    the same reference buffer from the output side via
    `OutputAudioCapture`.

  - `AudioPath` + `select_audio_path()`: platform-aware dispatch
    that decides the transport's input channel count, the pre-VAD
    processor chain, and whether an AEC pair is installed.

  - `_default_aec_filter_factory`: production callable that
    `select_audio_path` uses when no explicit factory is passed.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.local.audio import LocalAudioTransport

from tend.audio.aec import ReferenceBuffer, resolve_aec_engine
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
class AECPair:
    """Pairs an AEC filter with the ReferenceBuffer that feeds it.

    The factory returns this so the Hub can construct OutputAudioCapture
    against the same buffer the filter reads from. Encodes the
    filter↔reference matched-pair invariant in the type.
    """
    filter: BaseAudioFilter
    reference: ReferenceBuffer


@dataclass(frozen=True)
class AudioPath:
    """Result of platform-aware audio-path selection."""
    in_channels: int
    pre_vad_processors: tuple[FrameProcessor, ...]
    aec: AECPair | None
    transport_factory: Callable[[TransportParams], BaseTransport] = LocalAudioTransport


def _default_aec_filter_factory(settings: Settings) -> AECPair | None:
    """Build the AEC pair using the production engine resolver.

    Returns None when AEC is disabled (e.g. `aec_engine="off"` or the
    `auto` resolver settles on `"off"` on Linux).
    """
    from tend.audio.aec import make_aec_filter, resolve_aec_engine

    engine = resolve_aec_engine(settings)
    if engine == "off":
        return None
    reference = ReferenceBuffer()
    return AECPair(filter=make_aec_filter(engine, reference=reference), reference=reference)


def select_audio_path(
    settings: Settings,
    *,
    aec_filter_factory: Callable[[Settings], AECPair | None] = _default_aec_filter_factory,
) -> AudioPath:
    """Return the audio-path tuple appropriate for this platform.

    `aec_filter_factory` is injected so this function can be unit-tested
    without pulling in the AEC engine; production callers don't pass it
    (the default wires up the real AEC engine). Tests pass a stub factory
    instead so they don't pull in pyaec/webrtc.
    """
    # VPIO short-circuit: AVAudioTransport handles AEC upstream of pipecat,
    # so we return no AEC pair and do NOT invoke the factory.
    if resolve_aec_engine(settings) == "vpio":
        from tend.audio.av_audio import AVAudioTransport
        return AudioPath(
            in_channels=1,
            pre_vad_processors=(),
            aec=None,
            transport_factory=AVAudioTransport,
        )

    explicit = settings.mic_channels
    if explicit == 2 or (explicit is None and sys.platform != "darwin"):
        return AudioPath(
            in_channels=2,
            pre_vad_processors=(StereoToMonoLeft(),),
            aec=aec_filter_factory(settings),
            transport_factory=LocalAudioTransport,
        )
    return AudioPath(
        in_channels=1,
        pre_vad_processors=(),
        aec=aec_filter_factory(settings),
        transport_factory=LocalAudioTransport,
    )

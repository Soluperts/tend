"""Tests for tend.audio.av_audio — AVAudioTransport and helpers."""

from __future__ import annotations

import sys

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_params_is_transport_params_subclass():
    from pipecat.transports.base_transport import TransportParams
    from tend.audio.av_audio import AVAudioTransportParams

    p = AVAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=16000,
        audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    assert isinstance(p, TransportParams)
    assert p.audio_in_sample_rate == 16000

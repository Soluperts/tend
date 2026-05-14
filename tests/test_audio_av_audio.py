"""Tests for tend.audio.av_audio."""

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


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_int16_pcm_buffer_round_trip():
    """A 320-frame buffer constructed from int16 PCM bytes must read back
    identical bytes via the channelData pointer."""
    import struct

    from tend.audio.av_audio import _int16_pcm_buffer

    pcm = struct.pack("<320h", *range(100, 420))
    buf = _int16_pcm_buffer(pcm, sample_rate=16000)

    assert buf.frameLength() == 320
    assert buf.format().sampleRate() == 16000.0

    mv = bytes(buf.int16ChannelData()[0].as_buffer(320))
    assert mv == pcm


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_int16_bytes_from_buffer_reads_set_frame_length():
    """The reader must respect setFrameLength_, not frameCapacity."""
    import struct

    from tend.audio.av_audio import _int16_bytes_from_buffer, _int16_pcm_buffer

    pcm = struct.pack("<100h", *range(0, 100))
    buf = _int16_pcm_buffer(pcm, sample_rate=16000)
    out = _int16_bytes_from_buffer(buf)
    assert out == pcm
    assert len(out) == 200

"""AVAudioEngine-based transport for macOS, with VoiceProcessingIO enabled.

Why this exists: built-in MacBook mic+speaker generate echo that the speex
software AEC (~12-26 dB) doesn't suppress below Deepgram's transcription
threshold. AVAudioEngine + setVoiceProcessingEnabled gives OS-grade AEC +
NS + AGC — same path FaceTime uses — which drops the residual below
Deepgram's threshold and stops the bot from transcribing its own voice
back into the LLM context.

AVAudioEngine uses CoreAudio device UIDs (strings), not PyAudio indices,
so device-selection fields would require a different shape than
LocalAudioTransportParams — that's why no device fields are added in v1.
"""

from __future__ import annotations

from pipecat.transports.base_transport import TransportParams


class AVAudioTransportParams(TransportParams):
    """Parameters for AVAudioTransport."""


def _int16_pcm_buffer(pcm_bytes: bytes, sample_rate: int) -> "AVAudioPCMBuffer":
    """Wrap raw int16 mono PCM bytes in an AVAudioPCMBuffer at sample_rate.

    PyObjC's varlist exposes the channel-0 storage as a memoryview via
    .as_buffer(n_frames) — slice-assign the PCM bytes into it and set the
    frame length.
    """
    from AVFoundation import (
        AVAudioFormat,
        AVAudioPCMBuffer,
        AVAudioPCMFormatInt16,
    )

    n_frames = len(pcm_bytes) // 2
    fmt = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatInt16, float(sample_rate), 1, False,
    )
    buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(fmt, n_frames)
    buf.setFrameLength_(n_frames)
    mv = buf.int16ChannelData()[0].as_buffer(n_frames)
    mv[:] = pcm_bytes
    return buf


def _int16_bytes_from_buffer(buf: "AVAudioPCMBuffer") -> bytes:
    """Read int16 mono PCM bytes out of an AVAudioPCMBuffer's channelData.

    Respects buf.frameLength() (the populated frame count), not frameCapacity.
    """
    n = buf.frameLength()
    if n == 0:
        return b""
    return bytes(buf.int16ChannelData()[0].as_buffer(n))

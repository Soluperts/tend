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


def _make_input_converter(
    native_format: "AVAudioFormat", target_sample_rate: int,
) -> "AVAudioConverter":
    """Build an AVAudioConverter from the input node's native format
    (typically 9ch Float32 48 kHz under VPIO) to 16 kHz int16 mono.

    The converter's default channelMap on a many-to-one mapping is [-1]
    (drop output), which produces zero-valued buffers. We must explicitly
    set channelMap=[0] to take channel 0 from the input, which under VPIO
    is the post-VPIO mic signal. PoC commit 223ac9d proved this is required.
    """
    from AVFoundation import (
        AVAudioConverter,
        AVAudioFormat,
        AVAudioPCMFormatInt16,
    )

    target = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatInt16, float(target_sample_rate), 1, False,
    )
    conv = AVAudioConverter.alloc().initFromFormat_toFormat_(native_format, target)
    conv.setChannelMap_([0])
    return conv


def _convert_to_int16_bytes(
    converter: "AVAudioConverter",
    in_buf: "AVAudioPCMBuffer",
    *,
    target_sample_rate: int,
) -> bytes:
    """Run one convert pass and return the result as int16 mono bytes.

    Stateful when resampling — keeping the same converter instance across
    tap calls preserves the resampler's interpolation state, avoiding
    clicks at chunk boundaries.
    """
    from AVFoundation import AVAudioPCMBuffer

    target_format = converter.outputFormat()
    in_frames = in_buf.frameLength()
    native_sr = in_buf.format().sampleRate()
    out_cap = max(int(in_frames * target_sample_rate / native_sr) + 64, 1)
    out_buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
        target_format, out_cap,
    )

    supplied = {"done": False}

    def supply_input(num_packets, status_ptr):
        # AVAudioConverterInputStatus_HaveData=0, NoDataNow=1, EndOfStream=2
        if supplied["done"]:
            return (None, 1)
        supplied["done"] = True
        return (in_buf, 0)

    status, err = converter.convertToBuffer_error_withInputFromBlock_(
        out_buf, None, supply_input,
    )
    if status not in (0, 1):
        from loguru import logger
        logger.error(f"AVAudioConverter status={status} err={err}")
        return b""

    return _int16_bytes_from_buffer(out_buf)


def _make_output_converter(
    source_sample_rate: int, target_format: "AVAudioFormat",
) -> "AVAudioConverter":
    """Build an AVAudioConverter from int16 mono PCM at source_sample_rate
    to whatever target_format the output node requires.

    On built-in MacBook hardware with VPIO active, target_format is typically
    2ch Float32 deinterleaved at 48 kHz — the output node's natural format.
    The converter handles channel-doubling (mono → stereo duplicate via
    channelMap=[0, 0]) and resampling in one pass.
    """
    from AVFoundation import (
        AVAudioConverter,
        AVAudioFormat,
        AVAudioPCMFormatInt16,
    )

    source = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatInt16, float(source_sample_rate), 1, False,
    )
    conv = AVAudioConverter.alloc().initFromFormat_toFormat_(source, target_format)
    target_channels = target_format.channelCount()
    conv.setChannelMap_([0] * target_channels)
    return conv


def _convert_int16_buffer_to_float32_buffer(
    converter: "AVAudioConverter",
    src: "AVAudioPCMBuffer",
    *,
    target_format: "AVAudioFormat",
) -> "AVAudioPCMBuffer":
    """One-shot convert an int16 source buffer to the target Float32 format.

    Output capacity is sized for resampling overhead (target/source rate
    ratio plus 64 frames slack).
    """
    from AVFoundation import AVAudioPCMBuffer

    src_frames = src.frameLength()
    src_sr = src.format().sampleRate()
    target_sr = target_format.sampleRate()
    out_cap = max(int(src_frames * target_sr / src_sr) + 64, 1)
    out = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(target_format, out_cap)

    supplied = {"done": False}

    def supply_input(num_packets, status_ptr):
        if supplied["done"]:
            return (None, 1)
        supplied["done"] = True
        return (src, 0)

    status, err = converter.convertToBuffer_error_withInputFromBlock_(
        out, None, supply_input,
    )
    if status not in (0, 1):
        from loguru import logger
        logger.error(f"output AVAudioConverter status={status} err={err}")
    return out

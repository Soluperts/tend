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

import asyncio
from typing import TYPE_CHECKING

from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

if TYPE_CHECKING:
    from AVFoundation import (
        AVAudioConverter,
        AVAudioEngine,
        AVAudioFormat,
        AVAudioPCMBuffer,
        AVAudioPlayerNode,
    )


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
    # Generous output capacity: in_frames * (target/native) + 64 frames
    # slack for the resampler's lookahead window. The +1 minimum guards
    # against degenerate empty inputs.
    out_cap = max(int(in_frames * target_sample_rate / native_sr) + 64, 1)
    out_buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
        target_format, out_cap,
    )

    supplied = {"done": False}

    def supply_input(num_packets, status_ptr):
        # AVAudioConverterInputStatus_HaveData=0, NoDataNow=1, EndOfStream=2
        if supplied["done"]:
            return (None, 1)  # NoDataNow — flush whatever the converter has
        supplied["done"] = True
        return (in_buf, 0)  # HaveData

    status, err = converter.convertToBuffer_error_withInputFromBlock_(
        out_buf, None, supply_input,
    )
    if status not in (0, 1):
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
        # AVAudioConverterInputStatus_HaveData=0, NoDataNow=1, EndOfStream=2
        if supplied["done"]:
            return (None, 1)  # NoDataNow — flush whatever the converter has
        supplied["done"] = True
        return (src, 0)  # HaveData

    status, err = converter.convertToBuffer_error_withInputFromBlock_(
        out, None, supply_input,
    )
    if status not in (0, 1):
        logger.error(f"output AVAudioConverter status={status} err={err}")
    return out


class AVAudioInputTransport(BaseInputTransport):
    """Captures audio from AVAudioEngine's inputNode with VPIO enabled.

    The transport doesn't own the engine — AVAudioTransport does. We get
    the engine handed to us in the constructor so input and output share
    one engine instance.
    """

    _params: AVAudioTransportParams

    # +12 dB linear gain. VPIO noise-gates user speech ~12 dB at desk
    # distance (validated in PoC commit 223ac9d). Residual echo stays at
    # -78 dB after this boost — well below STT thresholds.
    _POST_TAP_GAIN = 4.0  # ≈ 10 ** (12.0 / 20.0)

    def __init__(
        self,
        params: AVAudioTransportParams,
        *,
        engine: "AVAudioEngine",
    ):
        super().__init__(params)
        self._engine = engine
        self._input_node = None
        self._converter: "AVAudioConverter | None" = None
        self._target_sample_rate = 0
        self._tap_installed = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._tap_installed:
            return

        self._target_sample_rate = (
            self._params.audio_in_sample_rate or frame.audio_in_sample_rate
        )
        self._input_node = self._engine.inputNode()

        # VPIO MUST be enabled BEFORE engine.start() and before any other
        # configuration. It cannot toggle while the engine is running.
        ok, err = self._input_node.setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise RuntimeError(
                f"setVoiceProcessingEnabled returned False; error={err}. "
                "Voice processing (AEC + NS + AGC) cannot be activated on "
                "this Mac. macOS 10.15+ required."
            )
        logger.info(
            f"[av_audio] VPIO enabled: "
            f"isVoiceProcessingEnabled={self._input_node.isVoiceProcessingEnabled()}"
        )

        native_format = self._input_node.outputFormatForBus_(0)
        # Under VPIO this is typically 9 channels (mic + speaker refs) at
        # 48 kHz Float32 deinterleaved. The converter takes channel 0 only
        # (the post-VPIO mic) via the explicit channelMap=[0] set in
        # _make_input_converter.
        logger.info(
            f"[av_audio] native input format: "
            f"sr={native_format.sampleRate()} ch={native_format.channelCount()}"
        )
        self._converter = _make_input_converter(
            native_format, target_sample_rate=self._target_sample_rate,
        )

        loop = asyncio.get_running_loop()
        target_sr = self._target_sample_rate
        converter = self._converter
        push = self.push_audio_frame
        gain = self._POST_TAP_GAIN

        import numpy as _np

        def tap_callback(in_buf, when):
            try:
                pcm = _convert_to_int16_bytes(
                    converter, in_buf, target_sample_rate=target_sr,
                )
            except Exception as exc:
                logger.exception(f"[av_audio] tap conversion failed: {exc}")
                return
            if not pcm:
                return
            # Post-VPIO gain. Multiply with int32 headroom then clip back
            # to int16 to avoid wrap-around at peaks.
            samples = _np.frombuffer(pcm, dtype=_np.int16).astype(_np.int32)
            boosted = _np.clip(samples * gain, -32768, 32767).astype(_np.int16)
            audio_frame = InputAudioRawFrame(
                audio=boosted.tobytes(),
                sample_rate=target_sr,
                num_channels=1,
            )
            asyncio.run_coroutine_threadsafe(push(audio_frame), loop)

        # Tap at NATIVE 9-channel Float32 48 kHz format. The converter
        # downmixes + resamples; post-tap gain runs in tap_callback.
        # Installing at a different format than the bus is unreliable per
        # Apple's docs and produced silent buffers in PoC testing.
        self._input_node.installTapOnBus_bufferSize_format_block_(
            0, 1024, native_format, tap_callback,
        )
        self._tap_installed = True

        # Idempotent: engine.start() returns True if already running.
        started, err = self._engine.startAndReturnError_(None)
        if not started:
            raise RuntimeError(f"engine.startAndReturnError_ failed: {err}")

        await self.set_transport_ready(frame)

    async def cleanup(self):
        await super().cleanup()
        if self._tap_installed and self._input_node is not None:
            self._input_node.removeTapOnBus_(0)
            self._tap_installed = False
        # Engine.stop() is the AVAudioTransport's responsibility (Task 11)
        # because input + output share one engine.
        self._input_node = None
        self._converter = None


def _make_player_node() -> "AVAudioPlayerNode":
    """Factory so tests can inject a fake player without monkey-patching AVFoundation."""
    from AVFoundation import AVAudioPlayerNode
    return AVAudioPlayerNode.alloc().init()


class AVAudioOutputTransport(BaseOutputTransport):
    """Plays audio via AVAudioPlayerNode → outputNode (direct, no mixer).

    Under VPIO, the mainMixerNode's default 44.1 kHz format conflicts
    with the outputNode's 48 kHz, and engine.start() fails with
    err=-10875. So the player connects directly to outputNode at
    outputNode.inputFormat(forBus: 0) (typically 2ch Float32 48 kHz
    deinterleaved on built-in MacBook speakers).

    write_audio_frame (Task 9) converts incoming int16 mono PCM to the
    output node's format via _convert_int16_buffer_to_float32_buffer,
    then schedules with .dataPlayedBack completion type for realtime-
    paced backpressure.
    """

    _params: AVAudioTransportParams

    def __init__(
        self,
        params: AVAudioTransportParams,
        *,
        engine: "AVAudioEngine",
    ):
        super().__init__(params)
        self._engine = engine
        self._player: "AVAudioPlayerNode | None" = None
        self._output_format: "AVAudioFormat | None" = None
        self._converter: "AVAudioConverter | None" = None
        self._sample_rate = 0
        self._started = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._started:
            return

        self._sample_rate = (
            self._params.audio_out_sample_rate or frame.audio_out_sample_rate
        )
        output_node = self._engine.outputNode()
        # The output node's input format is what the speaker hardware
        # demands under VPIO — typically 2ch 48 kHz Float32 deinterleaved.
        self._output_format = output_node.inputFormatForBus_(0)
        logger.info(
            f"[av_audio] output node format: "
            f"sr={self._output_format.sampleRate()} "
            f"ch={self._output_format.channelCount()}"
        )

        self._player = _make_player_node()
        self._engine.attachNode_(self._player)
        # DIRECT connect to outputNode, skipping mainMixerNode (its 44.1 kHz
        # default conflicts with VPIO's 48 kHz output).
        self._engine.connect_to_format_(
            self._player, output_node, self._output_format,
        )

        self._converter = _make_output_converter(
            source_sample_rate=self._sample_rate,
            target_format=self._output_format,
        )

        # Idempotent — input transport may already have started the engine.
        started, err = self._engine.startAndReturnError_(None)
        if not started:
            raise RuntimeError(f"engine.startAndReturnError_ failed: {err}")
        self._player.play()
        self._started = True
        await self.set_transport_ready(frame)

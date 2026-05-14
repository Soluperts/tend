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


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_converter_downsamples_float32_to_int16_at_16k():
    """Converter takes a 48 kHz Float32 buffer and produces 16 kHz int16 mono."""
    import math
    import struct

    from AVFoundation import (
        AVAudioFormat,
        AVAudioPCMBuffer,
        AVAudioPCMFormatFloat32,
    )

    from tend.audio.av_audio import _make_input_converter, _convert_to_int16_bytes

    native = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatFloat32, 48000.0, 1, False,
    )
    in_buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(native, 480)
    in_buf.setFrameLength_(480)
    mv = in_buf.floatChannelData()[0].as_buffer(480)
    samples = b"".join(
        struct.pack("<f", 0.3 * math.sin(2 * math.pi * 1000 * i / 48000))
        for i in range(480)
    )
    mv[:] = samples

    converter = _make_input_converter(native, target_sample_rate=16000)
    out_bytes = _convert_to_int16_bytes(converter, in_buf, target_sample_rate=16000)

    # 480 frames @ 48k → 160 frames @ 16k → 320 bytes ± resampler windowing
    assert 280 <= len(out_bytes) <= 360


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_input_converter_sets_channel_map_to_zero():
    """Without this, AVAudioConverter's default channelMap on a many-to-one
    mapping is [-1] which drops the output to zero — silent capture.

    Uses 2ch input (smallest valid many-to-one scenario; 9ch requires
    the real VPIO engine layout which CoreAudio rejects in unit tests).
    """
    from AVFoundation import AVAudioFormat, AVAudioPCMFormatFloat32
    from tend.audio.av_audio import _make_input_converter

    native = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatFloat32, 48000.0, 2, False,
    )
    conv = _make_input_converter(native, target_sample_rate=16000)
    cm = conv.channelMap()
    assert list(cm) == [0]


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_output_converter_int16_mono_to_float32_stereo_48k():
    """Output side: convert 16 kHz int16 mono → 48 kHz Float32 stereo
    (the format the output node demands under VPIO)."""
    import math
    import struct

    from AVFoundation import AVAudioFormat, AVAudioPCMFormatFloat32

    from tend.audio.av_audio import (
        _int16_pcm_buffer,
        _make_output_converter,
        _convert_int16_buffer_to_float32_buffer,
    )

    pcm = struct.pack(
        "<320h",
        *(int(0.3 * 32767 * math.sin(2 * math.pi * 1000 * i / 16000)) for i in range(320)),
    )
    src = _int16_pcm_buffer(pcm, sample_rate=16000)

    target = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatFloat32, 48000.0, 2, False,
    )
    converter = _make_output_converter(source_sample_rate=16000, target_format=target)
    out = _convert_int16_buffer_to_float32_buffer(converter, src, target_format=target)

    # 320 frames @ 16k → 960 frames @ 48k (±resampler windowing)
    assert 850 <= out.frameLength() <= 1050
    assert out.format().channelCount() == 2
    fc = out.floatChannelData()
    ch0 = bytes(fc[0].as_buffer(out.frameLength()))
    assert any(b != 0 for b in ch0[:32])


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
@pytest.mark.asyncio
async def test_input_transport_start_enables_vpio_and_installs_tap():
    """start() must enable VPIO on the input node BEFORE engine.start.
    Order matters — VPIO can't toggle while engine is running."""
    from unittest.mock import MagicMock, patch

    from pipecat.frames.frames import StartFrame
    from tend.audio.av_audio import AVAudioInputTransport, AVAudioTransportParams

    # Use a shared call log to capture cross-mock call ordering.
    call_log: list[str] = []

    fake_input = MagicMock(name="fake_input_node")
    fake_input.setVoiceProcessingEnabled_error_.side_effect = (
        lambda enabled, err: call_log.append("setVoiceProcessingEnabled") or (True, None)
    )
    fake_input.installTapOnBus_bufferSize_format_block_.side_effect = (
        lambda *a: call_log.append("installTapOnBus")
    )
    fake_input.outputFormatForBus_.return_value = MagicMock(
        name="native_format", sampleRate=lambda: 48000.0, channelCount=lambda: 9,
    )

    fake_engine = MagicMock(name="fake_engine")
    fake_engine.inputNode.return_value = fake_input
    fake_engine.startAndReturnError_.side_effect = (
        lambda err: call_log.append("startAndReturnError") or (True, None)
    )

    params = AVAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=False,
        audio_in_sample_rate=16000,
        audio_in_channels=1,
        audio_out_sample_rate=16000,
    )

    inp = AVAudioInputTransport(params, engine=fake_engine)
    frame = StartFrame(
        audio_in_sample_rate=16000,
        audio_out_sample_rate=16000,
    )
    # Patch the converter factory so the test doesn't touch real AVFoundation
    # with a mock native_format (AVAudioConverter rejects non-ObjC objects).
    # Also patch set_transport_ready to skip pipeline infrastructure (audio task
    # creation) that requires a fully initialized pipecat TaskManager.
    with patch("tend.audio.av_audio._make_input_converter", return_value=MagicMock()), \
         patch.object(inp, "set_transport_ready", return_value=None):
        await inp.start(frame)

    # Verify call order: setVoiceProcessingEnabled → installTap → engine.start.
    vpio_idx = call_log.index("setVoiceProcessingEnabled")
    tap_idx = call_log.index("installTapOnBus")
    start_idx = call_log.index("startAndReturnError")
    assert vpio_idx < tap_idx < start_idx


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
@pytest.mark.asyncio
async def test_input_transport_tap_callback_pushes_frame_with_gain(monkeypatch):
    """When the tap callback fires from a worker thread, the bridge pushes
    an InputAudioRawFrame whose PCM is the input × 4 (post-tap +12 dB gain)."""
    import asyncio
    import struct
    import threading
    from unittest.mock import MagicMock

    from pipecat.frames.frames import InputAudioRawFrame, StartFrame
    from tend.audio.av_audio import AVAudioInputTransport, AVAudioTransportParams

    fake_engine = MagicMock()
    fake_engine.startAndReturnError_.return_value = (True, None)
    fake_input = MagicMock()
    fake_input.setVoiceProcessingEnabled_error_.return_value = (True, None)
    fake_input.outputFormatForBus_.return_value = MagicMock(
        sampleRate=lambda: 48000.0, channelCount=lambda: 9,
    )
    fake_engine.inputNode.return_value = fake_input

    # Stub the conversion path. Pre-gain PCM samples; expect 4× after gain.
    raw_pcm = struct.pack("<5h", 100, 200, 300, 400, 500)
    expected_after_gain = struct.pack("<5h", 400, 800, 1200, 1600, 2000)
    monkeypatch.setattr(
        "tend.audio.av_audio._make_input_converter",
        lambda *a, **kw: MagicMock(),
    )
    monkeypatch.setattr(
        "tend.audio.av_audio._convert_to_int16_bytes",
        lambda *a, **kw: raw_pcm,
    )

    params = AVAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=False,
        audio_in_sample_rate=16000,
        audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    inp = AVAudioInputTransport(params, engine=fake_engine)

    # Capture pushed frames without depending on real TaskManager init.
    pushed_frames = []
    async def fake_push(frame):
        pushed_frames.append(frame)
    monkeypatch.setattr(inp, "push_audio_frame", fake_push)
    # Avoid pipecat's TaskManager-dependent set_transport_ready.
    # Must be an async callable because start() awaits it.
    async def fake_set_transport_ready(_frame):
        pass
    monkeypatch.setattr(inp, "set_transport_ready", fake_set_transport_ready)

    frame = StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000)
    await inp.start(frame)

    install_call = fake_input.installTapOnBus_bufferSize_format_block_.call_args
    tap_callback = install_call[0][3]  # 4th positional arg is the block

    # Fire from a worker thread (CoreAudio thread simulation), then yield
    # to the asyncio loop so run_coroutine_threadsafe's scheduled coro runs.
    fake_buf = MagicMock()
    fake_time = MagicMock()
    threading.Thread(target=tap_callback, args=(fake_buf, fake_time)).start()
    for _ in range(10):
        await asyncio.sleep(0.01)

    assert len(pushed_frames) == 1
    assert isinstance(pushed_frames[0], InputAudioRawFrame)
    assert pushed_frames[0].audio == expected_after_gain
    assert pushed_frames[0].sample_rate == 16000
    assert pushed_frames[0].num_channels == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
@pytest.mark.asyncio
async def test_input_transport_cleanup_removes_tap(monkeypatch):
    from unittest.mock import MagicMock

    from pipecat.frames.frames import StartFrame
    from tend.audio.av_audio import AVAudioInputTransport, AVAudioTransportParams

    fake_engine = MagicMock()
    fake_engine.startAndReturnError_.return_value = (True, None)
    fake_input = MagicMock()
    fake_input.setVoiceProcessingEnabled_error_.return_value = (True, None)
    fake_input.outputFormatForBus_.return_value = MagicMock(
        sampleRate=lambda: 48000.0, channelCount=lambda: 9,
    )
    fake_engine.inputNode.return_value = fake_input

    monkeypatch.setattr(
        "tend.audio.av_audio._make_input_converter", lambda *a, **kw: MagicMock(),
    )

    async def fake_set_ready(frame):
        return None
    params = AVAudioTransportParams(
        audio_in_enabled=True, audio_out_enabled=False,
        audio_in_sample_rate=16000, audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    inp = AVAudioInputTransport(params, engine=fake_engine)
    monkeypatch.setattr(inp, "set_transport_ready", fake_set_ready)

    await inp.start(StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000))

    await inp.cleanup()

    fake_input.removeTapOnBus_.assert_called_once_with(0)


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
@pytest.mark.asyncio
async def test_output_transport_start_attaches_player_to_output_node(monkeypatch):
    """Player must connect to OUTPUT NODE directly, NOT mainMixerNode.
    Under VPIO, mainMixer's 44.1 kHz default conflicts with outputNode's
    48 kHz and engine.start() fails with err=-10875."""
    from unittest.mock import MagicMock

    from pipecat.frames.frames import StartFrame
    from tend.audio.av_audio import AVAudioOutputTransport, AVAudioTransportParams

    fake_engine = MagicMock()
    fake_engine.startAndReturnError_.return_value = (True, None)
    fake_player = MagicMock(name="player")
    fake_output_node = MagicMock(name="output_node")
    fake_output_format = MagicMock(
        name="output_format", channelCount=lambda: 2, sampleRate=lambda: 48000.0,
    )
    fake_output_node.inputFormatForBus_.return_value = fake_output_format
    fake_engine.outputNode.return_value = fake_output_node

    monkeypatch.setattr(
        "tend.audio.av_audio._make_player_node", lambda: fake_player,
    )
    monkeypatch.setattr(
        "tend.audio.av_audio._make_output_converter",
        lambda *a, **kw: MagicMock(),
    )

    async def fake_set_ready(frame):
        return None
    params = AVAudioTransportParams(
        audio_in_enabled=False, audio_out_enabled=True,
        audio_in_sample_rate=16000, audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    out = AVAudioOutputTransport(params, engine=fake_engine)
    monkeypatch.setattr(out, "set_transport_ready", fake_set_ready)

    await out.start(StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000))

    fake_engine.attachNode_.assert_called_once_with(fake_player)
    # KEY: connect target is outputNode, not mainMixerNode.
    connect_call = fake_engine.connect_to_format_.call_args
    assert connect_call[0][0] is fake_player
    assert connect_call[0][1] is fake_output_node
    # The mainMixerNode must NOT be referenced at all in the connect chain.
    assert not fake_engine.mainMixerNode.called
    fake_player.play.assert_called_once()


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
@pytest.mark.asyncio
async def test_output_transport_write_frame_blocks_until_completion(monkeypatch):
    """write_audio_frame schedules a buffer and awaits .dataPlayedBack
    completion. We verify it doesn't return until the fake completion
    callback fires."""
    import asyncio
    from unittest.mock import MagicMock

    from pipecat.frames.frames import OutputAudioRawFrame, StartFrame
    from tend.audio.av_audio import AVAudioOutputTransport, AVAudioTransportParams

    fake_engine = MagicMock()
    fake_engine.startAndReturnError_.return_value = (True, None)
    fake_player = MagicMock()

    # Capture the completion handler scheduleBuffer is given so the test
    # can fire it from outside.
    captured_handler = []
    def fake_schedule(buf, ctype, handler):
        captured_handler.append(handler)
    fake_player.scheduleBuffer_completionCallbackType_completionHandler_.side_effect = (
        fake_schedule
    )

    fake_output_node = MagicMock()
    fake_output_format = MagicMock(
        channelCount=lambda: 2, sampleRate=lambda: 48000.0,
    )
    fake_output_node.inputFormatForBus_.return_value = fake_output_format
    fake_engine.outputNode.return_value = fake_output_node

    monkeypatch.setattr("tend.audio.av_audio._make_player_node", lambda: fake_player)
    monkeypatch.setattr(
        "tend.audio.av_audio._int16_pcm_buffer",
        lambda pcm, sample_rate: MagicMock(),
    )
    monkeypatch.setattr(
        "tend.audio.av_audio._make_output_converter",
        lambda *a, **kw: MagicMock(),
    )
    monkeypatch.setattr(
        "tend.audio.av_audio._convert_int16_buffer_to_float32_buffer",
        lambda *a, **kw: MagicMock(name="float32_buf"),
    )

    async def fake_set_ready(frame):
        return None
    params = AVAudioTransportParams(
        audio_in_enabled=False, audio_out_enabled=True,
        audio_in_sample_rate=16000, audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    out = AVAudioOutputTransport(params, engine=fake_engine)
    monkeypatch.setattr(out, "set_transport_ready", fake_set_ready)
    await out.start(StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000))

    frame = OutputAudioRawFrame(audio=b"\x00\x01" * 160, sample_rate=16000, num_channels=1)
    task = asyncio.create_task(out.write_audio_frame(frame))

    # Give write_audio_frame a chance to enter the await.
    await asyncio.sleep(0.01)
    assert not task.done()
    assert len(captured_handler) == 1

    # Fire the completion handler from a "CoreAudio thread" (synchronous is fine for the test).
    captured_handler[0]()
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result is True


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
@pytest.mark.asyncio
async def test_output_transport_cleanup_stops_player(monkeypatch):
    from unittest.mock import MagicMock

    from pipecat.frames.frames import StartFrame
    from tend.audio.av_audio import AVAudioOutputTransport, AVAudioTransportParams

    fake_engine = MagicMock()
    fake_engine.startAndReturnError_.return_value = (True, None)
    fake_player = MagicMock()
    fake_output_node = MagicMock()
    fake_output_node.inputFormatForBus_.return_value = MagicMock(
        channelCount=lambda: 2, sampleRate=lambda: 48000.0,
    )
    fake_engine.outputNode.return_value = fake_output_node

    monkeypatch.setattr("tend.audio.av_audio._make_player_node", lambda: fake_player)
    monkeypatch.setattr(
        "tend.audio.av_audio._make_output_converter",
        lambda *a, **kw: MagicMock(),
    )

    async def fake_set_ready(frame):
        return None
    params = AVAudioTransportParams(
        audio_in_enabled=False, audio_out_enabled=True,
        audio_in_sample_rate=16000, audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    out = AVAudioOutputTransport(params, engine=fake_engine)
    monkeypatch.setattr(out, "set_transport_ready", fake_set_ready)
    await out.start(StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000))

    await out.cleanup()

    fake_player.stop.assert_called_once()

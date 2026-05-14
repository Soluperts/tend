# AVAudioTransport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace pipecat's `LocalAudioTransport` on macOS with `AVAudioTransport`, an `AVAudioEngine`-based transport that runs Apple's VoiceProcessingIO unit for OS-grade AEC + NS + AGC.

**Architecture:** Single new module `src/tend/audio/av_audio.py` that mirrors pipecat's `local/audio.py` shape. The transport owns one `AVAudioEngine` shared between input and output. Input installs a tap on `engine.inputNode` with `setVoiceProcessingEnabled` already turned on; tap callback converts each buffer via `AVAudioConverter` to 16 kHz int16 mono and bridges to asyncio via `run_coroutine_threadsafe`. Output owns an `AVAudioPlayerNode` connected to `mainMixerNode`; `write_audio_frame` schedules one `AVAudioPCMBuffer` at a time with `.dataPlayedBack` completion type and awaits the callback via an `asyncio.Event` set through `loop.call_soon_threadsafe`. The macOS path in `select_audio_path` returns `AppleAudioTransport` as the `transport_factory` and no AEC pair, since VPIO does AEC upstream of pipecat.

**Tech Stack:** PyObjC (pyobjc-framework-AVFoundation), pipecat 1.1 (`BaseTransport`, `BaseInputTransport`, `BaseOutputTransport`, `TransportParams`), asyncio, loguru.

**Spec:** [`docs/superpowers/specs/2026-05-14-tend-apple-vpio-transport-design.md`](../specs/2026-05-14-tend-apple-vpio-transport-design.md)

---

## File structure (created or modified by this plan)

| File | Status | Purpose |
|------|--------|---------|
| `scripts/audio_check_vpio.py` | NEW | Standalone PoC; validates VPIO + format conversion + suppression measurement on user's hardware before touching pipecat. |
| `src/tend/audio/av_audio.py` | NEW | `AVAudioTransport`, `AVAudioInputTransport`, `AVAudioOutputTransport`, `AVAudioTransportParams` + PCM-buffer helpers. ~300 LOC. |
| `src/tend/audio/channels.py` | MODIFY | Add `transport_factory` field to `AudioPath`; macOS VPIO branch in `select_audio_path`. |
| `src/tend/audio/aec.py` | MODIFY | Add `"vpio"` to `resolve_aec_engine`; macOS auto resolves to `"vpio"`. |
| `src/tend/audio/hub.py` | MODIFY | Use `path.transport_factory(params)` instead of constructing `LocalAudioTransport` directly. |
| `src/tend/config.py` | MODIFY | Update `aec_engine` field comment to mention `vpio`. |
| `tests/test_audio_av_audio.py` | NEW | Unit tests for the transport with mocked AVFoundation. |
| `tests/test_audio_channels_select_path.py` | MODIFY | Add macOS-VPIO branch test. |
| `tests/test_audio_aec.py` | MODIFY | Add tests for `vpio` engine name + macOS auto resolution. |
| `README.md` | MODIFY | Note VPIO is the macOS default AEC engine. |
| `ROADMAP.md` | MODIFY | Move VPIO line out of "After public release" into the changelog row when this ships. |

---

## Task 1: Standalone VPIO PoC script

**Why first:** De-risks the whole plan. Validates PyObjC bindings, format conversion, asyncio bridge, output scheduling, and suppression measurement on the user's macOS 26 hardware in ~120 LOC of standalone code. If something is wrong with the foundation, we find out here before any pipecat wiring.

**Files:**
- Create: `scripts/audio_check_vpio.py`

- [ ] **Step 1: Create the PoC script**

```python
"""Standalone AVAudioEngine + VoiceProcessingIO sanity check.

Validates: (1) setVoiceProcessingEnabled works on this Mac, (2) format
conversion from the input node's native format to 16 kHz int16 mono via
AVAudioConverter works, (3) output scheduling with .dataPlayedBack
completion gives correct backpressure, (4) measurable echo suppression
vs the same hardware without VPIO.

Usage:
    python scripts/audio_check_vpio.py
"""

from __future__ import annotations

import array
import math
import struct
import sys
import threading
import time

import objc
from AVFoundation import (
    AVAudioConverter,
    AVAudioEngine,
    AVAudioFormat,
    AVAudioPCMBuffer,
    AVAudioPCMFormatFloat32,
    AVAudioPCMFormatInt16,
    AVAudioPlayerNode,
    AVAudioPlayerNodeCompletionDataPlayedBack,
)

TARGET_SR = 16000
TONE_HZ = 1000.0
TONE_DURATION_S = 3.0


def _generate_tone_pcm(sr: int, hz: float, dur_s: float) -> bytes:
    n = int(sr * dur_s)
    samples = (
        int(0.3 * 32767 * math.sin(2 * math.pi * hz * i / sr))
        for i in range(n)
    )
    return b"".join(struct.pack("<h", s) for s in samples)


def _pcm_buffer_from_int16(pcm: bytes, sr: int) -> AVAudioPCMBuffer:
    fmt = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatInt16, float(sr), 1, False,
    )
    n_frames = len(pcm) // 2
    buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(fmt, n_frames)
    buf.setFrameLength_(n_frames)
    mv = buf.int16ChannelData()[0].as_buffer(n_frames)
    mv[:] = pcm
    return buf


def _rms(pcm_i16: bytes) -> float:
    if not pcm_i16:
        return 0.0
    a = array.array("h")
    a.frombytes(pcm_i16)
    acc = sum(s * s for s in a)
    return math.sqrt(acc / len(a))


def _band_rms_1khz(pcm_i16: bytes, sr: int) -> float:
    """Goertzel-style single-bin power at 1 kHz; cheap proxy for FFT bin."""
    a = array.array("h")
    a.frombytes(pcm_i16)
    if not a:
        return 0.0
    omega = 2 * math.pi * TONE_HZ / sr
    coeff = 2 * math.cos(omega)
    s1 = s2 = 0.0
    for sample in a:
        s0 = sample + coeff * s1 - s2
        s2 = s1
        s1 = s0
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    # normalise by length so result is comparable across different durations
    return math.sqrt(max(power, 0.0) / len(a))


def main() -> int:
    engine = AVAudioEngine.alloc().init()
    input_node = engine.inputNode()

    ok, err = input_node.setVoiceProcessingEnabled_error_(True, None)
    if not ok:
        print(f"FAIL: setVoiceProcessingEnabled returned False: {err}")
        return 1
    print(f"VPIO enabled: isVoiceProcessingEnabled={input_node.isVoiceProcessingEnabled()}")

    # Output side: AVAudioPlayerNode → mainMixer at 16 kHz int16 mono.
    player = AVAudioPlayerNode.alloc().init()
    engine.attachNode_(player)
    out_format = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatInt16, float(TARGET_SR), 1, False,
    )
    engine.connect_to_format_(player, engine.mainMixerNode(), out_format)

    # Converter for tap callback: input_node.outputFormat → 16 kHz int16 mono.
    native_in = input_node.outputFormatForBus_(0)
    target_in = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatInt16, float(TARGET_SR), 1, False,
    )
    converter = AVAudioConverter.alloc().initFromFormat_toFormat_(native_in, target_in)
    print(f"native input format: {native_in}")
    print(f"target  input format: {target_in}")

    captured = bytearray()
    captured_lock = threading.Lock()

    def tap_callback(in_buf, when):
        # convert in_buf (native format) -> target_in (16k int16 mono)
        # estimate output capacity: input frames at native rate * (target/native)
        in_frames = in_buf.frameLength()
        native_sr = native_in.sampleRate()
        out_capacity = max(
            int(in_frames * (TARGET_SR / native_sr)) + 64,
            1,
        )
        out_buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
            target_in, out_capacity,
        )

        # PyObjC needs a block that returns (input_buffer, status)
        def supply_input(num_packets, status_ptr):
            # AVAudioConverterInputStatus_HaveData = 0
            return (in_buf, 0)

        status, err = converter.convertToBuffer_error_withInputFromBlock_(
            out_buf, None, supply_input,
        )
        if status == 0:  # haveData
            n = out_buf.frameLength()
            mv = bytes(out_buf.int16ChannelData()[0].as_buffer(n))
            with captured_lock:
                captured.extend(mv)

    input_node.installTapOnBus_bufferSize_format_block_(
        0, 1024, native_in, tap_callback,
    )

    engine.prepare()
    started, err = engine.startAndReturnError_(None)
    if not started:
        print(f"FAIL: engine.start returned False: {err}")
        return 1
    print("engine started")

    # --- Scenario 1: play 1 kHz tone while recording. Measure 1 kHz residual ---
    tone_pcm = _generate_tone_pcm(TARGET_SR, TONE_HZ, TONE_DURATION_S)
    tone_buf = _pcm_buffer_from_int16(tone_pcm, TARGET_SR)
    tone_emit_band_rms = _band_rms_1khz(tone_pcm, TARGET_SR)
    print(f"emit  1 kHz band RMS: {tone_emit_band_rms:.0f}")

    done = threading.Event()
    player.scheduleBuffer_completionCallbackType_completionHandler_(
        tone_buf, AVAudioPlayerNodeCompletionDataPlayedBack, lambda *_: done.set(),
    )
    player.play()
    print(f"playing {TONE_DURATION_S}s tone; capturing mic with VPIO on...")
    done.wait(timeout=TONE_DURATION_S + 2.0)
    # give the tap a moment to flush
    time.sleep(0.2)

    with captured_lock:
        captured_bytes = bytes(captured)
        captured.clear()

    cap_band_rms = _band_rms_1khz(captured_bytes, TARGET_SR)
    cap_total_rms = _rms(captured_bytes)
    print(f"captured 1 kHz band RMS: {cap_band_rms:.0f}")
    print(f"captured total       RMS: {cap_total_rms:.0f}")
    if tone_emit_band_rms > 0 and cap_band_rms > 0:
        suppression_db = 20 * math.log10(tone_emit_band_rms / cap_band_rms)
    else:
        suppression_db = float("inf")
    print(f"1 kHz suppression vs emit: {suppression_db:+.1f} dB")

    # --- Scenario 2: capture user speech (no tone playing) ---
    print("now speak for 3 seconds (no tone) — measuring speech preserved...")
    time.sleep(0.3)
    with captured_lock:
        captured.clear()
    time.sleep(TONE_DURATION_S)
    with captured_lock:
        speech_bytes = bytes(captured)
    speech_rms = _rms(speech_bytes)
    print(f"captured speech total RMS: {speech_rms:.0f}")

    # Cleanup
    player.stop()
    input_node.removeTapOnBus_(0)
    engine.stop()

    # PASS criteria: ≥30 dB suppression and ≥500 RMS for speech
    suppression_ok = suppression_db >= 30.0
    speech_ok = speech_rms >= 500.0
    print(f"\nsuppression ≥30 dB: {'PASS' if suppression_ok else 'FAIL'}")
    print(f"speech RMS ≥500:    {'PASS' if speech_ok else 'FAIL'}")
    return 0 if (suppression_ok and speech_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the PoC**

Run on user's MacBook (built-in mic + speaker):

```bash
python scripts/audio_check_vpio.py
```

Expected output: engine starts, native input format prints (typically `48000.0 Hz, Float32`), tone plays for 3 s, captured 1 kHz band RMS is much lower than emit RMS (target ≥30 dB suppression), user speaks and speech RMS is ≥500.

If `suppression ≥30 dB: PASS` and `speech RMS ≥500: PASS`, the foundation is solid; continue to Task 2.

If FAIL: read the printed numbers and the error message. Common failures:
- `setVoiceProcessingEnabled returned False` → API not available; check macOS version.
- Suppression <10 dB → VPIO didn't actually activate. Check `isVoiceProcessingEnabled()` reported True. Verify converter is producing real output (debug by writing `captured_bytes` to a WAV file).
- Speech RMS very low → microphone is muted or wrong device selected at the OS level (open System Settings → Sound).

- [ ] **Step 3: Commit**

```bash
git add scripts/audio_check_vpio.py
git commit -m "feat(audio): add VPIO PoC script

Standalone AVAudioEngine + setVoiceProcessingEnabled validator.
Measures 1 kHz suppression vs emit and speech-preserved RMS.
Used as Task 1 de-risking step before AVAudioTransport implementation."
```

---

## Task 2: AVAudioTransportParams

**Files:**
- Create: `src/tend/audio/av_audio.py` (initial scaffold)
- Create: `tests/test_audio_av_audio.py` (initial)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audio_av_audio.py
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
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_av_audio.py::test_params_is_transport_params_subclass -v
```

Expected: FAIL with `ImportError: cannot import name 'AVAudioTransportParams' from 'tend.audio.av_audio'` (the module doesn't exist yet).

- [ ] **Step 3: Create the module scaffold with `AVAudioTransportParams`**

```python
# src/tend/audio/av_audio.py
"""AVAudioEngine-based transport for macOS, with VoiceProcessingIO enabled.

Mirrors pipecat's LocalAudioTransport shape so it's pasteable upstream as
pipecat.transports.local.av_audio when we file the PR.

Why this exists: built-in MacBook mic+speaker generate echo that the speex
software AEC (~12-26 dB) doesn't suppress below Deepgram's transcription
threshold. AVAudioEngine + setVoiceProcessingEnabled gives OS-grade AEC +
NS + AGC — same path FaceTime uses — which drops the residual below
Deepgram's threshold and stops the bot from transcribing its own voice
back into the LLM context.
"""

from __future__ import annotations

from pipecat.transports.base_transport import TransportParams


class AVAudioTransportParams(TransportParams):
    """Parameters for AVAudioTransport.

    No extra fields for v1. Reserved for input_device_uid / output_device_uid
    additions later — AVAudioEngine uses CoreAudio device UIDs (strings) not
    PyAudio indices, so adding them would be a separate decision.
    """
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py::test_params_is_transport_params_subclass -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/av_audio.py tests/test_audio_av_audio.py
git commit -m "feat(audio): scaffold AVAudioTransportParams"
```

---

## Task 3: PCM buffer ↔ bytes helpers

These helpers convert between `bytes` (16 kHz int16 mono) and `AVAudioPCMBuffer`. The input tap converts a native-format buffer down to bytes; the output side wraps bytes back into a buffer for scheduling.

**Files:**
- Modify: `src/tend/audio/av_audio.py`
- Modify: `tests/test_audio_av_audio.py`

- [ ] **Step 1: Write the failing test for `_int16_pcm_buffer`**

Append to `tests/test_audio_av_audio.py`:

```python
@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_int16_pcm_buffer_round_trip():
    """A 320-frame buffer constructed from int16 PCM bytes must read back
    identical bytes via the channelData pointer."""
    import struct

    from tend.audio.av_audio import _int16_pcm_buffer

    pcm = struct.pack("<320h", *range(100, 420))  # 320 samples, predictable
    buf = _int16_pcm_buffer(pcm, sample_rate=16000)

    assert buf.frameLength() == 320
    assert buf.format().sampleRate() == 16000.0

    mv = bytes(buf.int16ChannelData()[0].as_buffer(320))
    assert mv == pcm
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_av_audio.py::test_int16_pcm_buffer_round_trip -v
```

Expected: FAIL with `ImportError: cannot import name '_int16_pcm_buffer'`.

- [ ] **Step 3: Implement `_int16_pcm_buffer` in `av_audio.py`**

Append to `src/tend/audio/av_audio.py`:

```python
def _int16_pcm_buffer(pcm_bytes: bytes, sample_rate: int) -> "AVAudioPCMBuffer":
    """Wrap raw int16 mono PCM bytes in an AVAudioPCMBuffer at sample_rate.

    Pattern: allocate the buffer at the right format + frame capacity, then
    copy the bytes into the channelData[0] memoryview. PyObjC's varlist
    exposes the channel-0 storage as a memoryview via .as_buffer(n_frames)
    — slice-assign the PCM bytes into it and set the frame length.
    """
    from AVFoundation import (
        AVAudioFormat,
        AVAudioPCMBuffer,
        AVAudioPCMFormatInt16,
    )

    n_frames = len(pcm_bytes) // 2  # int16 mono → 2 bytes/sample
    fmt = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatInt16, float(sample_rate), 1, False,
    )
    buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(fmt, n_frames)
    buf.setFrameLength_(n_frames)
    mv = buf.int16ChannelData()[0].as_buffer(n_frames)
    mv[:] = pcm_bytes
    return buf
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py::test_int16_pcm_buffer_round_trip -v
```

Expected: PASS.

- [ ] **Step 5: Write failing test for `_int16_bytes_from_buffer`**

Append:

```python
@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_int16_bytes_from_buffer_reads_set_frame_length():
    """The reader must respect setFrameLength_, not frameCapacity."""
    import struct

    from tend.audio.av_audio import _int16_bytes_from_buffer, _int16_pcm_buffer

    pcm = struct.pack("<100h", *range(0, 100))
    buf = _int16_pcm_buffer(pcm, sample_rate=16000)
    # buf was sized to 100 frames; the reader should return exactly 200 bytes
    out = _int16_bytes_from_buffer(buf)
    assert out == pcm
    assert len(out) == 200
```

- [ ] **Step 6: Run to verify failure**

```bash
pytest tests/test_audio_av_audio.py::test_int16_bytes_from_buffer_reads_set_frame_length -v
```

Expected: FAIL with `ImportError: cannot import name '_int16_bytes_from_buffer'`.

- [ ] **Step 7: Implement `_int16_bytes_from_buffer`**

Append to `src/tend/audio/av_audio.py`:

```python
def _int16_bytes_from_buffer(buf: "AVAudioPCMBuffer") -> bytes:
    """Read int16 mono PCM bytes out of an AVAudioPCMBuffer's channelData.

    Respects buf.frameLength() (the populated frame count), not
    frameCapacity (the allocated size). The tap callback receives
    buffers whose frameLength varies per chunk.
    """
    n = buf.frameLength()
    if n == 0:
        return b""
    return bytes(buf.int16ChannelData()[0].as_buffer(n))
```

- [ ] **Step 8: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 9: Commit**

```bash
git add src/tend/audio/av_audio.py tests/test_audio_av_audio.py
git commit -m "feat(audio): PCM buffer ↔ bytes helpers for AVAudioTransport"
```

---

## Task 4: Format conversion helper (native → 16 kHz int16 mono)

The input node's native output format is typically 48 kHz Float32 mono after VPIO. We need to convert per-buffer in the tap callback. AVAudioConverter handles this; the helper wraps the convert call with the inputBlock pattern PyObjC requires.

**Files:**
- Modify: `src/tend/audio/av_audio.py`
- Modify: `tests/test_audio_av_audio.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_converter_downsamples_float32_to_int16_at_16k():
    """Converter takes a 48 kHz Float32 buffer and produces 16 kHz int16 mono."""
    import math

    from AVFoundation import (
        AVAudioFormat,
        AVAudioPCMBuffer,
        AVAudioPCMFormatFloat32,
    )

    from tend.audio.av_audio import _make_input_converter, _convert_to_int16_bytes

    native = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatFloat32, 48000.0, 1, False,
    )
    # 480 frames of Float32 == 10 ms at 48 kHz → 160 frames at 16 kHz → 320 bytes
    in_buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(native, 480)
    in_buf.setFrameLength_(480)
    mv = in_buf.floatChannelData()[0].as_buffer(480)
    # write 480 Float32 samples of a 1 kHz sine at 48 kHz
    for i in range(480):
        v = 0.3 * math.sin(2 * math.pi * 1000 * i / 48000)
        mv[i * 4:(i + 1) * 4] = bytes(_f32_to_bytes(v))

    converter = _make_input_converter(native, target_sample_rate=16000)
    out_bytes = _convert_to_int16_bytes(converter, in_buf, target_sample_rate=16000)

    # 480 frames @ 48k → 160 frames @ 16k → 320 bytes of int16 mono
    assert 280 <= len(out_bytes) <= 360  # allow ±a frame for resampler windowing


def _f32_to_bytes(v: float) -> bytes:
    import struct
    return struct.pack("<f", v)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_av_audio.py::test_converter_downsamples_float32_to_int16_at_16k -v
```

Expected: FAIL with `ImportError: cannot import name '_make_input_converter'`.

- [ ] **Step 3: Implement the converter helpers**

Append to `src/tend/audio/av_audio.py`:

```python
def _make_input_converter(
    native_format: "AVAudioFormat", target_sample_rate: int,
) -> "AVAudioConverter":
    """Build an AVAudioConverter from the input node's native format to
    16 kHz int16 mono (or whatever target_sample_rate the caller picks)."""
    from AVFoundation import (
        AVAudioConverter,
        AVAudioFormat,
        AVAudioPCMFormatInt16,
    )

    target = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatInt16, float(target_sample_rate), 1, False,
    )
    return AVAudioConverter.alloc().initFromFormat_toFormat_(native_format, target)


def _convert_to_int16_bytes(
    converter: "AVAudioConverter",
    in_buf: "AVAudioPCMBuffer",
    *,
    target_sample_rate: int,
) -> bytes:
    """Run one convert pass and return the result as int16 mono bytes.

    AVAudioConverter is stateful when resampling — keeping the same
    converter instance across tap calls preserves the resampler's
    interpolation state, avoiding clicks at chunk boundaries.
    """
    from AVFoundation import AVAudioPCMBuffer

    target_format = converter.outputFormat()
    in_frames = in_buf.frameLength()
    native_sr = in_buf.format().sampleRate()
    # Generous output capacity: in_frames * (target/native) + slack for
    # the resampler's lookahead window.
    out_cap = max(int(in_frames * target_sample_rate / native_sr) + 64, 1)
    out_buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
        target_format, out_cap,
    )

    supplied = {"done": False}

    def supply_input(num_packets, status_ptr):
        # AVAudioConverterInputStatus_HaveData = 0
        # AVAudioConverterInputStatus_NoDataNow = 1
        # AVAudioConverterInputStatus_EndOfStream = 2
        if supplied["done"]:
            return (None, 1)  # NoDataNow — converter flushes what it has
        supplied["done"] = True
        return (in_buf, 0)

    status, err = converter.convertToBuffer_error_withInputFromBlock_(
        out_buf, None, supply_input,
    )
    if status not in (0, 1):  # HaveData or NoDataNow are both fine for one pass
        from loguru import logger
        logger.error(f"AVAudioConverter status={status} err={err}")
        return b""

    return _int16_bytes_from_buffer(out_buf)
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py::test_converter_downsamples_float32_to_int16_at_16k -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/av_audio.py tests/test_audio_av_audio.py
git commit -m "feat(audio): native-format → 16 kHz int16 mono converter helpers"
```

---

## Task 5: AVAudioInputTransport — engine setup

The input transport is responsible for: (a) constructing the engine, (b) enabling VPIO on the inputNode before anything else, (c) installing the tap, (d) starting the engine. The asyncio bridge from tap → `push_audio_frame` is the next task.

**Files:**
- Modify: `src/tend/audio/av_audio.py`
- Modify: `tests/test_audio_av_audio.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
@pytest.mark.asyncio
async def test_input_transport_start_enables_vpio_and_installs_tap():
    """start() must enable VPIO on the input node BEFORE engine.start.
    Order matters — VPIO can't toggle while engine is running."""
    from unittest.mock import MagicMock

    from pipecat.frames.frames import StartFrame
    from tend.audio.av_audio import AVAudioInputTransport, AVAudioTransportParams

    fake_engine = MagicMock(name="fake_engine")
    fake_engine.startAndReturnError_.return_value = (True, None)
    fake_input = MagicMock(name="fake_input_node")
    fake_input.setVoiceProcessingEnabled_error_.return_value = (True, None)
    fake_input.outputFormatForBus_.return_value = MagicMock(
        name="native_format", sampleRate=lambda: 48000.0,
    )
    fake_engine.inputNode.return_value = fake_input

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
    await inp.start(frame)

    # Verify the call order: setVoiceProcessingEnabled → installTap → engine.start.
    call_names = [c[0] for c in fake_engine.mock_calls + fake_input.mock_calls]
    vpio_idx = next(i for i, n in enumerate(call_names) if "setVoiceProcessingEnabled" in n)
    tap_idx = next(i for i, n in enumerate(call_names) if "installTapOnBus" in n)
    start_idx = next(i for i, n in enumerate(call_names) if "startAndReturnError" in n)
    assert vpio_idx < tap_idx < start_idx
```

- [ ] **Step 2: Add pytest-asyncio if missing**

```bash
pip install pytest-asyncio  # likely already present; check pyproject.toml
```

Ensure `tests/conftest.py` or pyproject configures `asyncio_mode = "auto"` (check existing tests for the pattern).

- [ ] **Step 3: Run to verify failure**

```bash
pytest tests/test_audio_av_audio.py::test_input_transport_start_enables_vpio_and_installs_tap -v
```

Expected: FAIL with `ImportError: cannot import name 'AVAudioInputTransport'`.

- [ ] **Step 4: Implement `AVAudioInputTransport.__init__` and `start`**

Append to `src/tend/audio/av_audio.py`:

```python
import asyncio
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport

if TYPE_CHECKING:
    from AVFoundation import (
        AVAudioConverter,
        AVAudioEngine,
        AVAudioFormat,
        AVAudioPCMBuffer,
        AVAudioPlayerNode,
    )


class AVAudioInputTransport(BaseInputTransport):
    """Captures audio from AVAudioEngine's inputNode with VPIO enabled.

    The transport doesn't own the engine — AVAudioTransport does. We get
    the engine handed to us in the constructor so input and output share
    one engine instance.
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

        # CRITICAL: enable VPIO BEFORE attaching anything else and BEFORE
        # engine.start(). VPIO cannot be toggled while the engine is running.
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
        logger.info(
            f"[av_audio] native input format: "
            f"sr={native_format.sampleRate()} ch={native_format.channelCount()}"
        )
        self._converter = _make_input_converter(
            native_format, target_sample_rate=self._target_sample_rate,
        )

        loop = self.get_event_loop()
        target_sr = self._target_sample_rate
        converter = self._converter
        push = self.push_audio_frame

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
            frame = InputAudioRawFrame(
                audio=pcm,
                sample_rate=target_sr,
                num_channels=1,
            )
            asyncio.run_coroutine_threadsafe(push(frame), loop)

        # Tap at native format — installing at a different format than the
        # bus produces is unreliable per Apple's docs. Convert per-buffer
        # in the callback instead.
        self._input_node.installTapOnBus_bufferSize_format_block_(
            0, 1024, native_format, tap_callback,
        )
        self._tap_installed = True

        # Idempotent: engine.start() returns True if already running.
        started, err = self._engine.startAndReturnError_(None)
        if not started:
            raise RuntimeError(f"engine.startAndReturnError_ failed: {err}")

        await self.set_transport_ready(frame)
```

- [ ] **Step 5: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py::test_input_transport_start_enables_vpio_and_installs_tap -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tend/audio/av_audio.py tests/test_audio_av_audio.py
git commit -m "feat(audio): AVAudioInputTransport.start enables VPIO + installs tap"
```

---

## Task 6: AVAudioInputTransport — tap → asyncio bridge

When the real-time tap callback fires (from a CoreAudio I/O thread), the converted bytes must be marshalled into the asyncio loop as an `InputAudioRawFrame`. This task verifies that the bridge invokes `push_audio_frame` with the right frame.

**Files:**
- Modify: `tests/test_audio_av_audio.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
@pytest.mark.asyncio
async def test_input_transport_tap_callback_pushes_frame(monkeypatch):
    """When the tap callback fires from a worker thread, the bridge pushes
    an InputAudioRawFrame onto the asyncio loop with the correct payload."""
    import struct
    import threading
    from unittest.mock import MagicMock

    from pipecat.frames.frames import StartFrame, InputAudioRawFrame
    from tend.audio.av_audio import AVAudioInputTransport, AVAudioTransportParams

    fake_engine = MagicMock()
    fake_engine.startAndReturnError_.return_value = (True, None)
    fake_input = MagicMock()
    fake_input.setVoiceProcessingEnabled_error_.return_value = (True, None)

    fake_native_format = MagicMock(sampleRate=lambda: 48000.0, channelCount=lambda: 1)
    fake_input.outputFormatForBus_.return_value = fake_native_format
    fake_engine.inputNode.return_value = fake_input

    # Stub the conversion path so we don't need real AVFoundation buffers.
    expected_pcm = struct.pack("<5h", 1, 2, 3, 4, 5)
    monkeypatch.setattr(
        "tend.audio.av_audio._make_input_converter",
        lambda *a, **kw: MagicMock(),
    )
    monkeypatch.setattr(
        "tend.audio.av_audio._convert_to_int16_bytes",
        lambda *a, **kw: expected_pcm,
    )

    params = AVAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=False,
        audio_in_sample_rate=16000,
        audio_in_channels=1,
        audio_out_sample_rate=16000,
    )

    inp = AVAudioInputTransport(params, engine=fake_engine)

    pushed_frames = []
    async def fake_push(frame):
        pushed_frames.append(frame)
    monkeypatch.setattr(inp, "push_audio_frame", fake_push)

    frame = StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000)
    await inp.start(frame)

    # Pull the tap callback out of installTapOnBus's call args.
    install_call = fake_input.installTapOnBus_bufferSize_format_block_.call_args
    tap_callback = install_call[0][3]  # 4th positional arg is the block

    # Fire from a worker thread, then yield to the loop so the
    # coroutine pushed via run_coroutine_threadsafe gets a chance to run.
    fake_buf = MagicMock()
    fake_time = MagicMock()
    threading.Thread(target=tap_callback, args=(fake_buf, fake_time)).start()
    # give it a moment + yield to the loop several times
    for _ in range(10):
        await asyncio.sleep(0.01)

    assert len(pushed_frames) == 1
    assert isinstance(pushed_frames[0], InputAudioRawFrame)
    assert pushed_frames[0].audio == expected_pcm
    assert pushed_frames[0].sample_rate == 16000
    assert pushed_frames[0].num_channels == 1
```

- [ ] **Step 2: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py::test_input_transport_tap_callback_pushes_frame -v
```

Expected: PASS — the bridge code is already in Task 5's implementation. If FAIL, the conversion stubbing or the thread-yield count needs adjustment.

- [ ] **Step 3: Commit**

```bash
git add tests/test_audio_av_audio.py
git commit -m "test(audio): cover tap callback → asyncio bridge in AVAudioInputTransport"
```

---

## Task 7: AVAudioInputTransport — cleanup

When the transport is torn down, the tap must be removed and the engine stopped. (Output transport does the engine stop in practice — see Task 11 — but the input transport at least removes its tap.)

**Files:**
- Modify: `src/tend/audio/av_audio.py`
- Modify: `tests/test_audio_av_audio.py`

- [ ] **Step 1: Write the failing test**

```python
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
        sampleRate=lambda: 48000.0,
    )
    fake_engine.inputNode.return_value = fake_input

    monkeypatch.setattr("tend.audio.av_audio._make_input_converter", lambda *a, **kw: MagicMock())

    params = AVAudioTransportParams(
        audio_in_enabled=True, audio_out_enabled=False,
        audio_in_sample_rate=16000, audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    inp = AVAudioInputTransport(params, engine=fake_engine)
    await inp.start(StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000))

    await inp.cleanup()

    fake_input.removeTapOnBus_.assert_called_once_with(0)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_av_audio.py::test_input_transport_cleanup_removes_tap -v
```

Expected: FAIL — `cleanup` doesn't exist yet (or doesn't call `removeTapOnBus_`).

- [ ] **Step 3: Implement `cleanup`**

Append to the `AVAudioInputTransport` class:

```python
    async def cleanup(self):
        await super().cleanup()
        if self._tap_installed and self._input_node is not None:
            self._input_node.removeTapOnBus_(0)
            self._tap_installed = False
        # Note: engine.stop() is the AVAudioTransport's responsibility
        # (Task 11) because input + output share one engine.
        self._input_node = None
        self._converter = None
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py -v
```

Expected: all input-transport tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/av_audio.py tests/test_audio_av_audio.py
git commit -m "feat(audio): AVAudioInputTransport.cleanup removes tap"
```

---

## Task 8: AVAudioOutputTransport — setup

The output transport attaches an `AVAudioPlayerNode` to the engine's `mainMixerNode`, then plays it. `write_audio_frame` (Task 9) drives the actual buffer scheduling.

**Files:**
- Modify: `src/tend/audio/av_audio.py`
- Modify: `tests/test_audio_av_audio.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
@pytest.mark.asyncio
async def test_output_transport_start_attaches_player(monkeypatch):
    from unittest.mock import MagicMock

    from pipecat.frames.frames import StartFrame
    from tend.audio.av_audio import AVAudioOutputTransport, AVAudioTransportParams

    fake_engine = MagicMock()
    fake_engine.startAndReturnError_.return_value = (True, None)
    fake_player = MagicMock(name="player")
    fake_engine.mainMixerNode.return_value = MagicMock(name="mixer")

    # Inject the player class as a factory so the test doesn't need real AVFoundation.
    monkeypatch.setattr(
        "tend.audio.av_audio._make_player_node",
        lambda: fake_player,
    )

    params = AVAudioTransportParams(
        audio_in_enabled=False, audio_out_enabled=True,
        audio_in_sample_rate=16000, audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    out = AVAudioOutputTransport(params, engine=fake_engine)
    await out.start(StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000))

    fake_engine.attachNode_.assert_called_once_with(fake_player)
    fake_engine.connect_to_format_.assert_called_once()
    fake_player.play.assert_called_once()
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_av_audio.py::test_output_transport_start_attaches_player -v
```

Expected: FAIL with `ImportError: cannot import name 'AVAudioOutputTransport'`.

- [ ] **Step 3: Implement `AVAudioOutputTransport.__init__` and `start`**

Append to `src/tend/audio/av_audio.py`:

```python
def _make_player_node() -> "AVAudioPlayerNode":
    """Factory so tests can inject a fake player without monkey-patching AVFoundation."""
    from AVFoundation import AVAudioPlayerNode
    return AVAudioPlayerNode.alloc().init()


def _make_output_format(sample_rate: int) -> "AVAudioFormat":
    from AVFoundation import AVAudioFormat, AVAudioPCMFormatInt16
    return AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatInt16, float(sample_rate), 1, False,
    )


class AVAudioOutputTransport(BaseOutputTransport):
    """Plays audio via AVAudioPlayerNode → mainMixer → outputNode.

    write_audio_frame schedules one buffer at a time and awaits its
    .dataPlayedBack completion — this gives playback-rate-paced writes,
    matching the semantics pipecat's TTSStopFrame interruption assumes.
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
        self._sample_rate = 0
        self._started = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._started:
            return

        self._sample_rate = (
            self._params.audio_out_sample_rate or frame.audio_out_sample_rate
        )
        self._player = _make_player_node()
        self._engine.attachNode_(self._player)
        out_format = _make_output_format(self._sample_rate)
        self._engine.connect_to_format_(
            self._player, self._engine.mainMixerNode(), out_format,
        )
        # Idempotent — input transport may already have started the engine.
        started, err = self._engine.startAndReturnError_(None)
        if not started:
            raise RuntimeError(f"engine.startAndReturnError_ failed: {err}")
        self._player.play()
        self._started = True
        await self.set_transport_ready(frame)
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py::test_output_transport_start_attaches_player -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/av_audio.py tests/test_audio_av_audio.py
git commit -m "feat(audio): AVAudioOutputTransport.start attaches player node"
```

---

## Task 9: AVAudioOutputTransport — write_audio_frame with backpressure

Each call to `write_audio_frame` schedules one buffer with `.dataPlayedBack` completion, then awaits an `asyncio.Event` set by the completion callback. This makes writes paced to actual playback wall-clock.

**Files:**
- Modify: `src/tend/audio/av_audio.py`
- Modify: `tests/test_audio_av_audio.py`

- [ ] **Step 1: Write the failing test**

```python
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

    # Capture the completion handler that scheduleBuffer is given so the
    # test can invoke it from a "fake CoreAudio thread".
    captured_handler = []
    def fake_schedule(buf, ctype, handler):
        captured_handler.append(handler)
    fake_player.scheduleBuffer_completionCallbackType_completionHandler_.side_effect = (
        fake_schedule
    )

    monkeypatch.setattr("tend.audio.av_audio._make_player_node", lambda: fake_player)
    # Bypass real AVAudioPCMBuffer construction.
    monkeypatch.setattr(
        "tend.audio.av_audio._int16_pcm_buffer",
        lambda pcm, sample_rate: MagicMock(),
    )

    params = AVAudioTransportParams(
        audio_in_enabled=False, audio_out_enabled=True,
        audio_in_sample_rate=16000, audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    out = AVAudioOutputTransport(params, engine=fake_engine)
    await out.start(StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000))

    frame = OutputAudioRawFrame(audio=b"\x00\x01" * 160, sample_rate=16000, num_channels=1)
    task = asyncio.create_task(out.write_audio_frame(frame))

    # Give write_audio_frame a chance to enter the await.
    await asyncio.sleep(0.01)
    assert not task.done()
    assert len(captured_handler) == 1

    # Fire the completion handler from a "CoreAudio thread" — just sync here.
    captured_handler[0]()
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result is True
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_av_audio.py::test_output_transport_write_frame_blocks_until_completion -v
```

Expected: FAIL — `write_audio_frame` not implemented (or default `BaseOutputTransport` impl returns immediately).

- [ ] **Step 3: Implement `write_audio_frame`**

Append to the `AVAudioOutputTransport` class:

```python
    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        if self._player is None:
            return False

        from AVFoundation import AVAudioPlayerNodeCompletionDataPlayedBack

        buf = _int16_pcm_buffer(frame.audio, sample_rate=self._sample_rate)
        loop = self.get_event_loop()
        done = asyncio.Event()

        def completion(*args, **kwargs):
            # Fires on a CoreAudio thread. Marshal to loop.
            loop.call_soon_threadsafe(done.set)

        self._player.scheduleBuffer_completionCallbackType_completionHandler_(
            buf, AVAudioPlayerNodeCompletionDataPlayedBack, completion,
        )
        await done.wait()
        return True
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py::test_output_transport_write_frame_blocks_until_completion -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/av_audio.py tests/test_audio_av_audio.py
git commit -m "feat(audio): AVAudioOutputTransport.write_audio_frame with .dataPlayedBack backpressure"
```

---

## Task 10: AVAudioOutputTransport — cleanup

Stops the player and removes the engine reference. Engine-level stop is done by the parent transport (Task 11).

**Files:**
- Modify: `src/tend/audio/av_audio.py`
- Modify: `tests/test_audio_av_audio.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
@pytest.mark.asyncio
async def test_output_transport_cleanup_stops_player(monkeypatch):
    from unittest.mock import MagicMock

    from pipecat.frames.frames import StartFrame
    from tend.audio.av_audio import AVAudioOutputTransport, AVAudioTransportParams

    fake_engine = MagicMock()
    fake_engine.startAndReturnError_.return_value = (True, None)
    fake_player = MagicMock()
    monkeypatch.setattr("tend.audio.av_audio._make_player_node", lambda: fake_player)

    params = AVAudioTransportParams(
        audio_in_enabled=False, audio_out_enabled=True,
        audio_in_sample_rate=16000, audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    out = AVAudioOutputTransport(params, engine=fake_engine)
    await out.start(StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=16000))

    await out.cleanup()

    fake_player.stop.assert_called_once()
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_av_audio.py::test_output_transport_cleanup_stops_player -v
```

Expected: FAIL.

- [ ] **Step 3: Implement `cleanup`**

Append to the `AVAudioOutputTransport` class:

```python
    async def cleanup(self):
        await super().cleanup()
        if self._player is not None:
            self._player.stop()
            self._player = None
        self._started = False
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py -v
```

Expected: all output-transport tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/av_audio.py tests/test_audio_av_audio.py
git commit -m "feat(audio): AVAudioOutputTransport.cleanup stops player"
```

---

## Task 11: AVAudioTransport — composite class

The top-level transport. Owns one `AVAudioEngine`, lazy-instantiates input + output subtransports against that shared engine. Mirrors pipecat's `LocalAudioTransport` shape exactly.

**Files:**
- Modify: `src/tend/audio/av_audio.py`
- Modify: `tests/test_audio_av_audio.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_avaudio_transport_lazy_input_output_share_engine():
    from unittest.mock import MagicMock

    from tend.audio.av_audio import AVAudioTransport, AVAudioTransportParams

    fake_engine = MagicMock(name="shared_engine")

    params = AVAudioTransportParams(
        audio_in_enabled=True, audio_out_enabled=True,
        audio_in_sample_rate=16000, audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    t = AVAudioTransport(params, engine_factory=lambda: fake_engine)

    inp1 = t.input()
    inp2 = t.input()
    out1 = t.output()
    out2 = t.output()

    # Cached per call
    assert inp1 is inp2
    assert out1 is out2
    # Share the same engine
    assert inp1._engine is fake_engine
    assert out1._engine is fake_engine


@pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation requires macOS")
def test_avaudio_transport_default_engine_factory_is_avaudioengine():
    """Smoke: with no engine_factory override, AVAudioTransport constructs
    a real AVAudioEngine instance."""
    from AVFoundation import AVAudioEngine
    from tend.audio.av_audio import AVAudioTransport, AVAudioTransportParams

    params = AVAudioTransportParams(
        audio_in_enabled=True, audio_out_enabled=True,
        audio_in_sample_rate=16000, audio_in_channels=1,
        audio_out_sample_rate=16000,
    )
    t = AVAudioTransport(params)
    inp = t.input()
    assert isinstance(inp._engine, AVAudioEngine)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_av_audio.py -v -k avaudio_transport
```

Expected: FAIL with `ImportError: cannot import name 'AVAudioTransport'`.

- [ ] **Step 3: Implement `AVAudioTransport`**

Append to `src/tend/audio/av_audio.py`:

```python
def _default_engine_factory() -> "AVAudioEngine":
    from AVFoundation import AVAudioEngine
    return AVAudioEngine.alloc().init()


class AVAudioTransport(BaseTransport):
    """Complete macOS AVAudioEngine transport with VPIO-enabled input.

    Owns one AVAudioEngine, lazy-constructs input/output subtransports
    that share the engine. Mirrors pipecat's LocalAudioTransport API
    so it can be a drop-in replacement on macOS.
    """

    def __init__(
        self,
        params: AVAudioTransportParams,
        *,
        engine_factory: Callable[[], "AVAudioEngine"] = _default_engine_factory,
    ):
        super().__init__()
        self._params = params
        self._engine = engine_factory()
        self._input: AVAudioInputTransport | None = None
        self._output: AVAudioOutputTransport | None = None

    def input(self):
        if self._input is None:
            self._input = AVAudioInputTransport(self._params, engine=self._engine)
        return self._input

    def output(self):
        if self._output is None:
            self._output = AVAudioOutputTransport(self._params, engine=self._engine)
        return self._output
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_av_audio.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/av_audio.py tests/test_audio_av_audio.py
git commit -m "feat(audio): AVAudioTransport composite class with shared engine"
```

---

## Task 12: AudioPath gains `transport_factory` field

Add the factory hook to `AudioPath` so the Hub can swap transports without conditional branches.

**Files:**
- Modify: `src/tend/audio/channels.py`
- Modify: `tests/test_audio_channels_select_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_audio_channels_select_path.py` (reuses `_stub_factory` already defined in the file):

```python
def test_audio_path_has_transport_factory_field_with_default(monkeypatch):
    """AudioPath gains a transport_factory field; default for non-VPIO
    branches is LocalAudioTransport so existing Linux paths keep working."""
    from pipecat.transports.local.audio import LocalAudioTransport

    monkeypatch.setattr(sys, "platform", "linux")
    settings = Settings(_env_file=None)
    path = select_audio_path(settings, aec_filter_factory=_stub_factory)

    assert path.transport_factory is LocalAudioTransport
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_channels_select_path.py::test_audio_path_has_transport_factory_field_with_default -v
```

Expected: FAIL with `AttributeError: 'AudioPath' object has no attribute 'transport_factory'`.

- [ ] **Step 3: Add `transport_factory` to `AudioPath` and `select_audio_path`**

Edit `src/tend/audio/channels.py` — update imports, the dataclass, and `select_audio_path`:

```python
# Update imports at top of file:
from typing import Callable

from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.local.audio import LocalAudioTransport

# Update the AudioPath dataclass:
@dataclass(frozen=True)
class AudioPath:
    """Result of platform-aware audio-path selection."""
    in_channels: int
    pre_vad_processors: tuple[FrameProcessor, ...]
    aec: AECPair | None
    transport_factory: Callable[[TransportParams], BaseTransport] = LocalAudioTransport
```

Update both `return AudioPath(...)` sites in `select_audio_path` to pass `transport_factory=LocalAudioTransport` explicitly (no behavior change yet — Task 13 adds the macOS-VPIO branch).

```python
def select_audio_path(
    settings: Settings,
    *,
    aec_filter_factory: Callable[[Settings], AECPair | None] = _default_aec_filter_factory,
) -> AudioPath:
    """Return the audio-path tuple appropriate for this platform."""
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
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_channels_select_path.py -v
```

Expected: all tests PASS (existing tests pick up the dataclass default and new test passes).

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/channels.py tests/test_audio_channels_select_path.py
git commit -m "feat(audio): AudioPath gains transport_factory field"
```

---

## Task 13: select_audio_path — macOS VPIO branch

When `aec_engine` resolves to `"vpio"`, return `AudioPath(..., aec=None, transport_factory=AVAudioTransport)`. The factory wrapping is needed because `AVAudioTransport` takes `AVAudioTransportParams`, not `LocalAudioTransportParams` — but the Hub will construct the right params subclass based on which factory it's using (Task 15).

**Files:**
- Modify: `src/tend/audio/channels.py`
- Modify: `tests/test_audio_channels_select_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_audio_channels_select_path.py`:

```python
def test_macos_vpio_engine_selects_avaudio_transport(monkeypatch):
    """When aec_engine resolves to 'vpio' on macOS, AudioPath uses
    AVAudioTransport as the factory and has no AEC pair (VPIO handles
    AEC upstream of pipecat)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    settings = Settings(_env_file=None, aec_engine="vpio")

    # The AEC filter factory should not be invoked — VPIO doesn't use a filter.
    def boom(_):
        raise AssertionError("AEC factory should not run for vpio engine")

    path = select_audio_path(settings, aec_filter_factory=boom)

    from tend.audio.av_audio import AVAudioTransport
    assert path.in_channels == 1
    assert path.pre_vad_processors == ()
    assert path.aec is None
    assert path.transport_factory is AVAudioTransport
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_channels_select_path.py::test_macos_vpio_engine_selects_avaudio_transport -v
```

Expected: FAIL — either select_audio_path doesn't branch on `aec_engine`, or it still calls the AEC factory.

- [ ] **Step 3: Implement the VPIO branch in `select_audio_path`**

Edit `src/tend/audio/channels.py`:

```python
def select_audio_path(
    settings: Settings,
    *,
    aec_filter_factory: Callable[[Settings], AECPair | None] = _default_aec_filter_factory,
) -> AudioPath:
    """Return the audio-path tuple appropriate for this platform."""
    from tend.audio.aec import resolve_aec_engine

    # VPIO short-circuit: AVAudioTransport with no filter.
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
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_channels_select_path.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/channels.py tests/test_audio_channels_select_path.py
git commit -m "feat(audio): select_audio_path picks AVAudioTransport for vpio engine"
```

---

## Task 14: resolve_aec_engine — add "vpio" + macOS auto resolution

Add `"vpio"` as a valid `aec_engine` value. The macOS auto resolver picks `"vpio"` (previously it picked `"webrtc-aec3"` if importable else `"speex"`). `"vpio"` on non-macOS raises a clear `ValueError`.

**Files:**
- Modify: `src/tend/audio/aec.py`
- Modify: `tests/test_audio_aec.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_audio_aec.py`:

```python
def test_resolve_aec_engine_macos_auto_returns_vpio(monkeypatch):
    """macOS + auto should resolve to vpio (the new default)."""
    import sys as _sys
    from tend.audio.aec import resolve_aec_engine

    monkeypatch.setattr(_sys, "platform", "darwin")

    class _S:
        aec_engine = "auto"

    assert resolve_aec_engine(_S()) == "vpio"


def test_resolve_aec_engine_macos_explicit_vpio_passes_through(monkeypatch):
    import sys as _sys
    from tend.audio.aec import resolve_aec_engine

    monkeypatch.setattr(_sys, "platform", "darwin")

    class _S:
        aec_engine = "vpio"

    assert resolve_aec_engine(_S()) == "vpio"


def test_resolve_aec_engine_vpio_on_linux_raises():
    """Explicit vpio on Linux is a user error — raise with a clear message."""
    import sys as _sys
    import pytest
    from tend.audio.aec import resolve_aec_engine

    saved = _sys.platform
    try:
        _sys.platform = "linux"

        class _S:
            aec_engine = "vpio"

        with pytest.raises(ValueError, match="vpio.*only supported on macOS"):
            resolve_aec_engine(_S())
    finally:
        _sys.platform = saved


def test_resolve_aec_engine_macos_explicit_speex_passes_through(monkeypatch):
    """User can still opt out of VPIO by explicitly picking speex."""
    import sys as _sys
    from tend.audio.aec import resolve_aec_engine

    monkeypatch.setattr(_sys, "platform", "darwin")

    class _S:
        aec_engine = "speex"

    assert resolve_aec_engine(_S()) == "speex"
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_audio_aec.py -v -k resolve_aec
```

Expected: FAIL — `resolve_aec_engine` doesn't accept `"vpio"` or auto-resolves to something else on macOS.

- [ ] **Step 3: Update `resolve_aec_engine`**

Edit `src/tend/audio/aec.py`:

```python
def resolve_aec_engine(settings) -> str:
    """Resolve `[audio] aec_engine` to a concrete engine name.

    Returns one of: "off", "vpio", "speex", "webrtc-aec3".
    """
    engine = (settings.aec_engine or "auto").lower()

    if engine == "vpio":
        if sys.platform != "darwin":
            raise ValueError(
                "aec_engine='vpio' is only supported on macOS. "
                "On Linux use 'speex' (or 'off' if your mic has hardware AEC)."
            )
        return "vpio"

    if engine in ("off", "speex", "webrtc-aec3"):
        return engine

    if engine != "auto":
        logger.warning("Unknown aec_engine=%r, defaulting to off", engine)
        return "off"

    # auto resolution
    if sys.platform == "darwin":
        return "vpio"
    logger.info(
        "aec_engine=auto on %s resolved to off — assumes hardware AEC "
        "(e.g. XVF3800). Set [audio] aec_engine=speex to override.",
        sys.platform,
    )
    return "off"
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_audio_aec.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/aec.py tests/test_audio_aec.py
git commit -m "feat(audio): resolve_aec_engine adds vpio + macOS auto resolves to vpio"
```

---

## Task 15: Hub.build_pipeline — use transport_factory

Hub currently constructs `LocalAudioTransport` (or `RefTappedLocalAudioTransport`) directly. Switch to `path.transport_factory(params)` so the macOS-VPIO path gets `AVAudioTransport`. Construct the right params subclass per factory.

**Files:**
- Modify: `src/tend/audio/hub.py`

- [ ] **Step 1: Read the current Hub.build_pipeline (lines 134-225 of hub.py)**

Familiarise yourself with the existing structure. The key block is:

```python
if path.aec is not None:
    from tend.audio.transport import RefTappedLocalAudioTransport
    transport = RefTappedLocalAudioTransport(params, reference=path.aec.reference)
else:
    transport = LocalAudioTransport(params)
```

- [ ] **Step 2: Replace transport construction with factory-driven dispatch**

Edit `src/tend/audio/hub.py`, replacing the block above with:

```python
# Construct the right params subclass for the chosen transport factory.
# AVAudioTransport needs AVAudioTransportParams; LocalAudioTransport variants
# need LocalAudioTransportParams. They share TransportParams as a base so
# all the AEC/IO-related fields exist on both.
from tend.audio.av_audio import AVAudioTransport, AVAudioTransportParams

if path.transport_factory is AVAudioTransport:
    av_params = AVAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=self._settings.sample_rate,
        audio_in_channels=path.in_channels,
        audio_out_sample_rate=self._tts_sample_rate,
    )
    transport = AVAudioTransport(av_params)
elif path.aec is not None:
    from tend.audio.transport import RefTappedLocalAudioTransport
    transport = RefTappedLocalAudioTransport(params, reference=path.aec.reference)
else:
    transport = path.transport_factory(params)
```

- [ ] **Step 3: Verify existing audio + tests still pass**

```bash
pytest tests/test_audio_av_audio.py tests/test_audio_channels_select_path.py tests/test_audio_aec.py -v
```

Expected: all tests PASS. Hub itself doesn't have a unit test that exercises `build_pipeline` (it's covered by integration / manual smoke tests).

- [ ] **Step 4: Commit**

```bash
git add src/tend/audio/hub.py
git commit -m "feat(audio): Hub.build_pipeline uses path.transport_factory"
```

---

## Task 16: Settings field doc + README + ROADMAP

Bookkeeping. Update the field comment in `config.py`, note VPIO is the macOS default in `README.md`, move the VPIO line out of the "candidates" list in `ROADMAP.md`.

**Files:**
- Modify: `src/tend/config.py`
- Modify: `README.md`
- Modify: `ROADMAP.md`

- [ ] **Step 1: Update Settings field comment**

Edit `src/tend/config.py` — find the line:

```python
    aec_engine: str = "auto"                # auto | webrtc-aec3 | speex | off
```

Replace with:

```python
    aec_engine: str = "auto"                # auto | vpio | speex | webrtc-aec3 | off
                                            # macOS auto → vpio; Linux auto → off
```

- [ ] **Step 2: Update README**

Find the macOS install/AEC section in `README.md`. Add (or update existing AEC note):

```markdown
**AEC on macOS:** tend defaults to Apple's VoiceProcessingIO (`aec_engine = "vpio"`)
for AEC + noise suppression + AGC — the same audio processing path FaceTime uses.
This is the recommended setting for built-in MacBook mic+speaker.

To override: set `aec_engine = "speex"` in `tend.toml` for the software AEC
(useful if you have an external mic with no echo path).
```

- [ ] **Step 3: Update ROADMAP**

Edit `ROADMAP.md` — remove the "Mac AEC via Apple VoiceProcessingIO" bullet from the "After public release" section. Add a CHANGELOG-style note instead (or, if no CHANGELOG yet, add one line to the "Where we are now" section noting AVAudioTransport shipped).

Specifically:
- Delete the bullet beginning `- **Mac AEC via Apple VoiceProcessingIO...`
- In the "Where we are now (v0.1.0-dev)" paragraph, append: `On macOS the audio transport uses AVAudioEngine with VoiceProcessingIO for OS-grade AEC + NS + AGC; software-AEC paths remain available as explicit opt-outs.`

- [ ] **Step 4: Commit**

```bash
git add src/tend/config.py README.md ROADMAP.md
git commit -m "docs: VPIO is the macOS default AEC; remove from roadmap candidates"
```

---

## Task 17: Manual end-to-end smoke test

Run tend on the user's MacBook, validate the conversational loop with VPIO active. This is the acceptance criterion — not automated.

**Files:** none (manual test)

- [ ] **Step 1: Run the PoC one more time to confirm baseline**

```bash
python scripts/audio_check_vpio.py
```

Expected: `suppression ≥30 dB: PASS` and `speech RMS ≥500: PASS`.

- [ ] **Step 2: Start tend**

```bash
python -m tend
```

Watch for `[av_audio] VPIO enabled` and `[av_audio] native input format` log lines confirming the transport is active.

Expected log lines (approximately):
```
[hub] AudioPath: in_channels=1 pre_vad=[] aec=None
[av_audio] VPIO enabled: isVoiceProcessingEnabled=True
[av_audio] native input format: sr=48000.0 ch=1
```

- [ ] **Step 3: Wake-word test**

Say the wake phrase. Verify tend acknowledges and responds. Confirm:
- No spurious wake on bot's own voice (used to happen with speex residual on the 1.5 s cooldown boundary).
- STT picks up user speech cleanly.

- [ ] **Step 4: Barge-in test**

Ask a long-answer question (e.g., "introduce yourself to the group"). While the bot is speaking, interrupt with "stop" or a different question. Verify:
- TTS stops within ~200 ms of user speaking.
- Conversation continues with the new input.

- [ ] **Step 5: Echo-residual test**

Ask the bot to respond with a phrase that historically caused echo-residual transcription (e.g., "say hello, I'm Jarvis"). After the bot speaks, look at the logs:

```bash
journalctl --user -u tend -n 100 2>/dev/null || tail -n 100 ~/Library/Logs/tend/tend.log
```

Look for `transcription` lines around the bot's speaking window. Expected: **no user-side transcription of the bot's voice**. Compare to behavior with `aec_engine="speex"` (set in `tend.toml`, restart, observe transcription fragments appear).

- [ ] **Step 6: Toggle test (speex still works)**

Edit `tend.toml`:

```toml
[audio]
aec_engine = "speex"
```

Restart tend. Verify it boots with the speex path — log should show `[hub] AudioPath: in_channels=1 pre_vad=[] aec=AECPair`. This confirms the opt-out path is still usable.

Revert `aec_engine = "auto"` (or `"vpio"`) and restart.

- [ ] **Step 7: Record findings, commit (if any extra fixes)**

If a real issue surfaces during the smoke test, fix it as part of this task (per the task scope guidance — bugs found during development belong in the same task). Otherwise, this task is complete with no commit.

---

## Self-Review Checklist

After all tasks pass, verify against the spec:

- [ ] **Spec section: Problem** — addressed by VPIO replacing speex; no AEC filter runs when vpio is active.
- [ ] **Spec section: Goal** — `AVAudioTransport` lives in `src/tend/audio/av_audio.py` (Task 11), mirrors pipecat's local/audio.py shape, voice processing is default-on (Task 5 enables it in start()).
- [ ] **Spec section: Approach** — chosen approach (AVAudioEngine + setVoiceProcessingEnabled) implemented; alternatives (raw AudioUnit, manual rendering) not built. ✓
- [ ] **Spec section: Architecture (single module)** — yes, all in `av_audio.py` (Tasks 2-11).
- [ ] **Spec section: Engine lifecycle** — Task 5 (VPIO before start), Task 8 (player attach before play), Task 11 (shared engine).
- [ ] **Spec section: Format handling** — Task 4 (AVAudioConverter), Task 3 (PCM helpers).
- [ ] **Spec section: Threading** — Task 6 (input bridge via run_coroutine_threadsafe), Task 9 (output bridge via call_soon_threadsafe).
- [ ] **Spec section: Output backpressure** — Task 9 (.dataPlayedBack completion type).
- [ ] **Spec section: Wiring (`transport_factory`)** — Task 12 (field added), Task 13 (VPIO branch), Task 15 (Hub uses factory).
- [ ] **Spec section: aec_engine setting** — Task 14 ("vpio" + macOS auto).
- [ ] **Spec section: De-risking PoC** — Task 1.
- [ ] **Spec section: Configuration changes** — Task 16 (Settings comment, README, ROADMAP).
- [ ] **Spec section: Failure modes** — VPIO failure raises in Task 5; AVAudioConverter failure logs in Task 4; route-change handling intentionally deferred per spec.
- [ ] **Spec section: Dependencies** — pyobjc-framework-AVFoundation already installed; pyproject.toml not touched. ✓
- [ ] **Spec section: Testing** — unit tests in Tasks 2-11; PoC in Task 1; manual smoke in Task 17.
- [ ] **Spec section: Out of scope** — upstreaming, device selection, route-change auto-recovery, ducking — none of these are tasked.

---

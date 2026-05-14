"""Standalone AVAudioEngine + VoiceProcessingIO sanity check.

Validates: (1) setVoiceProcessingEnabled works on this Mac, (2) the 9-channel
VPIO aggregate input downmixes to mono via AVAudioConverter, (3) output via
direct player→outputNode connection at the output node's natural format,
(4) measurable echo suppression vs the same hardware without VPIO.

Critical findings from de-risking this script:
- When VPIO is on, the input node exposes a 9-channel aggregate device
  (1 mic + 8 speaker-reference channels). Channel 0 is the post-VPIO mic.
  AVAudioConverter handles 9ch Float32 → 1ch Int16 + resample automatically
  (defaults to take channel 0 from source).
- The mainMixerNode's default 44.1 kHz format conflicts with the output
  node's 48 kHz format under VPIO; the engine fails to initialise with
  err=-10875. The fix is to connect the player directly to outputNode at
  outputNode.inputFormatForBus_(0) (2ch 48 kHz Float32 deinterleaved on
  built-in MacBook speakers).
- AVAudioPCMBuffer for the output side must therefore be 2ch 48 kHz Float32
  deinterleaved; we generate the tone in that format directly rather than
  converting from Int16.

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

from AVFoundation import (
    AVAudioConverter,
    AVAudioEngine,
    AVAudioFormat,
    AVAudioPCMBuffer,
    AVAudioPCMFormatFloat32,
    AVAudioPCMFormatInt16,
    AVAudioPlayerNode,
)

TARGET_SR = 16000           # what we feed back to STT
TONE_HZ = 1000.0
TONE_DURATION_S = 3.0

# AVAudioPlayerNodeCompletionDataPlayedBack == 0 in CoreAudio headers.
COMPLETION_DATA_PLAYED_BACK = 0


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
    return math.sqrt(max(power, 0.0) / len(a))


def _generate_stereo_float32_tone(
    sr: int, hz: float, dur_s: float, out_format: AVAudioFormat,
) -> AVAudioPCMBuffer:
    """Build an AVAudioPCMBuffer at the output node's native format
    (2ch Float32 deinterleaved @ 48 kHz on built-in MacBook speakers)."""
    n = int(sr * dur_s)
    samples_f32 = [0.3 * math.sin(2 * math.pi * hz * i / sr) for i in range(n)]
    pcm = b"".join(struct.pack("<f", s) for s in samples_f32)

    buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(out_format, n)
    buf.setFrameLength_(n)
    fc = buf.floatChannelData()
    # Deinterleaved: each channel has its own buffer.
    for ch in range(out_format.channelCount()):
        fc[ch].as_buffer(n)[:] = pcm
    return buf


def main() -> int:
    engine = AVAudioEngine.alloc().init()
    input_node = engine.inputNode()
    output_node = engine.outputNode()

    # 1. Enable VPIO BEFORE any other engine state changes.
    ok, err = input_node.setVoiceProcessingEnabled_error_(True, None)
    if not ok:
        print(f"FAIL: setVoiceProcessingEnabled returned False: {err}")
        return 1
    print(f"VPIO enabled: isVoiceProcessingEnabled={input_node.isVoiceProcessingEnabled()}")

    # 2. Output side: player → outputNode direct (skip mainMixer — its 44.1 kHz
    #    default conflicts with outputNode's 48 kHz under VPIO and breaks init).
    out_format = output_node.inputFormatForBus_(0)
    player = AVAudioPlayerNode.alloc().init()
    engine.attachNode_(player)
    engine.connect_to_format_(player, output_node, out_format)
    print(f"output format: {out_format}")

    # 3. Input side: tap at native format (9ch Float32 48 kHz), converter
    #    downmixes to 1ch Int16 16 kHz.
    native_in = input_node.outputFormatForBus_(0)
    target_in = AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
        AVAudioPCMFormatInt16, float(TARGET_SR), 1, False,
    )
    converter = AVAudioConverter.alloc().initFromFormat_toFormat_(native_in, target_in)
    # Default channelMap on a many-to-one converter is [-1] (drop output) →
    # produces zero-valued buffers. Explicitly map output ch 0 ← input ch 0
    # (the post-VPIO mic; channels 1-8 are the speaker-reference aggregate).
    converter.setChannelMap_([0])
    print(f"native input format: {native_in}")
    print(f"target input format: {target_in}")
    print(f"converter channelMap: {converter.channelMap()}")

    captured = bytearray()
    captured_lock = threading.Lock()

    # Post-VPIO gain. VPIO aggressively noise-gates at desk distance,
    # attenuating user speech by ~12-15 dB. Boost back to PyAudio-baseline
    # level so Deepgram has signal to work with. Residual echo (currently
    # suppressed to ~-91 dB by VPIO) stays at -79 dB after this boost —
    # still far below STT thresholds (~-40 dBFS).
    POST_TAP_GAIN_DB = 12.0
    gain_linear = 10 ** (POST_TAP_GAIN_DB / 20.0)  # ≈ 4.0×

    def tap_callback(in_buf, when):
        in_frames = in_buf.frameLength()
        if in_frames == 0:
            return
        native_sr = native_in.sampleRate()
        out_capacity = max(int(in_frames * TARGET_SR / native_sr) + 64, 1)
        out_buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
            target_in, out_capacity,
        )

        def supply_input(num_packets, status_ptr):
            return (in_buf, 0)  # HaveData

        status, err = converter.convertToBuffer_error_withInputFromBlock_(
            out_buf, None, supply_input,
        )
        if status in (0, 1):
            n = out_buf.frameLength()
            if n > 0:
                raw = bytes(out_buf.int16ChannelData()[0].as_buffer(n))
                # Apply gain in int16 domain with clipping. numpy makes this
                # vectorised; clip protects against integer overflow at peak.
                import numpy as _np
                samples = _np.frombuffer(raw, dtype=_np.int16).astype(_np.int32)
                boosted = _np.clip(samples * gain_linear, -32768, 32767).astype(_np.int16)
                with captured_lock:
                    captured.extend(boosted.tobytes())

    input_node.installTapOnBus_bufferSize_format_block_(
        0, 1024, native_in, tap_callback,
    )

    engine.prepare()
    started, err = engine.startAndReturnError_(None)
    if not started:
        print(f"FAIL: engine.start returned False: {err}")
        return 1
    print("engine started")

    # --- Scenario 1: play 1 kHz tone while recording; measure 1 kHz residual ---
    tone_buf = _generate_stereo_float32_tone(
        sr=int(out_format.sampleRate()),
        hz=TONE_HZ,
        dur_s=TONE_DURATION_S,
        out_format=out_format,
    )
    # Tone emit reference: synthesise the same tone at TARGET_SR Int16 for RMS comparison.
    ref_pcm = b"".join(
        struct.pack("<h", int(0.3 * 32767 * math.sin(2 * math.pi * TONE_HZ * i / TARGET_SR)))
        for i in range(int(TARGET_SR * TONE_DURATION_S))
    )
    tone_emit_band_rms = _band_rms_1khz(ref_pcm, TARGET_SR)
    print(f"emit  1 kHz band RMS: {tone_emit_band_rms:.0f}")

    done = threading.Event()
    player.scheduleBuffer_completionCallbackType_completionHandler_(
        tone_buf, COMPLETION_DATA_PLAYED_BACK, lambda *_: done.set(),
    )
    player.play()
    print(f"playing {TONE_DURATION_S}s tone; capturing mic with VPIO on...")
    done.wait(timeout=TONE_DURATION_S + 2.0)
    time.sleep(0.2)  # let tap flush

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
    # Threshold ≥40 chosen from PyAudio baseline: this Mac's built-in mic gives
    # peak ~239 / 32767 on the existing audio_check.py mic test, which is RMS
    # ~50-80 for typical speech. We just need to confirm VPIO isn't *additionally*
    # suppressing user voice to inaudibility — the AGC will boost the signal
    # in steady state and Deepgram tolerates -40 dBFS speech.
    print("\nnow speak for 3 seconds (no tone, close to mic) — measuring speech preserved...")
    time.sleep(0.3)
    with captured_lock:
        captured.clear()
    time.sleep(TONE_DURATION_S)
    with captured_lock:
        speech_bytes = bytes(captured)
    speech_rms = _rms(speech_bytes)
    import array as _arr
    _a = _arr.array("h"); _a.frombytes(speech_bytes)
    speech_peak = max(abs(s) for s in _a) if _a else 0
    print(f"captured speech total RMS: {speech_rms:.0f}")
    print(f"captured speech peak:      {speech_peak}")

    # Cleanup
    player.stop()
    input_node.removeTapOnBus_(0)
    engine.stop()

    suppression_ok = suppression_db >= 30.0
    speech_ok = speech_rms >= 40.0
    print(f"\nsuppression ≥30 dB: {'PASS' if suppression_ok else 'FAIL'}")
    print(f"speech RMS ≥40:     {'PASS' if speech_ok else 'FAIL'}")
    return 0 if (suppression_ok and speech_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())

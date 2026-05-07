"""Standalone PyAudio sanity check for the mic and speaker.

Same library path tend itself uses (PyAudio → PortAudio → ALSA), so a pass
here means the pipeline-level routing is the culprit, not the device.

Usage:
    python scripts/audio_check.py speaker   # play a 1 kHz tone for 1 s
    python scripts/audio_check.py mic       # record 3 s, save to /tmp/tend_mic.wav
    python scripts/audio_check.py loopback  # record 3 s, then play it back
"""

from __future__ import annotations

import math
import struct
import sys
import wave
from pathlib import Path

import pyaudio

SAMPLE_RATE = 16000
WAV_PATH = Path("/tmp/tend_mic.wav")


def _devices(pa: pyaudio.PyAudio) -> tuple[int, int]:
    in_idx = pa.get_default_input_device_info()["index"]
    out_idx = pa.get_default_output_device_info()["index"]
    print(f"input  device [{in_idx}]: {pa.get_device_info_by_index(in_idx)['name']}")
    print(f"output device [{out_idx}]: {pa.get_device_info_by_index(out_idx)['name']}")
    return in_idx, out_idx


def speaker(pa: pyaudio.PyAudio) -> None:
    _, out_idx = _devices(pa)
    duration_s = 1.0
    freq = 1000.0
    n = int(SAMPLE_RATE * duration_s)
    samples = (
        int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        for i in range(n)
    )
    pcm = b"".join(struct.pack("<h", s) for s in samples)

    stream = pa.open(
        format=pa.get_format_from_width(2),
        channels=1,
        rate=SAMPLE_RATE,
        output=True,
        output_device_index=out_idx,
    )
    print(f"playing 1 kHz tone for {duration_s}s @ {SAMPLE_RATE} Hz...")
    stream.write(pcm)
    stream.stop_stream()
    stream.close()
    print("done.")


def mic(pa: pyaudio.PyAudio, duration_s: float = 3.0) -> Path:
    in_idx, _ = _devices(pa)
    chunk = 1024
    n_chunks = int(SAMPLE_RATE * duration_s / chunk)

    stream = pa.open(
        format=pa.get_format_from_width(2),
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=in_idx,
        frames_per_buffer=chunk,
    )
    print(f"recording {duration_s}s @ {SAMPLE_RATE} Hz... speak now.")
    frames = [stream.read(chunk, exception_on_overflow=False) for _ in range(n_chunks)]
    stream.stop_stream()
    stream.close()

    with wave.open(str(WAV_PATH), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))

    peak = max(abs(int.from_bytes(b"".join(frames)[i:i + 2], "little", signed=True))
               for i in range(0, len(b"".join(frames)), 2))
    print(f"saved {WAV_PATH} (peak amplitude {peak} / 32767)")
    if peak < 200:
        print("  WARNING: very low signal — mic may be muted or wrong device.")
    return WAV_PATH


def loopback(pa: pyaudio.PyAudio) -> None:
    path = mic(pa)
    _, out_idx = _devices(pa)
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        data = wf.readframes(wf.getnframes())
    stream = pa.open(format=pa.get_format_from_width(sw), channels=ch, rate=rate, output=True, output_device_index=out_idx)
    print(f"playing back {path}...")
    stream.write(data)
    stream.stop_stream()
    stream.close()
    print("done.")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("speaker", "mic", "loopback"):
        print(__doc__)
        return 2
    pa = pyaudio.PyAudio()
    try:
        {"speaker": speaker, "mic": mic, "loopback": loopback}[sys.argv[1]](pa)
    finally:
        pa.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

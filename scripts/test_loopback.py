"""Record 6 seconds from the mic, then play it back through the speaker.

Usage: .venv/bin/python scripts/test_loopback.py
"""
from __future__ import annotations

import struct
import time
import wave

import pyaudio

from hasat.config import settings

DURATION = 6
RATE = 16000
CHUNK = 1600
WAV_PATH = "/tmp/hasat_loopback.wav"


def main() -> None:
    p = pyaudio.PyAudio()

    in_idx = settings.input_device_index if settings.input_device_index is not None else 1
    out_idx = settings.output_device_index if settings.output_device_index is not None else 1

    stream_in = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=RATE,
        input=True,
        input_device_index=in_idx,
        frames_per_buffer=CHUNK,
    )

    print(f"Recording {DURATION}s from input device {in_idx} — speak now...", flush=True)
    frames: list[bytes] = []
    peak = 0
    t0 = time.time()
    while time.time() - t0 < DURATION:
        data = stream_in.read(CHUNK, exception_on_overflow=False)
        frames.append(data)
        samples = struct.unpack(f"{len(data) // 2}h", data)
        m = max(abs(x) for x in samples)
        if m > peak:
            peak = m
    stream_in.stop_stream()
    stream_in.close()

    audio = b"".join(frames)
    print(f"Done. Peak amplitude: {peak}/32767 ({100 * peak / 32767:.1f}%)", flush=True)

    with wave.open(WAV_PATH, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio)
    print(f"Saved to {WAV_PATH}", flush=True)

    print(f"Playing back through output device {out_idx}...", flush=True)
    stream_out = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=RATE,
        output=True,
        output_device_index=out_idx,
    )
    stream_out.write(audio)
    stream_out.stop_stream()
    stream_out.close()
    p.terminate()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()

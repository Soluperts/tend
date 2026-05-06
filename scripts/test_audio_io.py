"""Open simultaneous PyAudio capture + playback exactly like LocalAudioTransport
does, then push a few seconds of silence to playback while reading from the mic.
If THIS segfaults, the crash is PyAudio/ALSA-side, not pipecat.

Usage: .venv/bin/python scripts/test_audio_io.py
"""
from __future__ import annotations

import faulthandler
import sys
import time

import pyaudio

from hasat.config import settings

faulthandler.enable(file=sys.stderr, all_threads=True)

IN_RATE = settings.sample_rate          # 16000
OUT_RATE = 24000                        # ElevenLabs native
IN_DEV = settings.input_device_index    # 1
OUT_DEV = settings.output_device_index  # 1
CHUNK = 320                             # 20 ms @ 16 kHz


def _list_devices(p: pyaudio.PyAudio) -> None:
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        print(
            f"  [{i}] {info['name']!r} "
            f"in={info['maxInputChannels']} out={info['maxOutputChannels']} "
            f"defaultSR={info['defaultSampleRate']}",
            flush=True,
        )


def main() -> None:
    p = pyaudio.PyAudio()
    print("PyAudio devices:", flush=True)
    _list_devices(p)
    print(
        f"\nopening: in dev={IN_DEV} rate={IN_RATE}, out dev={OUT_DEV} rate={OUT_RATE}",
        flush=True,
    )

    in_stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=IN_RATE,
        input=True,
        input_device_index=IN_DEV,
        frames_per_buffer=CHUNK,
    )
    print("input stream opened", flush=True)

    out_stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=OUT_RATE,
        output=True,
        output_device_index=OUT_DEV,
        frames_per_buffer=int(OUT_RATE * 0.02),
    )
    print("output stream opened", flush=True)

    silence_24k = b"\x00\x00" * int(OUT_RATE * 0.02)
    print("running 3s of duplex…", flush=True)
    t0 = time.monotonic()
    frames_read = 0
    while time.monotonic() - t0 < 3.0:
        data = in_stream.read(CHUNK, exception_on_overflow=False)
        frames_read += 1
        out_stream.write(silence_24k)
    print(f"duplex ok, {frames_read} input chunks, {time.monotonic() - t0:.2f}s", flush=True)

    print("closing…", flush=True)
    in_stream.stop_stream()
    in_stream.close()
    out_stream.stop_stream()
    out_stream.close()
    p.terminate()
    print("done — no segfault", flush=True)


if __name__ == "__main__":
    main()

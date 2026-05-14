"""Software AEC engine factory + speaker-reference ring buffer.

Pipecat's `BaseAudioFilter.filter(audio: bytes) -> bytes` only sees the
mic chunk. To run AEC we also need the speaker reference signal — the
audio that was just played. `OutputAudioCapture` (in `output_tap.py`)
appends each `OutputAudioRawFrame.audio` to a `ReferenceBuffer`. The
AEC filter reads aligned bytes back from the same buffer at filter
time.

The "alignment" is naive — both engines (Speex and WebRTC AEC3) have
internal adaptive delay estimation, so the buffer just hands back the
most-recently-played bytes for the requested length.
"""

from __future__ import annotations

import logging
import sys
import threading

logger = logging.getLogger(__name__)


class ReferenceBuffer:
    """Thread-safe ring buffer holding the most recent speaker samples.

    Reads always return `n` bytes — silence is padded at the front if
    fewer than `n` bytes have been written.
    """

    def __init__(self, capacity_bytes: int = 32_000):  # 1 s at 16 kHz mono
        self._buf = bytearray(capacity_bytes)
        self._capacity = capacity_bytes
        self._size = 0      # number of bytes written, capped at capacity
        self._head = 0      # next write position
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            for b in data:
                self._buf[self._head] = b
                self._head = (self._head + 1) % self._capacity
                if self._size < self._capacity:
                    self._size += 1

    def read(self, n: int) -> bytes:
        """Return the n most-recent bytes, padding silence at the front."""
        with self._lock:
            if self._size < n:
                pad = b"\x00" * (n - self._size)
                # the actual self._size bytes start at (head - size) mod capacity
                start = (self._head - self._size) % self._capacity
                tail = self._read_at(start, self._size)
                return pad + tail
            start = (self._head - n) % self._capacity
            return self._read_at(start, n)

    def _read_at(self, start: int, n: int) -> bytes:
        if start + n <= self._capacity:
            return bytes(self._buf[start:start + n])
        first = self._capacity - start
        return bytes(self._buf[start:]) + bytes(self._buf[:n - first])


def _webrtc_importable() -> bool:
    """Return True if the optional webrtc-audio-processing package is importable."""
    try:
        import webrtc_audio_processing  # noqa: F401
        return True
    except ImportError:
        return False


def resolve_aec_engine(settings) -> str:
    """Resolve `[audio] aec_engine` to a concrete engine name.

    Returns one of: "off", "speex", "webrtc-aec3".
    """
    engine = (settings.aec_engine or "auto").lower()

    if engine in ("off", "speex", "webrtc-aec3"):
        return engine

    if engine != "auto":
        logger.warning("Unknown aec_engine=%r, defaulting to off", engine)
        return "off"

    # auto resolution
    if sys.platform == "darwin":
        return "webrtc-aec3" if _webrtc_importable() else "speex"
    return "off"

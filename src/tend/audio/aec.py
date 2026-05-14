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
from typing import Optional

from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterControlFrame, FilterEnableFrame

logger = logging.getLogger(__name__)


class ReferenceBuffer:
    """Thread-safe ring buffer holding the most recent speaker samples.

    Reads always return `n` bytes — silence is padded at the front if
    fewer than `n` bytes have been written.
    """

    def __init__(self, capacity_bytes: int = 32_000):  # 1 s at 16 kHz mono 16-bit PCM
        self._buf = bytearray(capacity_bytes)
        self._capacity = capacity_bytes
        self._size = 0      # number of bytes written, capped at capacity
        self._head = 0      # next write position
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            n = len(data)
            if n == 0:
                return
            if n >= self._capacity:
                # Writing more than the buffer holds — keep only the last `capacity` bytes.
                self._buf[:] = data[-self._capacity:]
                self._head = 0
                self._size = self._capacity
                return
            end = self._head + n
            if end <= self._capacity:
                self._buf[self._head:end] = data
            else:
                first = self._capacity - self._head
                self._buf[self._head:] = data[:first]
                self._buf[:n - first] = data[first:]
            self._head = (self._head + n) % self._capacity
            self._size = min(self._size + n, self._capacity)

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


class SpeexAECFilter(BaseAudioFilter):
    """Pipecat-compatible AEC filter backed by pyaec (SpeexDSP).

    Reads a same-length reference chunk from `reference` on each
    `filter()` call and asks pyaec to cancel the echo. Length in == length out.
    """

    # 20 ms of int16 mono at 16 kHz = 320 samples = 640 bytes; pyaec is
    # happy with any matched-length pair but frame size 320 matches
    # pipecat's transport chunking.
    _FRAME_SAMPLES = 320
    _FILTER_LENGTH = 320 * 8  # ~160 ms tail; covers desk-distance echo + hw latency

    def __init__(self, reference: ReferenceBuffer):
        self._reference = reference
        self._sample_rate = 0
        self._aec = None
        self._pyaec_lib = None
        self._enabled = True

    async def start(self, sample_rate: int):
        from pyaec import Aec
        from pyaec import lib as _pyaec_lib
        self._sample_rate = sample_rate
        # Keep a reference to pyaec's ctypes lib on the instance. pyaec stores it
        # as a module-level global; during CPython interpreter shutdown the global
        # can be cleared before Aec.__del__ runs, causing a SIGSEGV. Holding our
        # own reference here keeps it alive until we explicitly tear down in stop().
        self._pyaec_lib = _pyaec_lib
        self._aec = Aec(
            frame_size=self._FRAME_SAMPLES,
            filter_length=self._FILTER_LENGTH,
            sample_rate=sample_rate,
            enable_preprocess=True,
        )

    async def stop(self):
        # Explicitly call AecDestroy while our lib reference is still valid,
        # then null pyaec's internal pointer so Aec.__del__ skips a double-free.
        if self._aec is not None and self._pyaec_lib is not None and self._aec._aec:
            self._pyaec_lib.AecDestroy(self._aec._aec)
            self._aec._aec = None
        self._aec = None
        self._pyaec_lib = None

    async def process_frame(self, frame: FilterControlFrame):
        if isinstance(frame, FilterEnableFrame):
            self._enabled = frame.enable

    async def filter(self, audio: bytes) -> bytes:
        if not self._enabled or self._aec is None:
            return audio

        # Read aligned reference bytes.
        ref = self._reference.read(len(audio))

        import array
        from ctypes import c_int16

        mic = array.array("h")
        mic.frombytes(audio)
        rfa = array.array("h")
        rfa.frombytes(ref)

        # Call AecCancelEcho directly with named ctypes arrays rather than via
        # pyaec.Aec.cancel_echo(), which creates unnamed ctypes temporaries that
        # enter a GC cycle with asyncio internals and survive into interpreter
        # shutdown, causing a SIGSEGV after pyaec's module-level state is torn
        # down. Named references avoid that ordering hazard entirely.
        frame_size = len(mic)
        mic_c = (c_int16 * frame_size)(*mic.tolist())
        ref_c = (c_int16 * frame_size)(*rfa.tolist())
        out_c = (c_int16 * frame_size)()
        self._pyaec_lib.AecCancelEcho(
            self._aec._aec, mic_c, ref_c, out_c, frame_size,
        )
        out = array.array("h", list(out_c)).tobytes()

        # Defensive: if pyaec ever returns mismatched length, pad/truncate to input length.
        if len(out) != len(audio):
            out = (out + b"\x00" * len(audio))[:len(audio)]
        return out


def make_aec_filter(
    engine: str,
    *,
    sample_rate: int,
    reference: ReferenceBuffer,
) -> Optional[BaseAudioFilter]:
    """Instantiate an AEC filter for the given engine name.

    "off" returns None (caller installs no filter).
    "speex" returns a SpeexAECFilter.
    "webrtc-aec3" raises RuntimeError if the optional dep is missing.
    """
    if engine == "off":
        return None
    if engine == "speex":
        return SpeexAECFilter(reference=reference)
    if engine == "webrtc-aec3":
        if not _webrtc_importable():
            raise RuntimeError(
                "aec_engine='webrtc-aec3' requested but webrtc-audio-processing "
                "is not installed. Install with: pipx inject tend webrtc-audio-processing"
            )
        # Implementation gated to Task 15.
        raise NotImplementedError("webrtc-aec3 filter — see Task 15")
    raise ValueError(f"Unknown aec engine: {engine!r}")


def resolve_aec_engine(settings) -> str:
    """Resolve `[audio] aec_engine` to a concrete engine name.

    Returns one of: "off", "speex", "webrtc-aec3".

    Note: `settings` is intentionally untyped to keep this module free of
    a `tend.config.Settings` import, which would pull pydantic-settings
    into every AEC test. Any object with an `aec_engine` attribute works.
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
    logger.info(
        "aec_engine=auto on %s resolved to off — assumes hardware AEC "
        "(e.g. XVF3800). Set [audio] aec_engine=speex to override.",
        sys.platform,
    )
    return "off"

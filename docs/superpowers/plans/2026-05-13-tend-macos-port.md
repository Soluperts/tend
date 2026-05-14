# tend macOS port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make tend installable and runnable on macOS 14+ (Apple Silicon) with software AEC that preserves barge-in, local TTS via Apple's AVSpeechSynthesizer, and a launchd-based service install — without breaking the existing Pi/Linux flow.

**Architecture:** Hub's pipeline becomes platform-agnostic by consuming an `AudioPath` from `select_audio_path(settings)`. Software AEC plugs into pipecat's `audio_in_filter` slot on `LocalAudioTransportParams`; a new `OutputAudioCapture` processor taps speaker output into a `ReferenceBuffer` the AEC filter reads. TTS is provider-dispatched in `services._make_tts`; macOS gets a new `AVSpeechSynthesizerTTSService` backed by `pyobjc-framework-AVFoundation`. The `service.py` CLI grows mac branches using `launchctl bootstrap/bootout/kickstart/kill/print`. TCC permission is probed in `tend setup` and `tend doctor` by opening a brief PyAudio stream.

**Tech Stack:** Python 3.11+, pipecat-ai, pipecat-subagents, PyAudio + PortAudio, pyaec (SpeexDSP), optional webrtc-audio-processing, pyobjc-framework-AVFoundation, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-05-13-tend-macos-port-design.md`

**Working directory assumption:** all commands are run from the repo root unless stated otherwise.

**Test convention:** every test file matches the project's existing style — `from __future__ import annotations`, `pytest.mark.asyncio` for async, monkeypatch for platform mocking. Run pytest with `python -m pytest tests/ -v`.

---

## Task 1: AudioPath dataclass + select_audio_path scaffold

**Goal:** Add the platform-dispatch helper to `audio/channels.py`. No AEC engine yet — the helper takes an injected filter factory so it can be tested in isolation.

**Files:**
- Modify: `src/tend/audio/channels.py`
- Create: `tests/test_audio_channels_select_path.py`

- [ ] **Step 1: Write failing test for Linux default path**

Create `tests/test_audio_channels_select_path.py`:

```python
"""Tests for tend.audio.channels.select_audio_path."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from tend.audio.channels import AudioPath, StereoToMonoLeft, select_audio_path
from tend.config import Settings


def _stub_factory(*args, **kwargs):
    return None


def test_linux_default_returns_xvf3800_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    settings = Settings(_env_file=None)
    path = select_audio_path(settings, aec_filter_factory=_stub_factory)

    assert path.in_channels == 2
    assert len(path.pre_vad_processors) == 1
    assert isinstance(path.pre_vad_processors[0], StereoToMonoLeft)
    assert path.aec_filter is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_audio_channels_select_path.py::test_linux_default_returns_xvf3800_path -v`

Expected: FAIL with `ImportError: cannot import name 'AudioPath' from 'tend.audio.channels'`.

- [ ] **Step 3: Add AudioPath + select_audio_path skeleton**

Append to `src/tend/audio/channels.py`:

```python
from dataclasses import dataclass
from typing import Callable, Optional

from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.processors.frame_processor import FrameProcessor

from tend.config import Settings


@dataclass(frozen=True)
class AudioPath:
    """Result of platform-aware audio-path selection."""
    in_channels: int
    pre_vad_processors: tuple[FrameProcessor, ...]
    aec_filter: Optional[BaseAudioFilter]


def select_audio_path(
    settings: Settings,
    *,
    aec_filter_factory: Callable[..., Optional[BaseAudioFilter]],
) -> AudioPath:
    """Return the audio-path tuple appropriate for this platform.

    `aec_filter_factory` is injected so this function can be unit-tested
    without pulling in the AEC engine; production callers pass
    `tend.audio.aec.make_aec_filter`.
    """
    import sys as _sys

    explicit = settings.mic_channels
    if explicit == 2 or (explicit is None and _sys.platform != "darwin"):
        return AudioPath(
            in_channels=2,
            pre_vad_processors=(StereoToMonoLeft(),),
            aec_filter=aec_filter_factory(settings) if _sys.platform == "darwin" else None,
        )
    return AudioPath(
        in_channels=1,
        pre_vad_processors=(),
        aec_filter=aec_filter_factory(settings),
    )
```

- [ ] **Step 4: Add the matching Settings fields**

Modify `src/tend/config.py` — find the `class Settings(BaseSettings):` block and add:

```python
    # Audio path & AEC (macOS port)
    mic_channels: int | None = None         # platform default: 1 on macOS, 2 on Linux
    aec_engine: str = "auto"                # auto | webrtc-aec3 | speex | off
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_audio_channels_select_path.py::test_linux_default_returns_xvf3800_path -v`

Expected: PASS.

- [ ] **Step 6: Add remaining tests**

Append to `tests/test_audio_channels_select_path.py`:

```python
def test_macos_default_returns_mono_path_no_xvf(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    sentinel = MagicMock(name="aec_filter")
    settings = Settings(_env_file=None)
    path = select_audio_path(settings, aec_filter_factory=lambda s: sentinel)

    assert path.in_channels == 1
    assert path.pre_vad_processors == ()
    assert path.aec_filter is sentinel


def test_explicit_mic_channels_1_overrides_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    settings = Settings(_env_file=None, mic_channels=1)
    path = select_audio_path(settings, aec_filter_factory=_stub_factory)

    assert path.in_channels == 1
    assert path.pre_vad_processors == ()


def test_explicit_mic_channels_2_overrides_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    sentinel = MagicMock(name="aec_filter")
    settings = Settings(_env_file=None, mic_channels=2)
    path = select_audio_path(settings, aec_filter_factory=lambda s: sentinel)

    assert path.in_channels == 2
    assert len(path.pre_vad_processors) == 1
    assert isinstance(path.pre_vad_processors[0], StereoToMonoLeft)
    assert path.aec_filter is sentinel
```

- [ ] **Step 7: Run all tests in the file**

Run: `python -m pytest tests/test_audio_channels_select_path.py -v`

Expected: 4 PASS.

- [ ] **Step 8: Run the whole audio test subset to catch regressions**

Run: `python -m pytest tests/ -k audio -v`

Expected: all existing audio tests still PASS.

- [ ] **Step 9: Commit**

```bash
git add src/tend/audio/channels.py src/tend/config.py tests/test_audio_channels_select_path.py
git commit -m "feat(audio): AudioPath + select_audio_path platform dispatch"
```

---

## Task 2: ReferenceBuffer ring + resolve_aec_engine

**Goal:** Create `tend/audio/aec.py` with the ring buffer that holds speaker reference samples, and the engine-resolver function that turns the config string into a concrete engine name. No filter implementations yet.

**Files:**
- Create: `src/tend/audio/aec.py`
- Create: `tests/test_audio_aec.py`

- [ ] **Step 1: Write failing tests for ReferenceBuffer**

Create `tests/test_audio_aec.py`:

```python
"""Tests for tend.audio.aec — ReferenceBuffer + resolve_aec_engine."""

from __future__ import annotations

import sys

import pytest

from tend.audio.aec import ReferenceBuffer, resolve_aec_engine
from tend.config import Settings


def test_reference_buffer_starts_empty():
    rb = ReferenceBuffer(capacity_bytes=64)
    assert rb.read(8) == b"\x00" * 8


def test_reference_buffer_returns_most_recent_bytes():
    rb = ReferenceBuffer(capacity_bytes=64)
    rb.write(b"A" * 8)
    rb.write(b"B" * 8)
    assert rb.read(8) == b"B" * 8


def test_reference_buffer_wraps_around():
    rb = ReferenceBuffer(capacity_bytes=8)
    rb.write(b"A" * 4)
    rb.write(b"B" * 4)
    rb.write(b"C" * 4)
    # buffer now holds last 8 bytes: BBBBCCCC
    assert rb.read(8) == b"B" * 4 + b"C" * 4


def test_reference_buffer_partial_fill_pads_with_silence():
    rb = ReferenceBuffer(capacity_bytes=64)
    rb.write(b"X" * 3)
    out = rb.read(5)
    # 3 real bytes + 2 silence at the front (oldest)
    assert out == b"\x00\x00" + b"X" * 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_audio_aec.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'tend.audio.aec'`.

- [ ] **Step 3: Implement ReferenceBuffer**

Create `src/tend/audio/aec.py`:

```python
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
```

- [ ] **Step 4: Run buffer tests to verify they pass**

Run: `python -m pytest tests/test_audio_aec.py -v`

Expected: 4 PASS.

- [ ] **Step 5: Write failing tests for resolve_aec_engine**

Append to `tests/test_audio_aec.py`:

```python
def test_resolve_aec_engine_off_returns_off(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    settings = Settings(_env_file=None, aec_engine="off")
    assert resolve_aec_engine(settings) == "off"


def test_resolve_aec_engine_explicit_webrtc(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    settings = Settings(_env_file=None, aec_engine="webrtc-aec3")
    assert resolve_aec_engine(settings) == "webrtc-aec3"


def test_resolve_aec_engine_explicit_speex(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    settings = Settings(_env_file=None, aec_engine="speex")
    assert resolve_aec_engine(settings) == "speex"


def test_resolve_aec_engine_auto_linux_returns_off(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    settings = Settings(_env_file=None, aec_engine="auto")
    assert resolve_aec_engine(settings) == "off"


def test_resolve_aec_engine_auto_macos_webrtc_present(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    # Force the import probe to succeed
    monkeypatch.setattr(
        "tend.audio.aec._webrtc_importable", lambda: True,
    )
    settings = Settings(_env_file=None, aec_engine="auto")
    assert resolve_aec_engine(settings) == "webrtc-aec3"


def test_resolve_aec_engine_auto_macos_webrtc_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "tend.audio.aec._webrtc_importable", lambda: False,
    )
    settings = Settings(_env_file=None, aec_engine="auto")
    assert resolve_aec_engine(settings) == "speex"
```

- [ ] **Step 6: Run resolver tests to verify they fail**

Run: `python -m pytest tests/test_audio_aec.py -v -k resolve`

Expected: FAIL with `ImportError: cannot import name 'resolve_aec_engine'`.

- [ ] **Step 7: Implement resolve_aec_engine**

Append to `src/tend/audio/aec.py`:

```python
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
```

- [ ] **Step 8: Run all aec tests**

Run: `python -m pytest tests/test_audio_aec.py -v`

Expected: 10 PASS.

- [ ] **Step 9: Commit**

```bash
git add src/tend/audio/aec.py tests/test_audio_aec.py
git commit -m "feat(audio): ReferenceBuffer + resolve_aec_engine"
```

---

## Task 3: SpeexAECFilter + make_aec_filter factory

**Goal:** Add the SpeexDSP-backed AEC filter (via the `pyaec` ctypes binding) and the `make_aec_filter` factory that resolves an engine name to a `BaseAudioFilter` instance.

**Files:**
- Modify: `src/tend/audio/aec.py`
- Modify: `tests/test_audio_aec.py`
- Modify: `pyproject.toml` (add `pyaec>=1.0.1` to deps)

- [ ] **Step 1: Add pyaec to base dependencies**

Modify `pyproject.toml` — under `[project]` `dependencies = [...]`, append:

```toml
    "pyaec>=1.0.1",
```

- [ ] **Step 2: Install the new dep**

Run: `pip install -e .`

Expected: pyaec wheel installs cleanly (~400 KB native lib).

- [ ] **Step 3: Verify pyaec imports**

Run: `python -c "from pyaec import Aec; print('ok')"`

Expected: `ok`. If you see `Error loading shared library`, your pip install picked the wrong wheel; reinstall with `pip install --force-reinstall pyaec`.

- [ ] **Step 4: Write failing tests for SpeexAECFilter**

Append to `tests/test_audio_aec.py`:

```python
import pytest

from tend.audio.aec import SpeexAECFilter, make_aec_filter


@pytest.mark.asyncio
async def test_speex_filter_passthrough_when_reference_empty():
    """When the reference buffer is silent, the filter should return
    audio that is at most the original (echo cancellation against
    silence is the identity-ish operation, possibly with mild
    suppression but never length-changed)."""
    rb = ReferenceBuffer()
    f = SpeexAECFilter(reference=rb)
    await f.start(sample_rate=16000)

    mic = b"\x10\x00" * 160  # 10 ms of int16 value 16 at 16 kHz mono
    out = await f.filter(mic)

    assert isinstance(out, bytes)
    assert len(out) == len(mic)


@pytest.mark.asyncio
async def test_speex_filter_round_trip_length_preserved():
    """For any input size matching a multiple of the frame size, the
    output is the same number of bytes."""
    rb = ReferenceBuffer()
    f = SpeexAECFilter(reference=rb)
    await f.start(sample_rate=16000)

    rb.write(b"\x05\x00" * 160)
    mic = b"\x20\x00" * 160
    out = await f.filter(mic)

    assert len(out) == len(mic)


def test_make_aec_filter_off_returns_none():
    rb = ReferenceBuffer()
    assert make_aec_filter("off", sample_rate=16000, reference=rb) is None


def test_make_aec_filter_speex_returns_filter():
    rb = ReferenceBuffer()
    f = make_aec_filter("speex", sample_rate=16000, reference=rb)
    assert isinstance(f, SpeexAECFilter)


def test_make_aec_filter_webrtc_unavailable_raises(monkeypatch):
    monkeypatch.setattr("tend.audio.aec._webrtc_importable", lambda: False)
    rb = ReferenceBuffer()
    with pytest.raises(RuntimeError, match="webrtc-audio-processing"):
        make_aec_filter("webrtc-aec3", sample_rate=16000, reference=rb)
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `python -m pytest tests/test_audio_aec.py -v -k "speex or make_aec"`

Expected: FAIL with `ImportError: cannot import name 'SpeexAECFilter'`.

- [ ] **Step 6: Implement SpeexAECFilter + make_aec_filter**

Append to `src/tend/audio/aec.py`:

```python
from pipecat.frames.frames import FilterControlFrame, FilterEnableFrame


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
        self._enabled = True

    async def start(self, sample_rate: int):
        from pyaec import Aec
        self._sample_rate = sample_rate
        self._aec = Aec(
            frame_size=self._FRAME_SAMPLES,
            filter_length=self._FILTER_LENGTH,
            sample_rate=sample_rate,
            enable_preprocess=True,
        )

    async def stop(self):
        self._aec = None

    async def process_frame(self, frame: FilterControlFrame):
        if isinstance(frame, FilterEnableFrame):
            self._enabled = frame.enable

    async def filter(self, audio: bytes) -> bytes:
        if not self._enabled or self._aec is None:
            return audio

        # Read aligned reference bytes.
        ref = self._reference.read(len(audio))

        # pyaec works on int16 lists; convert via array.array for speed.
        import array
        mic = array.array("h"); mic.frombytes(audio)
        rfa = array.array("h"); rfa.frombytes(ref)

        # pyaec.cancel_echo wants equal-length buffers, returns list[int16].
        cleaned = self._aec.cancel_echo(list(mic), list(rfa))

        out = array.array("h", cleaned).tobytes()
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
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_audio_aec.py -v`

Expected: all PASS (10 from Task 2 + 5 new = 15).

- [ ] **Step 8: Commit**

```bash
git add src/tend/audio/aec.py tests/test_audio_aec.py pyproject.toml
git commit -m "feat(audio): SpeexAECFilter + make_aec_filter factory"
```

---

## Task 4: OutputAudioCapture FrameProcessor

**Goal:** A pipecat `FrameProcessor` that taps `OutputAudioRawFrame`s into a `ReferenceBuffer`. Pure pass-through; introduces no latency.

**Files:**
- Create: `src/tend/audio/output_tap.py`
- Create: `tests/test_audio_output_tap.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_audio_output_tap.py`:

```python
"""Tests for tend.audio.output_tap.OutputAudioCapture."""

from __future__ import annotations

import pytest
from pipecat.frames.frames import OutputAudioRawFrame, TextFrame
from pipecat.processors.frame_processor import FrameDirection

from tend.audio.aec import ReferenceBuffer
from tend.audio.output_tap import OutputAudioCapture


class _Sink:
    """Captures pushed frames for assertion."""

    def __init__(self):
        self.frames = []

    async def queue_frame(self, frame, direction):
        self.frames.append((frame, direction))


@pytest.mark.asyncio
async def test_output_audio_capture_appends_audio_to_reference():
    rb = ReferenceBuffer(capacity_bytes=64)
    cap = OutputAudioCapture(reference=rb)
    sink = _Sink()
    cap.link(sink)

    frame = OutputAudioRawFrame(
        audio=b"\x11" * 16, sample_rate=16000, num_channels=1,
    )
    await cap.process_frame(frame, FrameDirection.DOWNSTREAM)

    # The audio bytes should be in the buffer.
    assert rb.read(16) == b"\x11" * 16
    # And the frame should be passed through.
    assert len(sink.frames) == 1
    assert sink.frames[0][0] is frame


@pytest.mark.asyncio
async def test_output_audio_capture_passes_through_non_output_frames():
    rb = ReferenceBuffer(capacity_bytes=64)
    cap = OutputAudioCapture(reference=rb)
    sink = _Sink()
    cap.link(sink)

    frame = TextFrame(text="hi")
    await cap.process_frame(frame, FrameDirection.DOWNSTREAM)

    # Reference buffer stays empty.
    assert rb.read(4) == b"\x00\x00\x00\x00"
    # Frame still passes through.
    assert len(sink.frames) == 1
    assert sink.frames[0][0] is frame
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_audio_output_tap.py -v`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement OutputAudioCapture**

Create `src/tend/audio/output_tap.py`:

```python
"""OutputAudioCapture — tap speaker frames into the AEC ReferenceBuffer.

Sits in the Hub pipeline just before `transport.output()` so it sees
every TTS chunk on its way to the speaker. Pure pass-through; the
captured bytes are read back by the AEC filter sitting on the input
side of the same pipeline.
"""

from __future__ import annotations

from pipecat.frames.frames import Frame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from tend.audio.aec import ReferenceBuffer


class OutputAudioCapture(FrameProcessor):
    def __init__(self, reference: ReferenceBuffer, **kwargs):
        super().__init__(**kwargs)
        self._reference = reference

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            self._reference.write(frame.audio)
        await self.push_frame(frame, direction)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_audio_output_tap.py -v`

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/output_tap.py tests/test_audio_output_tap.py
git commit -m "feat(audio): OutputAudioCapture speaker-reference tap"
```

---

## Task 5: Wire select_audio_path + AEC into Hub.build_pipeline

**Goal:** Replace the hardcoded XVF3800 assumptions in `Hub.build_pipeline` with a call to `select_audio_path`, conditionally inserting `OutputAudioCapture` before `transport.output()`. This task should keep Pi behavior identical.

**Files:**
- Modify: `src/tend/audio/hub.py`
- Modify: `src/tend/audio/channels.py` (wire the real factory)
- Modify: `tests/test_audio_channels_select_path.py` (verify production factory)

- [ ] **Step 1: Wire the real factory into channels.py**

Modify `src/tend/audio/channels.py` — at the bottom, add a convenience wrapper that uses the real `make_aec_filter`:

```python
def _default_aec_filter_factory(settings: Settings):
    """Build the AEC filter using the production engine resolver."""
    from tend.audio.aec import ReferenceBuffer, make_aec_filter, resolve_aec_engine

    engine = resolve_aec_engine(settings)
    if engine == "off":
        return None
    # Each Hub gets its own buffer; transport sample rate comes from Settings.
    reference = ReferenceBuffer()
    f = make_aec_filter(engine, sample_rate=settings.sample_rate, reference=reference)
    # Attach the buffer so the Hub can grab it for OutputAudioCapture.
    f._tend_reference = reference  # type: ignore[attr-defined]
    return f
```

And change `select_audio_path`'s default for `aec_filter_factory`:

```python
def select_audio_path(
    settings: Settings,
    *,
    aec_filter_factory: Callable[..., Optional[BaseAudioFilter]] = _default_aec_filter_factory,
) -> AudioPath:
    ...
```

- [ ] **Step 2: Inspect Hub.build_pipeline**

Open `src/tend/audio/hub.py` and locate `build_pipeline` (around line 128). Note the current shape — it hardcodes `audio_in_channels=2` and unconditionally inserts `StereoToMonoLeft()` in the pipeline.

- [ ] **Step 3: Refactor Hub.build_pipeline to use select_audio_path**

In `src/tend/audio/hub.py`, replace the body of `build_pipeline` with:

```python
    async def build_pipeline(self) -> Pipeline:
        from tend.audio.channels import select_audio_path
        from tend.audio.output_tap import OutputAudioCapture

        path = select_audio_path(self._settings)

        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=self._settings.sample_rate,
                audio_in_channels=path.in_channels,
                audio_in_filter=path.aec_filter,
                audio_out_sample_rate=self._tts_sample_rate,
            )
        )

        aggregators = LLMContextAggregatorPair(self._context)
        bridge = BusBridgeProcessor(
            bus=self.bus,
            agent_name=self.name,
            exclude_frames=(TTSSpeakFrame,),
            name=f"{self.name}::voice-bridge",
        )

        output_tap = []
        if path.aec_filter is not None and hasattr(path.aec_filter, "_tend_reference"):
            output_tap = [OutputAudioCapture(reference=path.aec_filter._tend_reference)]

        return Pipeline([
            transport.input(),
            *path.pre_vad_processors,
            VADProcessor(vad_analyzer=SileroVADAnalyzer()),
            OpenWakeWordGate(
                model_name=self._settings.openwakeword_model,
                threshold=self._settings.wake_threshold,
                hub=self,
                brain=self._brain,
            ),
            self._stt,
            InputLatencyLogger(),
            SleepPhraseGate(
                sleep_phrase=self._settings.sleep_phrase,
                fuzz_ratio=self._settings.sleep_fuzz_ratio,
                timeout_s=self._settings.awake_timeout_s,
                hub=self,
                brain=self._brain,
            ),
            aggregators.user(),
            bridge,
            self._tts,
            OutputLatencyLogger(),
            *output_tap,
            transport.output(),
            aggregators.assistant(),
        ])
```

Remove the now-unused `from tend.audio.channels import StereoToMonoLeft` import (it's still used inside `channels.py` itself).

- [ ] **Step 4: Manual smoke test on Linux (regression check)**

If a Pi is reachable: SSH in, pull the branch, run `python -m tend` for ~30 seconds and verify wake-word, STT, TTS, and sleep-phrase still work. If no Pi is reachable, document this as a deferred check before merging.

If no Pi is available, at minimum run the entire test suite locally:

Run: `python -m pytest tests/ -v`

Expected: all existing tests still PASS (no regressions).

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/hub.py src/tend/audio/channels.py
git commit -m "feat(audio): Hub.build_pipeline consumes select_audio_path"
```

---

## Task 6: AVSpeechSynthesizerTTSService

**Goal:** Add the Mac-native local TTS service backed by `pyobjc-framework-AVFoundation`. Yields `TTSStartedFrame → TTSAudioRawFrame×N → TTSStoppedFrame`.

**Files:**
- Modify: `src/tend/services.py`
- Create: `tests/test_services_avspeech_tts.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add pyobjc-framework-AVFoundation as a Darwin-only dep**

Modify `pyproject.toml` — under `dependencies = [...]`, append:

```toml
    'pyobjc-framework-AVFoundation>=10; platform_system == "Darwin"',
```

- [ ] **Step 2: Install on Mac**

Run: `pip install -e .` (this is a no-op on Linux because of the marker).

Expected: pyobjc-framework-AVFoundation installs cleanly on macOS.

- [ ] **Step 3: Write a failing test (mocked AVFoundation)**

Create `tests/test_services_avspeech_tts.py`:

```python
"""Tests for tend.services.AVSpeechSynthesizerTTSService.

The tests mock AVFoundation so they run on any platform.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_avspeech_service_emits_started_audio_stopped(monkeypatch):
    """When run_tts is called, the service emits the standard pipecat
    sequence: TTSStartedFrame → TTSAudioRawFrame(...) → TTSStoppedFrame."""
    fake_avfoundation = MagicMock(name="AVFoundation")
    monkeypatch.setitem(sys.modules, "AVFoundation", fake_avfoundation)

    from tend.services import AVSpeechSynthesizerTTSService
    from pipecat.frames.frames import (
        TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame,
    )

    svc = AVSpeechSynthesizerTTSService(voice_identifier="", sample_rate=16000)

    # Stub the synth helper so the service can yield deterministic PCM.
    async def fake_synth(text):
        yield b"\x12\x34" * 80   # 10 ms at 16 kHz mono
        yield b"\x56\x78" * 80

    monkeypatch.setattr(svc, "_synthesize_to_pcm", fake_synth)

    frames = []
    async for frame in svc.run_tts("hello"):
        frames.append(frame)

    assert isinstance(frames[0], TTSStartedFrame)
    assert any(isinstance(f, TTSAudioRawFrame) for f in frames)
    assert isinstance(frames[-1], TTSStoppedFrame)

    audio_frames = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
    assert sum(len(f.audio) for f in audio_frames) == 320
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python -m pytest tests/test_services_avspeech_tts.py -v`

Expected: FAIL with `ImportError: cannot import name 'AVSpeechSynthesizerTTSService'`.

- [ ] **Step 5: Implement AVSpeechSynthesizerTTSService**

Append to `src/tend/services.py`:

```python
class AVSpeechSynthesizerTTSService(TTSService):
    """Local TTS on macOS via Apple's AVSpeechSynthesizer.

    Uses `write(_:toBufferCallback:)` (macOS 13+) to receive
    AVAudioPCMBuffer chunks and emits them as `TTSAudioRawFrame`s at
    the pipecat-requested sample rate. Voice is selected via
    `AVSpeechSynthesisVoice(identifier:)`; empty identifier means the
    system default voice.

    Siri-quality voices are not exposed by this API. Premium/Enhanced
    voices installed via VoiceOver Utility (or Read & Speak on older
    macOS) are reachable.
    """

    def __init__(
        self,
        *,
        voice_identifier: str = "",
        sample_rate: int = 16000,
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._voice_identifier = voice_identifier
        self._sample_rate = sample_rate

    def can_generate_metrics(self) -> bool:
        return True

    async def _synthesize_to_pcm(self, text: str):
        """Async generator: yields raw int16 mono PCM bytes at self._sample_rate.

        On macOS, drives AVSpeechSynthesizer via PyObjC. Resampling to
        self._sample_rate is done from the buffer's native format.
        """
        import AVFoundation
        import asyncio
        import array
        import audioop

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        synth = AVFoundation.AVSpeechSynthesizer.new()
        utt = AVFoundation.AVSpeechUtterance.speechUtteranceWithString_(text)
        if self._voice_identifier:
            voice = AVFoundation.AVSpeechSynthesisVoice.voiceWithIdentifier_(
                self._voice_identifier,
            )
            if voice is not None:
                utt.setVoice_(voice)

        def callback(buffer):
            if buffer is None:
                loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)
                return
            # Buffer is an AVAudioPCMBuffer; pull int16 PCM at its sample rate
            fmt = buffer.format()
            channels = fmt.channelCount()
            native_sr = int(fmt.sampleRate())
            n_frames = buffer.frameLength()

            # AVAudioPCMBuffer's int16ChannelData might be None if format is float.
            i16 = buffer.int16ChannelData()
            if i16 is not None:
                # Single-channel pointer at i16[0]; pyobjc exposes as ObjectiveC pointer.
                # The easiest portable extraction: copy via floatChannelData → convert.
                pass

            # Fall back to float→int16 conversion via audioop.
            fc = buffer.floatChannelData()
            if fc is None:
                loop.call_soon_threadsafe(queue.put_nowait, b"")
                return
            # Read mono channel as a memoryview of float32, then convert.
            floats = array.array("f")
            floats.frombytes(
                bytes(fc[0][:n_frames * 4])
                if isinstance(fc[0], (bytes, bytearray))
                else _ptr_to_bytes(fc[0], n_frames * 4)
            )
            # Convert to int16
            i16_bytes = audioop.lin2lin(
                array.array("h", [max(-32768, min(32767, int(x * 32767))) for x in floats]).tobytes(),
                2, 2,
            )
            # Resample if needed
            if native_sr != self._sample_rate:
                i16_bytes, _ = audioop.ratecv(
                    i16_bytes, 2, channels, native_sr, self._sample_rate, None,
                )
            loop.call_soon_threadsafe(queue.put_nowait, i16_bytes)

        synth.writeUtterance_toBufferCallback_(utt, callback)

        while True:
            chunk = await queue.get()
            if chunk is SENTINEL:
                break
            if chunk:
                yield chunk

    async def run_tts(self, text: str):
        from pipecat.frames.frames import (
            TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame,
        )

        yield TTSStartedFrame()
        async for pcm in self._synthesize_to_pcm(text):
            yield TTSAudioRawFrame(
                audio=pcm, sample_rate=self._sample_rate, num_channels=1,
            )
        yield TTSStoppedFrame()


def _ptr_to_bytes(ptr, n: int) -> bytes:
    """Read `n` bytes from a PyObjC C-pointer."""
    import ctypes
    return ctypes.string_at(int(ptr), n)
```

> **Note for the implementer:** the PCM extraction from `AVAudioPCMBuffer.floatChannelData()` via PyObjC is the fiddliest bit of this whole task. If `floatChannelData()` returns something that doesn't have an indexable `[0]`, consult the PyObjC AVFoundation bridge tests in `pyobjc-framework-AVFoundation` for the canonical way to copy raw frames; the structure varies slightly across PyObjC versions. The test in this task mocks `_synthesize_to_pcm` away, so the test passes regardless of native plumbing — but a manual smoke test on a real Mac is required before claiming the task done.

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_services_avspeech_tts.py -v`

Expected: PASS.

- [ ] **Step 7: Manual smoke test on Mac**

Run on a Mac:

```bash
python -c "
import asyncio
from tend.services import AVSpeechSynthesizerTTSService

async def main():
    svc = AVSpeechSynthesizerTTSService(voice_identifier='', sample_rate=16000)
    async for f in svc.run_tts('Testing tend on macOS.'):
        print(type(f).__name__, getattr(f, 'audio', b'')[:8])

asyncio.run(main())
"
```

Expected: prints `TTSStartedFrame`, then several `TTSAudioRawFrame` lines, then `TTSStoppedFrame`. Audio bytes should look like non-zero PCM. If they're all zeros or the PCM extraction errors, fix the floatChannelData copy path per the note in step 5 before continuing.

- [ ] **Step 8: Commit**

```bash
git add src/tend/services.py tests/test_services_avspeech_tts.py pyproject.toml
git commit -m "feat(tts): AVSpeechSynthesizerTTSService for macOS local TTS"
```

---

## Task 7: TTS provider dispatch in services._make_tts

**Goal:** Replace the current key-presence inference with explicit `[tts] provider = "auto" | "elevenlabs" | "avspeech" | "piper"` dispatch.

**Files:**
- Modify: `src/tend/services.py`
- Modify: `src/tend/config.py`
- Create: `tests/test_services_make_tts.py`

- [ ] **Step 1: Add tts_provider + avspeech_voice to Settings**

Modify `src/tend/config.py` — add to `Settings`:

```python
    # TTS provider (see roadmap item #5; macOS port lands the provider field early)
    tts_provider: str = "auto"           # auto | elevenlabs | avspeech | piper
    avspeech_voice: str = ""             # AVSpeechSynthesisVoice identifier; empty = system default
```

- [ ] **Step 2: Inspect current _make_tts**

Open `src/tend/services.py` and locate `_make_tts(settings)`. Note the current logic (likely: "if ELEVENLABS key set → ElevenLabs else Piper").

- [ ] **Step 3: Write failing tests**

Create `tests/test_services_make_tts.py`:

```python
"""Tests for tend.services._make_tts provider dispatch."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from tend.config import Settings
from tend.services import _make_tts


def test_make_tts_explicit_avspeech_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AVFoundation", MagicMock())
    settings = Settings(_env_file=None, tts_provider="avspeech")
    svc, sr = _make_tts(settings)
    assert type(svc).__name__ == "AVSpeechSynthesizerTTSService"


def test_make_tts_explicit_avspeech_on_linux_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    settings = Settings(_env_file=None, tts_provider="avspeech")
    with pytest.raises(RuntimeError, match="macOS"):
        _make_tts(settings)


def test_make_tts_explicit_piper_on_macos_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    settings = Settings(_env_file=None, tts_provider="piper")
    with pytest.raises(RuntimeError, match="Linux"):
        _make_tts(settings)


def test_make_tts_auto_macos_avfoundation_present(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AVFoundation", MagicMock())
    settings = Settings(_env_file=None, tts_provider="auto")
    svc, sr = _make_tts(settings)
    assert type(svc).__name__ == "AVSpeechSynthesizerTTSService"


def test_make_tts_auto_macos_avfoundation_missing_with_eleven_key(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delitem(sys.modules, "AVFoundation", raising=False)
    # Force the AVFoundation import probe to fail
    monkeypatch.setattr(
        "tend.services._avfoundation_importable", lambda: False,
    )
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    settings = Settings(_env_file=None, tts_provider="auto")
    svc, sr = _make_tts(settings)
    assert "ElevenLabs" in type(svc).__name__


def test_make_tts_auto_macos_no_options_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "tend.services._avfoundation_importable", lambda: False,
    )
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    settings = Settings(_env_file=None, tts_provider="auto")
    with pytest.raises(RuntimeError, match="no working TTS"):
        _make_tts(settings)
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/test_services_make_tts.py -v`

Expected: tests fail (some with attribute errors for `_avfoundation_importable`).

- [ ] **Step 5: Implement provider dispatch**

In `src/tend/services.py`, add near the top:

```python
def _avfoundation_importable() -> bool:
    try:
        import AVFoundation  # noqa: F401
        return True
    except ImportError:
        return False
```

Then replace the body of `_make_tts(settings)` with:

```python
def _make_tts(settings):
    """Resolve [tts] provider to a concrete pipecat TTSService instance.

    Returns: (service, sample_rate).
    """
    import os, sys

    provider = (settings.tts_provider or "auto").lower()

    if provider == "auto":
        if sys.platform == "darwin":
            if _avfoundation_importable():
                provider = "avspeech"
            elif os.environ.get("ELEVENLABS_API_KEY"):
                provider = "elevenlabs"
            else:
                raise RuntimeError(
                    "no working TTS provider on macOS: AVFoundation is not "
                    "importable and ELEVENLABS_API_KEY is not set. Install "
                    "pyobjc-framework-AVFoundation or set ELEVENLABS_API_KEY."
                )
        else:
            # Linux: existing behavior — prefer ElevenLabs key if present, else Piper.
            provider = "elevenlabs" if os.environ.get("ELEVENLABS_API_KEY") else "piper"

    if provider == "avspeech":
        if sys.platform != "darwin":
            raise RuntimeError(
                "tts_provider='avspeech' is macOS-only (uses AVSpeechSynthesizer)."
            )
        svc = AVSpeechSynthesizerTTSService(
            voice_identifier=settings.avspeech_voice,
            sample_rate=settings.sample_rate,
        )
        return svc, settings.sample_rate

    if provider == "piper":
        if sys.platform == "darwin":
            raise RuntimeError(
                "tts_provider='piper' requires a Linux Piper HTTP server. "
                "On macOS use tts_provider='avspeech' or 'elevenlabs'."
            )
        # ... existing Piper instantiation code stays here ...
        # (preserve current ElevenLabs/Piper instantiation logic from before this edit)

    if provider == "elevenlabs":
        # ... existing ElevenLabs instantiation code stays here ...

    raise ValueError(f"Unknown tts_provider: {provider!r}")
```

> **Important:** the "existing instantiation code stays here" comments refer to whatever was in `_make_tts` before this edit — preserve the working Piper-HTTP and ElevenLabs constructor calls. Don't rewrite them.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_services_make_tts.py -v`

Expected: 6 PASS.

- [ ] **Step 7: Run the wider services tests for regressions**

Run: `python -m pytest tests/ -k services -v`

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/tend/services.py src/tend/config.py tests/test_services_make_tts.py
git commit -m "feat(tts): provider dispatch in _make_tts with auto resolution"
```

---

## Task 8: launchd plist template

**Goal:** Ship the plist template alongside the systemd one.

**Files:**
- Create: `src/tend/_defaults/launchd/com.tend.daemon.plist.tmpl`

- [ ] **Step 1: Verify the systemd template's format is matched**

Read `src/tend/_defaults/systemd/tend.service.tmpl` to see the placeholder convention (`{{PYTHON}}` and `{{TEND_HOME}}`).

- [ ] **Step 2: Create the launchd template**

Create `src/tend/_defaults/launchd/com.tend.daemon.plist.tmpl`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tend.daemon</string>

    <key>ProgramArguments</key>
    <array>
        <string>{{PYTHON}}</string>
        <string>-m</string>
        <string>tend</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>TEND_HOME</key>
        <string>{{TEND_HOME}}</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>{{TEND_HOME}}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>/tmp/tend.launchd.out</string>

    <key>StandardErrorPath</key>
    <string>/tmp/tend.launchd.err</string>

    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
```

- [ ] **Step 3: Verify the template is included as package-data**

Read `pyproject.toml` and confirm `"tend._defaults" = ["**/*"]` is set under `[tool.setuptools.package-data]`. If it's there (it should be — the systemd template uses it), no change needed.

- [ ] **Step 4: Verify the template resolves from importlib.resources**

Run: `python -c "from importlib.resources import files; print(files('tend._defaults').joinpath('launchd/com.tend.daemon.plist.tmpl').read_text()[:80])"`

Expected: prints the first 80 characters of the plist.

- [ ] **Step 5: Commit**

```bash
git add src/tend/_defaults/launchd/com.tend.daemon.plist.tmpl
git commit -m "feat(service): launchd plist template"
```

---

## Task 9: service.py macOS branches

**Goal:** Replace `MacOSNotSupported` raises with real launchd implementations in `install/uninstall/start/stop/status`.

**Files:**
- Modify: `src/tend/service.py`
- Create: `tests/test_service_macos.py`

- [ ] **Step 1: Write failing tests for unit_path + render_unit on macOS**

Create `tests/test_service_macos.py`:

```python
"""Tests for tend.service macOS (launchd) branches."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tend import service


def test_unit_path_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    p = service.unit_path()
    assert p.name == "com.tend.daemon.plist"
    assert "LaunchAgents" in p.parts


def test_render_unit_macos_substitutes_placeholders(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    body = service.render_unit(python="/usr/bin/python3", tend_home=tmp_path)
    assert "/usr/bin/python3" in body
    assert str(tmp_path) in body
    assert "{{PYTHON}}" not in body
    assert "{{TEND_HOME}}" not in body


def test_install_macos_calls_launchctl_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    monkeypatch.setattr(service.paths, "tend_home", lambda: tmp_path)
    target = tmp_path / "LaunchAgents" / "com.tend.daemon.plist"
    monkeypatch.setattr(service, "unit_path", lambda: target)

    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        return MagicMock(returncode=0)
    monkeypatch.setattr(service.subprocess, "run", fake_run)

    out = service.install()

    assert out == target
    assert target.exists()
    # First call should be launchctl bootstrap.
    assert calls[0][0] == "launchctl"
    assert calls[0][1] == "bootstrap"
    assert calls[0][2] == f"gui/{os.getuid()}"


def test_uninstall_macos_calls_bootout_and_deletes(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    target = tmp_path / "com.tend.daemon.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("<plist/>")
    monkeypatch.setattr(service, "unit_path", lambda: target)

    calls = []
    monkeypatch.setattr(
        service.subprocess, "run",
        lambda args, **kw: (calls.append(args), MagicMock(returncode=0))[1],
    )

    service.uninstall()

    assert not target.exists()
    assert calls[0][0] == "launchctl"
    assert calls[0][1] == "bootout"


def test_start_macos_kickstart(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    target = tmp_path / "com.tend.daemon.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    monkeypatch.setattr(service, "unit_path", lambda: target)

    calls = []
    monkeypatch.setattr(
        service.subprocess, "run",
        lambda args, **kw: (calls.append(args), MagicMock(returncode=0))[1],
    )

    rc = service.start()

    assert rc == 0
    assert calls[0][:2] == ["launchctl", "kickstart"]


def test_stop_macos_kill_sigterm(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    target = tmp_path / "com.tend.daemon.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    monkeypatch.setattr(service, "unit_path", lambda: target)

    calls = []
    monkeypatch.setattr(
        service.subprocess, "run",
        lambda args, **kw: (calls.append(args), MagicMock(returncode=0))[1],
    )

    rc = service.stop()

    assert rc == 0
    assert calls[0][:3] == ["launchctl", "kill", "SIGTERM"]


def test_status_macos_parses_launchctl_print(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    target = tmp_path / "com.tend.daemon.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    monkeypatch.setattr(service, "unit_path", lambda: target)

    sample_print = """\
gui/501/com.tend.daemon = {
    active count = 1
    state = running
    program = /usr/local/bin/python3.11
}
"""
    monkeypatch.setattr(
        service.subprocess, "run",
        lambda args, **kw: MagicMock(returncode=0, stdout=sample_print, stderr=""),
    )

    state, details = service.status()
    assert state == "running"
    assert "com.tend.daemon" in details
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_service_macos.py -v`

Expected: most fail with `MacOSNotSupported` or with missing helpers.

- [ ] **Step 3: Refactor service.py to add mac branches**

Open `src/tend/service.py` and:

1. Delete the `class MacOSNotSupported(NotImplementedError):` definition entirely.

2. Add two helpers near the top (after `_is_macos`):

```python
def _label() -> str:
    return "com.tend.daemon"

def _domain() -> str:
    return f"gui/{os.getuid()}"
```

(Add `import os` to the imports if not already present.)

3. Replace `unit_path()`:

```python
def unit_path() -> Path:
    if _is_macos():
        return Path.home() / "Library" / "LaunchAgents" / f"{_label()}.plist"
    return Path.home() / ".config" / "systemd" / "user" / "tend.service"
```

4. Replace `render_unit()`:

```python
def render_unit(*, python: str, tend_home: Path) -> str:
    name = (
        "launchd/com.tend.daemon.plist.tmpl"
        if _is_macos()
        else "systemd/tend.service.tmpl"
    )
    body = files("tend._defaults").joinpath(name).read_text(encoding="utf-8")
    return (
        body
        .replace("{{PYTHON}}", python)
        .replace("{{TEND_HOME}}", str(tend_home))
    )
```

5. Replace `install()`:

```python
def install(*, force: bool = False) -> Path:
    target = unit_path()
    new_body = render_unit(python=sys.executable, tend_home=paths.tend_home())

    if (
        target.exists()
        and target.read_text(encoding="utf-8") != new_body
        and not force
    ):
        raise ServiceFileExists(
            f"{target} already exists; pass force=True to overwrite"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_body, encoding="utf-8")

    if _is_macos():
        # `bootstrap` is idempotent on most macOS versions; if it fails because
        # the agent is already loaded, fall back to `load`.
        r = subprocess.run(
            ["launchctl", "bootstrap", _domain(), str(target)],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            subprocess.run(
                ["launchctl", "load", str(target)],
                check=False,
            )
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    return target
```

6. Replace `uninstall()`:

```python
def uninstall() -> None:
    target = unit_path()
    if _is_macos():
        subprocess.run(
            ["launchctl", "bootout", f"{_domain()}/{_label()}"],
            check=False,
        )
        if target.exists():
            target.unlink()
    else:
        if target.exists():
            target.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
```

7. Replace `_require_installed_linux()` with a platform-aware version:

```python
def _require_installed() -> None:
    if not unit_path().exists():
        raise ServiceNotInstalled(
            f"no unit at {unit_path()}; run `tend service install` first"
        )
```

8. Replace `start()`, `stop()`, `status()`:

```python
def start() -> int:
    _require_installed()
    if _is_macos():
        return subprocess.run(
            ["launchctl", "kickstart", f"{_domain()}/{_label()}"],
            check=False,
        ).returncode
    return subprocess.run(
        ["systemctl", "--user", "start", "tend"], check=False,
    ).returncode


def stop() -> int:
    _require_installed()
    if _is_macos():
        return subprocess.run(
            ["launchctl", "kill", "SIGTERM", f"{_domain()}/{_label()}"],
            check=False,
        ).returncode
    return subprocess.run(
        ["systemctl", "--user", "stop", "tend"], check=False,
    ).returncode


def status() -> tuple[str, str]:
    if not unit_path().exists():
        return ("not-installed", f"no unit at {unit_path()}")

    if _is_macos():
        r = subprocess.run(
            ["launchctl", "print", f"{_domain()}/{_label()}"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            return ("unknown", (r.stderr or r.stdout or "").strip())
        text = r.stdout or ""
        state = "unknown"
        for line in text.splitlines():
            if "state =" in line:
                state = line.split("=", 1)[1].strip()
                break
        return (state, text.strip())

    r = subprocess.run(
        ["systemctl", "--user", "is-active", "tend"],
        capture_output=True, text=True, check=False,
    )
    state = (r.stdout or "").strip() or "unknown"
    r2 = subprocess.run(
        ["systemctl", "--user", "status", "tend", "--no-pager", "-n", "0"],
        capture_output=True, text=True, check=False,
    )
    return (state, (r2.stdout or "").strip())
```

- [ ] **Step 4: Update the file's docstring**

Replace the line `"""Linux only — macOS launchd support is sub-project #3 (macOS port)."""` with:

```python
"""service-unit install/uninstall for tend.

Writes either a systemd user unit (Linux) or a launchd LaunchAgent
plist (macOS), and wraps `systemctl`/`launchctl` for start/stop/status.
"""
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_service_macos.py -v`

Expected: 8 PASS.

- [ ] **Step 6: Run existing service tests for regressions**

Run: `python -m pytest tests/ -k service -v`

Expected: all PASS (Linux tests still work because the mac branch is gated on `_is_macos()`).

- [ ] **Step 7: Manual smoke on Mac (if available)**

```bash
tend service install
tend service status
tend service start
tend service status     # expect state = "running"
tend service stop
tend service status
tend service uninstall
```

Verify the plist appears/disappears at `~/Library/LaunchAgents/com.tend.daemon.plist` and `launchctl list | grep tend` reflects the state.

- [ ] **Step 8: Commit**

```bash
git add src/tend/service.py tests/test_service_macos.py
git commit -m "feat(service): launchd branches in install/uninstall/start/stop/status"
```

---

## Task 10: probe_microphone_access + doctor checks

**Goal:** Add the TCC mic-permission probe used by both `tend setup` and `tend doctor`. Add doctor checks for the AEC engine and TTS provider.

**Files:**
- Modify: `src/tend/checks.py`
- Create: `tests/test_checks_microphone_access.py`
- Create: `tests/test_checks_aec_engine.py`
- Create: `tests/test_checks_tts_provider.py`

- [ ] **Step 1: Write failing test for probe_microphone_access**

Create `tests/test_checks_microphone_access.py`:

```python
"""Tests for tend.checks.probe_microphone_access."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from tend.checks import probe_microphone_access


def test_probe_microphone_access_success(monkeypatch):
    fake_pa = MagicMock()
    fake_stream = MagicMock()
    fake_stream.read.return_value = b"\x05\x00" * 800  # 50 ms of non-silence
    fake_pa.PyAudio.return_value.open.return_value = fake_stream
    monkeypatch.setitem(sys.modules, "pyaudio", fake_pa)

    result = probe_microphone_access()
    assert result.status == "ok"


def test_probe_microphone_access_denied(monkeypatch):
    fake_pa = MagicMock()
    fake_pa.PyAudio.return_value.open.side_effect = OSError(
        "[Errno -9986] Internal PortAudio error"
    )
    monkeypatch.setitem(sys.modules, "pyaudio", fake_pa)

    result = probe_microphone_access()
    assert result.status == "fail"
    assert "permission" in result.detail.lower() or "denied" in result.detail.lower()


def test_probe_microphone_access_silent_returns_warn(monkeypatch):
    fake_pa = MagicMock()
    fake_stream = MagicMock()
    fake_stream.read.return_value = b"\x00" * 1600
    fake_pa.PyAudio.return_value.open.return_value = fake_stream
    monkeypatch.setitem(sys.modules, "pyaudio", fake_pa)

    result = probe_microphone_access()
    # Silent capture could be denied (user dismissed prompt) — warn is acceptable.
    assert result.status in ("warn", "ok")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_checks_microphone_access.py -v`

Expected: FAIL with `ImportError: cannot import name 'probe_microphone_access'`.

- [ ] **Step 3: Implement probe_microphone_access**

Append to `src/tend/checks.py`:

```python
import sys as _sys


def probe_microphone_access() -> CheckResult:
    """Open a brief PyAudio input stream to probe TCC microphone access.

    On macOS, opening the stream triggers the system permission prompt
    (the first time only). Returns:
      ok    — stream opened and captured non-zero samples
      warn  — stream opened but captured silence (could be a quiet env, or
              the user dismissed the prompt)
      fail  — stream open raised, typically because TCC denied access
    """
    try:
        import pyaudio
    except ImportError:
        return CheckResult(
            "microphone", "fail",
            "pyaudio is not installed",
            remediation="pip install pyaudio (and brew install portaudio on macOS)",
        )

    pa = pyaudio.PyAudio()
    try:
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                frames_per_buffer=800,
            )
        except OSError as e:
            python_path = _sys.executable
            return CheckResult(
                "microphone", "fail",
                f"could not open microphone: {e}",
                remediation=(
                    f"grant Microphone access for {python_path} in "
                    f"System Settings → Privacy & Security → Microphone"
                ),
            )

        try:
            data = stream.read(800, exception_on_overflow=False)
        except OSError as e:
            return CheckResult(
                "microphone", "fail",
                f"could not read from microphone: {e}",
                remediation="grant Microphone permission in System Settings",
            )
        finally:
            stream.stop_stream()
            stream.close()

        # Detect silence vs. signal.
        max_amp = 0
        for i in range(0, len(data), 2):
            sample = int.from_bytes(data[i:i+2], "little", signed=True)
            if abs(sample) > max_amp:
                max_amp = abs(sample)

        if max_amp > 50:  # arbitrary low threshold above DC noise floor
            return CheckResult(
                "microphone", "ok",
                f"captured signal (peak amplitude {max_amp})",
            )
        return CheckResult(
            "microphone", "warn",
            "captured silence — confirm the prompt was approved and the mic is unmuted",
        )
    finally:
        pa.terminate()
```

- [ ] **Step 4: Run microphone tests to verify they pass**

Run: `python -m pytest tests/test_checks_microphone_access.py -v`

Expected: 3 PASS.

- [ ] **Step 5: Write failing tests for check_aec_engine**

Create `tests/test_checks_aec_engine.py`:

```python
"""Tests for tend.checks.check_aec_engine."""

from __future__ import annotations

import sys

import pytest

from tend.checks import check_aec_engine
from tend.config import Settings


def test_check_aec_engine_off(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    s = Settings(_env_file=None, aec_engine="off")
    r = check_aec_engine(s)
    assert r.status == "ok"
    assert "off" in r.detail.lower()


def test_check_aec_engine_auto_macos_webrtc_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("tend.audio.aec._webrtc_importable", lambda: False)
    s = Settings(_env_file=None, aec_engine="auto")
    r = check_aec_engine(s)
    assert r.status == "warn"
    assert "speex" in r.detail.lower()
    assert "aec-webrtc" in (r.remediation or "")


def test_check_aec_engine_explicit_webrtc_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("tend.audio.aec._webrtc_importable", lambda: False)
    s = Settings(_env_file=None, aec_engine="webrtc-aec3")
    r = check_aec_engine(s)
    assert r.status == "fail"
```

- [ ] **Step 6: Run to verify they fail**

Run: `python -m pytest tests/test_checks_aec_engine.py -v`

Expected: FAIL with `ImportError`.

- [ ] **Step 7: Implement check_aec_engine**

Append to `src/tend/checks.py`:

```python
def check_aec_engine(settings) -> CheckResult:
    """Report the resolved AEC engine and whether its dependency is importable."""
    from tend.audio.aec import _webrtc_importable, resolve_aec_engine

    configured = settings.aec_engine or "auto"
    resolved = resolve_aec_engine(settings)

    if resolved == "off":
        return CheckResult("aec_engine", "ok", "AEC disabled (off)")

    if resolved == "speex":
        if configured == "auto" and _sys.platform == "darwin" and not _webrtc_importable():
            return CheckResult(
                "aec_engine", "warn",
                "AEC: speex (fallback; webrtc-audio-processing not installed)",
                remediation=(
                    "for state-of-the-art AEC: brew install webrtc-audio-processing "
                    "&& pipx inject tend webrtc-audio-processing"
                ),
            )
        return CheckResult("aec_engine", "ok", "AEC: speex")

    if resolved == "webrtc-aec3":
        if not _webrtc_importable():
            return CheckResult(
                "aec_engine", "fail",
                "AEC: webrtc-aec3 configured but webrtc-audio-processing not importable",
                remediation=(
                    "brew install webrtc-audio-processing "
                    "&& pipx inject tend webrtc-audio-processing"
                ),
            )
        return CheckResult("aec_engine", "ok", "AEC: webrtc-aec3")

    return CheckResult(
        "aec_engine", "warn", f"unknown aec_engine={configured!r}",
    )
```

- [ ] **Step 8: Run AEC engine tests to verify they pass**

Run: `python -m pytest tests/test_checks_aec_engine.py -v`

Expected: 3 PASS.

- [ ] **Step 9: Write failing tests for check_tts_provider**

Create `tests/test_checks_tts_provider.py`:

```python
"""Tests for tend.checks.check_tts_provider."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from tend.checks import check_tts_provider
from tend.config import Settings


def test_check_tts_provider_avspeech_on_macos_ok(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AVFoundation", MagicMock())
    s = Settings(_env_file=None, tts_provider="avspeech")
    r = check_tts_provider(s)
    assert r.status == "ok"


def test_check_tts_provider_avspeech_on_linux_fails(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    s = Settings(_env_file=None, tts_provider="avspeech")
    r = check_tts_provider(s)
    assert r.status == "fail"


def test_check_tts_provider_piper_on_macos_fails(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    s = Settings(_env_file=None, tts_provider="piper")
    r = check_tts_provider(s)
    assert r.status == "fail"


def test_check_tts_provider_auto_macos_avfoundation_missing_warn(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("tend.services._avfoundation_importable", lambda: False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    s = Settings(_env_file=None, tts_provider="auto")
    r = check_tts_provider(s)
    assert r.status == "fail"  # no working provider available
```

- [ ] **Step 10: Run to verify they fail**

Run: `python -m pytest tests/test_checks_tts_provider.py -v`

Expected: FAIL with `ImportError`.

- [ ] **Step 11: Implement check_tts_provider**

Append to `src/tend/checks.py`:

```python
def check_tts_provider(settings) -> CheckResult:
    """Report the resolved TTS provider and whether it can be instantiated.

    Does not actually call the network or hardware — just dispatches the
    same resolution logic as services._make_tts and reports the outcome.
    """
    from tend.services import _make_tts

    try:
        svc, _sr = _make_tts(settings)
    except RuntimeError as e:
        return CheckResult(
            "tts_provider", "fail", str(e),
            remediation="check [tts] provider in tend.toml or install the missing dep",
        )
    return CheckResult(
        "tts_provider", "ok",
        f"TTS: {type(svc).__name__}",
    )
```

- [ ] **Step 12: Run TTS provider tests**

Run: `python -m pytest tests/test_checks_tts_provider.py -v`

Expected: 4 PASS.

- [ ] **Step 13: Wire the new checks into doctor's run_all**

Modify `src/tend/checks.py::run_all`:

```python
def run_all(settings: Settings | None = None) -> list[CheckResult]:
    s = settings or Settings()
    results = [
        check_workspace(),
        check_config(s),
        check_anthropic_key(),
        check_webhook_token(),
        check_stt(s),
        check_tts(s),                # existing — keep until full provider migration
        check_tts_provider(s),       # NEW
        check_aec_engine(s),         # NEW
        check_wake_model(s),
        check_audio(),
        check_claude_cli(),
        check_gws_cli(),
    ]
    if _sys.platform == "darwin":
        results.append(probe_microphone_access())  # NEW: mac TCC probe
    return results
```

- [ ] **Step 14: Run the doctor end-to-end on this machine**

Run: `tend doctor`

Expected: prints the existing checks plus the new `aec_engine`, `tts_provider`, and (on macOS) `microphone` lines. None should be `fail` if your environment was working before.

- [ ] **Step 15: Commit**

```bash
git add src/tend/checks.py tests/test_checks_microphone_access.py tests/test_checks_aec_engine.py tests/test_checks_tts_provider.py
git commit -m "feat(checks): mic permission probe + AEC engine + TTS provider doctor checks"
```

---

## Task 11: tend voices CLI subcommand

**Goal:** Add `tend voices {list,set,test}` for managing the AVSpeechSynthesizer voice. Mac-only — on Linux the subcommand prints a clear "not available on this platform" message.

**Files:**
- Create: `src/tend/cli/voices.py`
- Modify: `src/tend/cli/__init__.py` (register sub-app)
- Create: `tests/test_cli_voices.py`

- [ ] **Step 1: Write failing test for `voices list`**

Create `tests/test_cli_voices.py`:

```python
"""Tests for the `tend voices` subcommand."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

runner = CliRunner()


def test_voices_list_on_linux_prints_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    from tend.cli.voices import voices_app
    result = runner.invoke(voices_app, ["list"])
    assert result.exit_code == 1
    assert "macOS" in result.stdout


def test_voices_list_on_macos_prints_voices(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    fake_voice_a = MagicMock()
    fake_voice_a.identifier.return_value = "com.apple.voice.compact.en-US.Samantha"
    fake_voice_a.name.return_value = "Samantha"
    fake_voice_a.language.return_value = "en-US"
    fake_voice_a.quality.return_value = 1   # default

    fake_voice_b = MagicMock()
    fake_voice_b.identifier.return_value = "com.apple.voice.premium.en-US.Ava"
    fake_voice_b.name.return_value = "Ava (Premium)"
    fake_voice_b.language.return_value = "en-US"
    fake_voice_b.quality.return_value = 3   # premium

    fake_av = MagicMock()
    fake_av.AVSpeechSynthesisVoice.speechVoices.return_value = [fake_voice_a, fake_voice_b]
    fake_av.AVSpeechSynthesisVoiceQualityDefault = 1
    fake_av.AVSpeechSynthesisVoiceQualityEnhanced = 2
    fake_av.AVSpeechSynthesisVoiceQualityPremium = 3
    monkeypatch.setitem(sys.modules, "AVFoundation", fake_av)

    from tend.cli.voices import voices_app
    result = runner.invoke(voices_app, ["list"])

    assert result.exit_code == 0
    assert "Samantha" in result.stdout
    assert "Ava" in result.stdout
    assert "Premium" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_voices.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'tend.cli.voices'`.

- [ ] **Step 3: Implement voices_app**

Create `src/tend/cli/voices.py`:

```python
"""`tend voices` — list, select, and test AVSpeechSynthesizer voices."""

from __future__ import annotations

import sys

import typer
from rich.console import Console
from rich.table import Table

voices_app = typer.Typer(help="Manage Apple TTS voices (macOS).")

_QUALITY_NAMES = {1: "Default", 2: "Enhanced", 3: "Premium"}


def _require_macos() -> None:
    if sys.platform != "darwin":
        Console().print(
            "[red]`tend voices` is macOS-only "
            "(uses AVSpeechSynthesizer).[/red]"
        )
        raise typer.Exit(code=1)


def _load_voices() -> list[dict]:
    import AVFoundation
    out = []
    for v in AVFoundation.AVSpeechSynthesisVoice.speechVoices():
        out.append({
            "identifier": v.identifier(),
            "name": v.name(),
            "language": v.language(),
            "quality": int(v.quality()),
        })
    return out


@voices_app.command("list")
def voices_list() -> None:
    """List installed Apple voices, sorted by quality tier."""
    _require_macos()
    voices = _load_voices()
    voices.sort(key=lambda v: (-v["quality"], v["language"], v["name"]))

    console = Console()
    table = Table("Quality", "Language", "Name", "Identifier")
    for v in voices:
        table.add_row(
            _QUALITY_NAMES.get(v["quality"], str(v["quality"])),
            v["language"],
            v["name"],
            v["identifier"],
        )
    console.print(table)


@voices_app.command("set")
def voices_set(
    identifier: str = typer.Argument(..., help="AVSpeechSynthesisVoice identifier."),
) -> None:
    """Write the chosen identifier to [tts] avspeech_voice in tend.toml."""
    _require_macos()
    from tend import paths
    import tomllib

    toml_path = paths.tend_home() / "tend.toml"
    if toml_path.exists():
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    else:
        data = {}
    data.setdefault("tts", {})["avspeech_voice"] = identifier

    # tomllib doesn't write; emit a minimal-but-readable TOML by hand.
    lines = []
    for section, body in data.items():
        lines.append(f"[{section}]")
        for k, val in body.items():
            if isinstance(val, str):
                lines.append(f'{k} = "{val}"')
            else:
                lines.append(f"{k} = {val}")
        lines.append("")
    toml_path.write_text("\n".join(lines), encoding="utf-8")
    Console().print(f"[green]✓[/green] Set tts.avspeech_voice = {identifier}")


@voices_app.command("test")
def voices_test(
    identifier: str = typer.Argument("", help="Identifier (default: configured)."),
) -> None:
    """Speak a short sample with the given (or configured) voice."""
    _require_macos()
    import asyncio
    from tend.config import Settings
    from tend.services import AVSpeechSynthesizerTTSService

    settings = Settings()
    voice_id = identifier or settings.avspeech_voice

    async def main():
        svc = AVSpeechSynthesizerTTSService(
            voice_identifier=voice_id, sample_rate=settings.sample_rate,
        )
        async for frame in svc.run_tts(
            "tend is now using this voice. It sounds like this."
        ):
            pass

    Console().print(
        f"Speaking sample with voice "
        f"[bold]{voice_id or '(system default)'}[/bold]..."
    )
    asyncio.run(main())
    Console().print("[green]✓[/green] Done.")
```

- [ ] **Step 4: Register the sub-app in cli/__init__.py**

Open `src/tend/cli/__init__.py` and find where other sub-apps are registered (look for `app.add_typer(...)` calls). Append:

```python
from tend.cli.voices import voices_app
app.add_typer(voices_app, name="voices")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli_voices.py -v`

Expected: 2 PASS.

- [ ] **Step 6: Manual smoke on Mac**

```bash
tend voices list
```

Expected: a rich-formatted table of installed voices, Premium ones at the top.

- [ ] **Step 7: Commit**

```bash
git add src/tend/cli/voices.py src/tend/cli/__init__.py tests/test_cli_voices.py
git commit -m "feat(cli): tend voices {list,set,test} subcommand"
```

---

## Task 12: setup wizard macOS additions

**Goal:** Replace `_ask_tts` with a Mac-aware version, add the TCC mic-permission probe step, and add the Premium-voice install nudge.

**Files:**
- Modify: `src/tend/cli/setup.py`

- [ ] **Step 1: Inspect the current `_ask_tts`**

Open `src/tend/cli/setup.py:74-83`:

```python
def _ask_tts(noninteractive: bool) -> str:
    if noninteractive:
        return "elevenlabs" if secrets.secret_backend("ELEVENLABS_API_KEY") else "piper"
    pick = questionary.select(
        "Text-to-speech provider?",
        choices=["ElevenLabs (cloud)", "Piper (local)"],
    ).ask()
    if pick is None:
        raise typer.Exit(code=2)
    return "elevenlabs" if pick.startswith("ElevenLabs") else "piper"
```

- [ ] **Step 2: Replace `_ask_tts` with a platform-aware version**

Replace the function:

```python
def _ask_tts(noninteractive: bool) -> str:
    if sys.platform == "darwin":
        return _ask_tts_macos(noninteractive)

    # Linux (existing behavior)
    if noninteractive:
        return "elevenlabs" if secrets.secret_backend("ELEVENLABS_API_KEY") else "piper"
    pick = questionary.select(
        "Text-to-speech provider?",
        choices=["ElevenLabs (cloud)", "Piper (local)"],
    ).ask()
    if pick is None:
        raise typer.Exit(code=2)
    return "elevenlabs" if pick.startswith("ElevenLabs") else "piper"


def _ask_tts_macos(noninteractive: bool) -> str:
    if noninteractive:
        return "elevenlabs" if secrets.secret_backend("ELEVENLABS_API_KEY") else "avspeech"
    pick = questionary.select(
        "How should tend speak?",
        choices=[
            "Apple's built-in voice (free, fast, runs offline)",
            "ElevenLabs (cloud, best quality, paid)",
        ],
    ).ask()
    if pick is None:
        raise typer.Exit(code=2)
    return "avspeech" if pick.startswith("Apple") else "elevenlabs"
```

Add `import sys` to the file's imports if not already present.

- [ ] **Step 3: Write the TTS provider into tend.toml**

In `setup_command`, after the existing `tts = _ask_tts(noninteractive)` and the optional ElevenLabs key prompt, persist the choice. Add this helper:

```python
def _persist_tts_provider(provider: str, avspeech_voice: str = "") -> None:
    """Write the chosen TTS provider to ~/.tend/tend.toml."""
    from tend import paths
    import tomllib

    toml_path = paths.tend_home() / "tend.toml"
    if toml_path.exists():
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    else:
        data = {}
    tts = data.setdefault("tts", {})
    tts["provider"] = provider
    if avspeech_voice:
        tts["avspeech_voice"] = avspeech_voice

    lines = []
    for section, body in data.items():
        lines.append(f"[{section}]")
        for k, val in body.items():
            if isinstance(val, str):
                lines.append(f'{k} = "{val}"')
            elif isinstance(val, bool):
                lines.append(f"{k} = {'true' if val else 'false'}")
            else:
                lines.append(f"{k} = {val}")
        lines.append("")
    toml_path.write_text("\n".join(lines), encoding="utf-8")
```

After the `tts = _ask_tts(...)` block, add: `_persist_tts_provider(tts)`.

- [ ] **Step 4: Add the mic-permission probe step (macOS only)**

In `setup_command`, after the optional-skills section and before the "Setup complete." print, add:

```python
    if sys.platform == "darwin":
        _mac_mic_permission_step(console)
        if tts == "avspeech":
            _mac_premium_voice_nudge(noninteractive, console)
```

Then add the two helpers:

```python
def _mac_mic_permission_step(console: Console) -> None:
    """Trigger the TCC microphone permission prompt and verify."""
    from tend.checks import probe_microphone_access

    console.print(
        "\n[bold]Microphone access (macOS)[/bold]\n"
        "  macOS will ask to grant tend access to your microphone.\n"
        "  When the prompt appears, click [bold]Allow[/bold]."
    )
    if not questionary.confirm("Continue?", default=True).ask():
        raise typer.Exit(code=2)

    result = probe_microphone_access()
    if result.status == "ok":
        console.print("  [green]✓[/green] Microphone access granted.")
    elif result.status == "warn":
        console.print(
            "  [yellow]⚠[/yellow] Captured silence — confirm the OS prompt "
            "was approved and the mic is unmuted. Run `tend doctor` later to retry."
        )
    else:
        console.print(f"  [red]✗[/red] {result.detail}")
        if result.remediation:
            console.print(f"  remediation: {result.remediation}")


def _mac_premium_voice_nudge(noninteractive: bool, console: Console) -> None:
    """Offer to open VoiceOver Utility for Premium voice install."""
    import subprocess

    console.print(
        "\n[bold]Premium voice (optional, free)[/bold]\n"
        "  Apple's default voice is okay. For a markedly better Premium\n"
        "  voice (~300–600 MB), download one via VoiceOver Utility:\n"
        "    1.  Open VoiceOver Utility (⌃ ⌥ Fn F8)\n"
        "    2.  Speech → Voices → +\n"
        "    3.  Pick a language → pick a voice marked Premium or Enhanced\n"
        "    4.  Click Download → wait for it to finish\n"
        "    5.  Close VoiceOver Utility\n\n"
        "  Note: Siri-quality voices are not accessible to tend; Premium\n"
        "  voices are the highest tier the AVSpeechSynthesizer API exposes."
    )
    if noninteractive or not questionary.confirm(
        "Open VoiceOver Utility now?", default=False,
    ).ask():
        console.print(
            "  Skipped. Later: `open -a 'VoiceOver Utility'`, "
            "then `tend voices set <identifier>`."
        )
        return

    subprocess.run(["open", "-a", "VoiceOver Utility"], check=False)
    console.print(
        "  Opened. After your download finishes, run:\n"
        "    tend voices list\n"
        "    tend voices set <identifier>\n"
        "    tend voices test"
    )
```

- [ ] **Step 5: Manual smoke test on Mac**

Run on a fresh Mac (or after wiping the tend workspace):

```bash
tend setup
```

Verify:
- The TTS step offers "Apple's built-in voice" and "ElevenLabs" (no Piper).
- After picking Apple, the wizard runs the mic-permission step → TCC prompt fires → grant → "Microphone access granted."
- The Premium-voice nudge appears with the VoiceOver Utility instructions.
- Picking "open now" actually launches VoiceOver Utility.
- After setup, `cat ~/.tend/tend.toml` shows `[tts]\nprovider = "avspeech"`.

- [ ] **Step 6: Commit**

```bash
git add src/tend/cli/setup.py
git commit -m "feat(cli): macOS-aware setup wizard with mic probe + voice nudge"
```

---

## Task 13: Pyproject conditional deps + macOS version pin

**Goal:** Lock down the dependency markers (piper → Linux-only, pyobjc → Darwin-only, pyaec → all, webrtc → optional extra), pin macOS 14 minimum, and verify everything installs cleanly.

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Apply the full dependency block**

Edit `pyproject.toml` `[project]` section to look like:

```toml
[project]
name = "tend"
version = "0.1.0"
description = "Personal AI assistant for desk workers (Raspberry Pi voice agent)"
requires-python = ">=3.11"
classifiers = [
    "Operating System :: POSIX :: Linux",
    "Operating System :: MacOS :: MacOS X",
]
dependencies = [
    "anthropic>=0.40",
    "aiohttp>=3.9",
    "croniter>=1.4",
    "loguru>=0.7",
    "openwakeword>=0.4",
    "pipecat-ai[whisper,silero,local,deepgram,elevenlabs]>=1.1",
    'pipecat-ai[piper]>=1.1; platform_system == "Linux"',
    "pipecat-ai-subagents==0.4.0",
    "rapidfuzz>=3.0",
    "pydantic-settings>=2.2",
    "typer>=0.12",
    "questionary>=2.0",
    "rich>=13.0",
    "keyring>=24.0",
    "pyaec>=1.0.1",
    'pyobjc-framework-AVFoundation>=10; platform_system == "Darwin"',
]

[project.optional-dependencies]
aec-webrtc = ["webrtc-audio-processing>=0.1.3"]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "freezegun>=1.4",
]
```

- [ ] **Step 2: Add the runtime macOS version check**

Modify `src/tend/main.py` — at the very top of the file's executable region (before the agent setup), add:

```python
def _check_macos_minimum() -> None:
    import platform
    import sys
    if sys.platform != "darwin":
        return
    ver = platform.mac_ver()[0]
    if not ver:
        return
    major = int(ver.split(".")[0])
    if major < 14:
        print(
            f"tend requires macOS 14 (Sonoma) or later; detected {ver}",
            file=sys.stderr,
        )
        sys.exit(1)
```

And call it from `if __name__ == "__main__":` or whatever entry runs first:

```python
_check_macos_minimum()
```

- [ ] **Step 3: Verify installation on the dev machine**

Run: `pip install -e .`

Expected: succeeds. On macOS, both `pyaec` and `pyobjc-framework-AVFoundation` should install from wheels. On Linux, `pyaec` installs from wheel and `pyobjc-framework-AVFoundation` is skipped (marker-gated).

- [ ] **Step 4: Verify import paths**

Run: `python -c "import pyaec; print(pyaec.__file__)"` — expected: prints the path.

If on Mac, also run: `python -c "import AVFoundation; print('ok')"` — expected: `ok`.

- [ ] **Step 5: Verify the optional extra installs**

On Mac (if testing AEC3): `brew install webrtc-audio-processing` then `pip install ".[aec-webrtc]"`.

Expected: builds and installs (slow; needs Xcode CLT). If it fails, document the failure and continue — pyaec is the default fallback.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/tend/main.py
git commit -m "feat(packaging): conditional deps + macOS 14 minimum check"
```

---

## Task 14: Documentation updates

**Goal:** README install blocks, CLAUDE.md architecture note, conventions doc reconciliation. Roadmap entry already updated in the spec PR.

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/conventions.md`

- [ ] **Step 1: Inspect the current README**

Open `README.md` and look at the current "Running" / "Install" section.

- [ ] **Step 2: Add Linux + macOS install blocks**

Replace the install/run section with:

```markdown
## Install

### Linux (Raspberry Pi / Ubuntu / Debian)

Prereqs:

    sudo apt install portaudio19-dev python3-pip pipx
    pipx ensurepath

Install:

    pipx install tend

Bootstrap and verify:

    tend setup
    tend doctor

Install as a systemd user service and start at login:

    tend service install
    tend service start
    tend service status

### macOS (Apple Silicon, macOS 14+)

Prereqs:

    brew install python portaudio

Install:

    pipx install tend

Optional state-of-the-art AEC (otherwise tend uses pyaec/Speex by default):

    xcode-select --install
    brew install webrtc-audio-processing
    pipx inject tend webrtc-audio-processing

Bootstrap and verify:

    tend setup           # grants microphone access when prompted
    tend doctor

Install as a LaunchAgent and start:

    tend service install
    tend service start
    tend service status

#### Optional: install a Premium voice

The default Apple TTS voice is dated. For markedly better quality
(~300–600 MB, free), download a Premium voice via VoiceOver Utility,
which exposes the full voice catalog on macOS 14+:

    open -a "VoiceOver Utility"

Then: Speech → Voices → + → pick a language → pick a voice marked
Premium or Enhanced → Click Download.

After the download finishes:

    tend voices list                   # see what tend can use
    tend voices set <identifier>       # pick it
    tend voices test                   # confirm

Note: Apple's Siri-tier voices are not exposed to third-party apps
via AVSpeechSynthesizer. Premium voices are the highest quality
tier tend can reach.

## Running

Development:

    python -m tend

Production (after `tend service install`):

    # Linux
    systemctl --user start tend
    # macOS
    launchctl kickstart gui/$UID/com.tend.daemon
```

- [ ] **Step 3: Update CLAUDE.md architecture note**

Find the "Architecture (current)" block in `CLAUDE.md` and update the pipeline description to mention `select_audio_path`. Replace the existing pipeline ascii art's input block with:

```
        transport.in → *select_audio_path(settings).pre_vad
          → VAD → OpenWakeWordGate → STT
          ...
```

Also append a one-paragraph note at the bottom of "Architecture (current)":

```markdown
On macOS the pipeline diverges slightly: `audio_in_channels=1` and
no `StereoToMonoLeft`. AEC runs at pipecat's `audio_in_filter` slot
(default engine: pyaec/Speex). The OutputAudioCapture processor sits
before `transport.output()` to feed the speaker reference signal
into the AEC's ring buffer. See
`docs/superpowers/specs/2026-05-13-tend-macos-port-design.md`.
```

- [ ] **Step 4: Reconcile docs/conventions.md**

Open `docs/conventions.md` and find the `**audio/channels.py**` bullet:

> `audio/channels.py` selects mono-mic on macOS (no XVF3800 left-channel downmix); selects the XVF3800 path on Linux when the device-name match hits.

Update to match what was actually built:

```markdown
- **`audio/channels.py::select_audio_path`** dispatches by `sys.platform`: macOS → mono + AEC filter; Linux → stereo + StereoToMonoLeft (XVF3800). An optional `[audio] mic_channels = N` override forces the layout for edge cases.
```

Find the install bullet (in the distribution section) that says `pipx install tend (Linux/Mac)` and update to mention the macOS optional `[aec-webrtc]` extra and `brew install portaudio` prereq.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md docs/conventions.md
git commit -m "docs(macos-port): install blocks, architecture note, conventions reconciliation"
```

---

## Task 15: WebRTC AEC3 implementation (gated)

**Goal:** Implement the `webrtc-aec3` engine path in `aec.py`. Only run this task after the manual smoke test from Task 5 reveals that pyaec/Speex quality is inadequate for the built-in MacBook mic+speaker configuration. If Speex is adequate, **skip this task entirely** and mark in the roadmap.

**Files:**
- Modify: `src/tend/audio/aec.py`
- Modify: `tests/test_audio_aec.py`

- [ ] **Step 1: Verify the optional dep installs**

Run on a Mac with Xcode CLT installed:

```bash
brew install webrtc-audio-processing
pip install webrtc-audio-processing
```

Expected: source build succeeds. If it fails, fix the build environment before continuing.

- [ ] **Step 2: Inspect the webrtc-audio-processing Python API**

Run: `python -c "from webrtc_audio_processing import AudioProcessingModule; help(AudioProcessingModule)" | head -50`

Note the constructor signature and the `process_stream(near_end, far_end)` (or equivalent) entry point. The exact API names vary by version — read what's actually shipped.

- [ ] **Step 3: Write failing test**

Append to `tests/test_audio_aec.py`:

```python
@pytest.mark.asyncio
async def test_webrtc_filter_length_preserved(monkeypatch):
    pytest.importorskip("webrtc_audio_processing")
    from tend.audio.aec import WebRTCAEC3Filter, ReferenceBuffer

    rb = ReferenceBuffer()
    f = WebRTCAEC3Filter(reference=rb)
    await f.start(sample_rate=16000)

    rb.write(b"\x05\x00" * 160)
    mic = b"\x20\x00" * 160
    out = await f.filter(mic)

    assert isinstance(out, bytes)
    assert len(out) == len(mic)
```

- [ ] **Step 4: Implement WebRTCAEC3Filter**

In `src/tend/audio/aec.py`, replace the `NotImplementedError` raise in `make_aec_filter` for `"webrtc-aec3"`:

```python
    if engine == "webrtc-aec3":
        if not _webrtc_importable():
            raise RuntimeError(
                "aec_engine='webrtc-aec3' requested but webrtc-audio-processing "
                "is not installed. Install with: pipx inject tend webrtc-audio-processing"
            )
        return WebRTCAEC3Filter(reference=reference)
```

And add the filter class. The exact API depends on what `webrtc-audio-processing` 0.1.3 actually exposes — the implementer must consult `python -c "import webrtc_audio_processing; help(webrtc_audio_processing)"` and adapt. Reference shape:

```python
class WebRTCAEC3Filter(BaseAudioFilter):
    """Pipecat-compatible AEC filter backed by libwebrtc-audio-processing."""

    _FRAME_SAMPLES = 160         # 10 ms at 16 kHz; webrtc AEC3's native chunk
    _SAMPLE_BYTES = 2

    def __init__(self, reference: ReferenceBuffer):
        self._reference = reference
        self._sample_rate = 0
        self._apm = None         # webrtc_audio_processing.AudioProcessingModule
        self._enabled = True

    async def start(self, sample_rate: int):
        from webrtc_audio_processing import AudioProcessingModule
        self._sample_rate = sample_rate
        self._apm = AudioProcessingModule(aec_type=2)  # 2 = AEC3
        self._apm.set_stream_format(sample_rate, 1, sample_rate, 1)
        self._apm.set_reverse_stream_format(sample_rate, 1)

    async def stop(self):
        self._apm = None

    async def process_frame(self, frame: FilterControlFrame):
        if isinstance(frame, FilterEnableFrame):
            self._enabled = frame.enable

    async def filter(self, audio: bytes) -> bytes:
        if not self._enabled or self._apm is None:
            return audio

        chunk_bytes = self._FRAME_SAMPLES * self._SAMPLE_BYTES
        out_chunks = []
        for off in range(0, len(audio), chunk_bytes):
            mic_chunk = audio[off:off + chunk_bytes]
            ref_chunk = self._reference.read(len(mic_chunk))
            # Pad to frame size if needed.
            if len(mic_chunk) < chunk_bytes:
                mic_chunk = mic_chunk + b"\x00" * (chunk_bytes - len(mic_chunk))
                ref_chunk = ref_chunk + b"\x00" * (chunk_bytes - len(ref_chunk))
            self._apm.process_reverse_stream(ref_chunk)
            cleaned = self._apm.process_stream(mic_chunk)
            out_chunks.append(cleaned)
        out = b"".join(out_chunks)
        return out[:len(audio)]
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_audio_aec.py -v`

Expected: all PASS, including the new `test_webrtc_filter_length_preserved` (only when webrtc-audio-processing is installed; otherwise the test skips).

- [ ] **Step 6: Manual ear-test A/B**

With `aec_engine = "speex"`: run a turn, intentionally have tend speak while you say "stop." Capture residual echo in the next STT transcript (look for words from tend's own reply leaking into the user transcript).

Switch to `aec_engine = "webrtc-aec3"`: repeat the same turn. Compare.

- [ ] **Step 7: Commit**

```bash
git add src/tend/audio/aec.py tests/test_audio_aec.py
git commit -m "feat(audio): WebRTCAEC3Filter implementation"
```

---

## Task 16: Manual smoke pass + ROADMAP marker update

**Goal:** End-to-end verification on a real MacBook, then mark the roadmap entry shipped.

**Files:**
- Modify: `ROADMAP.md`

- [ ] **Step 1: Fresh-install smoke**

On a Mac account that doesn't have tend installed:

```bash
brew install python portaudio
pipx install tend
tend setup
```

Verify each wizard step works:
- Workspace creation prints the path.
- STT pick saves a Deepgram or Whisper choice.
- TTS pick offers Apple + ElevenLabs (no Piper).
- Picking Apple triggers the mic-permission step → TCC prompt → grant → "granted" line.
- Premium-voice nudge appears with VoiceOver Utility instructions.
- "Open VoiceOver Utility?" Y actually launches it.

After setup completes, verify `~/.tend/tend.toml` has `[tts] provider = "avspeech"`.

- [ ] **Step 2: Doctor green**

```bash
tend doctor
```

Expected: all checks `ok`, `warn` is acceptable on `aec_engine` if AEC3 isn't installed. No `fail`.

- [ ] **Step 3: Service lifecycle**

```bash
tend service install
ls ~/Library/LaunchAgents/com.tend.daemon.plist     # should exist
tend service start
launchctl list | grep tend                          # should show the agent
tend service status                                  # state = "running"
# Speak the wake word, get a reply.
tend service stop
tend service status                                  # state != "running"
```

- [ ] **Step 4: Barge-in smoke**

Wake tend, ask a question that returns a long reply ("describe your morning routine"). While tend is speaking, say the configured sleep phrase. Verify:
- tend stops speaking promptly.
- The sleep-phrase trigger fires (Brain deactivates).

This confirms AEC isn't masking the user's speech enough to break the gate.

- [ ] **Step 5: Voices subcommand**

```bash
tend voices list
tend voices set com.apple.voice.compact.en-US.Samantha
tend voices test
```

Expected: the test utterance speaks aloud in the selected voice.

- [ ] **Step 6: Update ROADMAP.md**

In `ROADMAP.md`, move item #2 out of "Next" — either delete it (and add to CHANGELOG if one exists) or mark it shipped:

```markdown
2. ~~**macOS port**~~ — shipped via `docs/superpowers/specs/2026-05-13-tend-macos-port-design.md` + `docs/superpowers/plans/2026-05-13-tend-macos-port.md` (commit <hash>).
```

Also append to "After public release" candidates (if not already there from the spec PR):

```markdown
- **Cross-platform local TTS via sherpa-onnx.** Same VITS voice on Pi and Mac for users who run tend across devices. ~1 day. Optional; ship if there's user demand.
```

- [ ] **Step 7: Commit + tag**

```bash
git add ROADMAP.md
git commit -m "docs(roadmap): mark macOS port shipped"
```

---

## Implementation notes

**Pi regression safety:** Tasks 1–4 add new modules without modifying behavior. Task 5 is the moment Pi behavior could change — `select_audio_path` on Linux returns the same `(2, (StereoToMonoLeft,), None)` tuple that the old hardcoded pipeline used. Run a smoke turn on the Pi after Task 5 lands.

**Test-only-on-Mac items:**
- Task 6 step 7 (AVSpeech native synth) — has no automated test that runs on Linux; the unit test mocks AVFoundation.
- Task 11 step 6 (voices list) — same.
- Task 12 step 5 (setup wizard end-to-end) — same.
- Tasks 15 and 16 — manual passes only.

**WebRTC AEC3 gate (Task 15):** the entire task is optional. The criterion is: does the speex path leave audible residual echo that degrades STT or barge-in? If no, skip; if yes, do.

**Common failure modes the implementer should watch for:**

1. `pip install pyaec` picks a wheel for the wrong architecture (e.g., x86_64 wheel on arm64 Mac running x86 Python). Symptom: `OSError: cannot load library`. Fix: confirm `python -c "import platform; print(platform.machine())"` matches the wheel's tag, reinstall with `--force-reinstall`.
2. `pyobjc-framework-AVFoundation` install picks an old version that doesn't expose the `writeUtterance_toBufferCallback_` selector. Pin to `>=10`; if needed, bump to `>=11`.
3. `AVSpeechSynthesizer.writeUtterance_toBufferCallback_` is macOS 13+. On macOS 12, this call returns `nil` and never fires the callback. The macOS-14 minimum check in Task 13 prevents this case but worth noting.
4. `launchctl bootstrap` may fail with "already bootstrapped" on a fresh install if a stale agent reference exists. The fallback to `launchctl load` in Task 9's `install()` handles this.
5. TCC double-prompt: described in spec's "Microphone permission" section. Task 9's manual smoke (step 7) is the right time to surface and grant the second prompt.

## Self-review

**Spec coverage check** — walking the spec section by section:

- Audio path branching → Task 1 ✓
- AEC engine selection → Task 2 ✓
- AEC plumbing (filter + output tap + reference buffer) → Tasks 3, 4 ✓
- Barge-in preservation → covered by Task 16 step 4 ✓
- Local TTS on macOS → Task 6 ✓
- TTS provider selection → Task 7 ✓
- Service install via launchd → Tasks 8, 9 ✓
- Microphone permission (TCC) → Task 10 + Task 12 ✓
- Premium voice install flow → Tasks 11, 12 ✓
- Packaging → Task 13 ✓
- Versioning & architecture commitment → Task 13 (the macOS 14 check) ✓
- Module changes summary — covered file-by-file across tasks ✓
- Test plan — embedded in each task's TDD steps + Task 16 manual smoke ✓
- Implementation order — followed (with Task 15 deferred) ✓
- Known limitations → documented in spec; surfaced via doctor warns ✓
- Roadmap & docs deltas → Task 14, Task 16 ✓

**Placeholder scan:** no TBD/TODO. Every step has either concrete code or a concrete command + expected output. Task 6 step 5 has a "note for the implementer" about a known PyObjC fiddliness; the surrounding test mocks it away so the task passes regardless, and a manual smoke is required to claim it done — that's a real implementation note, not a placeholder.

**Type consistency:** `ReferenceBuffer.read()/write()`, `make_aec_filter(engine, sample_rate, reference)`, `select_audio_path(settings, aec_filter_factory)`, `AudioPath(in_channels, pre_vad_processors, aec_filter)`, `probe_microphone_access() -> CheckResult` — all consistent across the tasks that reference them.

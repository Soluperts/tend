"""Tests for tend.audio.aec — ReferenceBuffer + resolve_aec_engine."""

from __future__ import annotations

import sys

import pytest

from tend.audio.aec import (
    ReferenceBuffer,
    SpeexAECFilter,
    make_aec_filter,
    resolve_aec_engine,
)
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


def test_resolve_aec_engine_off_returns_off():
    settings = Settings(_env_file=None, aec_engine="off")
    assert resolve_aec_engine(settings) == "off"


def test_resolve_aec_engine_explicit_webrtc():
    settings = Settings(_env_file=None, aec_engine="webrtc-aec3")
    assert resolve_aec_engine(settings) == "webrtc-aec3"


def test_resolve_aec_engine_explicit_speex():
    settings = Settings(_env_file=None, aec_engine="speex")
    assert resolve_aec_engine(settings) == "speex"


def test_resolve_aec_engine_auto_linux_returns_off(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    settings = Settings(_env_file=None, aec_engine="auto")
    assert resolve_aec_engine(settings) == "off"


def test_resolve_aec_engine_auto_macos_returns_vpio(monkeypatch):
    """macOS auto always resolves to vpio regardless of webrtc availability."""
    monkeypatch.setattr(sys, "platform", "darwin")
    settings = Settings(_env_file=None, aec_engine="auto")
    assert resolve_aec_engine(settings) == "vpio"


# ---------------------------------------------------------------------------
# SpeexAECFilter + make_aec_filter
# ---------------------------------------------------------------------------


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
    assert make_aec_filter("off", reference=rb) is None


def test_make_aec_filter_speex_returns_filter():
    rb = ReferenceBuffer()
    f = make_aec_filter("speex", reference=rb)
    assert isinstance(f, SpeexAECFilter)


def test_make_aec_filter_webrtc_unavailable_raises(monkeypatch):
    monkeypatch.setattr("tend.audio.aec._webrtc_importable", lambda: False)
    rb = ReferenceBuffer()
    with pytest.raises(RuntimeError, match="webrtc-audio-processing"):
        make_aec_filter("webrtc-aec3", reference=rb)


def test_make_aec_filter_unknown_engine_raises():
    rb = ReferenceBuffer()
    with pytest.raises(ValueError, match="Unknown aec engine"):
        make_aec_filter("not-an-engine", reference=rb)


@pytest.mark.asyncio
async def test_speex_filter_stop_releases_resources():
    f = SpeexAECFilter(reference=ReferenceBuffer())
    await f.start(sample_rate=16000)
    assert f._aec is not None
    assert f._pyaec_lib is not None

    await f.stop()

    assert f._aec is None
    assert f._pyaec_lib is None

    # Second call must be idempotent (no exception).
    await f.stop()


# ---------------------------------------------------------------------------
# resolve_aec_engine — vpio support (Task 14)
# ---------------------------------------------------------------------------


def test_resolve_aec_engine_macos_auto_returns_vpio(monkeypatch):
    """macOS + auto should resolve to vpio (the new default)."""
    monkeypatch.setattr(sys, "platform", "darwin")

    class _S:
        aec_engine = "auto"

    assert resolve_aec_engine(_S()) == "vpio"


def test_resolve_aec_engine_macos_explicit_vpio_passes_through(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    class _S:
        aec_engine = "vpio"

    assert resolve_aec_engine(_S()) == "vpio"


def test_resolve_aec_engine_vpio_on_linux_raises(monkeypatch):
    """Explicit vpio on Linux is a user error — raise with a clear message."""
    monkeypatch.setattr(sys, "platform", "linux")

    class _S:
        aec_engine = "vpio"

    with pytest.raises(ValueError, match="vpio.*only supported on macOS"):
        resolve_aec_engine(_S())


def test_resolve_aec_engine_macos_explicit_speex_passes_through(monkeypatch):
    """User can still opt out of VPIO by explicitly picking speex."""
    monkeypatch.setattr(sys, "platform", "darwin")

    class _S:
        aec_engine = "speex"

    assert resolve_aec_engine(_S()) == "speex"

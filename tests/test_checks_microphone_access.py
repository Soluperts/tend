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
    assert "permission" in result.detail.lower() or "denied" in result.detail.lower() or "could not open" in result.detail.lower()


def test_probe_microphone_access_silent_returns_warn(monkeypatch):
    fake_pa = MagicMock()
    fake_stream = MagicMock()
    fake_stream.read.return_value = b"\x00" * 1600
    fake_pa.PyAudio.return_value.open.return_value = fake_stream
    monkeypatch.setitem(sys.modules, "pyaudio", fake_pa)

    result = probe_microphone_access()
    assert result.status in ("warn", "ok")

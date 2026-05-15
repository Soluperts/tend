# SPDX-License-Identifier: MIT
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

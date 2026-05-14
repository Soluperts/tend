"""Tests for tend.checks.check_tts_provider."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Pre-import tend.services so the lazy import inside check_tts_provider doesn't
# trigger the pipecat → loguru → sysconfig chain while sys.platform is patched
# to a non-native value (which would cause a _sysconfigdata lookup error).
import tend.services  # noqa: F401

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

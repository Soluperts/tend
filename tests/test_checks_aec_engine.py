# SPDX-License-Identifier: MIT
"""Tests for tend.checks.check_aec_engine."""

from __future__ import annotations

import sys

import pytest

# Pre-import tend.audio.aec so the lazy import inside check_aec_engine doesn't
# trigger the pipecat → loguru → sysconfig chain while sys.platform is patched
# to a non-native value (which would cause a _sysconfigdata lookup error).
import tend.audio.aec  # noqa: F401

from tend.checks import check_aec_engine
from tend.config import Settings


def test_check_aec_engine_off(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    s = Settings(_env_file=None, aec_engine="off")
    r = check_aec_engine(s)
    assert r.status == "ok"
    assert "off" in r.detail.lower()


def test_check_aec_engine_auto_macos_resolves_to_vpio(monkeypatch):
    """macOS + auto resolves to vpio regardless of webrtc availability."""
    monkeypatch.setattr(sys, "platform", "darwin")
    s = Settings(_env_file=None, aec_engine="auto")
    r = check_aec_engine(s)
    assert r.status == "ok"
    assert "vpio" in r.detail.lower()


def test_check_aec_engine_explicit_webrtc_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("tend.audio.aec._webrtc_importable", lambda: False)
    s = Settings(_env_file=None, aec_engine="webrtc-aec3")
    r = check_aec_engine(s)
    assert r.status == "fail"

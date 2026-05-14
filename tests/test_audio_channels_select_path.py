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

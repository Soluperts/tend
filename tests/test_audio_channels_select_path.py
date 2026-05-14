"""Tests for tend.audio.channels.select_audio_path."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from tend.audio.aec import ReferenceBuffer
from tend.audio.channels import AECPair, AudioPath, StereoToMonoLeft, select_audio_path
from tend.config import Settings


def _stub_factory(*args, **kwargs):
    return None


def _make_sentinel_pair():
    return AECPair(filter=MagicMock(name="aec_filter"), reference=ReferenceBuffer())


def test_linux_default_returns_xvf3800_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    settings = Settings(_env_file=None)
    path = select_audio_path(settings, aec_filter_factory=_stub_factory)

    assert path.in_channels == 2
    assert len(path.pre_vad_processors) == 1
    assert isinstance(path.pre_vad_processors[0], StereoToMonoLeft)
    assert path.aec is None


def test_macos_default_returns_mono_path_no_xvf(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    sentinel_pair = _make_sentinel_pair()
    settings = Settings(_env_file=None)
    path = select_audio_path(settings, aec_filter_factory=lambda s: sentinel_pair)

    assert path.in_channels == 1
    assert path.pre_vad_processors == ()
    assert path.aec is sentinel_pair


def test_explicit_mic_channels_1_overrides_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    settings = Settings(_env_file=None, mic_channels=1)
    path = select_audio_path(settings, aec_filter_factory=_stub_factory)

    assert path.in_channels == 1
    assert path.pre_vad_processors == ()


def test_explicit_mic_channels_2_overrides_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    sentinel_pair = _make_sentinel_pair()
    settings = Settings(_env_file=None, mic_channels=2)
    path = select_audio_path(settings, aec_filter_factory=lambda s: sentinel_pair)

    assert path.in_channels == 2
    assert len(path.pre_vad_processors) == 1
    assert isinstance(path.pre_vad_processors[0], StereoToMonoLeft)
    assert path.aec is sentinel_pair


def test_explicit_mic_channels_2_on_linux_calls_factory(monkeypatch):
    """Even on Linux, explicit mic_channels=2 should defer to the factory.
    The factory itself decides whether to return an AEC filter based on
    [audio] aec_engine — this test asserts select_audio_path doesn't shortcut
    the decision."""
    monkeypatch.setattr(sys, "platform", "linux")
    sentinel_pair = _make_sentinel_pair()
    settings = Settings(_env_file=None, mic_channels=2)
    path = select_audio_path(settings, aec_filter_factory=lambda s: sentinel_pair)

    assert path.in_channels == 2
    assert path.aec is sentinel_pair


def test_audio_path_has_transport_factory_field_with_default(monkeypatch):
    """AudioPath gains a transport_factory field; default for non-VPIO
    branches is LocalAudioTransport so existing Linux paths keep working."""
    from pipecat.transports.local.audio import LocalAudioTransport

    monkeypatch.setattr(sys, "platform", "linux")
    settings = Settings(_env_file=None)
    path = select_audio_path(settings, aec_filter_factory=_stub_factory)

    assert path.transport_factory is LocalAudioTransport


def test_macos_vpio_engine_selects_avaudio_transport(monkeypatch):
    """When aec_engine resolves to 'vpio' on macOS, AudioPath uses
    AVAudioTransport as the factory and has no AEC pair (VPIO handles
    AEC upstream of pipecat)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    # resolve_aec_engine doesn't accept 'vpio' until Task 14 — monkeypatch it.
    import tend.audio.channels as channels_mod
    monkeypatch.setattr(channels_mod, "resolve_aec_engine", lambda s: "vpio")

    settings = Settings(_env_file=None)

    def boom(_):
        raise AssertionError("AEC factory should not run for vpio engine")

    path = select_audio_path(settings, aec_filter_factory=boom)

    from tend.audio.av_audio import AVAudioTransport
    assert path.in_channels == 1
    assert path.pre_vad_processors == ()
    assert path.aec is None
    assert path.transport_factory is AVAudioTransport

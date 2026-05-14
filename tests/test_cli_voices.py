"""Tests for the `tend voices` subcommand."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

# Import at module level so the module is cached before any platform patching.
from tend.cli.voices import voices_app

runner = CliRunner()


def test_voices_list_on_linux_prints_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
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

    result = runner.invoke(voices_app, ["list"])

    assert result.exit_code == 0
    assert "Samantha" in result.stdout
    assert "Ava" in result.stdout
    assert "Premium" in result.stdout

"""Tests for the service factories — preflight + cloud-or-local fallback."""

from unittest.mock import MagicMock

import httpx
import pytest

from tend.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# STT factory --------------------------------------------------------------

def test_make_stt_returns_whisper_when_no_deepgram_key():
    from tend.services import _make_stt
    s = _settings(deepgram_api_key=None)
    stt = _make_stt(s)
    assert stt.__class__.__name__ == "WhisperSTTService"


def test_make_stt_returns_deepgram_on_healthy_preflight(monkeypatch):
    from tend.services import _make_stt

    fake_response = MagicMock(status_code=200, json=lambda: {"projects": [1]})
    monkeypatch.setattr(httpx, "get", MagicMock(return_value=fake_response))

    s = _settings(deepgram_api_key="dg-test")
    stt = _make_stt(s)
    assert stt.__class__.__name__ == "DeepgramSTTService"


def test_make_stt_falls_back_to_whisper_on_401(monkeypatch):
    from tend.services import _make_stt

    fake_response = MagicMock(status_code=401, text="unauthorised")
    monkeypatch.setattr(httpx, "get", MagicMock(return_value=fake_response))

    s = _settings(deepgram_api_key="bad-key")
    stt = _make_stt(s)
    assert stt.__class__.__name__ == "WhisperSTTService"


# TTS factory --------------------------------------------------------------

def test_make_tts_returns_piper_when_no_elevenlabs_key():
    from tend.services import _make_tts
    s = _settings(elevenlabs_api_key=None)
    tts, rate = _make_tts(s)
    assert tts.__class__.__name__ == "PiperTTSService"
    assert rate == 22050


def test_make_tts_returns_elevenlabs_on_healthy_preflight(monkeypatch):
    from tend.services import _make_tts

    fake_response = MagicMock(
        status_code=200,
        json=lambda: {"character_count": 100, "character_limit": 10000},
    )
    monkeypatch.setattr(httpx, "get", MagicMock(return_value=fake_response))

    s = _settings(elevenlabs_api_key="el-test")
    tts, rate = _make_tts(s)
    assert tts.__class__.__name__ == "ElevenLabsTTSService"
    assert rate == 24000


def test_make_tts_falls_back_when_quota_exhausted(monkeypatch):
    from tend.services import _make_tts

    fake_response = MagicMock(
        status_code=200,
        json=lambda: {"character_count": 9999, "character_limit": 10000},
    )
    monkeypatch.setattr(httpx, "get", MagicMock(return_value=fake_response))

    s = _settings(elevenlabs_api_key="el-test")
    tts, rate = _make_tts(s)
    assert tts.__class__.__name__ == "PiperTTSService"

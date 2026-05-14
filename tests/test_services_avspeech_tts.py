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

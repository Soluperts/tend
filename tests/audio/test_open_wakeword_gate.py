# SPDX-License-Identifier: MIT
"""Tests for OpenWakeWordGate — gating + activation behaviour without real audio fixtures."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection


@pytest.fixture
def mock_oww_model():
    """A fake openwakeword Model that returns a controllable score."""
    m = MagicMock()
    m.predict = MagicMock(return_value={"hey_jarvis": 0.0})
    return m


@pytest.fixture
def mock_brain():
    b = MagicMock()
    b.active = False
    return b


@pytest.fixture
def mock_hub():
    h = MagicMock()
    h.activate_agent = AsyncMock()
    return h


def _audio_frame() -> InputAudioRawFrame:
    # 1280 int16 samples = 80ms at 16kHz
    return InputAudioRawFrame(
        audio=b"\x00\x00" * 1280, sample_rate=16000, num_channels=1
    )


async def _drive(gate, frame, captured):
    async def fake_push(f, direction=FrameDirection.DOWNSTREAM):
        captured.append(f)
    gate.push_frame = fake_push
    await gate.process_frame(frame, FrameDirection.DOWNSTREAM)


async def test_drops_audio_when_brain_inactive_and_no_match(mock_oww_model, mock_brain, mock_hub):
    from tend.audio.gates import OpenWakeWordGate
    gate = OpenWakeWordGate(
        model_name="hey_jarvis", threshold=0.5, hub=mock_hub, brain=mock_brain
    )
    gate._model = mock_oww_model

    captured = []
    await _drive(gate, _audio_frame(), captured)

    assert captured == []  # frame dropped
    mock_hub.activate_agent.assert_not_called()


async def test_activates_brain_and_emits_yes_on_wake(mock_oww_model, mock_brain, mock_hub):
    from tend.audio.gates import OpenWakeWordGate
    mock_oww_model.predict.return_value = {"hey_jarvis": 0.95}
    gate = OpenWakeWordGate(
        model_name="hey_jarvis", threshold=0.5, hub=mock_hub, brain=mock_brain
    )
    gate._model = mock_oww_model

    captured = []
    await _drive(gate, _audio_frame(), captured)

    mock_hub.activate_agent.assert_awaited_once_with("brain")
    yes_frames = [f for f in captured if isinstance(f, TTSSpeakFrame) and f.text == "Yes?"]
    assert len(yes_frames) == 1


async def test_forwards_audio_when_brain_active(mock_oww_model, mock_brain, mock_hub):
    from tend.audio.gates import OpenWakeWordGate
    mock_brain.active = True
    gate = OpenWakeWordGate(
        model_name="hey_jarvis", threshold=0.5, hub=mock_hub, brain=mock_brain
    )
    gate._model = mock_oww_model

    captured = []
    audio = _audio_frame()
    await _drive(gate, audio, captured)

    assert audio in captured
    # When brain is active, oww doesn't run (saves CPU)
    mock_oww_model.predict.assert_not_called()
    mock_hub.activate_agent.assert_not_called()


async def test_cooldown_after_deactivation_skips_oww_and_resets_model(mock_oww_model, mock_brain, mock_hub):
    """After Brain deactivates, gate must drop audio without running OWW for the cooldown window."""
    from tend.audio.gates import OpenWakeWordGate, _POST_SLEEP_COOLDOWN_S
    mock_brain.active = True  # start awake
    gate = OpenWakeWordGate(
        model_name="hey_jarvis", threshold=0.5, hub=mock_hub, brain=mock_brain
    )
    gate._model = mock_oww_model

    captured = []

    # Awake: forwards audio, OWW not called.
    await _drive(gate, _audio_frame(), captured)
    assert captured  # frame forwarded

    # User says sleep phrase; SleepPhraseGate flips Brain → inactive.
    mock_brain.active = False

    # Next audio frame after deactivation: gate notices the transition, resets
    # the oww model, and drops the frame without calling predict (cooldown).
    await _drive(gate, _audio_frame(), [])
    mock_oww_model.reset.assert_called_once()
    mock_oww_model.predict.assert_not_called()

    # Fast-forward past the cooldown window; predict is now allowed.
    gate._deactivated_at -= _POST_SLEEP_COOLDOWN_S + 0.1
    await _drive(gate, _audio_frame(), [])
    mock_oww_model.predict.assert_called_once()


async def test_buffers_subwindow_frames_until_chunk_complete(mock_oww_model, mock_brain, mock_hub):
    """Pipecat transport delivers frames smaller than openWakeWord's window — gate must buffer."""
    from tend.audio.gates import OpenWakeWordGate
    gate = OpenWakeWordGate(
        model_name="hey_jarvis", threshold=0.5, hub=mock_hub, brain=mock_brain
    )
    gate._model = mock_oww_model

    # 320-sample frames (= 20 ms @ 16 kHz). Need 4 to reach the 1280-sample window.
    small_frame = lambda: InputAudioRawFrame(
        audio=b"\x00\x00" * 320, sample_rate=16000, num_channels=1
    )
    captured = []

    for _ in range(3):
        await _drive(gate, small_frame(), captured)
    mock_oww_model.predict.assert_not_called()

    await _drive(gate, small_frame(), captured)
    mock_oww_model.predict.assert_called_once()
    chunk = mock_oww_model.predict.call_args.args[0]
    assert len(chunk) == 1280


async def test_non_audio_frames_pass_through(mock_oww_model, mock_brain, mock_hub):
    from tend.audio.gates import OpenWakeWordGate
    gate = OpenWakeWordGate(
        model_name="hey_jarvis", threshold=0.5, hub=mock_hub, brain=mock_brain
    )
    gate._model = mock_oww_model

    captured = []
    transcription = TranscriptionFrame(text="anything", user_id="u", timestamp="")
    await _drive(gate, transcription, captured)

    assert transcription in captured

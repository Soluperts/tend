"""Tests for SleepPhraseGate — fuzzy sleep match + silence timeout."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import (
    TranscriptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection


@pytest.fixture
def mock_brain():
    b = MagicMock()
    b.active = True  # gate only acts while brain is active
    return b


@pytest.fixture
def mock_hub():
    h = MagicMock()
    h.deactivate_agent = AsyncMock()
    h.on_brain_deactivated = AsyncMock()
    return h


async def _drive(gate, frame, captured):
    async def fake_push(f, direction=FrameDirection.DOWNSTREAM):
        captured.append(f)
    gate.push_frame = fake_push
    await gate.process_frame(frame, FrameDirection.DOWNSTREAM)


async def test_sleep_phrase_match_deactivates_brain(mock_brain, mock_hub):
    from tend.audio.gates import SleepPhraseGate
    gate = SleepPhraseGate(
        sleep_phrase="goodbye jarvis", fuzz_ratio=0.85, timeout_s=30,
        hub=mock_hub, brain=mock_brain,
    )

    captured = []
    frame = TranscriptionFrame(text="goodbye jarvis", user_id="u", timestamp="")
    await _drive(gate, frame, captured)

    mock_hub.deactivate_agent.assert_awaited_once_with("brain")
    assert frame not in captured  # swallowed


async def test_sleep_fuzzy_match(mock_brain, mock_hub):
    """rapidfuzz catches near-misses like 'good buy jarvis'."""
    from tend.audio.gates import SleepPhraseGate
    gate = SleepPhraseGate(
        sleep_phrase="goodbye jarvis", fuzz_ratio=0.85, timeout_s=30,
        hub=mock_hub, brain=mock_brain,
    )

    captured = []
    frame = TranscriptionFrame(text="good buy jarvis", user_id="u", timestamp="")
    await _drive(gate, frame, captured)

    mock_hub.deactivate_agent.assert_awaited_once_with("brain")


async def test_non_sleep_transcript_passes_through(mock_brain, mock_hub):
    from tend.audio.gates import SleepPhraseGate
    gate = SleepPhraseGate(
        sleep_phrase="goodbye jarvis", fuzz_ratio=0.85, timeout_s=30,
        hub=mock_hub, brain=mock_brain,
    )

    captured = []
    frame = TranscriptionFrame(text="what's the time?", user_id="u", timestamp="")
    await _drive(gate, frame, captured)

    assert frame in captured
    mock_hub.deactivate_agent.assert_not_called()


async def test_does_not_act_when_brain_inactive(mock_brain, mock_hub):
    from tend.audio.gates import SleepPhraseGate
    mock_brain.active = False
    gate = SleepPhraseGate(
        sleep_phrase="goodbye jarvis", fuzz_ratio=0.85, timeout_s=30,
        hub=mock_hub, brain=mock_brain,
    )

    captured = []
    frame = TranscriptionFrame(text="goodbye jarvis", user_id="u", timestamp="")
    await _drive(gate, frame, captured)

    mock_hub.deactivate_agent.assert_not_called()


async def test_silence_timeout_triggers_deactivate(mock_brain, mock_hub):
    from tend.audio.gates import SleepPhraseGate
    gate = SleepPhraseGate(
        sleep_phrase="goodbye jarvis", fuzz_ratio=0.85, timeout_s=0.05,
        hub=mock_hub, brain=mock_brain,
    )

    captured = []
    # Simulate user activity: start speaking starts/resets the timer.
    await _drive(gate, UserStartedSpeakingFrame(), captured)
    # Wait for timeout to expire.
    await asyncio.sleep(0.15)

    mock_hub.deactivate_agent.assert_awaited_with("brain")


async def test_user_speaking_resets_silence_timer(mock_brain, mock_hub):
    from tend.audio.gates import SleepPhraseGate
    gate = SleepPhraseGate(
        sleep_phrase="goodbye jarvis", fuzz_ratio=0.85, timeout_s=0.1,
        hub=mock_hub, brain=mock_brain,
    )

    captured = []
    await _drive(gate, UserStartedSpeakingFrame(), captured)
    await asyncio.sleep(0.05)
    await _drive(gate, UserStartedSpeakingFrame(), captured)  # reset
    await asyncio.sleep(0.07)
    # Total elapsed ≈ 0.12s, but the second activity reset the timer at 0.05s,
    # so only ~0.07s have passed since the last reset — should NOT have fired yet.
    mock_hub.deactivate_agent.assert_not_called()

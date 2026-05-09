"""Integration: full wake / conversation / sleep cycle through gates + brain state.

Mocks the audio transport, STT, TTS, and the LLM service; asserts that the
gate-driven state transitions on Brain happen correctly when the right frames
are injected.
"""

from unittest.mock import MagicMock

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from tend.audio.gates import OpenWakeWordGate, SleepPhraseGate


def _audio_frame() -> InputAudioRawFrame:
    return InputAudioRawFrame(
        audio=b"\x00\x00" * 1280, sample_rate=16000, num_channels=1
    )


@pytest.fixture
def mock_oww():
    m = MagicMock()
    m.predict = MagicMock(return_value={"hey_jarvis": 0.0})
    return m


class FakeBrain:
    """A minimal stand-in for Brain — exposes `.active` and tracks transitions."""
    def __init__(self):
        self.active = False
        self.activations = 0
        self.deactivations = 0


class FakeHub:
    """A stand-in for Hub that just flips the brain's active flag."""
    def __init__(self, brain: FakeBrain):
        self._brain = brain

    async def activate_agent(self, name: str):
        assert name == "brain"
        self._brain.active = True
        self._brain.activations += 1

    async def deactivate_agent(self, name: str):
        assert name == "brain"
        self._brain.active = False
        self._brain.deactivations += 1

    async def on_brain_deactivated(self):
        pass


async def _push_through(processor, frame):
    captured = []
    async def fake_push(f, direction=FrameDirection.DOWNSTREAM):
        captured.append(f)
    processor.push_frame = fake_push
    await processor.process_frame(frame, FrameDirection.DOWNSTREAM)
    return captured


async def test_wake_conversation_sleep_full_cycle(mock_oww):
    brain = FakeBrain()
    hub = FakeHub(brain)
    wake = OpenWakeWordGate(
        model_name="hey_jarvis", threshold=0.5, hub=hub, brain=brain
    )
    wake._model = mock_oww
    sleep = SleepPhraseGate(
        sleep_phrase="goodbye jarvis", fuzz_ratio=0.85, timeout_s=10,
        hub=hub, brain=brain,
    )

    # 1. Asleep: audio dropped before STT.
    pushed = await _push_through(wake, _audio_frame())
    assert pushed == []
    assert brain.active is False

    # 2. Wake: oww fires.
    mock_oww.predict.return_value = {"hey_jarvis": 0.95}
    pushed = await _push_through(wake, _audio_frame())
    assert brain.active is True
    assert brain.activations == 1

    # 3. Awake: audio passes through wake gate.
    mock_oww.predict.return_value = {"hey_jarvis": 0.0}
    pushed = await _push_through(wake, _audio_frame())
    assert len(pushed) == 1  # forwarded

    # 4. SleepPhraseGate forwards normal transcripts.
    pushed = await _push_through(sleep, TranscriptionFrame(
        text="what's the time?", user_id="u", timestamp=""
    ))
    assert len(pushed) == 1

    # 5. Sleep phrase deactivates brain and is swallowed.
    pushed = await _push_through(sleep, TranscriptionFrame(
        text="goodbye jarvis", user_id="u", timestamp=""
    ))
    assert pushed == []
    assert brain.active is False
    assert brain.deactivations == 1

    # 6. Back asleep: audio dropped again.
    pushed = await _push_through(wake, _audio_frame())
    assert pushed == []

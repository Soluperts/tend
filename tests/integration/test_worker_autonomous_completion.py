"""Integration: worker completes while Brain is deactivated.

Asserts that:
1. ReminderWorker publishes a TTSSpeakFrame to the bus (Hub's BusBridge picks
   it up via on_bus_message and forwards to TTS).
2. Brain.on_task_update is invoked even when Brain.active is False, and Brain
   publishes an LLMMessagesAppendFrame so Hub's context picks up the
   announcement on the next conversation turn.

This is the spec §11 regression — protects us if subagents ever changes the
activation gating semantics.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat_subagents.bus.messages import (
    BusFrameMessage,
    BusTaskRequestMessage,
    BusTaskUpdateMessage,
)


@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.send = AsyncMock()
    return bus


def _request(seconds: float, what: str) -> BusTaskRequestMessage:
    msg = MagicMock(spec=BusTaskRequestMessage)
    msg.task_id = "task-abc"
    msg.payload = {"seconds": seconds, "what": what}
    return msg


async def test_worker_completes_and_brain_publishes_context_append_while_inactive(mock_bus):
    """Even with Brain.active=False, the worker speaks and Brain queues a context append."""
    from tend.brain import Brain
    from tend.workers.reminder import ReminderWorker

    brain = Brain("brain", bus=mock_bus, llm_service=None)
    assert brain.active is False  # asleep-mode

    worker = ReminderWorker("reminder", bus=mock_bus)
    captured_updates = []

    async def capture_update(task_id, update):
        captured_updates.append((task_id, update))

    worker.send_task_update = capture_update
    worker.send_task_response = AsyncMock()

    await worker.handle_reminder(_request(seconds=0.001, what="drink water"))

    # 1. TTSSpeakFrame was published on the bus for Hub's BusBridge.
    assert any(
        isinstance(c.args[0], BusFrameMessage)
        and isinstance(c.args[0].frame, TTSSpeakFrame)
        for c in mock_bus.publish.await_args_list
    )

    # 2. Worker sent task_update; feed it into Brain.on_task_update directly,
    #    simulating what the framework's bus dispatch would do.
    assert len(captured_updates) == 1
    task_id, update = captured_updates[0]
    fake_update_msg = MagicMock(spec=BusTaskUpdateMessage)
    fake_update_msg.update = update
    fake_update_msg.task_id = task_id
    await brain.on_task_update(fake_update_msg)

    # 3. Brain published an LLMMessagesAppendFrame to the bus so Hub's context
    #    captures the announcement.
    appended = [
        c.args[0].frame
        for c in mock_bus.publish.await_args_list
        if isinstance(c.args[0], BusFrameMessage)
        and isinstance(c.args[0].frame, LLMMessagesAppendFrame)
    ]
    assert appended, "Brain should publish an LLMMessagesAppendFrame for the announcement"
    flat = [m for f in appended for m in f.messages]
    assert any(
        m["role"] == "system"
        and "drink water" in m["content"]
        for m in flat
    )

    assert brain.active is False  # still asleep

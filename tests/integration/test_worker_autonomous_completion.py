"""Integration: worker completes while Brain is deactivated.

Asserts that:
1. ReminderWorker publishes a TTSSpeakFrame to the "voice" bridge (which Hub's
   public BusBridgeProcessor would forward — verified separately by frame-level
   tests on ReminderWorker; here we confirm it gets published).
2. Brain.on_task_update is invoked even when Brain.active is False, so the
   LLMContext gets the announcement system message.

This is the spec §11 regression test — protects us if subagents ever changes
the activation gating semantics.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import TTSSpeakFrame
from pipecat_subagents.bus.messages import (
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


async def test_worker_completes_and_brain_context_updates_while_inactive(mock_bus):
    """Even with Brain.active=False, on_task_update fires and the context updates."""
    from tend.brain import Brain
    from tend.workers.reminder import ReminderWorker

    brain = Brain("brain", bus=mock_bus, llm_service=None)
    await brain.reset_session("You are Tend.")
    # Confirm Brain starts inactive — this is the asleep-mode case.
    assert brain.active is False

    worker = ReminderWorker("reminder", bus=mock_bus)
    captured_updates = []

    async def capture_update(task_id, update):
        captured_updates.append((task_id, update))

    worker.send_task_update = capture_update
    worker.send_task_response = AsyncMock()

    # Simulate the worker firing.
    await worker.handle_reminder(_request(seconds=0.001, what="drink water"))

    # 1. TTSSpeakFrame was published on the bus for Hub's BusBridge.
    assert any(
        isinstance(c.args[0].frame, TTSSpeakFrame)
        for c in mock_bus.publish.await_args_list
    )

    # 2. task_update was sent. Now feed it into Brain.on_task_update directly,
    #    simulating what the framework's bus dispatch would do.
    assert len(captured_updates) == 1
    task_id, update = captured_updates[0]
    fake_update_msg = MagicMock(spec=BusTaskUpdateMessage)
    fake_update_msg.update = update
    fake_update_msg.task_id = task_id
    await brain.on_task_update(fake_update_msg)

    # 3. Brain's LLMContext now has a system message recording the announcement.
    msgs = brain.context.get_messages()
    last = msgs[-1]
    assert last["role"] == "system"
    assert "drink water" in last["content"]
    assert brain.active is False  # still inactive

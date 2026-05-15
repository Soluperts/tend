# SPDX-License-Identifier: MIT
"""Tests for Brain — on_task_update publishes context updates to the bus."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat_subagents.bus.messages import BusFrameMessage, BusTaskUpdateMessage


@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def brain(mock_bus):
    from tend.brain import Brain
    return Brain("brain", bus=mock_bus, llm_service=None)


def _published_messages(mock_bus) -> list[dict]:
    out = []
    for call in mock_bus.publish.await_args_list:
        msg = call.args[0]
        if isinstance(msg, BusFrameMessage) and isinstance(msg.frame, LLMMessagesAppendFrame):
            out.extend(msg.frame.messages)
    return out


async def test_on_task_update_announcement_publishes_context_append(brain, mock_bus):
    msg = MagicMock(spec=BusTaskUpdateMessage)
    msg.update = {
        "kind": "announcement",
        "spoken": "Reminder: drink water",
        "context": {"what": "drink water"},
    }
    await brain.on_task_update(msg)

    msgs = _published_messages(mock_bus)
    assert any(
        m["role"] == "system"
        and "drink water" in m["content"]
        and "announced to the user" in m["content"].lower()
        for m in msgs
    )


async def test_on_task_update_error_publishes_context_append(brain, mock_bus):
    msg = MagicMock(spec=BusTaskUpdateMessage)
    msg.update = {
        "kind": "error",
        "spoken": "Sorry, I couldn't complete the reminder.",
        "context": {"what": "drink water", "error": "boom"},
    }
    await brain.on_task_update(msg)

    msgs = _published_messages(mock_bus)
    assert any(
        m["role"] == "system"
        and ("could not complete" in m["content"].lower() or "failed" in m["content"].lower())
        for m in msgs
    )


async def test_on_task_update_unknown_kind_publishes_nothing(brain, mock_bus):
    msg = MagicMock(spec=BusTaskUpdateMessage)
    msg.update = {"kind": "unknown", "data": "noise"}
    await brain.on_task_update(msg)
    assert _published_messages(mock_bus) == []


async def test_remind_in_uses_scheduler():
    """remind_in should add a one-shot at-job via scheduler.add_job."""
    from tend.brain import Brain
    from unittest.mock import AsyncMock, MagicMock
    bus = MagicMock()
    scheduler = MagicMock()
    job = MagicMock()
    job.id = "x"
    job.name = "reminder"
    scheduler.add_job = MagicMock(return_value=job)
    brain = Brain(
        "brain", bus=bus, llm_service=MagicMock(),
        store=MagicMock(), scheduler=scheduler,
    )
    p = MagicMock()
    p.result_callback = AsyncMock()
    await brain.remind_in(p, seconds=120, what="oven")
    scheduler.add_job.assert_called_once()
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["when"].lower().startswith("in ")
    assert "120s" in kwargs["when"]
    assert kwargs["request"] == "oven"

"""Tests for Brain — reset_session, on_task_update context update, @tool dispatch."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat_subagents.bus.messages import BusTaskUpdateMessage


@pytest.fixture
def mock_bus():
    return MagicMock()


@pytest.fixture
def brain(mock_bus):
    from tend.brain import Brain

    # Build with no LLMService — we test the agent's own logic.
    b = Brain("brain", bus=mock_bus, llm_service=None)
    return b


async def test_reset_session_clears_messages_and_sets_system_prompt(brain):
    brain.context.add_message({"role": "user", "content": "hello"})
    brain.context.add_message({"role": "assistant", "content": "hi"})

    await brain.reset_session("You are Tend, a helpful assistant.")

    msgs = brain.context.get_messages()
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"
    assert "Tend" in msgs[0]["content"]


async def test_on_task_update_announcement_appends_system_message(brain):
    msg = MagicMock(spec=BusTaskUpdateMessage)
    msg.update = {
        "kind": "announcement",
        "spoken": "Reminder: drink water",
        "context": {"what": "drink water"},
    }
    await brain.on_task_update(msg)

    msgs = brain.context.get_messages()
    last = msgs[-1]
    assert last["role"] == "system"
    assert "drink water" in last["content"]
    assert "announced to the user" in last["content"].lower()


async def test_on_task_update_error_appends_system_message(brain):
    msg = MagicMock(spec=BusTaskUpdateMessage)
    msg.update = {
        "kind": "error",
        "spoken": "Sorry, I couldn't complete the reminder.",
        "context": {"what": "drink water", "error": "boom"},
    }
    await brain.on_task_update(msg)

    msgs = brain.context.get_messages()
    last = msgs[-1]
    assert last["role"] == "system"
    assert "could not complete" in last["content"].lower() or "failed" in last["content"].lower()


async def test_on_task_update_unknown_kind_is_ignored(brain):
    """Unknown kinds shouldn't pollute the context."""
    initial_len = len(brain.context.get_messages())
    msg = MagicMock(spec=BusTaskUpdateMessage)
    msg.update = {"kind": "unknown", "data": "noise"}
    await brain.on_task_update(msg)
    assert len(brain.context.get_messages()) == initial_len

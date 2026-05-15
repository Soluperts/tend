# SPDX-License-Identifier: MIT
"""Tests for ProactiveAnnouncer — cooldown, deferral, urgency, drain."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat_subagents.bus.messages import BusFrameMessage

from tend.announcer import ProactiveAnnouncer


@pytest.fixture
def bus():
    b = MagicMock()
    b.publish = AsyncMock()
    return b


@pytest.fixture
def is_active():
    state = {"value": False}
    def f():
        return state["value"]
    f.set = lambda v: state.update(value=v)
    return f


def _texts_published(bus) -> list[str]:
    out = []
    for call in bus.publish.call_args_list:
        msg = call.args[0]
        if isinstance(msg.frame, TTSSpeakFrame):
            out.append(msg.frame.text)
    return out


async def test_announce_when_idle_publishes_tts_and_context(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    delivered = await a.announce(
        "Hello.", source="test", category="general", urgent=False,
    )
    assert delivered is True
    # Two publishes: TTSSpeakFrame and LLMMessagesAppendFrame
    assert bus.publish.call_count == 2
    frames = [c.args[0].frame for c in bus.publish.call_args_list]
    assert isinstance(frames[0], TTSSpeakFrame)
    assert frames[0].text == "Hello."
    assert isinstance(frames[1], LLMMessagesAppendFrame)
    msg = frames[1].messages[0]
    assert msg["role"] == "system"
    assert "test/general" in msg["content"]
    assert "Hello." in msg["content"]


async def test_cooldown_drops_repeat_in_same_category(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={"posture": 600},
    )
    await a.announce("Sit up.", source="vision", category="posture")
    bus.publish.reset_mock()
    delivered = await a.announce(
        "Sit up.", source="vision", category="posture",
    )
    assert delivered is False
    assert bus.publish.call_count == 0


async def test_different_categories_independent(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    await a.announce("a", source="x", category="alpha")
    await a.announce("b", source="x", category="beta")
    assert _texts_published(bus) == ["a", "b"]


async def test_urgent_bypasses_cooldown(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    await a.announce("first", source="x", category="alarm")
    bus.publish.reset_mock()
    await a.announce(
        "second", source="x", category="alarm", urgent=True,
    )
    assert "second" in _texts_published(bus)


async def test_defer_when_brain_active(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    is_active.set(True)
    delivered = await a.announce(
        "wait", source="x", category="alpha", urgent=False,
    )
    assert delivered is True  # queued counts as delivered
    assert bus.publish.call_count == 0


async def test_drain_publishes_pending(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    is_active.set(True)
    await a.announce("first", source="x", category="alpha")
    await a.announce("second", source="x", category="beta")
    bus.publish.reset_mock()

    is_active.set(False)
    await a.drain_pending()
    texts = _texts_published(bus)
    assert "first" in texts
    assert "second" in texts


async def test_urgent_speaks_immediately_even_if_brain_active(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    is_active.set(True)
    await a.announce(
        "URGENT", source="alarm", category="fire", urgent=True,
    )
    assert "URGENT" in _texts_published(bus)


async def test_drain_respects_cooldown(bus, is_active):
    """If a queued announcement's category went on cooldown via urgent
    delivery while pending, drain still respects that."""
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    is_active.set(True)
    # Queue one normal announcement
    await a.announce("queued", source="x", category="alpha")
    # Urgent of same category fires immediately (sets cooldown)
    await a.announce("urgent", source="x", category="alpha", urgent=True)
    bus.publish.reset_mock()

    is_active.set(False)
    await a.drain_pending()
    # The queued one should be dropped because cooldown is now active.
    assert "queued" not in _texts_published(bus)

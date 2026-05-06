"""Tests for SessionManager — soul loading, reset propagation, daily reset scheduling."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tend.session import SessionManager


@pytest.fixture
def soul_file(tmp_path: Path) -> Path:
    p = tmp_path / "soul.md"
    p.write_text("You are Tend, a helpful workroom assistant.")
    return p


@pytest.fixture
def mock_brain():
    brain = MagicMock()
    brain.reset_session = AsyncMock()
    brain.active = False
    return brain


async def test_start_loads_soul_and_calls_reset(mock_brain, soul_file):
    sm = SessionManager(brain=mock_brain, soul_path=soul_file,
                        reset_time="04:00", timezone="UTC")
    sm._schedule_daily_reset = AsyncMock()  # don't actually schedule
    await sm.start()

    mock_brain.reset_session.assert_awaited_once()
    soul_arg = mock_brain.reset_session.await_args.args[0]
    assert "Tend" in soul_arg
    assert "workroom" in soul_arg


async def test_start_uses_default_when_soul_missing(mock_brain, tmp_path):
    missing = tmp_path / "no-such-file.md"
    sm = SessionManager(brain=mock_brain, soul_path=missing,
                        reset_time="04:00", timezone="UTC")
    sm._schedule_daily_reset = AsyncMock()
    await sm.start()

    mock_brain.reset_session.assert_awaited_once()
    soul_arg = mock_brain.reset_session.await_args.args[0]
    assert "voice assistant" in soul_arg.lower()  # the built-in default


async def test_reset_now_swaps_context_when_brain_inactive(mock_brain, soul_file):
    sm = SessionManager(brain=mock_brain, soul_path=soul_file,
                        reset_time="04:00", timezone="UTC")
    sm._schedule_daily_reset = AsyncMock()
    await sm.start()
    mock_brain.reset_session.reset_mock()

    mock_brain.active = False
    await sm.reset_now()

    mock_brain.reset_session.assert_awaited_once()


async def test_reset_now_defers_when_brain_active(mock_brain, soul_file):
    sm = SessionManager(brain=mock_brain, soul_path=soul_file,
                        reset_time="04:00", timezone="UTC")
    sm._schedule_daily_reset = AsyncMock()
    await sm.start()
    mock_brain.reset_session.reset_mock()

    mock_brain.active = True
    await sm.reset_now()
    mock_brain.reset_session.assert_not_awaited()
    assert sm._pending_reset is True

    # Simulate the brain becoming inactive
    mock_brain.active = False
    await sm.on_brain_deactivated()
    mock_brain.reset_session.assert_awaited_once()
    assert sm._pending_reset is False

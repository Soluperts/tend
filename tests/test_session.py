"""Tests for SessionManager — soul loading, reset propagation, daily reset scheduling."""

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
    brain.active = False
    return brain


@pytest.fixture
def mock_hub():
    hub = MagicMock()
    hub.reset_session = AsyncMock()
    return hub


def _make(mock_brain, mock_hub, soul_path):
    sm = SessionManager(brain=mock_brain, hub=mock_hub, soul_path=soul_path,
                        reset_time="04:00", timezone="UTC")
    sm._schedule_daily_reset = AsyncMock()  # don't actually schedule
    return sm


async def test_start_loads_soul_and_resets_hub(mock_brain, mock_hub, soul_file):
    sm = _make(mock_brain, mock_hub, soul_file)
    await sm.start()

    mock_hub.reset_session.assert_awaited_once()
    soul_arg = mock_hub.reset_session.await_args.args[0]
    assert "Tend" in soul_arg
    assert "workroom" in soul_arg


async def test_start_uses_default_when_soul_missing(mock_brain, mock_hub, tmp_path):
    missing = tmp_path / "no-such-file.md"
    sm = _make(mock_brain, mock_hub, missing)
    await sm.start()

    mock_hub.reset_session.assert_awaited_once()
    soul_arg = mock_hub.reset_session.await_args.args[0]
    assert "voice assistant" in soul_arg.lower()  # the built-in default


async def test_reset_now_resets_when_brain_inactive(mock_brain, mock_hub, soul_file):
    sm = _make(mock_brain, mock_hub, soul_file)
    await sm.start()
    mock_hub.reset_session.reset_mock()

    mock_brain.active = False
    await sm.reset_now()

    mock_hub.reset_session.assert_awaited_once()


async def test_reset_now_defers_when_brain_active(mock_brain, mock_hub, soul_file):
    sm = _make(mock_brain, mock_hub, soul_file)
    await sm.start()
    mock_hub.reset_session.reset_mock()

    mock_brain.active = True
    await sm.reset_now()
    mock_hub.reset_session.assert_not_awaited()
    assert sm._pending_reset is True

    mock_brain.active = False
    await sm.on_brain_deactivated()
    mock_hub.reset_session.assert_awaited_once()
    assert sm._pending_reset is False

"""Tests for SessionManager — soul loading, reset propagation, daily reset scheduling."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tend.session import SessionManager


@pytest.fixture
def isolated_workspace(monkeypatch, tmp_path):
    """Redirect $TEND_HOME and prevent fallback to the real shipped soul.md."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend import paths
    # Point shipped_soul_md at a non-existent file so the "no user soul.md"
    # test exercises the hardcoded fallback rather than the wheel default.
    monkeypatch.setattr(paths, "shipped_soul_md", lambda: tmp_path / "no-shipped.md")
    return tmp_path


@pytest.fixture
def soul_file(isolated_workspace) -> Path:
    p = isolated_workspace / "soul.md"
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


def _make(mock_brain, mock_hub):
    sm = SessionManager(
        brain=mock_brain, hub=mock_hub,
        reset_time="04:00", timezone="UTC",
    )
    sm._schedule_daily_reset = AsyncMock()  # don't actually schedule
    return sm


async def test_start_loads_soul_and_resets_hub(mock_brain, mock_hub, soul_file):
    sm = _make(mock_brain, mock_hub)
    await sm.start()

    mock_hub.reset_session.assert_awaited_once()
    soul_arg = mock_hub.reset_session.await_args.args[0]
    assert "Tend" in soul_arg
    assert "workroom" in soul_arg


async def test_start_uses_default_when_soul_missing(mock_brain, mock_hub, isolated_workspace):
    # No user soul.md was created in isolated_workspace, and shipped_soul_md
    # is monkeypatched to a non-existent path → hardcoded fallback wins.
    sm = _make(mock_brain, mock_hub)
    await sm.start()

    mock_hub.reset_session.assert_awaited_once()
    soul_arg = mock_hub.reset_session.await_args.args[0]
    assert "voice assistant" in soul_arg.lower()


async def test_reset_now_resets_when_brain_inactive(mock_brain, mock_hub, soul_file):
    sm = _make(mock_brain, mock_hub)
    await sm.start()
    mock_hub.reset_session.reset_mock()

    mock_brain.active = False
    await sm.reset_now()

    mock_hub.reset_session.assert_awaited_once()


async def test_reset_now_defers_when_brain_active(mock_brain, mock_hub, soul_file):
    sm = _make(mock_brain, mock_hub)
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

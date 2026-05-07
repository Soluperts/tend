"""Brain's session-aware tools that read the SessionStore + dispatch coding tasks."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tend.sessions import SessionStore


@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path)


@pytest.fixture
def brain(mock_bus, store):
    from tend.brain import Brain
    return Brain("brain", bus=mock_bus, llm_service=None, store=store)


async def test_list_recent_jobs_empty(brain):
    out = await brain.list_recent_jobs(MagicMock(), limit=5)
    assert "no jobs" in out.lower() or "empty" in out.lower()


async def test_list_recent_jobs_returns_running_then_done(brain, store):
    store.start(session_id="a", worker="coding", request="fix bug", cwd=None)
    store.start(session_id="b", worker="coding", request="rename foo", cwd=None)
    store.complete("b", status="done", spoken_summary="renamed")
    out = await brain.list_recent_jobs(MagicMock(), limit=5)
    assert "running" in out
    assert "done" in out
    assert "fix bug" in out
    assert "rename foo" in out


async def test_session_status_returns_summary(brain, store):
    store.start(session_id="a", worker="coding", request="x", cwd=None)
    store.complete("a", status="done", spoken_summary="finished it")
    out = await brain.session_status(MagicMock(), session_id="a")
    assert "done" in out
    assert "finished it" in out


async def test_session_status_unknown(brain):
    out = await brain.session_status(MagicMock(), session_id="nope")
    assert "no session" in out.lower() or "unknown" in out.lower()


async def test_code_in_dispatches_task(brain):
    """code_in calls request_task on the brain (fire-and-forget)."""
    brain.request_task = AsyncMock()
    brain._ensure_coding_worker = AsyncMock()
    out = await brain.code_in(MagicMock(), repo="/home/pi/hasat", request="fix it")
    brain.request_task.assert_awaited_once()
    args, kwargs = brain.request_task.await_args
    assert args[0] == "coding"
    assert kwargs["payload"]["request"] == "fix it"
    assert "i'll" in out.lower() or "got it" in out.lower()


async def test_continue_session_dispatches_with_resume_id(brain, store):
    store.start(session_id="prev", worker="coding", request="x", cwd=None)
    brain.request_task = AsyncMock()
    brain._ensure_coding_worker = AsyncMock()
    out = await brain.continue_session(
        MagicMock(), session_id="prev", follow_up="and rename y to z"
    )
    brain.request_task.assert_awaited_once()
    payload = brain.request_task.await_args.kwargs["payload"]
    assert payload.get("resume_session_id") == "prev"
    assert "rename y to z" in payload["request"]
    assert "follow" in out.lower() or "got it" in out.lower()


async def test_continue_session_unknown_id(brain):
    out = await brain.continue_session(
        MagicMock(), session_id="nope", follow_up="x"
    )
    assert "no session" in out.lower() or "couldn't find" in out.lower()

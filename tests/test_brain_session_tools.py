"""Brain's session-aware tools that read the SessionStore + dispatch tasks."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tend.sessions import SessionStore


class _ParamsCapture:
    """A FunctionCallParams stand-in that captures result_callback values."""

    def __init__(self):
        self.value = None

    async def result_callback(self, value):
        self.value = value


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


@pytest.fixture
def brain_no_store(mock_bus):
    from tend.brain import Brain
    return Brain("brain", bus=mock_bus, llm_service=None)


async def test_list_recent_jobs_empty(brain):
    out = await brain.list_recent_jobs(MagicMock(), limit=5)
    assert "no jobs" in out.lower() or "empty" in out.lower()


async def test_list_recent_jobs_returns_running_then_done(brain, store):
    store.start(session_id="a", worker="general", request="fix bug", cwd=None)
    store.start(session_id="b", worker="general", request="rename foo", cwd=None)
    store.complete("b", status="done", spoken_summary="renamed")
    out = await brain.list_recent_jobs(MagicMock(), limit=5)
    assert "running" in out
    assert "done" in out
    assert "fix bug" in out
    assert "rename foo" in out


async def test_session_status_returns_summary(brain, store):
    store.start(session_id="a", worker="general", request="x", cwd=None)
    store.complete("a", status="done", spoken_summary="finished it")
    out = await brain.session_status(MagicMock(), session_id="a")
    assert "done" in out
    assert "finished it" in out


async def test_session_status_unknown(brain):
    out = await brain.session_status(MagicMock(), session_id="nope")
    assert "no session" in out.lower() or "unknown" in out.lower()


async def test_do_task_dispatches_task(brain):
    """do_task calls request_task on the brain (fire-and-forget) and surfaces
    its acknowledgement via result_callback (not a return value)."""
    brain.request_task = AsyncMock()
    brain._ensure_general_worker = AsyncMock()
    params = _ParamsCapture()
    out = await brain.do_task(params, request="fix it")
    # @tool methods using result_callback return None.
    assert out is None
    brain._ensure_general_worker.assert_awaited_once()
    brain.request_task.assert_awaited_once()
    args, kwargs = brain.request_task.await_args
    assert args[0] == "general"
    assert kwargs["payload"]["request"] == "fix it"
    # repo is no longer part of the payload — workspace is implicit.
    assert "repo" not in kwargs["payload"]
    assert params.value is not None
    assert "got it" in params.value.lower() or "working on" in params.value.lower()


async def test_continue_session_dispatches_with_resume_id(brain, store):
    store.start(session_id="prev", worker="general", request="x", cwd=None)
    brain.request_task = AsyncMock()
    brain._ensure_general_worker = AsyncMock()
    out = await brain.continue_session(
        MagicMock(), session_id="prev", follow_up="and rename y to z"
    )
    brain.request_task.assert_awaited_once()
    args, kwargs = brain.request_task.await_args
    assert args[0] == "general"
    payload = kwargs["payload"]
    assert payload.get("resume_session_id") == "prev"
    assert "rename y to z" in payload["request"]
    assert "repo" not in payload
    assert "follow" in out.lower() or "got it" in out.lower()


async def test_continue_session_unknown_id(brain):
    out = await brain.continue_session(
        MagicMock(), session_id="nope", follow_up="x"
    )
    assert "no session" in out.lower() or "couldn't find" in out.lower()


async def test_continue_session_rejects_non_general_worker(brain, store):
    """Only the general worker can be resumed in v1."""
    store.start(session_id="rem1", worker="reminder", request="x", cwd=None)
    brain.request_task = AsyncMock()
    out = await brain.continue_session(
        MagicMock(), session_id="rem1", follow_up="and again",
    )
    brain.request_task.assert_not_awaited()
    assert "reminder" in out.lower()
    assert "doesn't support" in out.lower() or "not support" in out.lower()


async def test_list_recent_jobs_no_store(brain_no_store):
    out = await brain_no_store.list_recent_jobs(MagicMock(), limit=5)
    assert "unavailable" in out.lower()


async def test_session_status_no_store(brain_no_store):
    out = await brain_no_store.session_status(MagicMock(), session_id="x")
    assert "unavailable" in out.lower()


async def test_continue_session_no_store(brain_no_store):
    out = await brain_no_store.continue_session(
        MagicMock(), session_id="x", follow_up="y",
    )
    assert "unavailable" in out.lower()


async def test_do_task_no_store_does_not_dispatch(brain_no_store):
    """Without a store, do_task must NOT fire request_task at the unregistered
    general worker — it should fail loud via result_callback instead."""
    brain_no_store.request_task = AsyncMock()
    params = _ParamsCapture()
    await brain_no_store.do_task(params, request="anything")
    brain_no_store.request_task.assert_not_awaited()
    assert params.value is not None
    assert "unavailable" in params.value.lower()


async def test_list_skills_empty_when_no_skills(tmp_path, monkeypatch, brain):
    """list_skills surfaces a friendly message when no skills exist."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    monkeypatch.setenv("TEND_SKILLS_ROOT", str(skills_root))
    params = _ParamsCapture()
    await brain.list_skills(params)
    assert params.value is not None
    assert "haven't" in params.value.lower() or "no" in params.value.lower()


async def test_list_skills_reads_filesystem(tmp_path, monkeypatch, brain):
    """list_skills reads SKILL.md files from TEND_SKILLS_ROOT and returns
    them via result_callback."""
    skills_root = tmp_path / "skills"
    (skills_root / "meal-plan").mkdir(parents=True)
    (skills_root / "meal-plan" / "SKILL.md").write_text(
        "---\nname: meal-plan\ndescription: Plan meals.\n---\nbody",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEND_SKILLS_ROOT", str(skills_root))
    params = _ParamsCapture()
    await brain.list_skills(params)
    assert params.value is not None
    assert "meal-plan" in params.value
    assert "Plan meals." in params.value

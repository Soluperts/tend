"""CodingWorker — runs claude inside the persistent deskclaw workspace.

It ensures the workspace dir exists, spawns claude with cwd=workspace,
publishes a TTSSpeakFrame and a task_update, and returns. There is no
per-job worktree isolation; everything DeskClaw builds accumulates in
that one directory.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import json
import pytest
from pipecat.frames.frames import TTSSpeakFrame
from pipecat_subagents.bus.messages import BusFrameMessage, BusTaskRequestMessage

from tend.config import WorkerConfig
from tend.sessions import SessionStore


class _FakeProc:
    def __init__(self, events: list[bytes], returncode=0, stderr_lines=()):
        async def gen():
            for ev in events:
                yield ev
        async def err_gen():
            for line in stderr_lines:
                yield line
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdin.close = MagicMock()
        self.stdout = gen()
        self.stderr = err_gen()
        self.wait = AsyncMock(return_value=returncode)
        self.kill = MagicMock()
        self.returncode = returncode


def _result(text="changes applied"):
    return [
        (json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }) + "\n").encode(),
        (json.dumps({
            "type": "result", "subtype": "success",
            "total_cost_usd": 0.10, "usage": {},
        }) + "\n").encode(),
    ]


@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.send = AsyncMock()
    return bus


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "store")


@pytest.fixture
def workspace(tmp_path):
    return tmp_path / "workspace"


def _request(request: str) -> BusTaskRequestMessage:
    msg = MagicMock(spec=BusTaskRequestMessage)
    msg.task_id = "t-1"
    msg.payload = {"request": request}
    return msg


async def test_coding_worker_runs_claude_in_workspace(
    store, mock_bus, workspace
):
    from tend.workers.coding import CodingWorker

    proc = _FakeProc(_result("Edited two files."))
    async def factory(*args, **kwargs):
        factory.args = args
        factory.kwargs = kwargs
        return proc

    cfg = WorkerConfig(
        model="claude-opus-4-7",
        setting_sources="user,project,local",
        allowed_tools=["Read", "Edit", "Write", "Bash"],
    )
    worker = CodingWorker(
        "coding", bus=mock_bus, store=store, config=cfg,
        subprocess_factory=factory, workspace_dir=workspace,
    )
    worker.send_task_update = AsyncMock()
    worker.send_task_response = AsyncMock()

    await worker.code_in(_request("rename foo to bar"))

    # 1. claude was spawned with cwd = the workspace dir
    assert factory.kwargs["cwd"] == str(workspace)

    # 2. workspace was created
    assert workspace.is_dir()

    # 3. TTSSpeakFrame published with the announce text
    publish = [c.args[0] for c in mock_bus.publish.await_args_list]
    speaks = [m.frame.text for m in publish
              if isinstance(m, BusFrameMessage) and isinstance(m.frame, TTSSpeakFrame)]
    assert any("Edited two files." in s for s in speaks)

    # 4. task_update has session_id + spoken_summary + workspace
    update = worker.send_task_update.await_args.args[1]
    assert update["kind"] == "announcement"
    assert "session_id" in update["context"]
    assert "workspace" in update["context"]
    assert update["spoken"]

    # 5. task_response delivered
    worker.send_task_response.assert_awaited_once()


def _resume_request(request: str, resume_id: str) -> BusTaskRequestMessage:
    msg = MagicMock(spec=BusTaskRequestMessage)
    msg.task_id = "t-resume"
    msg.payload = {"request": request, "resume_session_id": resume_id}
    return msg


async def test_coding_worker_resume_passes_resume_flag(
    store, mock_bus, workspace
):
    """Resume must pass --resume to claude and still cwd into the workspace."""
    from tend.workers.coding import CodingWorker

    # Seed a prior session so resume has something to refer to.
    store.start(
        session_id="prev123", worker="coding", request="initial",
        cwd=str(workspace),
    )
    store.complete("prev123", status="done", spoken_summary="initial done")

    proc = _FakeProc(_result("Follow-up applied."))
    async def factory(*args, **kwargs):
        factory.args = args
        factory.kwargs = kwargs
        return proc

    cfg = WorkerConfig(allowed_tools=["Read", "Edit"])
    worker = CodingWorker(
        "coding", bus=mock_bus, store=store, config=cfg,
        subprocess_factory=factory, workspace_dir=workspace,
    )
    worker.send_task_update = AsyncMock()
    worker.send_task_response = AsyncMock()

    await worker.code_in(_resume_request("follow up", "prev123"))

    # 1. --resume was passed to claude
    args = factory.args
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "prev123"
    # And --session-id is mutually excluded
    assert "--session-id" not in args

    # 2. Resume still cwds into the workspace.
    assert factory.kwargs["cwd"] == str(workspace)


async def test_coding_worker_announces_error_when_run_claude_fails(
    store, mock_bus, workspace
):
    """run_claude failure must trigger _announce_error, not _announce."""
    from tend.workers.coding import CodingWorker

    # Subprocess exits with non-zero → run_claude raises RuntimeError.
    proc = _FakeProc([], returncode=1)
    async def factory(*args, **kwargs):
        return proc

    cfg = WorkerConfig(allowed_tools=["Read"])
    worker = CodingWorker(
        "coding", bus=mock_bus, store=store, config=cfg,
        subprocess_factory=factory, workspace_dir=workspace,
    )
    worker.send_task_update = AsyncMock()
    worker.send_task_response = AsyncMock()

    await worker.code_in(_request("break things"))

    # Error TTS announced
    speaks = [
        c.args[0].frame.text for c in mock_bus.publish.await_args_list
        if isinstance(c.args[0], BusFrameMessage)
        and isinstance(c.args[0].frame, TTSSpeakFrame)
    ]
    assert any("didn't complete" in s.lower() for s in speaks)

    # task_update kind=error
    update = worker.send_task_update.await_args.args[1]
    assert update["kind"] == "error"
    assert "error" in update["context"]

    # task_response with error status
    worker.send_task_response.assert_awaited_once()
    response_kwargs = worker.send_task_response.await_args.kwargs
    response_args = worker.send_task_response.await_args.args
    # status is passed as a kwarg per ReminderWorker pattern
    assert "status" in response_kwargs or len(response_args) >= 3


async def test_coding_worker_default_workspace_resolves_under_home():
    """If no workspace_dir is configured, default to ~/.tend/workspace/."""
    from tend.workers.coding import CodingWorker, DEFAULT_WORKSPACE

    cfg = WorkerConfig()
    worker = CodingWorker(
        "coding", bus=MagicMock(), store=MagicMock(), config=cfg,
    )
    assert worker._workspace_dir == DEFAULT_WORKSPACE


async def test_coding_worker_workspace_dir_from_config(tmp_path):
    """An explicit `workspace_dir` in WorkerConfig wins over the default."""
    from tend.workers.coding import CodingWorker

    cfg = WorkerConfig(workspace_dir=str(tmp_path / "custom"))
    worker = CodingWorker(
        "coding", bus=MagicMock(), store=MagicMock(), config=cfg,
    )
    assert worker._workspace_dir == tmp_path / "custom"

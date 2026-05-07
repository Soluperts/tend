"""CodingWorker — the v1 ClaudeCliWorker subclass that proves the pattern.

It creates a worktree (via injectable factory), runs claude inside it,
publishes a TTSSpeakFrame and a task_update, and returns.
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
    def __init__(self, events: list[bytes], returncode=0):
        async def gen():
            for ev in events:
                yield ev
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdin.close = MagicMock()
        self.stdout = gen()
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
def fake_worktree(tmp_path):
    def factory(repo: Path, session_id: str) -> Path:
        wt = tmp_path / "worktrees" / session_id
        wt.mkdir(parents=True, exist_ok=True)
        return wt
    return factory


def _request(request: str, repo: str = "/home/pi/hasat") -> BusTaskRequestMessage:
    msg = MagicMock(spec=BusTaskRequestMessage)
    msg.task_id = "t-1"
    msg.payload = {"request": request, "repo": repo}
    return msg


async def test_coding_worker_runs_claude_and_announces(
    store, mock_bus, tmp_path, fake_worktree
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
        subprocess_factory=factory, worktree_factory=fake_worktree,
    )
    worker.send_task_update = AsyncMock()
    worker.send_task_response = AsyncMock()

    await worker.code_in(_request("rename foo to bar"))

    # 1. claude was spawned in the worktree dir
    assert "worktrees" in factory.kwargs["cwd"]

    # 2. TTSSpeakFrame published with the announce text
    publish = [c.args[0] for c in mock_bus.publish.await_args_list]
    speaks = [m.frame.text for m in publish
              if isinstance(m, BusFrameMessage) and isinstance(m.frame, TTSSpeakFrame)]
    assert any("Edited two files." in s for s in speaks)

    # 3. task_update has session_id + spoken_summary
    update = worker.send_task_update.await_args.args[1]
    assert update["kind"] == "announcement"
    assert "session_id" in update["context"]
    assert update["spoken"]

    # 4. task_response delivered
    worker.send_task_response.assert_awaited_once()

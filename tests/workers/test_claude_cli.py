"""Tests for ClaudeCliWorker building blocks. Subprocess is injected so
no actual `claude` is spawned in unit tests."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tend.sessions import SessionStore
from tend.workers.claude_cli import (
    CLAUDE_CLI_CLEAR_ENV,
    ClaudeRunSpec,
    _build_args,
    _scrubbed_env,
)


def test_build_args_minimal_first_run():
    spec = ClaudeRunSpec(prompt="hello")
    args = _build_args(spec, session_id="sid-1", system_prompt_path=None)
    assert args[0] == "claude"
    assert "-p" in args
    assert "--output-format" in args and "stream-json" in args
    assert "--include-partial-messages" in args
    assert "--verbose" in args
    assert "--setting-sources" in args
    sources_idx = args.index("--setting-sources")
    assert args[sources_idx + 1] == "user"
    assert "--session-id" in args
    sid_idx = args.index("--session-id")
    assert args[sid_idx + 1] == "sid-1"
    assert "--resume" not in args


def test_build_args_resume_omits_session_id():
    spec = ClaudeRunSpec(prompt="follow up", resume_session_id="sid-1")
    args = _build_args(spec, session_id="sid-1", system_prompt_path=None)
    assert "--resume" in args
    resume_idx = args.index("--resume")
    assert args[resume_idx + 1] == "sid-1"
    # Mutually exclusive: don't pass --session-id when resuming
    assert "--session-id" not in args


def test_build_args_includes_optional_flags():
    spec = ClaudeRunSpec(
        prompt="x",
        allowed_tools=["Read", "Edit", "mcp__sheets__*"],
        setting_sources="user,project,local",
        mcp_config_path=Path("/tmp/mcp.json"),
        model="claude-sonnet-4-6",
    )
    args = _build_args(spec, session_id="s", system_prompt_path=Path("/tmp/sys.txt"))
    assert "--allowedTools" in args
    tools_idx = args.index("--allowedTools")
    assert args[tools_idx + 1] == "Read,Edit,mcp__sheets__*"
    assert "--mcp-config" in args
    assert "--model" in args
    assert "--append-system-prompt-file" in args
    spf_idx = args.index("--append-system-prompt-file")
    assert args[spf_idx + 1] == "/tmp/sys.txt"
    sources_idx = args.index("--setting-sources")
    assert args[sources_idx + 1] == "user,project,local"


def test_build_args_no_system_prompt_when_resuming():
    """System prompt is first-run only — resumed sessions already have it."""
    spec = ClaudeRunSpec(prompt="next", resume_session_id="sid-1")
    args = _build_args(spec, session_id="sid-1", system_prompt_path=Path("/tmp/sys.txt"))
    assert "--append-system-prompt-file" not in args


def test_build_args_empty_allowed_tools_omits_flag():
    """Empty allowed_tools list must not emit a bare --allowedTools flag."""
    spec = ClaudeRunSpec(prompt="x", allowed_tools=[])
    args = _build_args(spec, session_id="s", system_prompt_path=None)
    assert "--allowedTools" not in args


def test_scrubbed_env_removes_dangerous_keys():
    env_in = {
        "PATH": "/usr/bin",
        "HOME": "/home/pi",
        "ANTHROPIC_API_KEY": "sk-leak",
        "CLAUDE_CONFIG_DIR": "/tmp/elsewhere",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://nope",
    }
    out = _scrubbed_env(env_in)
    # PATH is preserved (and the venv bin is prepended — see dedicated tests
    # below); we just check the original entry is still there.
    assert "PATH" in out and "/usr/bin" in out["PATH"].split(":")
    assert "HOME" in out
    for key in ("ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        assert key not in out
        assert key in CLAUDE_CLI_CLEAR_ENV


def test_scrubbed_env_prepends_venv_bin_to_path(monkeypatch):
    """venv's bin dir is on PATH so `tend scan-skill` resolves in claude's Bash."""
    import sys
    from pathlib import Path
    py_bin = str(Path(sys.executable).parent)
    env_in = {"PATH": "/usr/bin:/bin"}
    out = _scrubbed_env(env_in)
    parts = out["PATH"].split(":")
    assert parts[0] == py_bin
    assert "/usr/bin" in parts and "/bin" in parts


def test_scrubbed_env_does_not_duplicate_venv_bin(monkeypatch):
    import sys
    from pathlib import Path
    py_bin = str(Path(sys.executable).parent)
    env_in = {"PATH": f"{py_bin}:/usr/bin"}
    out = _scrubbed_env(env_in)
    parts = out["PATH"].split(":")
    assert parts.count(py_bin) == 1


def test_scrubbed_env_no_path_in_input_still_sets_one(monkeypatch):
    import sys
    from pathlib import Path
    py_bin = str(Path(sys.executable).parent)
    out = _scrubbed_env({})
    assert out["PATH"] == py_bin


def test_clear_env_includes_critical_keys():
    """Regression: never let these slip out of the scrub list."""
    must_clear = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
    assert must_clear.issubset(set(CLAUDE_CLI_CLEAR_ENV))


def _async_iter(lines: list[bytes]):
    async def gen():
        for line in lines:
            yield line
    return gen()


async def test_consume_stream_extracts_final_text(tmp_path):
    from tend.workers.claude_cli import _consume_stream

    transcript = tmp_path / "t.jsonl"
    events = [
        json.dumps({"type": "system", "subtype": "init"}).encode() + b"\n",
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello "}]},
        }).encode() + b"\n",
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "world."}]},
        }).encode() + b"\n",
        json.dumps({
            "type": "result", "subtype": "success",
            "total_cost_usd": 0.05, "usage": {"input_tokens": 10},
        }).encode() + b"\n",
    ]
    final, usage = await _consume_stream(_async_iter(events), transcript)
    assert final == "Hello world."
    assert usage.get("total_cost_usd") == 0.05
    # Transcript is on disk
    assert transcript.read_bytes().count(b"\n") == 4


async def test_consume_stream_skips_unparseable_lines(tmp_path):
    from tend.workers.claude_cli import _consume_stream

    events = [
        b"not-json\n",
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "ok"}]},
        }).encode() + b"\n",
    ]
    final, _ = await _consume_stream(_async_iter(events), tmp_path / "t.jsonl")
    assert final == "ok"


async def test_consume_stream_handles_no_assistant_text(tmp_path):
    from tend.workers.claude_cli import _consume_stream

    events = [json.dumps({"type": "result", "subtype": "success"}).encode() + b"\n"]
    final, usage = await _consume_stream(_async_iter(events), tmp_path / "t.jsonl")
    assert final == ""
    # usage always carries at least total_cost_usd, even when absent from event
    assert usage == {"total_cost_usd": None}


async def test_consume_stream_resets_on_user_turn(tmp_path):
    """Multi-turn streams (assistant narration → tool_use → tool_result →
    final assistant) must yield only the last assistant turn's text — not
    the concatenation of all narration steps."""
    from tend.workers.claude_cli import _consume_stream

    events = [
        # Turn 1: pre-tool narration we do NOT want spoken.
        (json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "Let me read the file..."},
                {"type": "tool_use", "name": "Read", "input": {}, "id": "x"},
            ]},
        }) + "\n").encode(),
        # Turn boundary: tool_result arrives in a user event.
        (json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "..."},
            ]},
        }) + "\n").encode(),
        # Turn 2: the actual final answer.
        (json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Done — renamed two files."}]},
        }) + "\n").encode(),
        (json.dumps({"type": "result", "subtype": "success"}) + "\n").encode(),
    ]
    final, _ = await _consume_stream(_async_iter(events), tmp_path / "t.jsonl")
    assert final == "Done — renamed two files."
    assert "Let me read" not in final


# ---------------------------------------------------------------------------
# ClaudeCliWorker.run_claude tests
# ---------------------------------------------------------------------------


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process."""

    def __init__(self, events: list[bytes], returncode: int = 0, stderr_lines=()):
        self._events = events
        self.returncode = returncode
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdin.close = MagicMock()
        self.stdout = self._async_iter(events)
        self.stderr = self._async_iter(stderr_lines)
        self.wait = AsyncMock(return_value=returncode)
        self.kill = MagicMock()

    @staticmethod
    async def _async_iter(items):
        for it in items:
            yield it


def _make_factory(proc):
    captured = {}

    async def factory(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc
    factory.captured = captured
    return factory


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path)


@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.send = AsyncMock()
    return bus


def _make_event(text: str) -> bytes:
    return (json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }) + "\n").encode()


def _result_event(cost: float = 0.01) -> bytes:
    return (json.dumps({
        "type": "result", "subtype": "success",
        "total_cost_usd": cost, "usage": {"input_tokens": 5},
    }) + "\n").encode()


async def test_run_claude_happy_path_records_done(store, mock_bus, monkeypatch):
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    proc = _FakeProc([_make_event("all done"), _result_event(0.02)], returncode=0)
    factory = _make_factory(proc)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-scrubbed")

    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)
    entry = await worker.run_claude(ClaudeRunSpec(prompt="hi"))

    assert entry.status == "done"
    assert entry.spoken_summary == "all done"
    assert entry.cost_usd == pytest.approx(0.02)

    # Env scrub: factory was called without ANTHROPIC_API_KEY
    env = factory.captured["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env


async def test_run_claude_failure_marks_failed(store, mock_bus):
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    proc = _FakeProc([], returncode=1)
    factory = _make_factory(proc)
    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)

    with pytest.raises(RuntimeError):
        await worker.run_claude(ClaudeRunSpec(prompt="hi"))

    rows = store.list_recent(limit=10)
    assert len(rows) == 1 and rows[0].status == "failed"


async def test_run_claude_creates_session_row(store, mock_bus):
    """Successful run leaves exactly one row in the store with a session_id."""
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    proc = _FakeProc([_make_event("ok"), _result_event()], returncode=0)
    factory = _make_factory(proc)
    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)

    await worker.run_claude(ClaudeRunSpec(prompt="hi"))
    rows = store.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].session_id


async def test_run_claude_spawn_failure_marks_failed(store, mock_bus):
    """If subprocess spawn raises, the store row goes from running → failed
    and the original exception propagates."""
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    async def bad_factory(*args, **kwargs):
        raise OSError("claude not found")

    worker = ClaudeCliWorker(
        "w", bus=mock_bus, store=store, subprocess_factory=bad_factory,
    )
    with pytest.raises(OSError, match="claude not found"):
        await worker.run_claude(ClaudeRunSpec(prompt="hi"))

    rows = store.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert "spawn failed" in (rows[0].error or "")


async def test_run_claude_cancelled_marks_killed_and_kills_proc(store, mock_bus):
    """asyncio.CancelledError must not leave a session 'running' or orphan
    the subprocess. The finally block converts the row to 'killed'."""
    import asyncio as aio
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    class _SlowProc(_FakeProc):
        def __init__(self):
            super().__init__([], returncode=None)

            async def slow_iter():
                await aio.sleep(60)
                if False:
                    yield b""

            async def slow_wait():
                await aio.sleep(60)

            self.stdout = slow_iter()
            self.wait = slow_wait
            self.returncode = None

    proc = _SlowProc()
    factory = _make_factory(proc)
    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)

    task = aio.create_task(worker.run_claude(ClaudeRunSpec(prompt="hi")))
    await aio.sleep(0.05)  # let it spawn + start consuming
    task.cancel()
    with pytest.raises(aio.CancelledError):
        await task

    rows = store.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "killed"
    proc.kill.assert_called()


async def test_run_claude_resume_uses_resume_arg(store, mock_bus):
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    # Seed the store with a completed prior session so resume has something
    # to touch.
    store.start(session_id="prev-1", worker="w", request="initial", cwd=None)
    store.complete(
        "prev-1", status="done",
        spoken_summary="initial done", usage={"total_cost_usd": 0.05},
    )

    proc = _FakeProc([_make_event("k"), _result_event(0.02)], returncode=0)
    factory = _make_factory(proc)
    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)

    final = await worker.run_claude(
        ClaudeRunSpec(prompt="more", resume_session_id="prev-1"),
    )
    args = factory.captured["args"]
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "prev-1"
    assert "--session-id" not in args
    # The resume completed, so the row is now "done" with the new summary
    # and the new cost. Prior cost from the first turn is overwritten by
    # the second turn's cost (claude reports cumulative-or-per-turn at
    # subprocess level — we accept whichever it gives us).
    assert final.status == "done"
    assert final.spoken_summary == "k"


async def test_run_claude_resume_unknown_session_raises(store, mock_bus):
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    proc = _FakeProc([], returncode=0)
    factory = _make_factory(proc)
    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)

    with pytest.raises(RuntimeError, match="Cannot resume"):
        await worker.run_claude(
            ClaudeRunSpec(prompt="x", resume_session_id="never-existed"),
        )


async def test_run_claude_writes_system_prompt_file(store, mock_bus):
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    proc = _FakeProc([_make_event("k"), _result_event()], returncode=0)
    factory = _make_factory(proc)
    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)

    await worker.run_claude(
        ClaudeRunSpec(prompt="x", system_prompt="You are a helper.")
    )
    args = factory.captured["args"]
    assert "--append-system-prompt-file" in args
    spf = args[args.index("--append-system-prompt-file") + 1]
    assert Path(spf).read_text() == "You are a helper."


async def test_run_claude_session_id_is_canonical_uuid(store, mock_bus):
    """claude --session-id rejects hex-only strings; must be 8-4-4-4-12 UUID."""
    import re
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    proc = _FakeProc([_make_event("k"), _result_event()], returncode=0)
    factory = _make_factory(proc)
    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)

    await worker.run_claude(ClaudeRunSpec(prompt="hi"))
    args = factory.captured["args"]
    assert "--session-id" in args
    sid = args[args.index("--session-id") + 1]
    # Canonical UUID, e.g. 550e8400-e29b-41d4-a716-446655440000
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", sid
    ), f"session id {sid!r} is not a canonical UUID"


async def test_run_claude_failure_surfaces_stderr(store, mock_bus):
    """When claude exits non-zero, its stderr message must appear in the
    failure record so the operator isn't left with an opaque exit code."""
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    proc = _FakeProc(
        [], returncode=1,
        stderr_lines=[b"Error: Invalid session ID. Must be a valid UUID.\n"],
    )
    factory = _make_factory(proc)
    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)

    with pytest.raises(RuntimeError, match="Invalid session ID"):
        await worker.run_claude(ClaudeRunSpec(prompt="hi"))

    rows = store.list_recent(limit=10)
    assert len(rows) == 1
    assert "Invalid session ID" in (rows[0].error or "")

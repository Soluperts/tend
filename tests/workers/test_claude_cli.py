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
    assert "PATH" in out and out["PATH"] == "/usr/bin"
    assert "HOME" in out
    for key in ("ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        assert key not in out
        assert key in CLAUDE_CLI_CLEAR_ENV


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


# ---------------------------------------------------------------------------
# ClaudeCliWorker.run_claude tests
# ---------------------------------------------------------------------------


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process."""

    def __init__(self, events: list[bytes], returncode: int = 0):
        self._events = events
        self.returncode = returncode
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdin.close = MagicMock()
        self.stdout = self._async_iter(events)
        self.stderr = self._async_iter([])
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


async def test_run_claude_writes_running_record_before_spawn(store, mock_bus):
    """If we crash mid-spawn, sessions.json must still show the running entry."""
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    proc = _FakeProc([_make_event("ok"), _result_event()], returncode=0)
    factory = _make_factory(proc)
    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)

    await worker.run_claude(ClaudeRunSpec(prompt="hi"))
    rows = store.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].session_id


async def test_run_claude_resume_uses_resume_arg(store, mock_bus):
    from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

    proc = _FakeProc([_make_event("k"), _result_event()], returncode=0)
    factory = _make_factory(proc)
    worker = ClaudeCliWorker("w", bus=mock_bus, store=store, subprocess_factory=factory)

    await worker.run_claude(ClaudeRunSpec(prompt="more", resume_session_id="prev-1"))
    args = factory.captured["args"]
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "prev-1"
    assert "--session-id" not in args


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

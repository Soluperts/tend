# Claude-CLI Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalise tend's worker tier into Claude-Code-subprocess-driven workers with on-disk session persistence, configurable per-worker `tend.toml` blocks, three new Brain tools for session inspection, and a small `tend` CLI for manual observability.

**Architecture:** Spec is at `docs/superpowers/specs/2026-05-06-claude-cli-workers-design.md`. In short: workers spawn `claude` as a subprocess (subscription-billed via the user's `~/.claude/.credentials.json` OAuth) with a scrubbed env, parse `--output-format stream-json` events line-by-line, archive the transcript, and update an on-disk session record so Brain (and the CLI) can answer "what's running?" across day-resets and process restarts.

**Tech Stack:** Python 3.11+, asyncio subprocess, pydantic-settings (existing), pipecat-subagents `BaseAgent` + `@task` decorator (existing), pytest + pytest-asyncio (existing). No new runtime dependencies.

---

## File Structure

| Path | What it owns |
|---|---|
| `src/tend/sessions.py` (new) | `SessionEntry` dataclass + `SessionStore` (atomic-rename JSON index + per-session JSONL transcripts + per-session system-prompt files) |
| `src/tend/workers/claude_cli.py` (new) | `ClaudeRunSpec` dataclass, `CLAUDE_CLI_CLEAR_ENV`, `_build_args`, `_scrubbed_env`, `_consume_stream`, `ClaudeCliWorker` base class |
| `src/tend/workers/coding.py` (new) | `CodingWorker(ClaudeCliWorker)` — the v1 validation worker; creates a worktree, runs claude, announces |
| `src/tend/cli.py` (new) | argparse CLI: `tend sessions list/show/tail/cat` + `tend snapshot` |
| `src/tend/config.py` (modify) | Add `WorkerConfig` model + `Settings.workers: dict[str, WorkerConfig]` |
| `src/tend/brain.py` (modify) | Inject `store: SessionStore`; add `code_in`, `list_recent_jobs`, `session_status`, `continue_session` `@tool`s |
| `src/tend/main.py` (modify) | Construct `SessionStore`; register `CodingWorker`; run boot preflight (`claude --version`, `claude auth status`) |
| `tend.toml` (modify) | Add `[workers.coding]` block |
| `pyproject.toml` (modify) | Change `[project.scripts] tend` from `tend.__main__:main` to `tend.cli:main` (systemd uses `python -m tend` so daemon entry is unaffected) |
| `tests/test_session_store.py` (new) | SessionStore unit tests |
| `tests/test_config_workers.py` (new) | WorkerConfig load test |
| `tests/workers/test_claude_cli.py` (new) | ClaudeCliWorker unit tests (args, env scrub, stream parse, run_claude happy + failure) |
| `tests/workers/test_coding.py` (new) | CodingWorker unit test |
| `tests/test_brain_session_tools.py` (new) | Brain session-tool unit tests |
| `tests/test_cli.py` (new) | CLI unit tests for `sessions list/show/tail/cat` and `snapshot` |
| `tests/test_preflight.py` (new) | Boot preflight unit test |

---

### Task 1: SessionStore — on-disk session index + transcripts

**Files:**
- Create: `src/tend/sessions.py`
- Test:   `tests/test_session_store.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_session_store.py
"""Tests for SessionStore — atomic JSON index + per-session transcripts."""

import json
import pytest

from tend.sessions import SessionEntry, SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path)


def test_start_writes_running_record_to_index(store):
    entry = store.start(
        session_id="abc123",
        worker="coding",
        request="fix the auth bug",
        cwd="/home/pi/hasat",
        task_id="t-1",
    )
    assert entry.session_id == "abc123"
    assert entry.status == "running"
    assert entry.started_at > 0
    assert entry.last_interaction_at == entry.started_at
    assert entry.ended_at is None
    assert entry.transcript_path.endswith("abc123.jsonl")

    # Round-trip from disk
    rows = store.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].session_id == "abc123"
    assert rows[0].status == "running"


def test_complete_updates_status_and_summary(store):
    store.start(session_id="x", worker="w", request="r", cwd=None)
    updated = store.complete(
        "x", status="done",
        spoken_summary="all done",
        usage={"total_cost_usd": 0.0123},
    )
    assert updated.status == "done"
    assert updated.spoken_summary == "all done"
    assert updated.cost_usd == pytest.approx(0.0123)
    assert updated.ended_at is not None


def test_complete_failure_records_error(store):
    store.start(session_id="x", worker="w", request="r", cwd=None)
    updated = store.complete("x", status="failed", error="boom" * 200)
    assert updated.status == "failed"
    # Index file holds the truncated error string
    raw = json.loads((store.root / "sessions.json").read_text())
    assert len(raw["x"]["error"]) <= 500


def test_list_recent_sorts_descending_by_started_at(store):
    store.start(session_id="old", worker="w", request="r", cwd=None)
    store.start(session_id="new", worker="w", request="r", cwd=None)
    rows = store.list_recent(limit=10)
    assert [r.session_id for r in rows] == ["new", "old"]


def test_list_recent_respects_limit(store):
    for i in range(5):
        store.start(session_id=f"s{i}", worker="w", request="r", cwd=None)
    rows = store.list_recent(limit=2)
    assert len(rows) == 2


def test_write_system_prompt_creates_file(store):
    p = store.write_system_prompt("abc", "You are helpful.")
    assert p.read_text() == "You are helpful."
    assert p.name == "abc.txt"


def test_atomic_write_recovers_from_corrupt_index(store, tmp_path):
    # Simulate truncation
    (tmp_path / "sessions.json").write_text("")
    # Should be treated as empty index, not crash
    rows = store.list_recent(limit=10)
    assert rows == []


def test_index_round_trip_via_disk(tmp_path):
    s1 = SessionStore(root=tmp_path)
    s1.start(session_id="x", worker="w", request="r", cwd=None)
    s1.complete("x", status="done", spoken_summary="ok")

    # Fresh instance reads same disk
    s2 = SessionStore(root=tmp_path)
    rows = s2.list_recent(limit=10)
    assert len(rows) == 1 and rows[0].status == "done" and rows[0].spoken_summary == "ok"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_session_store.py -v`
Expected: All tests FAIL with `ModuleNotFoundError: No module named 'tend.sessions'`.

- [ ] **Step 3: Implement `src/tend/sessions.py`**

```python
"""On-disk persistence for worker sessions.

Layout under `root` (defaults to ~/.tend/):

    sessions.json                # index keyed by session_id → entry dict
    sessions/<session_id>.jsonl  # per-session claude stream-json transcript
    system-prompts/<id>.txt      # per-session system prompt file (passed to claude --append-system-prompt-file)

All index writes go through `_write_index`, which serialises to a temp file
in the same directory and renames into place — atomic on POSIX so a crashing
process never leaves a half-written index.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass
class SessionEntry:
    session_id: str
    worker: str
    request: str
    status: Literal["running", "done", "failed", "killed"]
    started_at: int
    last_interaction_at: int
    transcript_path: str
    task_id: str | None = None
    cwd: str | None = None
    ended_at: int | None = None
    spoken_summary: str | None = None
    model: str | None = None
    cost_usd: float | None = None
    error: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class SessionStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else Path.home() / ".tend"
        (self.root / "sessions").mkdir(parents=True, exist_ok=True)
        (self.root / "system-prompts").mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "sessions.json"
        if not self._index_path.exists():
            self._write_index({})

    # --- public API ---

    def start(
        self,
        *,
        session_id: str,
        worker: str,
        request: str,
        cwd: str | None,
        task_id: str | None = None,
    ) -> SessionEntry:
        now = _now_ms()
        entry = SessionEntry(
            session_id=session_id,
            worker=worker,
            request=request,
            status="running",
            started_at=now,
            last_interaction_at=now,
            transcript_path=str(self.transcript_path(session_id)),
            task_id=task_id,
            cwd=cwd,
        )
        self._update(lambda idx: idx.__setitem__(session_id, asdict(entry)))
        return entry

    def complete(
        self,
        session_id: str,
        *,
        status: Literal["done", "failed", "killed"],
        spoken_summary: str | None = None,
        usage: dict | None = None,
        error: str | None = None,
    ) -> SessionEntry:
        def patch(idx: dict) -> None:
            row = idx.setdefault(session_id, {})
            row["status"] = status
            row["ended_at"] = _now_ms()
            row["last_interaction_at"] = _now_ms()
            if spoken_summary is not None:
                row["spoken_summary"] = spoken_summary
            if usage:
                row["cost_usd"] = usage.get("total_cost_usd")
            if error:
                row["error"] = error[:500]
        self._update(patch)
        return self._entry_from_dict(self._read_index()[session_id])

    def list_recent(self, *, limit: int = 10) -> list[SessionEntry]:
        idx = self._read_index()
        rows = sorted(idx.values(), key=lambda r: r.get("started_at", 0), reverse=True)
        return [self._entry_from_dict(r) for r in rows[:limit]]

    def get(self, session_id: str) -> SessionEntry | None:
        idx = self._read_index()
        row = idx.get(session_id)
        return self._entry_from_dict(row) if row else None

    def transcript_path(self, session_id: str) -> Path:
        return self.root / "sessions" / f"{session_id}.jsonl"

    def write_system_prompt(self, session_id: str, text: str) -> Path:
        p = self.root / "system-prompts" / f"{session_id}.txt"
        p.write_text(text)
        return p

    # --- internals ---

    def _update(self, mutator):
        idx = self._read_index()
        mutator(idx)
        self._write_index(idx)

    def _read_index(self) -> dict:
        try:
            text = self._index_path.read_text()
        except FileNotFoundError:
            return {}
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    def _write_index(self, idx: dict) -> None:
        # Atomic: temp file in same dir, fsync, then rename.
        fd, tmp_path = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(idx, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._index_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _entry_from_dict(row: dict) -> SessionEntry:
        # Tolerate unknown keys (forward compat) by stuffing them into extras.
        known = {f.name for f in SessionEntry.__dataclass_fields__.values()}
        extras = {k: v for k, v in row.items() if k not in known}
        kwargs = {k: v for k, v in row.items() if k in known and k != "extras"}
        kwargs.setdefault("extras", extras)
        return SessionEntry(**kwargs)


def _now_ms() -> int:
    return int(time.time() * 1000)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_session_store.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/sessions.py tests/test_session_store.py
git commit -m "sessions: add SessionStore with atomic JSON index + transcripts"
```

---

### Task 2: WorkerConfig in tend.config

**Files:**
- Modify: `src/tend/config.py`
- Test:   `tests/test_config_workers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_workers.py
"""WorkerConfig is loaded per-worker from tend.toml."""

from pathlib import Path

import pytest

from tend.config import Settings, WorkerConfig


def test_worker_config_defaults():
    cfg = WorkerConfig()
    assert cfg.model is None
    assert cfg.setting_sources == "user"
    assert cfg.allowed_tools == []
    assert cfg.mcp_config_path is None


def test_settings_loads_workers_block_from_toml(tmp_path, monkeypatch):
    toml = tmp_path / "tend.toml"
    toml.write_text(
        """
[workers.coding]
model = "claude-opus-4-7"
setting_sources = "user,project,local"
allowed_tools = ["Read", "Edit", "Bash"]

[workers.meal_plan]
model = "claude-sonnet-4-6"
allowed_tools = ["mcp__google_calendar__*"]
"""
    )
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert "coding" in s.workers
    assert s.workers["coding"].model == "claude-opus-4-7"
    assert s.workers["coding"].setting_sources == "user,project,local"
    assert s.workers["coding"].allowed_tools == ["Read", "Edit", "Bash"]
    assert s.workers["meal_plan"].setting_sources == "user"  # default kicks in
    assert s.workers["meal_plan"].allowed_tools == ["mcp__google_calendar__*"]


def test_settings_workers_default_empty():
    s = Settings()
    assert isinstance(s.workers, dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_workers.py -v`
Expected: FAIL — `ImportError: cannot import name 'WorkerConfig' from 'tend.config'`.

- [ ] **Step 3: Add `WorkerConfig` and the `workers` field to `src/tend/config.py`**

In `src/tend/config.py`, add a `WorkerConfig` BaseModel above the `Settings` class:

```python
from pydantic import BaseModel


class WorkerConfig(BaseModel):
    """Per-worker overrides loaded from `[workers.<name>]` blocks in tend.toml."""

    model: str | None = None
    setting_sources: str = "user"
    allowed_tools: list[str] = []
    mcp_config_path: str | None = None
```

Inside `Settings`, add the `workers` field next to the other config fields (e.g. after `log_path`):

```python
    # Per-worker config blocks. Keys are worker names; values are WorkerConfig.
    workers: dict[str, WorkerConfig] = {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_workers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/config.py tests/test_config_workers.py
git commit -m "config: add per-worker [workers.<name>] config blocks"
```

---

### Task 3: Claude-CLI args builder + env scrub

**Files:**
- Create: `src/tend/workers/claude_cli.py`
- Test:   `tests/workers/test_claude_cli.py`

- [ ] **Step 1: Write failing tests for `_build_args` and `_scrubbed_env`**

```python
# tests/workers/test_claude_cli.py
"""Tests for ClaudeCliWorker building blocks. Subprocess is injected so
no actual `claude` is spawned in unit tests."""

from pathlib import Path

import pytest

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/workers/test_claude_cli.py -v`
Expected: FAIL — module doesn't exist yet.

- [ ] **Step 3: Implement the args builder + env scrub**

Create `src/tend/workers/claude_cli.py` with the foundation pieces only (the `ClaudeCliWorker` class itself comes in Task 5):

```python
"""Claude-CLI worker base + helpers.

Workers in this module spawn the `claude` CLI as a subprocess so that API
calls bill against the user's Claude Pro/Max plan (subscription) rather
than per-token via the API. The trick: we never set ANTHROPIC_API_KEY
in the subprocess env — `claude` then falls through to its own OAuth
credentials at ~/.claude/.credentials.json.

See docs/superpowers/specs/2026-05-06-claude-cli-workers-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# Env vars to scrub before spawning `claude`. If any of these are set in
# the parent process, `claude` will silently route requests to a different
# provider, endpoint, or token source — including billing per-token instead
# of using the Pro/Max plan. Mirrors openclaw/extensions/anthropic/cli-shared.ts.
CLAUDE_CLI_CLEAR_ENV: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY_OLD",
    "ANTHROPIC_API_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_OAUTH_TOKEN",
    "ANTHROPIC_UNIX_SOCKET",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_PLUGIN_CACHE_DIR",
    "CLAUDE_CODE_PLUGIN_SEED_DIR",
    "CLAUDE_CODE_REMOTE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_VERTEX",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_LOGS_EXPORTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_SDK_DISABLED",
    "OTEL_TRACES_EXPORTER",
)


@dataclass
class ClaudeRunSpec:
    """Per-invocation parameters for spawning `claude`."""

    prompt: str
    system_prompt: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    setting_sources: str = "user"
    mcp_config_path: Path | None = None
    cwd: Path | None = None
    resume_session_id: str | None = None
    session_id: str | None = None       # explicit id (optional). If unset and
                                        # resume_session_id is unset, a fresh
                                        # uuid is generated by run_claude.
    model: str | None = None


def _build_args(
    spec: ClaudeRunSpec,
    *,
    session_id: str,
    system_prompt_path: Path | None,
) -> list[str]:
    """Compose the claude CLI argv. Pure function — no side effects."""
    args: list[str] = [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--setting-sources", spec.setting_sources,
    ]
    if spec.resume_session_id:
        args += ["--resume", spec.resume_session_id]
    else:
        args += ["--session-id", session_id]
    if spec.allowed_tools:
        args += ["--allowedTools", ",".join(spec.allowed_tools)]
    if spec.system_prompt and system_prompt_path and not spec.resume_session_id:
        args += ["--append-system-prompt-file", str(system_prompt_path)]
    if spec.mcp_config_path:
        args += ["--mcp-config", str(spec.mcp_config_path)]
    if spec.model:
        args += ["--model", spec.model]
    return args


def _scrubbed_env(env_in: dict) -> dict:
    """Return a copy of env_in with the dangerous keys removed."""
    return {k: v for k, v in env_in.items() if k not in CLAUDE_CLI_CLEAR_ENV}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/workers/test_claude_cli.py -v`
Expected: PASS (all six tests).

- [ ] **Step 5: Commit**

```bash
git add src/tend/workers/claude_cli.py tests/workers/test_claude_cli.py
git commit -m "workers: claude-cli args builder + env scrub list"
```

---

### Task 4: Stream parser — extract assistant text and archive transcript

**Files:**
- Modify: `src/tend/workers/claude_cli.py` (add `_consume_stream`)
- Modify: `tests/workers/test_claude_cli.py` (append stream-parsing tests)

- [ ] **Step 1: Append failing tests for `_consume_stream`**

Add to `tests/workers/test_claude_cli.py`:

```python
import asyncio
import json

import pytest


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
    final, _ = await _consume_stream(_async_iter(events), tmp_path / "t.jsonl")
    assert final == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/workers/test_claude_cli.py -v -k consume_stream`
Expected: FAIL — `ImportError: cannot import name '_consume_stream'`.

- [ ] **Step 3: Add `_consume_stream` to `src/tend/workers/claude_cli.py`**

Add these imports near the top:

```python
import json
from typing import AsyncIterator
```

Then append at end of file:

```python
async def _consume_stream(
    stdout: AsyncIterator[bytes],
    transcript_path: Path,
) -> tuple[str, dict]:
    """Read claude's JSONL event stream. Tee to disk, extract final assistant
    text and the trailing `result` event's usage dict."""
    final_parts: list[str] = []
    usage: dict = {}
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    with open(transcript_path, "ab") as f:
        async for raw_line in stdout:
            f.write(raw_line)
            try:
                ev = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        final_parts.append(block.get("text", ""))
            elif t == "result":
                usage = {
                    "total_cost_usd": ev.get("total_cost_usd"),
                    **(ev.get("usage") or {}),
                }
    return ("".join(final_parts).strip(), usage)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/workers/test_claude_cli.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/workers/claude_cli.py tests/workers/test_claude_cli.py
git commit -m "workers: stream-json parser tees transcript + extracts result"
```

---

### Task 5: ClaudeCliWorker.run_claude — orchestration with subprocess injection

**Files:**
- Modify: `src/tend/workers/claude_cli.py` (add the class)
- Modify: `tests/workers/test_claude_cli.py` (append run_claude tests)

- [ ] **Step 1: Append failing tests for `ClaudeCliWorker.run_claude`**

Add to `tests/workers/test_claude_cli.py`:

```python
from unittest.mock import AsyncMock, MagicMock

from tend.sessions import SessionStore


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
    # session_id is reachable from the entry
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/workers/test_claude_cli.py -v -k run_claude`
Expected: FAIL — `ImportError: cannot import name 'ClaudeCliWorker'`.

- [ ] **Step 3: Implement `ClaudeCliWorker` in `src/tend/workers/claude_cli.py`**

Add the imports:

```python
import asyncio
import os
import uuid

from pipecat_subagents.agents import BaseAgent
from pipecat_subagents.bus import AgentBus

from tend.sessions import SessionEntry, SessionStore
```

Append the class:

```python
class ClaudeCliWorker(BaseAgent):
    """Base for any worker that drives `claude` as a subprocess.

    Subclasses call `run_claude(spec)` from inside an `@task` method. The
    subprocess factory is injectable so tests can substitute a fake.
    """

    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        store: SessionStore,
        subprocess_factory=None,
    ):
        super().__init__(name, bus=bus)
        self._store = store
        # Default factory is asyncio.create_subprocess_exec — passes args as a
        # list, no shell, no injection risk.
        self._subprocess_factory = (
            subprocess_factory or asyncio.create_subprocess_exec
        )

    async def run_claude(self, spec: ClaudeRunSpec) -> SessionEntry:
        session_id = (
            spec.resume_session_id or spec.session_id or uuid.uuid4().hex
        )

        # 1. Persist a "running" record before doing anything risky.
        self._store.start(
            session_id=session_id,
            worker=self.name,
            request=spec.prompt[:200],
            cwd=str(spec.cwd) if spec.cwd else None,
        )

        # 2. System prompt → file (only on first run).
        sys_path = None
        if spec.system_prompt and not spec.resume_session_id:
            sys_path = self._store.write_system_prompt(session_id, spec.system_prompt)

        # 3. Build args + scrub env.
        args = _build_args(spec, session_id=session_id, system_prompt_path=sys_path)
        env = _scrubbed_env(dict(os.environ))

        # 4. Spawn.
        try:
            proc = await self._subprocess_factory(
                *args,
                cwd=str(spec.cwd) if spec.cwd else None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            self._store.complete(session_id, status="failed", error=f"spawn failed: {e}")
            raise

        # 5. Send prompt on stdin and close it.
        try:
            proc.stdin.write(spec.prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            pass  # claude may have exited already; let stream consumption decide.

        # 6. Stream + persist final outcome.
        try:
            transcript = self._store.transcript_path(session_id)
            final_text, usage = await _consume_stream(proc.stdout, transcript)
            rc = await proc.wait()
            if rc != 0:
                msg = f"claude exited with code {rc}"
                self._store.complete(session_id, status="failed", error=msg)
                raise RuntimeError(msg)
            return self._store.complete(
                session_id, status="done",
                spoken_summary=final_text, usage=usage,
            )
        except RuntimeError:
            raise
        except Exception as e:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            self._store.complete(session_id, status="failed", error=str(e))
            raise
```

- [ ] **Step 4: Run all claude_cli tests**

Run: `pytest tests/workers/test_claude_cli.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/workers/claude_cli.py tests/workers/test_claude_cli.py
git commit -m "workers: ClaudeCliWorker.run_claude with injectable subprocess"
```

---

### Task 6: CodingWorker — the v1 validation worker

**Files:**
- Create: `src/tend/workers/coding.py`
- Test:   `tests/workers/test_coding.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/workers/test_coding.py
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
    store, mock_bus, tmp_path, fake_worktree, monkeypatch
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/workers/test_coding.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `src/tend/workers/coding.py`**

```python
"""CodingWorker — runs `claude` in a per-job git worktree to do coding tasks.

Brain dispatches via `request_task('coding', payload={'repo': ..., 'request': ...})`.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat_subagents.agents.task_context import TaskStatus
from pipecat_subagents.agents.task_decorator import task
from pipecat_subagents.bus import AgentBus
from pipecat_subagents.bus.messages import BusFrameMessage

from tend.config import WorkerConfig
from tend.sessions import SessionStore
from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec


def default_worktree_factory(repo: Path, session_id: str) -> Path:
    """Create `~/.tend/worktrees/<session_id>/` as a git worktree off `repo`."""
    target = Path.home() / ".tend" / "worktrees" / session_id
    target.parent.mkdir(parents=True, exist_ok=True)
    branch = f"tend/coding/{session_id[:12]}"
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(target), "HEAD"],
        cwd=str(repo), check=True,
    )
    return target


class CodingWorker(ClaudeCliWorker):
    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        store: SessionStore,
        config: WorkerConfig,
        subprocess_factory=None,
        worktree_factory=default_worktree_factory,
    ):
        super().__init__(
            name, bus=bus, store=store, subprocess_factory=subprocess_factory
        )
        self._config = config
        self._worktree_factory = worktree_factory

    @task
    async def code_in(self, message) -> None:
        import uuid

        request = str(message.payload["request"])
        resume_id = message.payload.get("resume_session_id")

        try:
            if resume_id:
                # Resuming: reuse the existing worktree (whatever it was) — the
                # store knows the cwd already.
                spec = ClaudeRunSpec(
                    prompt=request,
                    resume_session_id=resume_id,
                    allowed_tools=self._config.allowed_tools,
                    setting_sources=self._config.setting_sources,
                    model=self._config.model,
                )
            else:
                repo = Path(message.payload["repo"]).expanduser()
                session_id = uuid.uuid4().hex
                worktree = self._worktree_factory(repo, session_id)
                spec = ClaudeRunSpec(
                    prompt=request,
                    session_id=session_id,
                    allowed_tools=self._config.allowed_tools,
                    setting_sources=self._config.setting_sources,
                    model=self._config.model,
                    cwd=worktree,
                )

            entry = await self.run_claude(spec)
            await self._announce(message.task_id, entry)
        except Exception as e:
            logger.exception("CodingWorker failed")
            await self._announce_error(message.task_id, request, e)

    async def _announce(self, task_id, entry):
        spoken = entry.spoken_summary or "Coding task complete."
        await self.bus.publish(BusFrameMessage(
            source=self.name,
            frame=TTSSpeakFrame(spoken),
            direction=FrameDirection.DOWNSTREAM,
        ))
        await self.send_task_update(task_id, {
            "kind": "announcement",
            "spoken": spoken,
            "context": {
                "session_id": entry.session_id,
                "worktree": entry.cwd,
                "request": entry.request,
            },
        })
        await self.send_task_response(task_id, {"delivered": True})

    async def _announce_error(self, task_id, request, exc):
        spoken = "Sorry, the coding task didn't complete."
        await self.bus.publish(BusFrameMessage(
            source=self.name,
            frame=TTSSpeakFrame(spoken),
            direction=FrameDirection.DOWNSTREAM,
        ))
        await self.send_task_update(task_id, {
            "kind": "error",
            "spoken": spoken,
            "context": {"request": request, "error": str(exc)},
        })
        await self.send_task_response(
            task_id, {"error": str(exc)}, status=TaskStatus.ERROR
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/workers/test_coding.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/workers/coding.py tests/workers/test_coding.py
git commit -m "workers: add CodingWorker (v1 ClaudeCliWorker validation)"
```

---

### Task 7: Brain session-tools — `code_in`, `list_recent_jobs`, `session_status`, `continue_session`

**Files:**
- Modify: `src/tend/brain.py`
- Test:   `tests/test_brain_session_tools.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_brain_session_tools.py
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


async def test_code_in_dispatches_task(brain, monkeypatch):
    """code_in calls request_task on the brain (fire-and-forget)."""
    brain.request_task = AsyncMock()
    out = await brain.code_in(MagicMock(), repo="/home/pi/hasat", request="fix it")
    brain.request_task.assert_awaited_once()
    args, kwargs = brain.request_task.await_args
    assert args[0] == "coding"
    assert kwargs["payload"]["request"] == "fix it"
    assert "i'll" in out.lower() or "got it" in out.lower()


async def test_continue_session_dispatches_with_resume_id(brain, store):
    store.start(session_id="prev", worker="coding", request="x", cwd=None)
    brain.request_task = AsyncMock()
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_brain_session_tools.py -v`
Expected: FAIL — Brain has no `store` parameter, tools don't exist.

- [ ] **Step 3: Modify `src/tend/brain.py` to accept the store and add the tools**

Update `Brain.__init__`:

```python
def __init__(
    self,
    name: str,
    *,
    bus: AgentBus,
    llm_service: LLMService | None,
    session_manager=None,
    store: "SessionStore | None" = None,
):
    super().__init__(name, bus=bus, bridged=())
    self._llm_service = llm_service
    self._session_manager = session_manager
    self._store = store
```

Add the import near the top:

```python
from tend.sessions import SessionStore
```

Add four new `@tool` methods to `Brain` (alongside `remind_in` and `start_fresh`):

```python
@tool
async def code_in(self, params: FunctionCallParams, repo: str, request: str):
    """Dispatch a coding task to the background coding worker.

    Args:
        repo (str): Absolute path to the git repository to operate on.
        request (str): What you want done, in plain English.
    """
    await self._ensure_coding_worker()
    await self.request_task(
        "coding", payload={"repo": repo, "request": request},
    )
    return f"Got it. I'll work on '{request[:80]}' and let you know when it's ready."

@tool
async def list_recent_jobs(self, params: FunctionCallParams, limit: int = 5):
    """List recent background jobs and their status.

    Args:
        limit (int): How many recent jobs to return (default 5).
    """
    if self._store is None:
        return "Session store unavailable."
    rows = self._store.list_recent(limit=limit)
    if not rows:
        return "No background jobs yet."
    lines = []
    for r in rows:
        lines.append(
            f"[{r.status}] {r.worker} {r.session_id[:8]}: {r.request[:80]}"
            + (f" — {r.spoken_summary}" if r.spoken_summary else "")
        )
    return "\n".join(lines)

@tool
async def session_status(self, params: FunctionCallParams, session_id: str):
    """Look up the status of a specific background job by session id.

    Args:
        session_id (str): The session id (full or unique prefix).
    """
    if self._store is None:
        return "Session store unavailable."
    rows = self._store.list_recent(limit=1000)
    match = next((r for r in rows if r.session_id.startswith(session_id)), None)
    if not match:
        return f"No session matching '{session_id}'."
    parts = [f"[{match.status}] {match.worker}: {match.request}"]
    if match.spoken_summary:
        parts.append(f"summary: {match.spoken_summary}")
    if match.error:
        parts.append(f"error: {match.error}")
    return " | ".join(parts)

@tool
async def continue_session(
    self, params: FunctionCallParams, session_id: str, follow_up: str,
):
    """Resume a previous background job with a follow-up instruction.

    Args:
        session_id (str): The session id (full or unique prefix) to resume.
        follow_up (str): What to do next, in plain English.
    """
    if self._store is None:
        return "Session store unavailable."
    rows = self._store.list_recent(limit=1000)
    match = next((r for r in rows if r.session_id.startswith(session_id)), None)
    if not match:
        return f"Couldn't find session '{session_id}'."
    await self._ensure_coding_worker()
    await self.request_task(
        match.worker,
        payload={
            "request": follow_up,
            "repo": match.cwd or "",
            "resume_session_id": match.session_id,
        },
    )
    return f"Got it. I'll follow up on session {match.session_id[:8]}."

async def _ensure_coding_worker(self) -> None:
    """Lazy-add the CodingWorker like _ensure_reminder_worker."""
    from tend.workers.coding import CodingWorker
    from tend.config import settings

    for child in getattr(self, "_children", []) or []:
        if getattr(child, "name", None) == "coding":
            return
    cfg = settings.workers.get("coding")
    if cfg is None:
        from tend.config import WorkerConfig
        cfg = WorkerConfig()
    try:
        await self.add_agent(CodingWorker(
            "coding", bus=self.bus, store=self._store, config=cfg,
        ))
    except Exception as e:
        logger.debug(f"coding worker may already exist: {e!r}")
```

No additional changes to `CodingWorker` are needed — Task 6 already wired it to honour `resume_session_id` from the payload.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brain_session_tools.py tests/test_brain.py -v`
Expected: All PASS (existing brain tests still green, new ones green).

- [ ] **Step 5: Commit**

```bash
git add src/tend/brain.py tests/test_brain_session_tools.py
git commit -m "brain: add code_in + session-status tools backed by SessionStore"
```

---

### Task 8: `tend` CLI — sessions list/show/tail/cat

**Files:**
- Create: `src/tend/cli.py`
- Test:   `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
"""CLI behaviour. We invoke main(argv) directly so tests don't need a shell."""

import json
from pathlib import Path

import pytest

from tend.sessions import SessionStore


@pytest.fixture
def store_with_rows(tmp_path, monkeypatch):
    """Set up a populated SessionStore at a temp path; patch the CLI's root."""
    root = tmp_path / "tend-home"
    store = SessionStore(root=root)
    store.start(session_id="aaaa1111", worker="coding", request="rename foo", cwd=None)
    store.complete("aaaa1111", status="done", spoken_summary="all renamed")
    store.start(session_id="bbbb2222", worker="coding", request="fix bug", cwd=None)
    # Tail content
    transcript = store.transcript_path("bbbb2222")
    transcript.write_bytes(
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": "hello"}]}}).encode()
        + b"\n"
        + json.dumps({"type": "result", "subtype": "success",
                      "total_cost_usd": 0.01}).encode()
        + b"\n"
    )
    monkeypatch.setattr("tend.cli._default_root", lambda: root)
    return store, root


def test_cli_sessions_list_shows_recent(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "list"])
    out = capsys.readouterr().out
    assert "aaaa1111"[:8] in out
    assert "bbbb2222"[:8] in out
    assert "rename foo" in out
    assert "running" in out and "done" in out


def test_cli_sessions_list_filter_status(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "list", "--status", "running"])
    out = capsys.readouterr().out
    assert "bbbb2222"[:8] in out
    assert "aaaa1111"[:8] not in out


def test_cli_sessions_list_json(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "list", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert {r["session_id"] for r in rows} == {"aaaa1111", "bbbb2222"}


def test_cli_sessions_show_resolves_prefix(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "show", "aaaa"])
    out = capsys.readouterr().out
    assert "aaaa1111" in out
    assert "rename foo" in out
    assert "done" in out


def test_cli_sessions_show_unknown_returns_nonzero(store_with_rows):
    from tend.cli import main
    with pytest.raises(SystemExit) as exc:
        main(["sessions", "show", "zzzz"])
    assert exc.value.code == 1


def test_cli_sessions_tail_pretty_prints(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "tail", "bbbb2222"])
    out = capsys.readouterr().out
    assert "[assistant]" in out
    assert "hello" in out
    assert "[result]" in out


def test_cli_sessions_tail_raw(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "tail", "bbbb2222", "--raw"])
    out = capsys.readouterr().out
    # Raw passes through the jsonl lines verbatim
    assert '"type": "assistant"' in out


def test_cli_sessions_cat_emits_bytes(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "cat", "bbbb2222"])
    out = capsys.readouterr().out
    assert "assistant" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v -k sessions`
Expected: FAIL — `tend.cli` doesn't exist.

- [ ] **Step 3: Implement `src/tend/cli.py` (sessions subcommands only — snapshot in next task)**

```python
"""Tend CLI — read-only inspection of worker sessions.

`tend sessions list/show/tail/cat` reads the on-disk SessionStore. Runs as
a separate process from the daemon; no IPC.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from tend.sessions import SessionStore


def _default_root() -> Path:
    return Path.home() / ".tend"


def _store() -> SessionStore:
    return SessionStore(root=_default_root())


def _ago(ts_ms: int) -> str:
    delta_s = int((time.time() * 1000 - ts_ms) / 1000)
    if delta_s < 60:
        return f"{delta_s}s ago"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    if delta_s < 86400:
        return f"{delta_s // 3600}h ago"
    return f"{delta_s // 86400}d ago"


def cmd_sessions_list(args) -> None:
    store = _store()
    rows = store.list_recent(limit=args.limit)
    if args.status:
        rows = [r for r in rows if r.status == args.status]
    if args.json:
        print(json.dumps([r.__dict__ for r in rows], indent=2, default=str))
        return
    if not rows:
        print("No sessions yet.")
        return
    fmt = "{status:<8} {worker:<12} {started:<10} {sid:<12} {request}"
    print(fmt.format(status="STATUS", worker="WORKER",
                     started="STARTED", sid="SESSION", request="REQUEST"))
    for r in rows:
        print(fmt.format(
            status=r.status, worker=r.worker,
            started=_ago(r.started_at), sid=r.session_id[:10],
            request=(r.request or "")[:60],
        ))


def _resolve_session(store: SessionStore, prefix: str):
    rows = store.list_recent(limit=10000)
    return next((r for r in rows if r.session_id.startswith(prefix)), None)


def cmd_sessions_show(args) -> None:
    store = _store()
    match = _resolve_session(store, args.session_id)
    if not match:
        print(f"No session matching '{args.session_id}'", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(match.__dict__, indent=2, default=str))
        return
    for k, v in match.__dict__.items():
        print(f"{k:<22} {v}")


def cmd_sessions_tail(args) -> None:
    store = _store()
    match = _resolve_session(store, args.session_id)
    if not match:
        print(f"No session matching '{args.session_id}'", file=sys.stderr)
        sys.exit(1)
    path = Path(match.transcript_path)
    if not path.exists():
        print(f"Transcript not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "r") as f:
        for line in f:
            _print_event(line, args.raw)
        if not getattr(args, "follow", False):
            return
        while True:
            line = f.readline()
            if line:
                _print_event(line, args.raw)
            else:
                time.sleep(0.2)


def cmd_sessions_cat(args) -> None:
    store = _store()
    match = _resolve_session(store, args.session_id)
    if not match:
        print(f"No session matching '{args.session_id}'", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(Path(match.transcript_path).read_text())


def _print_event(line: str, raw: bool) -> None:
    if raw:
        sys.stdout.write(line)
        return
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        sys.stdout.write(line)
        return
    t = ev.get("type", "?")
    if t == "assistant":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text":
                print(f"[assistant] {block.get('text', '')}")
            elif block.get("type") == "tool_use":
                print(f"[tool_use] {block.get('name')}({json.dumps(block.get('input', {}))[:120]})")
    elif t == "user":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                snippet = content if isinstance(content, str) else json.dumps(content)
                print(f"[tool_result] {snippet[:200]}")
    elif t == "result":
        cost = ev.get("total_cost_usd")
        print(f"[result] subtype={ev.get('subtype')} cost={cost}")
    else:
        print(f"[{t}] {json.dumps(ev)[:200]}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tend")
    sub = p.add_subparsers(dest="cmd", required=True)

    sessions = sub.add_parser("sessions").add_subparsers(dest="action", required=True)

    pl = sessions.add_parser("list"); pl.set_defaults(func=cmd_sessions_list)
    pl.add_argument("--limit", type=int, default=10)
    pl.add_argument("--status", choices=["running", "done", "failed", "killed"])
    pl.add_argument("--json", action="store_true")

    ps = sessions.add_parser("show"); ps.set_defaults(func=cmd_sessions_show)
    ps.add_argument("session_id")
    ps.add_argument("--json", action="store_true")

    pt = sessions.add_parser("tail"); pt.set_defaults(func=cmd_sessions_tail)
    pt.add_argument("session_id")
    pt.add_argument("--follow", action="store_true")
    pt.add_argument("--raw", action="store_true",
                    help="Print raw JSONL instead of pretty-printed events")

    pc = sessions.add_parser("cat"); pc.set_defaults(func=cmd_sessions_cat)
    pc.add_argument("session_id")

    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v -k sessions`
Expected: All sessions-related tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/cli.py tests/test_cli.py
git commit -m "cli: add tend sessions list/show/tail/cat for inspection"
```

---

### Task 9: `tend snapshot` — Claude Code environment cheat-sheet

**Files:**
- Modify: `src/tend/cli.py`
- Modify: `tests/test_cli.py` (append snapshot tests)

- [ ] **Step 1: Append failing tests**

```python
def test_cli_snapshot_writes_file(tmp_path, monkeypatch, capsys):
    """`tend snapshot` writes ~/.tend/claude-env.md with all expected sections."""
    from tend import cli

    root = tmp_path / "tend-home"
    root.mkdir()
    monkeypatch.setattr(cli, "_default_root", lambda: root)

    # Stub out claude_env_data so we don't shell out in tests.
    monkeypatch.setattr(cli, "_claude_version", lambda: "claude 1.2.3")
    monkeypatch.setattr(cli, "_mcp_list_markdown",
                        lambda: "| sheets | mcp__sheets__ | user | running |")

    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "skills" / "demo").mkdir(parents=True)
    (fake_home / ".claude" / "agents").mkdir(parents=True)
    (fake_home / ".claude" / "commands").mkdir(parents=True)
    (fake_home / ".claude" / "agents" / "agent-x.md").write_text("x")
    monkeypatch.setattr(cli, "_claude_home", lambda: fake_home / ".claude")

    cli.main(["snapshot"])
    out = capsys.readouterr().out
    target = root / "claude-env.md"
    assert target.exists()
    assert "Wrote" in out
    text = target.read_text()
    assert "MCP servers" in text
    assert "Skills" in text
    assert "demo" in text
    assert "Agents" in text
    assert "agent-x" in text
    assert "Suggested tend.toml additions" in text
    assert "claude 1.2.3" in text


def test_cli_snapshot_handles_missing_claude(monkeypatch, tmp_path, capsys):
    from tend import cli

    monkeypatch.setattr(cli, "_default_root", lambda: tmp_path / "tend-home")
    monkeypatch.setattr("shutil.which", lambda _: None)

    with pytest.raises(SystemExit) as exc:
        cli.main(["snapshot"])
    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "claude" in err.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v -k snapshot`
Expected: FAIL — snapshot subcommand doesn't exist.

- [ ] **Step 3: Add snapshot to `src/tend/cli.py`**

Append to `src/tend/cli.py`:

```python
import shutil
import subprocess
from datetime import datetime


def _claude_home() -> Path:
    return Path.home() / ".claude"


def _claude_version() -> str:
    r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return "(unknown)"
    return r.stdout.strip().splitlines()[0] if r.stdout else "(unknown)"


def _mcp_list_markdown() -> str:
    """Return a markdown table of `claude mcp list`. JSON form preferred."""
    j = subprocess.run(
        ["claude", "mcp", "list", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    if j.returncode == 0:
        try:
            data = json.loads(j.stdout)
            servers = data.get("servers") if isinstance(data, dict) else data
            lines = ["| Name | Tool prefix | Scope | Status |",
                     "|---|---|---|---|"]
            for s in servers or []:
                name = s.get("name", "?")
                scope = s.get("scope", "?")
                status = s.get("status", "?")
                prefix = f"mcp__{name.replace('-', '_')}__"
                lines.append(f"| {name} | {prefix} | {scope} | {status} |")
            if len(lines) == 2:
                return "_(no MCP servers configured)_"
            return "\n".join(lines)
        except json.JSONDecodeError:
            pass
    t = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=10)
    if t.returncode == 0:
        return f"```\n{t.stdout.strip() or '(empty)'}\n```"
    return "_(`claude mcp list` failed)_"


def _list_dir_md(d: Path) -> str:
    if not d.exists():
        return "_(none configured)_"
    items = sorted(
        p.stem for p in d.iterdir()
        if p.is_dir() or p.suffix == ".md"
    )
    return "\n".join(f"- {it}" for it in items) if items else "_(none configured)_"


def cmd_snapshot(args) -> None:
    if not shutil.which("claude"):
        print("`claude` CLI not found on PATH. Install Claude Code first.", file=sys.stderr)
        sys.exit(1)

    home = _claude_home()
    sections = []
    sections.append("# Claude Code Environment Snapshot\n")
    sections.append(
        f"Generated: {datetime.now().isoformat(timespec='minutes')} "
        f"(claude --version: {_claude_version()})\n"
    )
    sections.append("## MCP servers (`claude mcp list`)\n")
    sections.append(_mcp_list_markdown())
    sections.append("")

    for label, sub in [("Skills", "skills"), ("Agents", "agents"), ("Commands", "commands")]:
        sections.append(f"## {label} ({home}/{sub}/)\n")
        sections.append(_list_dir_md(home / sub))
        sections.append("")

    sections.append("## Native Claude Code tools\n")
    sections.append(
        "Read, Edit, Write, Bash, Grep, Glob, WebSearch, WebFetch, "
        "NotebookEdit, Task, TodoWrite\n"
    )

    sections.append("## Suggested tend.toml additions\n")
    sections.append(
        "For a meal-planning worker (replace MCP names with what shows above):\n\n"
        "```toml\n"
        "[workers.meal_plan]\n"
        'model = "claude-sonnet-4-6"\n'
        'setting_sources = "user"\n'
        "allowed_tools = [\n"
        '  "mcp__google_calendar__*",\n'
        '  "mcp__google_sheets__*",\n'
        "]\n"
        "```\n\n"
        "For a coding worker with full tool access:\n\n"
        "```toml\n"
        "[workers.coding]\n"
        'model = "claude-opus-4-7"\n'
        'setting_sources = "user,project,local"\n'
        'allowed_tools = ["Read", "Edit", "Write", "Bash", "Grep", "Glob", "mcp__*"]\n'
        "```\n"
    )

    out = _default_root() / "claude-env.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(sections))
    print(f"Wrote {out}")
```

Register in `build_parser()` (after the `sessions` parser):

```python
    psnap = sub.add_parser("snapshot", help="Snapshot the local Claude Code environment.")
    psnap.set_defaults(func=cmd_snapshot)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/cli.py tests/test_cli.py
git commit -m "cli: add tend snapshot for Claude Code env cheat-sheet"
```

---

### Task 10: Boot preflight + main wire-up + tend.toml + pyproject

**Files:**
- Create: `src/tend/preflight.py`
- Create: `tests/test_preflight.py`
- Modify: `src/tend/main.py`
- Modify: `tend.toml`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write the failing preflight test**

```python
# tests/test_preflight.py
"""Boot-time preflight for the claude CLI. Logs a warning, never raises."""

from unittest.mock import MagicMock, patch

import pytest

from tend.preflight import claude_cli_preflight


def test_preflight_returns_true_when_claude_ok():
    fake_run = MagicMock(side_effect=[
        MagicMock(returncode=0, stdout="claude 1.2.3\n", stderr=""),
        MagicMock(returncode=0, stdout="logged in", stderr=""),
    ])
    with patch("tend.preflight.subprocess.run", fake_run), \
         patch("tend.preflight.shutil.which", return_value="/usr/bin/claude"):
        assert claude_cli_preflight() is True


def test_preflight_returns_false_when_claude_missing():
    with patch("tend.preflight.shutil.which", return_value=None):
        assert claude_cli_preflight() is False


def test_preflight_returns_false_when_auth_status_fails():
    fake_run = MagicMock(side_effect=[
        MagicMock(returncode=0, stdout="claude 1.2.3\n", stderr=""),
        MagicMock(returncode=1, stdout="", stderr="not logged in"),
    ])
    with patch("tend.preflight.subprocess.run", fake_run), \
         patch("tend.preflight.shutil.which", return_value="/usr/bin/claude"):
        assert claude_cli_preflight() is False


def test_preflight_does_not_raise_on_subprocess_error():
    with patch("tend.preflight.shutil.which", return_value="/usr/bin/claude"), \
         patch("tend.preflight.subprocess.run", side_effect=OSError("boom")):
        assert claude_cli_preflight() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_preflight.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create `src/tend/preflight.py`**

```python
"""Boot-time checks for optional dependencies. Each preflight returns a bool;
the caller decides whether to log/proceed/abort based on its own policy."""

from __future__ import annotations

import shutil
import subprocess

from loguru import logger


def claude_cli_preflight() -> bool:
    """Verify `claude` is installed and authenticated. Never raises.

    Returns True only if both `claude --version` and `claude auth status`
    return zero. Otherwise logs a warning and returns False — claude-cli
    workers will still error cleanly on first dispatch in that case.
    """
    if not shutil.which("claude"):
        logger.warning("preflight: `claude` CLI not on PATH; claude-cli workers will fail.")
        return False
    try:
        v = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if v.returncode != 0:
            logger.warning(f"preflight: `claude --version` failed: {v.stderr.strip()}")
            return False
        logger.info(f"preflight: claude version → {v.stdout.strip()}")
        a = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if a.returncode != 0:
            logger.warning(
                "preflight: `claude auth status` failed; run `claude auth login`."
            )
            return False
        return True
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"preflight: claude check raised {e!r}")
        return False
```

- [ ] **Step 4: Run preflight tests to verify they pass**

Run: `pytest tests/test_preflight.py -v`
Expected: PASS.

- [ ] **Step 5: Wire `SessionStore` and the preflight into `src/tend/main.py`**

Add imports near the top of `src/tend/main.py`:

```python
from pathlib import Path

from tend.preflight import claude_cli_preflight
from tend.sessions import SessionStore
```

Inside `_run`, right after `runner = AgentRunner()`:

```python
    claude_cli_preflight()  # logs warning on failure; non-fatal
    store = SessionStore(root=Path.home() / ".tend")
```

Update the Brain construction line to inject the store:

```python
    brain = Brain("brain", bus=runner.bus, llm_service=llm_service, store=store)
```

(The `store=store` kwarg matches the new optional parameter from Task 7.)

- [ ] **Step 6: Add `[workers.coding]` block to `tend.toml`**

Append at the bottom of `tend.toml`:

```toml
# Worker config — see docs/superpowers/specs/2026-05-06-claude-cli-workers-design.md
[workers.coding]
model = "claude-opus-4-7"
setting_sources = "user,project,local"
allowed_tools = ["Read", "Edit", "Write", "Bash", "Grep", "Glob"]
```

- [ ] **Step 7: Update `pyproject.toml` console script entry**

Change:

```toml
[project.scripts]
tend = "tend.__main__:main"
```

…to:

```toml
[project.scripts]
tend = "tend.cli:main"
```

(The systemd unit at `deploy/tend.service` invokes `python -m tend`, which still routes through `__main__.py:main` — daemon entry is unaffected.)

- [ ] **Step 8: Reinstall the project so the new entry takes effect**

Run: `pip install -e .`
Expected: tend installed; new `tend` shell command points at `tend.cli:main`.

- [ ] **Step 9: Smoke-test the CLI end-to-end**

Run: `tend sessions list`
Expected: prints "No sessions yet." (or any pre-existing rows from your home directory).

Run: `tend snapshot`
Expected: prints `Wrote /home/pi/.tend/claude-env.md` and the file exists with the expected sections.

- [ ] **Step 10: Run the full test suite**

Run: `pytest -v`
Expected: All previously-passing tests still pass; all new tests pass.

- [ ] **Step 11: Commit**

```bash
git add src/tend/preflight.py src/tend/main.py tests/test_preflight.py tend.toml pyproject.toml
git commit -m "main: wire SessionStore + claude preflight + tend.toml workers.coding"
```

---

## Done when

- All ten tasks committed.
- `pytest -v` is green.
- `tend sessions list` works against the live `~/.tend` directory.
- `tend snapshot` writes `~/.tend/claude-env.md` referencing the user's actual MCPs.
- A live voice request like *"Make a coding change in tend that logs the wake-word score on every detection"* dispatches a `CodingWorker`, runs `claude` in a fresh worktree at `~/.tend/worktrees/<id>/`, and the assistant announces the result aloud when finished.
- The session shows up in `tend sessions list` as `done`, with the spoken summary and a working `tend sessions tail <id>` transcript.

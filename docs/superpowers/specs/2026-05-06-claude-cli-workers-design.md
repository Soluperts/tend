# Claude-CLI Workers — Design

How DeskClaw's "I'll handle that for you" actions actually run.

---

## The big picture (plain English)

DeskClaw is a personal assistant for someone who works at a desk all day. It listens through a smart speaker, can be poked through Telegram, watches passive data sources (smart scale, watch, calendar, email, a Google Sheet you fill in by hand), and helps you stay healthy and organised. When you ask it to do something — *"plan my meals for next week"*, *"work out a fitness routine based on my recent activity"*, *"block out tomorrow's calendar"* — it does the work in the background and tells you when it's done.

The diagram in `deskclaw.png` (in the repo root) splits the system into four kinds of pieces:

- **Channels** — how DeskClaw talks to you. Smart speaker (voice) and Telegram (text).
- **Data collectors** — the things DeskClaw watches passively. Gmail, calendar, smart scale, smart watch, ambient camera, Google Sheets.
- **AI agent** — the conversational core. It plans, remembers, and decides which workflow to run.
- **Workflows** — the heavy lifting. Each is a long-running task: meal planning, fitness planning, routine planning, reminders.

This document is about the **workflow layer** — specifically, how each workflow is implemented as a small program (a *worker*) that talks to Claude in the background, and how DeskClaw remembers what those workers are doing across hours, days, and restarts.

---

## What changes from what we have today

The current tend codebase has all the pieces of the AI-agent column already: `Hub` owns the audio I/O and the conversation context, `Brain` is the LLM that talks to you, `ReminderWorker` is a stub showing how a worker dispatches a task, announces a result, and folds it back into the conversation. What's missing for the diagram's vision is:

1. **A way for workers to actually do useful work**, beyond `asyncio.sleep()`. They need access to real tools — read your calendar, write to a Google Sheet, search the web, edit files, run commands. That's what Claude Code (`claude` on the command line) already provides. So workers will spawn `claude` as a subprocess and let it use its tools to do the job.
2. **A way to remember sessions**. If you ask DeskClaw to plan your meals on Monday and you wake it up Tuesday morning to ask "did the meal plan get done?", it has to know. That can't all live in conversation memory — it has to live on disk so it survives the daily 4 a.m. context reset and any process restarts.

Those two things — *Claude-CLI workers* and *persistent sessions* — are what this document covers.

---

## What a "claude-cli worker" actually is

In one sentence: **a small Python class that, when given a job, opens a Claude Code session in the background, lets Claude work, and tells you the result.**

A bit longer: every workflow in the diagram (meal plan, fitness plan, routine, etc.) becomes one Python class — say `MealPlanWorker`. When the AI agent decides to run it, the worker:

1. Picks a unique session ID and writes a "running" record to a file on disk.
2. Spawns the `claude` command-line tool as a child process, with a tailored prompt (something like: *"Plan three days of meals for this user, considering their recent weight, activity, and a recipe list in their Google Sheet. Use the Sheets MCP tool to read the recipes; use the calendar MCP to add prep tasks."*).
3. Reads Claude's output as it streams (a structured event log, one line of JSON per event).
4. Saves that whole transcript to disk for later inspection.
5. When Claude finishes, the worker takes the final answer, says it aloud (or sends it to Telegram), and updates the on-disk session record so DeskClaw knows the job is done.

That's the loop. **The worker is the thin Python wrapper; Claude is the agent doing the work.**

Why this rather than calling Anthropic's API directly?

- **Subscription billing.** When you spawn the real `claude` CLI, the API call goes through Anthropic's first-party tooling and is billed against your Claude Pro/Max plan, the same way it is when you use Claude Code interactively. If you call the API directly with an SDK, even using your OAuth token, Anthropic categorises that as "third-party harness usage" and bills per-token.
- **Tool surface for free.** Claude Code already ships with file-editing, shell, web search, web fetch, and any MCP servers you've installed. Workers don't have to re-implement any of that.
- **Skills, agents, and MCPs you've configured carry over automatically.** Whatever you set up at `~/.claude/`, every worker session inherits.

---

## A walk through one workflow, end to end

Setting: it's Sunday evening. You speak to the smart speaker.

> *"Plan three days of meals starting Monday, mostly Mediterranean, light on dairy."*

Here's what happens:

1. **Wake gate opens** — the local wake-word model fires, audio starts flowing to STT (Deepgram).
2. **Brain gets the transcript** and sees "plan three days of meals…". It looks at its tool list, finds `plan_meals(start_date, days, preferences)`, and calls it with the parsed args.
3. **The tool is a fire-and-forget dispatch** — it tells the `MealPlanWorker` to start a job and immediately returns to Brain: *"Got it, I'll plan those meals and tell you when it's ready."*
4. **Brain says that out loud** to you. The voice loop is free again. The wake gate can close — DeskClaw can go back to sleep.
5. **Meanwhile, in the background**, `MealPlanWorker` does this:
   1. Generates a session ID like `7f3a8e2d4c5b1a93`.
   2. Writes an entry to `~/.tend/sessions.json`: status `running`, started at *now*, request *"three days of Mediterranean meals starting Monday, light on dairy"*.
   3. Builds a system prompt that includes the user's persona (`soul.md`), available MCPs (Google Sheets, Calendar), and the meal-plan-specific guidance.
   4. Spawns `claude -p ...` (full command shown later) with the prompt as input.
   5. Reads each JSON event off Claude's stdout and writes it to `~/.tend/sessions/7f3a8e2d4c5b1a93.jsonl` for archival.
   6. Watches for the `result` event that signals completion.
6. **Claude does the work.** It might call `Sheets.read_recipes()`, look up your recent weight/activity, draft a plan, write it back to a meal-plan sheet, and add prep tasks to your calendar. All of that happens inside Claude's own tool loop, using your subscription quota.
7. **When Claude finishes**, the worker:
   1. Updates `sessions.json`: status `done`, ended at *now*, spoken summary set to *"Meal plan ready. Three days of Mediterranean dishes, dairy-light. Three prep tasks added to your calendar."*
   2. Publishes a `TTSSpeakFrame` with that summary to the bus. If you're awake and the speaker is quiet, you hear it. If not, it's queued.
   3. Sends a `task_update` to Brain, which appends the spoken summary plus the session ID to its conversation context — so when you wake DeskClaw later and say "tweak yesterday's plan," Brain has it.

That's a full trip. Most workflows will look like this — only the prompt, the MCP tools, and the announcement summary differ.

---
## The technical layer

### The base class every worker subclasses

```python
# src/tend/workers/claude_cli.py

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from pipecat_subagents.agents import BaseAgent
from tend.sessions import SessionStore, SessionEntry  # see below

# Env vars to delete before spawning `claude`. If any of these are set in
# the parent process, `claude` will silently route requests to a different
# provider, endpoint, or token source — including billing per-token instead
# of using the Pro/Max plan. Mirrors openclaw/extensions/anthropic/cli-shared.ts.
CLAUDE_CLI_CLEAR_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_API_TOKEN", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    # plus the OTEL_* family — see full list in source
)

@dataclass
class ClaudeRunSpec:
    prompt: str
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    setting_sources: str = "user"          # "user" | "user,project,local"
    mcp_config_path: Path | None = None
    cwd: Path | None = None
    resume_session_id: str | None = None
    model: str | None = None               # None → claude default

class ClaudeCliWorker(BaseAgent):
    """Base class for any worker that drives a `claude` subprocess."""

    def __init__(self, name: str, *, bus, store: SessionStore):
        super().__init__(name, bus=bus)
        self._store = store

    async def run_claude(self, spec: ClaudeRunSpec) -> SessionEntry:
        # Note: `claude --session-id` requires a canonical 8-4-4-4-12 UUID
        # (with dashes). Using the hex-only form fails fast with
        # "Error: Invalid session ID. Must be a valid UUID." See _new_session_id.
        session_id = spec.resume_session_id or _new_session_id()

        # 1. Write the "running" record to disk before we spawn anything.
        entry = self._store.start(
            session_id=session_id,
            worker=self.name,
            request=spec.prompt[:200],     # truncated for index readability
            cwd=str(spec.cwd) if spec.cwd else None,
        )

        # 2. Build the env. Inherit, then strip the dangerous keys.
        env = {k: v for k, v in os.environ.items() if k not in CLAUDE_CLI_CLEAR_ENV}

        # 3. Build the args. System prompt goes via file (avoids arg-length limits).
        args = ["claude", "-p",
                "--output-format", "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--setting-sources", spec.setting_sources,
                "--session-id", session_id]
        if spec.allowed_tools:
            args += ["--allowedTools", ",".join(spec.allowed_tools)]
        if spec.system_prompt:
            sys_path = self._store.write_system_prompt(session_id, spec.system_prompt)
            args += ["--append-system-prompt-file", str(sys_path)]
        if spec.mcp_config_path:
            args += ["--mcp-config", str(spec.mcp_config_path)]
        if spec.model:
            args += ["--model", spec.model]
        if spec.resume_session_id:
            args += ["--resume", spec.resume_session_id]

        # 4. Spawn and stream. Using create_subprocess_exec because it
        #    passes args as a list (no shell, no injection risk).
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=spec.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Send the prompt on stdin, then close it.
        proc.stdin.write(spec.prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # Read stderr concurrently — claude's actual error message lands
        # there on argparse / auth / config failures. If we don't drain it
        # the OS pipe buffer can fill and stall the process; if we don't
        # capture it the operator is left with an opaque exit code.
        stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))
        try:
            final_text, usage = await self._consume_stream(session_id, proc.stdout)
            rc = await proc.wait()
            stderr_text = await stderr_task
            if rc != 0:
                # Surface the trailing few stderr lines in the error record.
                detail = " | ".join(stderr_text.strip().splitlines()[-3:])
                msg = f"claude exited with code {rc}" + (f": {detail}" if detail else "")
                entry = self._store.complete(session_id, status="failed", error=msg)
                raise RuntimeError(msg)
            entry = self._store.complete(
                session_id, status="done",
                spoken_summary=final_text,
                usage=usage,
            )
        except Exception as exc:
            proc.kill()
            if not stderr_task.done():
                stderr_task.cancel()
            entry = self._store.complete(session_id, status="failed", error=str(exc))
            raise
        return entry

    async def _consume_stream(self, session_id, stdout) -> tuple[str, dict]:
        """Tee Claude's JSONL event stream to disk + extract the final answer."""
        transcript_path = self._store.transcript_path(session_id)
        final_text_parts: list[str] = []
        usage = {}
        with open(transcript_path, "ab") as f:
            async for raw_line in stdout:
                f.write(raw_line)
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                # claude's stream-json events look like:
                #   {"type":"system",...}
                #   {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
                #   {"type":"result","subtype":"success","total_cost_usd":...,"usage":{...}}
                if event.get("type") == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            final_text_parts.append(block.get("text", ""))
                elif event.get("type") == "result":
                    usage = event.get("usage", {})
        return ("".join(final_text_parts).strip(), usage)


def _new_session_id() -> str:
    # Canonical 8-4-4-4-12 UUID. `uuid.uuid4().hex` (no dashes) is rejected
    # by `claude --session-id` with "Invalid session ID. Must be a valid UUID."
    return str(uuid.uuid4())


async def _drain_stderr(stderr) -> str:
    """Read claude's stderr to a string. Bounded (~64 KiB) so a runaway
    process can't blow up memory. Cancellation-safe: returns whatever we
    captured so far. Stderr is only used to enrich error messages, never
    as primary signal."""
    chunks, total, LIMIT = [], 0, 64 * 1024
    try:
        async for line in stderr:
            chunks.append(line)
            total += len(line)
            if total >= LIMIT:
                break
    except asyncio.CancelledError:
        pass
    return b"".join(chunks).decode("utf-8", errors="replace")
```

That's the entire base class. About 100 lines. Subclasses get small.

### A concrete subclass: meal-plan worker

```python
# src/tend/workers/meal_plan.py

from pipecat_subagents.agents.task_decorator import task
from tend.config import WorkerConfig
from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec

class MealPlanWorker(ClaudeCliWorker):
    SYSTEM_PROMPT = """
    You are DeskClaw's meal planner. The user has a Google Sheet of approved
    recipes (use the sheets MCP tool to read it). Add a 'Meals' tab with the
    plan and add prep tasks to their calendar (calendar MCP tool).

    Respond with one or two sentences summarising what you did.
    """

    def __init__(self, name, *, bus, store, config: WorkerConfig):
        super().__init__(name, bus=bus, store=store)
        self._config = config       # injected from tend.toml at boot

    @task
    async def plan(self, message) -> None:
        days = int(message.payload["days"])
        prefs = str(message.payload["preferences"])
        start = str(message.payload["start_date"])

        entry = await self.run_claude(ClaudeRunSpec(
            prompt=f"Plan {days} days of meals starting {start}. Preferences: {prefs}.",
            system_prompt=self.SYSTEM_PROMPT,
            allowed_tools=self._config.allowed_tools,
            setting_sources=self._config.setting_sources,
            model=self._config.model,
        ))
        await self._announce(message.task_id, entry)

    async def _announce(self, task_id, entry):
        # Standard pattern from ReminderWorker — TTSSpeakFrame on the bus,
        # task_update with structured context for Brain's LLMContext.
        ...
```

Same shape every workflow follows. The only things that vary between meal-plan, fitness-plan, routine-planning, etc. are: the system prompt, the allowed MCP tools, and the announce text. And of those three, two — allowed tools and the model choice — live in config, not code.

### The coding worker — DeskClaw's escape hatch

DeskClaw's user-facing capabilities are workflow workers: meal-plan, fitness-plan, routine planning, reminders, dashboard. Coding is *not* one of those. The `CodingWorker` exists so that when DeskClaw needs to *build* something to enable a workflow — a small parser, a glue script, a one-off tool — it can dispatch the build job to a claude-cli worker and accumulate the result somewhere persistent. It's the escape hatch, not a top-level feature.

Because of that framing, the coding worker has two simplifications relative to what you might expect:

1. **One persistent workspace, not per-job worktrees.** All coding tasks share `~/.tend/workspace/` (configurable via `[workers.coding].workspace_dir` in `tend.toml`). DeskClaw's accumulated tools live there. There is no per-job git-worktree isolation, no auto-created branch, no cleanup. If you want history, `git init` the workspace yourself once.

2. **The LLM does not pick a repo.** Brain's `code_in(request)` tool takes only `request: str`. The cwd is fixed to the workspace. This keeps the surface area small and matches DeskClaw's intent — coding is for the assistant's own infrastructure, not a generic "code in any repo" agent.

```python
# src/tend/workers/coding.py (excerpt)

DEFAULT_WORKSPACE = Path.home() / ".tend" / "workspace"

class CodingWorker(ClaudeCliWorker):
    def __init__(self, name, *, bus, store, config, workspace_dir=None):
        super().__init__(name, bus=bus, store=store)
        self._config = config
        self._workspace_dir = workspace_dir or _resolve_workspace(config)

    @task
    async def code_in(self, message) -> None:
        request = str(message.payload["request"])
        resume_id = message.payload.get("resume_session_id")
        workspace = await asyncio.to_thread(self._ensure_workspace)
        spec = ClaudeRunSpec(
            prompt=request,
            resume_session_id=resume_id,
            allowed_tools=self._config.allowed_tools,
            setting_sources=self._config.setting_sources,
            model=self._config.model,
            cwd=workspace,                  # always the same dir
        )
        entry = await self.run_claude(spec)
        await self._announce(message.task_id, entry)
```

Resumes pass `--resume <session-id>` (canonical UUID) and still cwd into the same workspace; claude itself remembers the per-session state from disk.

### Worker config in `tend.toml`

The example above reads `allowed_tools`, `setting_sources`, and `model` from a per-worker config block. The actual MCP names (`mcp__google_calendar__*` vs `mcp__calendar__*` etc.) depend on what `claude mcp add ...` was run with on this Pi, so they belong in config, not pinned in source.

`tend.toml` gets a new `[workers.<name>]` section per worker:

```toml
# tend.toml (excerpt)

[workers.coding]
model = "claude-opus-4-7"
setting_sources = "user,project,local"   # load project CLAUDE.md too
allowed_tools = [
  "Read", "Edit", "Write", "Bash", "Grep", "Glob",
  "WebSearch", "WebFetch",
  "mcp__*",                              # any installed MCP, in case claude wants it
]
workspace_dir = "~/.tend/workspace"      # persistent shared workspace

[workers.meal_plan]
model = "claude-sonnet-4-6"
setting_sources = "user"                 # ignore arbitrary repo CLAUDE.md
allowed_tools = [
  "mcp__google_sheets__*",
  "mcp__google_calendar__*",
]

[workers.fitness_plan]
model = "claude-sonnet-4-6"
setting_sources = "user"
allowed_tools = [
  "mcp__google_sheets__*",
  "mcp__health_connect__*",              # if you wire one up
]
```

Loaded by pydantic-settings into `tend.config.Settings.workers`:

```python
# src/tend/config.py (additions)

from pydantic import BaseModel

class WorkerConfig(BaseModel):
    model: str | None = None
    setting_sources: str = "user"
    allowed_tools: list[str] = []
    mcp_config_path: str | None = None    # optional explicit --mcp-config override
    workspace_dir: str | None = None      # CodingWorker only — see "The coding worker"

class Settings(BaseSettings):
    # ... existing fields ...
    workers: dict[str, WorkerConfig] = {}
```

The wiring is one line in the runner setup — each worker is constructed with `config=settings.workers.get(worker_name) or WorkerConfig()`.

⚠️ **Empty `allowed_tools` does NOT lock claude down.** Omitting `--allowedTools` from the argv lets claude run with its full default tool surface (Bash, file editing, web search, web fetch). To actually restrict, you have to pass an explicit non-empty list. Because of that, when a worker dispatches with `allowed_tools=[]` (i.e. no `[workers.<name>]` block in `tend.toml`, or one with an empty list), `ClaudeCliWorker.run_claude` emits a loud warning at the start of the run. The warning is the loud signal; we deliberately did not error out at boot, so workers that genuinely want full access can opt in by configuring `allowed_tools = ["*"]` once that intent is explicit. (See open question #4.)

A note on the `mcp__*` wildcard: passing `mcp__*` allows every MCP-prefixed tool. Useful for the coding worker where Claude may need any tool you've installed. Less appropriate for narrow workers like meal-plan, where you want to be explicit so the model doesn't wander off and read your email when asked to plan dinner.

Note the system-prompt-shaped contract: the worker's `_consume_stream` captures Claude's final assistant text verbatim and that becomes the `spoken_summary`. So the system prompt **must** instruct Claude to keep its final response short and speakable (the meal-plan example does: *"Respond with one or two sentences summarising what you did."*). If a worker forgets that instruction, the speaker will read out a wall of text. For workers that produce structured output, the worker can post-process — e.g. take the final text as the spoken summary but also pull out tool-call results from the transcript for richer context.

### Why the env scrub is critical

`claude` reads environment variables before it consults `~/.claude/.credentials.json`. If your shell has `ANTHROPIC_API_KEY` set (a very common case for developers), `claude` will use that key — and you'll be billed per-token instead of from your Pro/Max plan, silently. The scrub list above is copied from OpenClaw's `extensions/anthropic/cli-shared.ts`, where they hit the same issue and worked through it. Without this, the whole subscription-billing premise breaks.

---
## Remembering what's running — the session store

### Why this needs to live on disk

Brain's conversation memory is wiped every day at 4 a.m. (the daily reset, designed to keep context fresh and bounded). Brain also forgets everything if the process restarts (planned: systemd brings it back up clean; unplanned: the Pi reboots).

Workers don't restart with conversation memory. So if a meal-plan worker is mid-run when the user goes to bed and the day-session reset fires at 4 a.m., the conversation context is wiped — but the worker is still running. When the user wakes up and asks "did the meal plan finish?", Brain needs to be able to look up the answer. Same problem if the Pi reboots overnight: any running session is dead, but the user shouldn't be left wondering.

So we put a small persistent record next to each session, written to disk before we spawn anything and updated as the session progresses.

### What gets stored, and where

```
~/.tend/
├── sessions.json                   # the index — one entry per session
├── sessions/
│   └── <session_id>.jsonl          # archival transcript per session
│                                   # (every JSONL event from claude's stream)
├── system-prompts/
│   └── <session_id>.txt            # system prompt used for that session
└── workspace/                      # the persistent CodingWorker workspace
                                    # — see "The coding worker" below
```

`sessions.json` is the index, keyed by session ID. Each value is a `SessionEntry`:

```python
# src/tend/sessions.py

from dataclasses import dataclass
from typing import Literal

@dataclass
class SessionEntry:
    session_id: str
    worker: str                                # "meal_plan" | "coding" | ...
    task_id: str | None                        # the bus task id that started it
    request: str                               # truncated for readability
    cwd: str | None                            # working dir of the subprocess
    status: Literal["running", "done", "failed", "killed"]
    started_at: int                            # epoch ms
    last_interaction_at: int                   # epoch ms — bumped each turn
    ended_at: int | None
    spoken_summary: str | None                 # what we said to the user
    transcript_path: str
    model: str | None
    cost_usd: float | None                     # from claude's `result` event
```

That schema is deliberately small. We can grow it as workflows surface real needs (e.g. linking a session to a Google Sheet URL, attaching a calendar event ID). We're skipping OpenClaw's plugin-extension slots, subagent lineage, and abort-cutoff machinery — we don't need them yet.

### How writes happen safely

Every write goes through a `SessionStore` class:

```python
# src/tend/sessions.py

import json, os, tempfile, time
from dataclasses import asdict
from pathlib import Path

class SessionStore:
    def __init__(self, root: Path = Path.home() / ".tend"):
        self.root = root
        (self.root / "sessions").mkdir(parents=True, exist_ok=True)
        (self.root / "system-prompts").mkdir(parents=True, exist_ok=True)
        self._index = self.root / "sessions.json"
        if not self._index.exists():
            self._write_index({})

    def start(self, *, session_id, worker, request, cwd, task_id=None) -> SessionEntry:
        entry = SessionEntry(
            session_id=session_id, worker=worker, task_id=task_id,
            request=request, cwd=cwd, status="running",
            started_at=_now_ms(), last_interaction_at=_now_ms(),
            ended_at=None, spoken_summary=None,
            transcript_path=str(self.transcript_path(session_id)),
            model=None, cost_usd=None,
        )
        self._update(lambda idx: idx.__setitem__(session_id, asdict(entry)))
        return entry

    def complete(self, session_id, *, status, spoken_summary=None, usage=None, error=None):
        def patch(idx):
            row = idx.get(session_id, {})
            row["status"] = status
            row["ended_at"] = _now_ms()
            if spoken_summary is not None:
                row["spoken_summary"] = spoken_summary
            if usage:
                row["cost_usd"] = usage.get("total_cost_usd")
            if error:
                row["error"] = error[:500]
            idx[session_id] = row
        self._update(patch)
        return SessionEntry(**self._read_index()[session_id])

    def list_recent(self, *, limit=10) -> list[SessionEntry]:
        idx = self._read_index()
        rows = sorted(idx.values(), key=lambda r: r["started_at"], reverse=True)
        return [SessionEntry(**r) for r in rows[:limit]]

    def transcript_path(self, session_id) -> Path:
        return self.root / "sessions" / f"{session_id}.jsonl"

    def write_system_prompt(self, session_id, text) -> Path:
        p = self.root / "system-prompts" / f"{session_id}.txt"
        p.write_text(text)
        return p

    def _update(self, mutator):
        idx = self._read_index()
        mutator(idx)
        self._write_index(idx)

    def _read_index(self) -> dict:
        return json.loads(self._index.read_text() or "{}")

    def _write_index(self, idx: dict):
        # Atomic write: temp file in same dir, then rename.
        tmp = tempfile.NamedTemporaryFile("w", dir=self.root, delete=False, suffix=".tmp")
        json.dump(idx, tmp, indent=2)
        tmp.close()
        os.replace(tmp.name, self._index)


def _now_ms() -> int:
    return int(time.time() * 1000)
```

The atomic write (`tempfile` + `os.replace`) is the core trick: if the Pi loses power mid-write, the index is either the old version or the new version, never half-written.

### What Brain can do with the store

Three new tools on Brain:

```python
@tool
async def list_recent_jobs(self, params, limit: int = 5) -> str:
    """List the last N background jobs and their status."""
    rows = self._store.list_recent(limit=limit)
    if not rows:
        return "No jobs in the last little while."
    return "\n".join(
        f"- [{r.status}] {r.worker}: {r.request} (started {_relative(r.started_at)})"
        for r in rows
    )

@tool
async def session_status(self, params, session_id: str) -> str:
    """Look up the status of a specific job."""
    ...

@tool
async def continue_session(self, params, session_id: str, follow_up: str):
    """Resume a previous job with a follow-up request."""
    # Dispatches a task to the worker that owns this session,
    # which calls run_claude(ClaudeRunSpec(..., resume_session_id=session_id)).
    ...
```

So when you say *"how did the meal plan go yesterday?"* — even after the day-session reset wiped Brain's memory — Brain can call `list_recent_jobs` and tell you, because the session store is still on disk.

---

## Manual inspection — the `tend` CLI

When something goes wrong (or you're just curious), you don't want to be limited to what Brain can tell you over voice. You want a terminal you can SSH into and see what's actually happening on disk.

So v1 ships a small read-only CLI that talks to the same on-disk session store the workers write to. It runs as a separate process — it doesn't interrupt the running daemon and doesn't need any IPC.

### Surface

```
$ tend sessions list                              # last 10 sessions, table form
$ tend sessions list --limit 50 --status running  # filter
$ tend sessions list --json                       # machine-readable for jq

$ tend sessions show <session_id>                 # full metadata for one session
$ tend sessions show <session_id> --json

$ tend sessions tail <session_id>                 # print all transcript events, once
$ tend sessions tail <session_id> --follow        # tail -f the JSONL transcript
$ tend sessions cat <session_id>                  # raw bytes of the transcript file
                                                  # (pipes well into jq)
```

The four verbs are enough to answer the questions a human will actually ask: *"what's been running today?", "what was that exact prompt?", "what is this session doing right now?", and "give me the raw events so I can grep them."*

### Implementation

A single new file — `src/tend/cli.py` — using stdlib `argparse`. Roughly 150 lines. Reads `SessionStore` directly:

```python
# src/tend/cli.py

import argparse
import json
import sys
import time
from pathlib import Path
from tend.sessions import SessionStore

def cmd_list(args):
    store = SessionStore()
    rows = store.list_recent(limit=args.limit)
    if args.status:
        rows = [r for r in rows if r.status == args.status]
    if args.json:
        print(json.dumps([r.__dict__ for r in rows], indent=2))
        return
    if not rows:
        print("No sessions yet.")
        return
    fmt = "{status:<8} {worker:<14} {started:<20} {id:<20} {request}"
    print(fmt.format(status="STATUS", worker="WORKER",
                     started="STARTED", id="SESSION", request="REQUEST"))
    for r in rows:
        print(fmt.format(
            status=r.status,
            worker=r.worker,
            started=_ago(r.started_at),
            id=r.session_id[:18],
            request=r.request[:60],
        ))

def cmd_show(args):
    store = SessionStore()
    rows = store.list_recent(limit=10000)
    match = next((r for r in rows if r.session_id.startswith(args.session_id)), None)
    if not match:
        print(f"No session matching '{args.session_id}'", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(match.__dict__, indent=2))
    else:
        for k, v in match.__dict__.items():
            print(f"{k:<22} {v}")

def cmd_tail(args):
    store = SessionStore()
    path = store.transcript_path(args.session_id)
    if not path.exists():
        # try prefix match
        for p in (store.root / "sessions").glob(f"{args.session_id}*.jsonl"):
            path = p
            break
    if not path.exists():
        print(f"No transcript for '{args.session_id}'", file=sys.stderr)
        sys.exit(1)
    with open(path, "r") as f:
        # Print existing content first
        for line in f:
            _print_event(line, args.raw)
        if not args.follow:
            return
        # Then follow
        while True:
            line = f.readline()
            if line:
                _print_event(line, args.raw)
            else:
                time.sleep(0.2)

def cmd_cat(args):
    store = SessionStore()
    path = store.transcript_path(args.session_id)
    sys.stdout.buffer.write(path.read_bytes())

def _print_event(line, raw):
    if raw:
        sys.stdout.write(line)
        return
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        sys.stdout.write(line); return
    t = ev.get("type", "?")
    if t == "assistant":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text":
                print(f"[assistant] {block['text']}")
            elif block.get("type") == "tool_use":
                print(f"[tool_use] {block['name']}({json.dumps(block.get('input', {}))[:120]})")
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

def _ago(ts_ms):
    delta_s = int((time.time() * 1000 - ts_ms) / 1000)
    if delta_s < 60: return f"{delta_s}s ago"
    if delta_s < 3600: return f"{delta_s // 60}m ago"
    if delta_s < 86400: return f"{delta_s // 3600}h ago"
    return f"{delta_s // 86400}d ago"

def main():
    p = argparse.ArgumentParser(prog="tend")
    sub = p.add_subparsers(dest="cmd", required=True)
    sessions = sub.add_parser("sessions").add_subparsers(dest="action", required=True)

    pl = sessions.add_parser("list"); pl.set_defaults(func=cmd_list)
    pl.add_argument("--limit", type=int, default=10)
    pl.add_argument("--status", choices=["running","done","failed","killed"])
    pl.add_argument("--json", action="store_true")

    ps = sessions.add_parser("show"); ps.set_defaults(func=cmd_show)
    ps.add_argument("session_id")
    ps.add_argument("--json", action="store_true")

    pt = sessions.add_parser("tail"); pt.set_defaults(func=cmd_tail)
    pt.add_argument("session_id")
    pt.add_argument("--follow", action="store_true")
    pt.add_argument("--raw", action="store_true",
                    help="Print raw JSONL instead of pretty-printed events")

    pc = sessions.add_parser("cat"); pc.set_defaults(func=cmd_cat)
    pc.add_argument("session_id")

    args = p.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
```

Registered as a console script in `pyproject.toml`:

```toml
[project.scripts]
tend = "tend.cli:main"
```

So after `pip install -e .`, you have `tend sessions list` available globally; running from a fresh checkout, `python -m tend.cli sessions list` works the same.

### `tend snapshot` — what's available in Claude Code right now

The hardest part of writing a `[workers.<name>]` block is knowing what to put in `allowed_tools`. The MCP server name needs to match what `claude mcp add ...` registered, the tool prefix follows a convention you have to remember, and the native tool list grows with every Claude Code release.

So `tend snapshot` introspects your local Claude Code installation and writes a copy-pasteable cheat-sheet:

```
$ tend snapshot
Wrote ~/.tend/claude-env.md
```

The file looks like this:

```markdown
# Claude Code Environment Snapshot

Generated: 2026-05-06 14:23 (claude --version: 1.x.y)

## MCP servers (`claude mcp list`)

| Name             | Tool prefix              | Scope     | Status     |
|------------------|--------------------------|-----------|------------|
| google-calendar  | mcp__google_calendar__   | user      | running    |
| google-sheets    | mcp__google_sheets__     | user      | running    |
| linear           | mcp__linear__            | project   | not running|

## Skills (~/.claude/skills/)
- brainstorming
- writing-plans
- executing-plans
- ...

## Agents (~/.claude/agents/)
- code-reviewer
- general-purpose
- ...

## Commands (~/.claude/commands/)
- review
- security-review
- ...

## Native Claude Code tools
Read, Edit, Write, Bash, Grep, Glob, WebSearch, WebFetch, NotebookEdit, Task, TodoWrite

## Suggested tend.toml additions

For a meal-planning worker:

\`\`\`toml
[workers.meal_plan]
model = "claude-sonnet-4-6"
setting_sources = "user"
allowed_tools = [
  "mcp__google_calendar__*",
  "mcp__google_sheets__*",
]
\`\`\`

For a coding worker with full tool access:

\`\`\`toml
[workers.coding]
model = "claude-opus-4-7"
setting_sources = "user,project,local"
allowed_tools = ["Read", "Edit", "Write", "Bash", "Grep", "Glob", "mcp__*"]
\`\`\`
```

(The triple-backticks above are literal in the generated file — they're what the user copy-pastes into `tend.toml`.)

Implementation, added to `src/tend/cli.py`:

```python
def cmd_snapshot(args):
    from datetime import datetime
    import shutil

    if not shutil.which("claude"):
        print("`claude` CLI not found on PATH. Install Claude Code first.", file=sys.stderr)
        sys.exit(1)

    sections = []
    sections.append(f"# Claude Code Environment Snapshot\n")
    sections.append(f"Generated: {datetime.now().isoformat(timespec='minutes')} "
                    f"(claude --version: {_run_capture('claude', '--version')})\n")

    # MCP servers — try JSON output first, fall back to text parse
    sections.append("## MCP servers (`claude mcp list`)\n")
    sections.append(_format_mcp_list())

    # Filesystem-resident config
    home_claude = Path.home() / ".claude"
    for label, subdir in [("Skills", "skills"), ("Agents", "agents"), ("Commands", "commands")]:
        sections.append(f"## {label} ({home_claude}/{subdir}/)\n")
        d = home_claude / subdir
        items = sorted(p.name for p in d.iterdir() if p.is_dir() or p.suffix == ".md") if d.exists() else []
        sections.append("\n".join(f"- {it}" for it in items) if items else "_(none configured)_")
        sections.append("")

    # Native tools — curated static list, version-stamped
    sections.append("## Native Claude Code tools\n")
    sections.append("Read, Edit, Write, Bash, Grep, Glob, WebSearch, WebFetch, "
                    "NotebookEdit, Task, TodoWrite")

    # Worked examples for tend.toml
    sections.append("\n## Suggested tend.toml additions\n")
    sections.append(_render_suggestions())

    out = Path.home() / ".tend" / "claude-env.md"
    out.write_text("\n".join(sections))
    print(f"Wrote {out}")

def _run_capture(*cmd) -> str:
    import subprocess
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return (r.stdout or "").strip().splitlines()[0] if r.returncode == 0 else "(error)"

def _format_mcp_list() -> str:
    """Run `claude mcp list` and return a markdown table."""
    import subprocess
    # Try JSON first; fall back to plaintext parsing.
    r = subprocess.run(["claude", "mcp", "list", "--json"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        try:
            servers = json.loads(r.stdout)
            return _mcp_table(servers)
        except json.JSONDecodeError:
            pass
    r = subprocess.run(["claude", "mcp", "list"],
                       capture_output=True, text=True, timeout=10)
    return f"```\n{r.stdout.strip()}\n```" if r.returncode == 0 else "_(`claude mcp list` failed)_"
```

Then a new subparser:

```python
psnap = sub.add_parser("snapshot", help="Snapshot the local Claude Code environment.")
psnap.set_defaults(func=cmd_snapshot)
```

So `tend snapshot` becomes a sibling of `tend sessions ...` at the top level.

### What it deliberately doesn't do

- **No cancellation.** The CLI is read-only. Killing a running claude subprocess belongs to the daemon, not a separate process — adding a `cancel` verb here would either need IPC or have it kill its own sibling, which is fiddly. Cancellation gets a Brain tool (`cancel_session`) when we add it.
- **No live status of the daemon itself.** That's `journalctl --user -u tend` and `systemctl --user status tend` — already covered.
- **No log search.** Use `grep` on `/tmp/tend.log` or `~/.tend/sessions/*.jsonl`.
- **No automatic `tend.toml` editing.** `tend snapshot` writes a reference document; the user (or a follow-up command later) does the toml edits. We don't want a CLI silently mutating committed config.

The CLI is the small, sharp tool for "tell me what happened with workers" and "tell me what Claude Code knows about." Big-picture daemon health stays in systemd / loguru land.

---

## Multi-turn: continuing a job later

Claude Code supports resuming a previous session with `--resume <session-id>`. Pass the same flag and the model picks up with full context, no re-sending the conversation.

The flow:

1. The first run of `MealPlanWorker.plan(...)` finishes. The session ID lives in `~/.tend/sessions.json` and was also captured by Brain when the worker announced its result (`task_update` carries it).
2. You say *"actually, swap Tuesday for something with more protein."*
3. Brain's LLM has the session ID in its context (recent assistant message) and calls `continue_session(session_id="7f3a8e2d4c5b1a93", follow_up="swap Tuesday for higher protein").`
4. The continuation tool dispatches a task to `MealPlanWorker`, which runs `run_claude(ClaudeRunSpec(prompt="swap Tuesday for higher protein", resume_session_id="7f3a8e2d4c5b1a93"))`. No system prompt this time — it's already in the resumed session.
5. Claude works inside the same session, with full memory of the original plan. New version is written to the same Sheet; transcript file gets appended.

If you ask the next day, Brain may not have the session ID in its current context (it was wiped at 4 a.m.). It can still call `list_recent_jobs` to find it. So both fast follow-ups (within the same conversation) and slow follow-ups (across day-session resets) work.

---

## Privacy and billing properties that fall out of this

- **Claude calls go through the `claude` CLI**, which authenticates with the OAuth credentials at `~/.claude/.credentials.json`. Pro/Max plan quota applies. No API key is ever set in the env we hand to the subprocess.
- **Workers run while Brain is asleep**, by design. That's already the documented privacy model: the wake gate protects ambient room audio from ever reaching the cloud, but workers running in the background talk to whatever services the user dispatched them to. A meal-plan worker hits Google Sheets and Anthropic; that's expected.
- **Transcripts are stored locally** under `~/.tend/sessions/`. They include the full prompt and Claude's full output. Anyone with read access to the Pi user account can read them. That's the right tradeoff for a single-user device; multi-user changes the calculus and isn't on the v1 list.
- **Claude's environment is the user's**: skills, agents, MCPs, settings the user has configured at `~/.claude/` all flow through to every worker session automatically. The user controls that surface; the worker doesn't try to hide it.

---

## What's in v1 / what's deferred

**In:**
- `ClaudeCliWorker` base class, with subprocess spawning, env scrub, JSONL stream parsing, transcript archival, and resume support.
- `SessionStore` (atomic JSON index + per-session transcript files + system-prompt files).
- One concrete subclass — probably `CodingWorker` — to validate the pattern end-to-end with the existing voice channel.
- Three new Brain tools: `list_recent_jobs`, `session_status`, `continue_session`.
- Per-worker config in `tend.toml` (`[workers.<name>]` blocks → model, setting_sources, allowed_tools).
- A `tend` CLI:
  - `tend sessions list | show | tail | cat` for manual session inspection.
  - `tend snapshot` to write a copy-pasteable cheat-sheet of the local Claude Code environment (MCPs, skills, agents, commands, native tools, suggested `tend.toml` snippets) to `~/.tend/claude-env.md`.
- Boot preflight: log a warning if `claude --version` or `claude auth status` fails. Tend startup proceeds either way — workers that don't need claude (Reminder) keep working; claude-cli workers will return a clear error on first dispatch.

**Deferred (separate brainstorms):**
- Concrete `MealPlanWorker`, `FitnessPlanWorker`, `RoutinePlanningWorker`. The base + one validation worker is enough to lock the pattern; the rest are content-design problems more than systems-design ones.
- A tend-owned MCP server that exposes tend-specific tools (read soul.md memory, dispatch a sub-worker, etc.). Adds value but isn't required to prove the architecture.
- Telegram channel. Today the only channel is voice; Telegram needs its own brainstorm covering input handling, message threading, and per-channel session keys.
- Data collectors (Gmail watch, smart-scale BLE, Google Sheets polling). Each is its own integration with its own auth.
- Dashboard. Read-only UI; depends on the session store being stable enough to read against.
- Cleanup/compaction of old sessions. At single-digit jobs per day, sessions.json stays small for years. Revisit if it grows.
- Cancellation (`cancel_session`). Easy to add but not core to the v1 happy path.

---

## Open questions before implementation

1. **Concurrent claude sessions.** Can two workers spawn `claude` at the same time without OAuth-token contention? Almost certainly yes (the credentials file is read-only at session start and tokens auto-refresh), but worth a quick test before we ship.
2. **Should Brain be allowed to dispatch to a worker that already has an in-flight session for the same workflow?** v1 default: yes, allow concurrency, no global lock. If it turns out two meal-plan jobs racing produces noisy Sheet edits, we add per-worker mutexes.
3. **Setting-sources policy per worker.** `CodingWorker` probably wants `user,project,local` so a project's CLAUDE.md is loaded; everything else probably wants `user`. Worth making it a `ClaudeRunSpec` field with a sensible per-worker default.
4. **Default `allowed_tools` when a worker has no config block.** *Resolved (deferred-strict): for v1, an empty `allowed_tools` list silently means "no `--allowedTools` flag", which gives claude its full default tool surface. To prevent this becoming an invisible footgun, `run_claude` emits a loud warning whenever it spawns claude with `allowed_tools=[]`. Boot does not error out, so a fresh worker still works in development. Revisit if a real workflow ships with a missing config block in production.*





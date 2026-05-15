"""Claude-CLI worker base + helpers.

Workers in this module spawn the `claude` CLI as a subprocess so that API
calls bill against the user's Claude Pro/Max plan (subscription) rather
than per-token via the API. The trick: we never set ANTHROPIC_API_KEY
in the subprocess env — `claude` then falls through to its own OAuth
credentials at ~/.claude/.credentials.json.

See docs/superpowers/specs/2026-05-06-claude-cli-workers-design.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from pipecat_subagents.agents import BaseAgent
from pipecat_subagents.bus import AgentBus

from tend.sessions import SessionEntry, SessionStore


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
                                        # resume_session_id is unset, run_claude
                                        # generates a fresh uuid.
    model: str | None = None
    # One of: default, acceptEdits, auto, bypassPermissions, dontAsk, plan.
    # None → omit the flag (claude falls back to its own default 'default').
    permission_mode: str | None = None


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
    if spec.permission_mode:
        args += ["--permission-mode", spec.permission_mode]
    # Caller decides whether to write a system prompt (i.e. whether to pass
    # `system_prompt_path`). _build_args only honours the path it's given,
    # gated by the resume rule (resumed sessions already have the prompt).
    if system_prompt_path and not spec.resume_session_id:
        args += ["--append-system-prompt-file", str(system_prompt_path)]
    if spec.mcp_config_path:
        args += ["--mcp-config", str(spec.mcp_config_path)]
    if spec.model:
        args += ["--model", spec.model]
    return args


def _scrubbed_env(env_in: dict[str, str]) -> dict[str, str]:
    """Return a copy of env_in with the dangerous keys removed.

    Also ensure the current interpreter's bin directory is on PATH so
    venv entry points (like `tend`) resolve in claude's Bash subshell.
    Under systemd, ExecStart invokes `<venv>/bin/python` directly without
    activating the venv, so `<venv>/bin` is not on PATH by default — and
    `tend scan-skill` (which `_GENERAL_PREAMBLE` instructs claude to call)
    would fail with "command not found". We use `Path(sys.executable).parent`
    rather than `.resolve().parent` because the venv's `python` is typically
    a symlink to the system interpreter; resolving it would point us at
    `/usr/bin` and miss the venv shims entirely.
    """
    out = {k: v for k, v in env_in.items() if k not in CLAUDE_CLI_CLEAR_ENV}
    py_bin = str(Path(sys.executable).parent)
    existing_path = out.get("PATH", "")
    path_parts = existing_path.split(os.pathsep) if existing_path else []
    if py_bin and py_bin not in path_parts:
        out["PATH"] = py_bin + (os.pathsep + existing_path if existing_path else "")
    return out


async def _consume_stream(
    stdout: AsyncIterator[bytes],
    transcript_path: Path,
) -> tuple[str, dict]:
    """Read claude's JSONL event stream. Tee to disk, extract the LAST
    assistant turn's text and the trailing `result` event's usage dict.

    User events (which carry tool_result blocks) mark turn boundaries, so
    we reset the text buffer on each one. That way `final_parts` only holds
    the assistant's last response — what TTS should speak — rather than the
    concatenation of every narration chunk across a multi-step tool loop.
    """
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
            if t == "user":
                # Turn boundary — drop intermediate narration so we keep only
                # the last assistant turn's text.
                final_parts.clear()
            elif t == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        final_parts.append(block.get("text", ""))
            elif t == "result":
                usage = {
                    "total_cost_usd": ev.get("total_cost_usd"),
                    **(ev.get("usage") or {}),
                }
    return ("".join(final_parts).strip(), usage)


async def _drain_stderr(stderr: AsyncIterator[bytes] | None) -> str:
    """Read claude's stderr to a string. Bounded so a runaway process can't
    blow up memory. Returning '' on cancellation/None is fine — stderr is
    only used to enrich error messages, never as primary signal."""
    if stderr is None:
        return ""
    chunks: list[bytes] = []
    total = 0
    LIMIT = 64 * 1024  # 64 KiB is plenty for an error message.
    try:
        async for line in stderr:
            chunks.append(line)
            total += len(line)
            if total >= LIMIT:
                break
    except asyncio.CancelledError:
        return b"".join(chunks).decode("utf-8", errors="replace")
    return b"".join(chunks).decode("utf-8", errors="replace")


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
        subprocess_factory: Callable[..., Awaitable[Any]] | None = None,
    ):
        super().__init__(name, bus=bus)
        self._store = store
        # Default factory is asyncio.create_subprocess_exec — passes args as a
        # list, no shell, no injection risk.
        self._subprocess_factory = (
            subprocess_factory or asyncio.create_subprocess_exec
        )

    async def run_claude(self, spec: ClaudeRunSpec) -> SessionEntry:
        # claude --session-id requires a canonical UUID (8-4-4-4-12 with
        # dashes); the hex-only form is rejected. Use str(), not .hex.
        session_id = (
            spec.resume_session_id or spec.session_id or str(uuid.uuid4())
        )

        # 1. Persist a "running" record before doing anything risky.
        if spec.resume_session_id:
            # Preserve prior fields (spoken_summary, cost_usd, ended_at, error).
            try:
                self._store.touch(session_id)
            except KeyError:
                # Stale resume id — fail clearly instead of fabricating a row.
                raise RuntimeError(
                    f"Cannot resume session {session_id!r}: not in store. "
                    "It may have been pruned, or never existed."
                )
        else:
            self._store.start(
                session_id=session_id,
                worker=self.name,
                request=spec.prompt[:200],
                cwd=str(spec.cwd) if spec.cwd else None,
            )

        # 1a. Log the resolved permission posture once per spawn. The
        # daemon's default is permission_mode='bypassPermissions' with no
        # allowlist — necessary because no human is available to approve
        # interactive prompts. Surface it loudly the first time so users
        # who don't realise can tighten by setting [workers.<name>]
        # allowed_tools (and optionally permission_mode='default') in
        # tend.toml.
        if not spec.resume_session_id:
            from loguru import logger
            if spec.permission_mode == "bypassPermissions" and not spec.allowed_tools:
                logger.warning(
                    f"{self.name}: spawning claude with "
                    "permission_mode=bypassPermissions and no allowedTools "
                    "restriction. Claude has full tool access without prompts. "
                    "To restrict: set [workers.<name>].permission_mode='default' "
                    "and populate allowed_tools in tend.toml."
                )
            else:
                logger.info(
                    f"{self.name}: spawning claude "
                    f"permission_mode={spec.permission_mode or 'default'} "
                    f"allowed_tools={spec.allowed_tools or '(none)'}"
                )

        # 2. System prompt → file (only on first run).
        sys_path = None
        if spec.system_prompt and not spec.resume_session_id:
            sys_path = self._store.write_system_prompt(session_id, spec.system_prompt)

        # 3. Build args + scrub env.
        args = _build_args(spec, session_id=session_id, system_prompt_path=sys_path)
        env = _scrubbed_env(dict(os.environ))

        # 4. Spawn.
        proc = None
        try:
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

            # 6. Stream + persist final outcome. We read stderr concurrently
            # so claude's actual error message (e.g. "Invalid session ID")
            # surfaces in the failure record — and so the pipe buffer can't
            # fill and deadlock claude on a verbose stderr.
            stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))
            try:
                transcript = self._store.transcript_path(session_id)
                final_text, usage = await _consume_stream(proc.stdout, transcript)
                rc = await proc.wait()
                stderr_text = await stderr_task
                if rc != 0:
                    detail = stderr_text.strip().splitlines()[-3:]  # last few lines
                    detail_str = " | ".join(detail) if detail else ""
                    msg = (
                        f"claude exited with code {rc}"
                        + (f": {detail_str}" if detail_str else "")
                    )
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
                except OSError:
                    pass  # already-dead / permission / etc — don't mask the real error.
                if not stderr_task.done():
                    stderr_task.cancel()
                self._store.complete(session_id, status="failed", error=str(e))
                raise
        finally:
            # CancelledError (BaseException, not Exception) bypasses the
            # except blocks above. Make sure we never leave a session
            # "running" or a subprocess orphaned on shutdown.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                current = self._store.get(session_id)
                if current and current.status == "running":
                    self._store.complete(
                        session_id, status="killed", error="task cancelled",
                    )

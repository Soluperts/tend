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
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

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
    """Return a copy of env_in with the dangerous keys removed."""
    return {k: v for k, v in env_in.items() if k not in CLAUDE_CLI_CLEAR_ENV}


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

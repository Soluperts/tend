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


def _scrubbed_env(env_in: dict) -> dict:
    """Return a copy of env_in with the dangerous keys removed."""
    return {k: v for k, v in env_in.items() if k not in CLAUDE_CLI_CLEAR_ENV}

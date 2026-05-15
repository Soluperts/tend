# SPDX-License-Identifier: MIT
"""`tend snapshot` — record the local Claude Code environment."""

from __future__ import annotations

import shutil
import sys
from datetime import datetime

import typer

from tend import paths
from tend.cli._shared import (
    claude_home, claude_version, list_dir_md, mcp_list_markdown,
)


def snapshot_command() -> None:
    """Snapshot the local Claude Code environment to $TEND_HOME/claude-env.md."""
    if not shutil.which("claude"):
        print(
            "`claude` CLI not found on PATH. Install Claude Code first.",
            file=sys.stderr,
        )
        raise typer.Exit(code=1)

    home = claude_home()
    sections: list[str] = []
    sections.append("# Claude Code Environment Snapshot\n")
    sections.append(
        f"Generated: {datetime.now().isoformat(timespec='minutes')} "
        f"(claude --version: {claude_version()})\n"
    )
    sections.append("## MCP servers (`claude mcp list`)\n")
    sections.append(mcp_list_markdown())
    sections.append("")

    for label, sub in [
        ("Skills", "skills"),
        ("Agents", "agents"),
        ("Commands", "commands"),
    ]:
        sections.append(f"## {label} ({home}/{sub}/)\n")
        sections.append(list_dir_md(home / sub))
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
        "For the general worker with full tool access:\n\n"
        "```toml\n"
        "[workers.general]\n"
        'model = "claude-opus-4-7"\n'
        'setting_sources = "user,project,local"\n'
        'allowed_tools = ["Read", "Edit", "Write", "Bash", "Grep", "Glob", "mcp__*"]\n'
        "```\n"
    )

    out = paths.tend_home() / "claude-env.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(sections))
    print(f"Wrote {out}")

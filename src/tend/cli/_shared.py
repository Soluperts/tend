# SPDX-License-Identifier: MIT
"""Helpers shared by tend CLI command modules."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import typer

from tend import paths
from tend.cron_store import CronStore
from tend.sessions import SessionStore


def store() -> SessionStore:
    return SessionStore(root=paths.tend_home())


def cron_store() -> CronStore:
    return CronStore(root=paths.tend_home())


def ago(ts_ms: int) -> str:
    delta_s = int((time.time() * 1000 - ts_ms) / 1000)
    if delta_s < 60:
        return f"{delta_s}s ago"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    if delta_s < 86400:
        return f"{delta_s // 3600}h ago"
    return f"{delta_s // 86400}d ago"


def validate_skill_name(name: str) -> str:
    """Reject empty, slashes, traversal, or dotfiles. Raises typer.Exit on bad input."""
    if not name or "/" in name or ".." in name or name.startswith("."):
        typer.echo(f"invalid skill name: {name!r}", err=True)
        raise typer.Exit(code=2)
    return name


def resolve_session(store_: SessionStore, prefix: str):
    rows = store_.list_recent(limit=10000)
    return next((r for r in rows if r.session_id.startswith(prefix)), None)


def print_event(line: str, raw: bool) -> None:
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
                print(
                    f"[tool_use] {block.get('name')}"
                    f"({json.dumps(block.get('input', {}))[:120]})"
                )
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


def claude_home() -> Path:
    return Path.home() / ".claude"


def claude_version() -> str:
    r = subprocess.run(
        ["claude", "--version"], capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return "(unknown)"
    return r.stdout.strip().splitlines()[0] if r.stdout else "(unknown)"


def mcp_table(servers) -> str:
    lines = ["| Name | Tool prefix | Scope | Status |", "|---|---|---|---|"]
    for s in servers or []:
        name = s.get("name", "?")
        scope = s.get("scope", "?")
        status = s.get("status", "?")
        prefix = f"mcp__{name.replace('-', '_')}__"
        lines.append(f"| {name} | {prefix} | {scope} | {status} |")
    if len(lines) == 2:
        return "_(no MCP servers configured)_"
    return "\n".join(lines)


def mcp_list_markdown() -> str:
    """Run `claude mcp list` and return a markdown table. JSON form preferred."""
    j = subprocess.run(
        ["claude", "mcp", "list", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    if j.returncode == 0:
        try:
            data = json.loads(j.stdout)
            if isinstance(data, dict):
                servers = data.get("servers")
            elif isinstance(data, list):
                servers = data
            else:
                servers = None
            if servers is not None:
                try:
                    return mcp_table(servers)
                except (TypeError, AttributeError):
                    pass
        except json.JSONDecodeError:
            pass
    t = subprocess.run(
        ["claude", "mcp", "list"], capture_output=True, text=True, timeout=10,
    )
    if t.returncode == 0:
        return f"```\n{t.stdout.strip() or '(empty)'}\n```"
    return "_(`claude mcp list` failed)_"


def list_dir_md(d: Path) -> str:
    if not d.exists():
        return "_(none configured)_"
    items = sorted(
        p.stem for p in d.iterdir()
        if p.is_dir() or p.suffix == ".md"
    )
    return "\n".join(f"- {it}" for it in items) if items else "_(none configured)_"

"""Tend CLI — read-only inspection of worker sessions.

`tend sessions list/show/tail/cat` reads the on-disk SessionStore. Runs as
a separate process from the daemon; no IPC.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from tend.sessions import SessionStore
from tend.skills import enumerate_skills


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
        if not args.follow:
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
    path = Path(match.transcript_path)
    if not path.exists():
        print(f"Transcript not found: {path}", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(path.read_text())


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


def _claude_home() -> Path:
    return Path.home() / ".claude"


def _claude_version() -> str:
    r = subprocess.run(
        ["claude", "--version"], capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return "(unknown)"
    return r.stdout.strip().splitlines()[0] if r.stdout else "(unknown)"


def _mcp_table(servers) -> str:
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


def _mcp_list_markdown() -> str:
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
                    return _mcp_table(servers)
                except (TypeError, AttributeError):
                    pass  # malformed shape — fall through to plain-text
        except json.JSONDecodeError:
            pass
    t = subprocess.run(
        ["claude", "mcp", "list"], capture_output=True, text=True, timeout=10,
    )
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
        print(
            "`claude` CLI not found on PATH. Install Claude Code first.",
            file=sys.stderr,
        )
        sys.exit(1)

    home = _claude_home()
    sections: list[str] = []
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
        "For the general worker with full tool access:\n\n"
        "```toml\n"
        "[workers.general]\n"
        'model = "claude-opus-4-7"\n'
        'setting_sources = "user,project,local"\n'
        'allowed_tools = ["Read", "Edit", "Write", "Bash", "Grep", "Glob", "mcp__*"]\n'
        "```\n"
    )

    out = _default_root() / "claude-env.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(sections))
    print(f"Wrote {out}")


def _skills_root() -> Path:
    """Resolve the skills root, env var first, then ~/.tend/skills."""
    override = os.environ.get("TEND_SKILLS_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".tend" / "skills"


def _skills_quarantine_root() -> Path:
    """Resolve the quarantine root, env var first, then ~/.tend/skills-quarantined."""
    override = os.environ.get("TEND_SKILLS_QUARANTINE_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".tend" / "skills-quarantined"


def _validate_skill_name(name: str) -> str:
    """Reject empty, slashes, traversal, or dotfiles. Raises SystemExit on bad input."""
    if not name or "/" in name or ".." in name or name.startswith("."):
        raise SystemExit(f"invalid skill name: {name!r}")
    return name


def cmd_skills_list(args) -> int:
    skills = enumerate_skills(_skills_root())
    if not skills:
        print("No skills installed.")
        return 0
    for s in skills:
        print(f"{s.name} — {s.description}")
    return 0


def cmd_skills_show(args) -> int:
    name = _validate_skill_name(args.name)
    skill_md = _skills_root() / name / "SKILL.md"
    if not skill_md.is_file():
        print(f"No skill named {name!r}", file=sys.stderr)
        return 2
    sys.stdout.write(skill_md.read_text(encoding="utf-8"))
    return 0


def cmd_skills_cat(args) -> int:
    # In v1, cat is an alias for show — both dump the raw SKILL.md.
    return cmd_skills_show(args)


def cmd_skills_rm(args) -> int:
    name = _validate_skill_name(args.name)
    target = _skills_root() / name
    if not target.is_dir():
        print(f"No skill named {name!r}", file=sys.stderr)
        return 2
    shutil.rmtree(target)
    return 0


def cmd_skills_quarantined(args) -> int:
    root = _skills_quarantine_root()
    entries = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    if not entries:
        print("No quarantined skills.")
        return 0
    for entry in entries:
        print(entry.name)
        findings_path = entry / "_findings.json"
        if not findings_path.is_file():
            print("  (no _findings.json)")
            continue
        try:
            findings = json.loads(findings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  (could not parse _findings.json: {e})")
            continue
        if not findings:
            print("  (no findings recorded)")
            continue
        for f in findings:
            rule = f.get("rule", "?")
            severity = f.get("severity", "?")
            line = f.get("line", "?")
            snippet = f.get("snippet", "")
            print(f"  [{severity}] {rule} (line {line}): {snippet}")
    return 0


def cmd_scan_skill(args) -> int:
    from tend.skills import scan_text
    name = _validate_skill_name(args.name)
    p = _skills_root() / name / "SKILL.md"
    if not p.is_file():
        print(f"no skill named {name!r}", file=sys.stderr)
        return 3
    report = scan_text(p.read_text(encoding="utf-8"))
    if report.is_clean:
        print(f"{name}: clean")
        return 0
    for f in report.findings:
        print(f"  [{f.severity}] {f.rule} (line {f.line}): {f.snippet}")
    return 2 if report.is_critical else 1


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

    psnap = sub.add_parser("snapshot", help="Snapshot the local Claude Code environment.")
    psnap.set_defaults(func=cmd_snapshot)

    scan = sub.add_parser("scan-skill", help="Run the safety scanner over a skill.")
    scan.add_argument("name")
    scan.set_defaults(func=cmd_scan_skill)

    skills = sub.add_parser("skills").add_subparsers(dest="action", required=True)

    sk_list = skills.add_parser("list"); sk_list.set_defaults(func=cmd_skills_list)

    sk_show = skills.add_parser("show"); sk_show.set_defaults(func=cmd_skills_show)
    sk_show.add_argument("name")

    sk_cat = skills.add_parser("cat"); sk_cat.set_defaults(func=cmd_skills_cat)
    sk_cat.add_argument("name")

    sk_rm = skills.add_parser("rm"); sk_rm.set_defaults(func=cmd_skills_rm)
    sk_rm.add_argument("name")

    sk_q = skills.add_parser("quarantined"); sk_q.set_defaults(func=cmd_skills_quarantined)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    rc = args.func(args)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    main()

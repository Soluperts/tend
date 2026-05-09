"""Tend CLI — inspect on-disk state of the tend voice assistant.

Subcommands:
- `tend sessions ...` — read worker session history from ~/.tend/sessions/.
- `tend skills ...` — list / show / remove installed skills under ~/.tend/skills/.
- `tend scan-skill <name>` — run the safety scanner over a skill.
- `tend snapshot` — record the local Claude Code environment to ~/.tend/claude-env.md.

Runs as a separate process from the daemon; no IPC.
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
    override = os.environ.get("TEND_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".tend"


def _store() -> SessionStore:
    return SessionStore(root=_default_root())


def _cron_store():
    from tend.cron_store import CronStore
    return CronStore(root=_default_root())


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


def cmd_skills_enable_triggers(args) -> int:
    """Print the schedules the running daemon would create. The daemon
    itself owns the schedules; the CLI is a planning aid (no IPC)."""
    name = _validate_skill_name(args.name)
    from tend.skills import enumerate_skills
    skills = [s for s in enumerate_skills(_skills_root()) if s.name == name]
    if not skills:
        print(f"No skill named {name!r}", file=sys.stderr)
        return 2
    info = skills[0]
    if not info.triggers:
        print(f"{name}: no triggers declared in frontmatter")
        return 0
    print(f"{name}: {len(info.triggers)} trigger(s) — ask the running daemon")
    print("to enable via voice ('enable triggers for x') or by editing")
    print("~/.tend/cron/jobs.json directly while tend is stopped.")
    for t in info.triggers:
        print(f"  - {t}")
    return 0


def cmd_skills_disable_triggers(args) -> int:
    name = _validate_skill_name(args.name)
    store = _cron_store()
    rows = store.find_by_source(f"skill:{name}")
    if not rows:
        print(f"{name}: no active triggers")
        return 0
    count = store.remove_by_source(f"skill:{name}")
    print(f"{name}: removed {count} trigger(s)")
    return 0


def cmd_webhook_test(args) -> int:
    import urllib.request
    token = os.environ.get("TEND_WEBHOOK_TOKEN")
    if not token:
        print(
            "TEND_WEBHOOK_TOKEN is not set; cannot test the webhook.",
            file=sys.stderr,
        )
        return 2
    from tend.config import settings
    url = f"http://{settings.webhook.host}:{settings.webhook.port}/say"
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps({
            "text": "tend webhook test",
            "category": "test",
            "urgent": True,
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            print(resp.read().decode())
        return 0
    except Exception as e:
        print(f"webhook unreachable: {e}", file=sys.stderr)
        return 1


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


def cmd_schedule_list(args) -> int:
    store = _cron_store()
    jobs = store.load_jobs()
    if not jobs:
        print("No schedules.")
        return 0
    for j in jobs:
        state = store.get_state(j.id)
        nfa = state.next_run_at or "(unknown)"
        print(
            f"{j.name:<24} {j.kind:<6} {j.schedule:<22} "
            f"source={j.source:<22} next={nfa}"
        )
    return 0


def cmd_schedule_show(args) -> int:
    store = _cron_store()
    name_or_id = args.name_or_id
    match = next(
        (j for j in store.load_jobs()
         if j.name == name_or_id or j.id.startswith(name_or_id)),
        None,
    )
    if match is None:
        print(f"No schedule matching {name_or_id!r}", file=sys.stderr)
        return 2
    print(f"id          {match.id}")
    print(f"name        {match.name}")
    print(f"kind        {match.kind}")
    print(f"schedule    {match.schedule}")
    print(f"tz          {match.tz}")
    print(f"source      {match.source}")
    print(f"enabled     {match.enabled}")
    print(f"created_at  {match.created_at}")
    print(f"payload     {json.dumps(match.payload)}")
    state = store.get_state(match.id)
    print(f"last_run    {state.last_run_at} ({state.last_run_status})")
    print(f"next_run    {state.next_run_at}")
    if state.last_error:
        print(f"last_error  {state.last_error}")
    return 0


def cmd_schedule_add(args) -> int:
    from tend.cron_time import InvalidWhen, parse_when, next_fire_at
    import datetime as _dt
    from zoneinfo import ZoneInfo

    if args.event and args.request:
        print(
            "--event and --request are mutually exclusive.",
            file=sys.stderr,
        )
        return 2
    if args.event and not args.payload:
        print("--event requires --payload (use '{}' for empty).", file=sys.stderr)
        return 2
    if not args.event and not args.request:
        print("either --request or --event is required.", file=sys.stderr)
        return 2

    event_payload: dict | None = None
    if args.event:
        try:
            event_payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"--payload is not valid JSON: {e}", file=sys.stderr)
            return 2
        if not isinstance(event_payload, dict):
            print("--payload must be a JSON object.", file=sys.stderr)
            return 2

    store = _cron_store()
    try:
        kind, schedule = parse_when(args.when)
    except InvalidWhen as e:
        print(f"invalid --when: {e}", file=sys.stderr)
        return 2

    if args.event:
        payload = {}
    else:
        payload = {"request": args.request}

    job = store.add_job(
        name=args.name, kind=kind, schedule=schedule,
        tz=args.tz or "UTC",
        payload=payload,
        source=args.source, enabled=True,
        event_kind=args.event,
        event_payload=event_payload,
    )
    nfa = next_fire_at(
        kind, schedule, args.tz or "UTC",
        _dt.datetime.now(tz=ZoneInfo("UTC")),
    )
    from tend.cron_store import JobState
    store.set_state(job.id, JobState(next_run_at=nfa.isoformat()))
    print(f"added {job.id[:8]} {job.name}")
    return 0


def cmd_schedule_rm(args) -> int:
    store = _cron_store()
    name_or_id = args.name_or_id
    match = next(
        (j for j in store.load_jobs()
         if j.name == name_or_id or j.id.startswith(name_or_id)),
        None,
    )
    if match is None:
        print(f"No schedule matching {name_or_id!r}", file=sys.stderr)
        return 2
    store.remove_job(match.id)
    store.remove_state(match.id)
    print(f"removed {match.name}")
    return 0


_TOP_EPILOG = """\
Environment variables:
  TEND_SKILLS_ROOT             Override skills root (default: ~/.tend/skills)
  TEND_SKILLS_QUARANTINE_ROOT  Override quarantine root
                               (default: ~/.tend/skills-quarantined)

Run `tend <command> --help` for command-specific options.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tend",
        description=(
            "tend — inspect and manage the on-disk state of the tend voice "
            "assistant. Reads ~/.tend/ directly; does not talk to the running "
            "daemon."
        ),
        epilog=_TOP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(
        dest="cmd",
        required=True,
        title="commands",
        metavar="<command>",
    )

    # sessions ---------------------------------------------------------------
    sessions_p = sub.add_parser(
        "sessions",
        help="Inspect worker session history (read-only).",
        description=(
            "Read-only inspection of worker sessions stored under "
            "~/.tend/sessions/. A session is one claude-CLI subprocess run "
            "dispatched by the brain."
        ),
    )
    sessions = sessions_p.add_subparsers(
        dest="action", required=True, title="actions", metavar="<action>",
    )

    pl = sessions.add_parser(
        "list",
        help="List recent sessions, newest first.",
        description="List recent worker sessions as a table (or JSON).",
    )
    pl.set_defaults(func=cmd_sessions_list)
    pl.add_argument(
        "--limit", type=int, default=10,
        help="Maximum sessions to show (default: 10).",
    )
    pl.add_argument(
        "--status", choices=["running", "done", "failed", "killed"],
        help="Only show sessions with this status.",
    )
    pl.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of a table.",
    )

    ps = sessions.add_parser(
        "show",
        help="Show metadata for a single session.",
        description=(
            "Print stored metadata (worker, status, request, timestamps, "
            "transcript path) for one session."
        ),
    )
    ps.set_defaults(func=cmd_sessions_show)
    ps.add_argument(
        "session_id", help="Session id, or any unique prefix of one.",
    )
    ps.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of key/value lines.",
    )

    pt = sessions.add_parser(
        "tail",
        help="Pretty-print a session's transcript event by event.",
        description=(
            "Walk the session's claude-CLI JSONL transcript and pretty-print "
            "each event. Pair with --follow to stream a running session."
        ),
    )
    pt.set_defaults(func=cmd_sessions_tail)
    pt.add_argument(
        "session_id", help="Session id, or any unique prefix of one.",
    )
    pt.add_argument(
        "--follow", action="store_true",
        help="Stream new events as they arrive (like `tail -f`).",
    )
    pt.add_argument(
        "--raw", action="store_true",
        help="Print raw JSONL lines instead of pretty-printed events.",
    )

    pc = sessions.add_parser(
        "cat",
        help="Dump a session's transcript file to stdout.",
        description="Write the raw JSONL transcript file to stdout.",
    )
    pc.set_defaults(func=cmd_sessions_cat)
    pc.add_argument(
        "session_id", help="Session id, or any unique prefix of one.",
    )

    # snapshot ---------------------------------------------------------------
    psnap = sub.add_parser(
        "snapshot",
        help="Snapshot the local Claude Code environment.",
        description=(
            "Probe the local `claude` CLI (version, MCP servers, skills, "
            "agents, commands) and write a markdown summary to "
            "~/.tend/claude-env.md."
        ),
    )
    psnap.set_defaults(func=cmd_snapshot)

    # scan-skill -------------------------------------------------------------
    scan = sub.add_parser(
        "scan-skill",
        help="Run the safety scanner over an installed skill.",
        description=(
            "Read ~/.tend/skills/<name>/SKILL.md and run the regex safety "
            "scanner over it. Exit codes: 0=clean, 1=warnings only, "
            "2=critical findings (block), 3=skill not found."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scan.add_argument(
        "name", help="Skill name (folder under ~/.tend/skills/).",
    )
    scan.set_defaults(func=cmd_scan_skill)

    # skills -----------------------------------------------------------------
    skills_p = sub.add_parser(
        "skills",
        help="List, inspect, and remove installed skills.",
        description=(
            "Manage user-authored skills under ~/.tend/skills/. Skills are "
            "markdown SKILL.md files claude reads on demand to follow a "
            "recurring workflow."
        ),
    )
    skills = skills_p.add_subparsers(
        dest="action", required=True, title="actions", metavar="<action>",
    )

    sk_list = skills.add_parser(
        "list",
        help="List installed skills with their descriptions.",
        description="One line per skill: `<name> — <description>`.",
    )
    sk_list.set_defaults(func=cmd_skills_list)

    sk_show = skills.add_parser(
        "show",
        help="Print a skill's SKILL.md to stdout.",
        description="Dump the full SKILL.md (frontmatter + body) for one skill.",
    )
    sk_show.set_defaults(func=cmd_skills_show)
    sk_show.add_argument("name", help="Skill name (folder under ~/.tend/skills/).")

    sk_cat = skills.add_parser(
        "cat",
        help="Alias for `skills show`.",
        description="Same as `tend skills show`. Kept for muscle memory.",
    )
    sk_cat.set_defaults(func=cmd_skills_cat)
    sk_cat.add_argument("name", help="Skill name (folder under ~/.tend/skills/).")

    sk_rm = skills.add_parser(
        "rm",
        help="Delete an installed skill (irreversible).",
        description=(
            "Remove ~/.tend/skills/<name>/ and everything inside it. "
            "Does not touch quarantined skills."
        ),
    )
    sk_rm.set_defaults(func=cmd_skills_rm)
    sk_rm.add_argument("name", help="Skill name (folder under ~/.tend/skills/).")

    sk_q = skills.add_parser(
        "quarantined",
        help="List skills the safety scanner blocked.",
        description=(
            "List entries under ~/.tend/skills-quarantined/ along with the "
            "scanner findings recorded in each entry's _findings.json."
        ),
    )
    sk_q.set_defaults(func=cmd_skills_quarantined)

    sk_en = skills.add_parser(
        "enable-triggers",
        help="Show triggers a skill will activate (informational; the running "
             "daemon owns the schedule).",
    )
    sk_en.add_argument("name")
    sk_en.set_defaults(func=cmd_skills_enable_triggers)

    sk_dis = skills.add_parser(
        "disable-triggers",
        help="Remove all schedule entries previously enabled from a skill.",
    )
    sk_dis.add_argument("name")
    sk_dis.set_defaults(func=cmd_skills_disable_triggers)

    # schedule ---------------------------------------------------------------
    sched_p = sub.add_parser(
        "schedule",
        help="List, add, and remove scheduled jobs.",
        description=(
            "Manage scheduled jobs at ~/.tend/cron/jobs.json. The running tend "
            "daemon picks up changes; add/rm here is equivalent to using the "
            "voice tools."
        ),
    )
    sched = sched_p.add_subparsers(
        dest="action", required=True, title="actions", metavar="<action>",
    )

    sl = sched.add_parser("list", help="List active schedules.")
    sl.set_defaults(func=cmd_schedule_list)

    ss = sched.add_parser("show", help="Show one schedule.")
    ss.add_argument("name_or_id")
    ss.set_defaults(func=cmd_schedule_show)

    sa = sched.add_parser("add", help="Add a new schedule.")
    sa.add_argument("--when", required=True,
                    help="cron expr / 'in 30m' / 'every 30m' / ISO timestamp")
    sa.add_argument("--name", required=True, help="Label for cancel/list later.")
    sa.add_argument("--tz", default=None, help="Timezone for cron schedules.")
    sa.add_argument("--request", default=None,
                    help="What the worker should do. Mutually exclusive with --event.")
    sa.add_argument("--event", default=None,
                    help="Event kind to dispatch (e.g. lunch.upcoming). Requires --payload.")
    sa.add_argument("--payload", default=None,
                    help="JSON payload for --event mode.")
    sa.add_argument("--source", default="cli",
                    help="Source label for the schedule entry (default: cli).")
    sa.set_defaults(func=cmd_schedule_add)

    sr = sched.add_parser("rm", help="Remove a schedule by name or id prefix.")
    sr.add_argument("name_or_id")
    sr.set_defaults(func=cmd_schedule_rm)

    # webhook ----------------------------------------------------------------
    wh_p = sub.add_parser(
        "webhook",
        help="Probe the local webhook server.",
    )
    wh_actions = wh_p.add_subparsers(
        dest="action", required=True, title="actions", metavar="<action>",
    )
    wt = wh_actions.add_parser(
        "test",
        help="POST a smoke message to /say (requires TEND_WEBHOOK_TOKEN).",
    )
    wt.set_defaults(func=cmd_webhook_test)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    rc = args.func(args)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    main()

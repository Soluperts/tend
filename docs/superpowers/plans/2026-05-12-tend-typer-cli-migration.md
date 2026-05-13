# Typer CLI Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port tend's argparse CLI to Typer with the user-facing surface preserved byte-for-byte, splitting the 845-line `cli.py` into a package and unifying on `tend.paths`.

**Architecture:** Single root `typer.Typer()` app in `src/tend/cli/__init__.py`. Each command group (`sessions`, `skills`, `schedule`, `webhook`, `snapshot`) is its own sub-module that registers a sub-`Typer` app or a top-level command. Tests keep calling `tend.cli.main(argv) -> int`; internally that runs the Typer app with `standalone_mode=False` and translates `click.exceptions.Exit` back into the return code.

**Tech Stack:** Python 3.11+, Typer (new), Click (transitive), pytest.

**Reference spec:** `docs/superpowers/specs/2026-05-12-tend-typer-cli-migration-design.md`.

---

## File Structure

After the migration:

```
src/tend/cli/
  __init__.py        Root Typer app; main(argv); scan-skill command lives here
                     because it's top-level. Exits via click.exceptions.Exit.
  _shared.py         _ago, _validate_skill_name, _print_event,
                     _resolve_session, _store, _cron_store, _claude_home,
                     _claude_version, _mcp_table, _mcp_list_markdown,
                     _list_dir_md (helpers used across multiple modules).
  sessions.py        `tend sessions {list,show,tail,cat}`
  skills.py          `tend skills {list,show,cat,rm,quarantined,
                     enable-triggers,disable-triggers}`
  schedule.py        `tend schedule {list,show,add,rm}`
  webhook.py         `tend webhook test`
  snapshot.py        `tend snapshot`
```

`src/tend/cli.py` is deleted. The package import path `tend.cli` resolves to
the new package. `pyproject.toml`'s `tend = "tend.cli:main"` console-script
entry continues to work unchanged.

---

## Task 1: Add Typer dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Locate the dependencies list**

Run: `grep -n "^dependencies" pyproject.toml`
Expected: a single line near the top of `[project]`.

- [ ] **Step 2: Add `typer>=0.12` to the dependencies list**

Open `pyproject.toml`, find the `dependencies = [...]` array (it currently
ends with entries like `"freezegun>=1.4",`). Add a new entry on its own
line, keeping the trailing-comma style:

```toml
    "typer>=0.12",
```

Place it alphabetically (between `rapidfuzz` and `pydantic-settings`).

- [ ] **Step 3: Install the new dep**

Run: `pip install -e .`
Expected: `Successfully installed typer-0.x.y click-...`. No errors.

- [ ] **Step 4: Confirm it imports**

Run: `python -c "import typer; print(typer.__version__)"`
Expected: a version string ≥ 0.12.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "$(cat <<'EOF'
build: add typer dependency

Prerequisite for the argparse → Typer CLI migration (roadmap #3).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `paths.skills_quarantine_root()`

**Files:**
- Modify: `src/tend/paths.py:88-90`
- Modify: `tests/test_paths.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paths.py` (somewhere among the workspace-path tests):

```python
def test_skills_quarantine_root_under_tend_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend import paths
    assert paths.skills_quarantine_root() == tmp_path / "skills-quarantined"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_paths.py::test_skills_quarantine_root_under_tend_home -v`
Expected: FAIL with `AttributeError: module 'tend.paths' has no attribute 'skills_quarantine_root'`.

- [ ] **Step 3: Add the function to `paths.py`**

Insert after `skills_backup_root` (currently around line 88):

```python
def skills_quarantine_root() -> Path:
    return tend_home() / "skills-quarantined"
```

- [ ] **Step 4: Run the new test**

Run: `pytest tests/test_paths.py::test_skills_quarantine_root_under_tend_home -v`
Expected: PASS.

- [ ] **Step 5: Run the full paths test file**

Run: `pytest tests/test_paths.py -q`
Expected: every test passes (20 total now).

- [ ] **Step 6: Commit**

```bash
git add src/tend/paths.py tests/test_paths.py
git commit -m "$(cat <<'EOF'
feat(paths): add skills_quarantine_root helper

Mirror skills_backup_root. The CLI uses this in the next commit;
breaking it out separately keeps that change focused.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Create `cli/_shared.py` with helpers

This task creates the new `cli/` package directory. We will start with just
the helpers module — no Typer app yet, no commands yet, no integration with
the rest of the codebase. That way step 5 is a green commit on a brand-new
file that nobody imports.

**Files:**
- Create: `src/tend/cli/__init__.py` (empty for now, to make `cli/` a package)
- Create: `src/tend/cli/_shared.py`

NOTE: There's a name collision. While both `src/tend/cli.py` and
`src/tend/cli/__init__.py` exist, Python picks **one** of them, and the
behaviour is undefined across setuptools layouts. To avoid that ambiguity we
must rename the old file FIRST. Do that as part of this task:

- [ ] **Step 1: Rename old `cli.py` out of the way**

Run:
```bash
git mv src/tend/cli.py src/tend/_cli_legacy.py
```

Then update the one consumer (`pyproject.toml`) so the console script still
resolves:

```toml
[project.scripts]
tend = "tend._cli_legacy:main"
```

This is a temporary pointer. We'll flip it back to `tend.cli:main` at the
end of the migration.

- [ ] **Step 2: Verify nothing else imported `tend.cli`**

Run: `grep -rn "from tend.cli\|import tend.cli" src/ tests/ scripts/`
Expected: matches only inside `tests/test_cli*.py` and `tests/test_cli_schedule.py`.

The tests use `from tend.cli import main`. After the rename, `tend.cli` no
longer exists. We need to fix that **right now** so the suite stays green
during the rest of the migration. Briefly point them at the legacy module:

Edit `tests/test_cli.py` and replace every `from tend.cli import main` with
`from tend._cli_legacy import main`. There are 15+ occurrences — use:

```bash
sed -i 's|from tend.cli import main|from tend._cli_legacy import main|g' \
    tests/test_cli.py tests/test_cli_schedule.py
```

Also fix the two `monkeypatch.setattr("tend.cli...")` calls and one
`from tend import cli` import in `test_cli.py`:

```bash
sed -i 's|monkeypatch\.setattr("tend\.cli\._default_root"|monkeypatch.setattr("tend._cli_legacy._default_root"|g' \
    tests/test_cli.py
sed -i 's|from tend import cli$|from tend import _cli_legacy as cli|g' \
    tests/test_cli.py
sed -i 's|from tend.cli import main as cli_main|from tend._cli_legacy import main as cli_main|g' \
    tests/test_cli_schedule.py
```

- [ ] **Step 3: Verify the suite is still green after the rename**

Run: `pytest tests/test_cli.py tests/test_cli_schedule.py -q`
Expected: every test passes. (We haven't changed behaviour, just the import path.)

- [ ] **Step 4: Create the `cli/` package and `_shared.py`**

Create `src/tend/cli/__init__.py` as an empty file (we'll fill it in later
tasks):

```python
"""tend CLI — Typer-based."""
```

Create `src/tend/cli/_shared.py` with the helpers lifted from
`_cli_legacy.py`. Copy these functions verbatim from the legacy file and
update their imports/path lookups:

```python
"""Helpers shared by tend CLI command modules."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
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
```

Note the dropped underscore prefixes on the public helpers (compared to the
old `_default_root`, `_skills_root` etc.). These helpers are module-private
in `_shared.py` but used across the package — keeping them un-underscored
makes intent clear.

- [ ] **Step 5: Confirm the new package imports cleanly**

Run: `python -c "from tend.cli import _shared; print(_shared.ago(0)[:5])"`
Expected: a string ending in `ago` (e.g., `19000d ago`). No ImportError.

- [ ] **Step 6: Run the full suite to confirm nothing broke**

Run: `pytest -q`
Expected: every test passes (the legacy `_cli_legacy.py` is still serving
all CLI requests).

- [ ] **Step 7: Commit**

```bash
git add src/tend/_cli_legacy.py src/tend/cli/ tests/test_cli.py \
        tests/test_cli_schedule.py pyproject.toml
git commit -m "$(cat <<'EOF'
refactor(cli): rename cli.py to _cli_legacy.py and scaffold cli/ package

Step 1 of the Typer migration. Pure rename + shared-helpers extraction;
no behaviour change. Tests temporarily point at _cli_legacy so the suite
stays green while the new cli/ package is built up over the next tasks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Implement `tend sessions ...` in the new package

**Files:**
- Create: `src/tend/cli/sessions.py`
- Modify: `src/tend/cli/__init__.py`

- [ ] **Step 1: Create `cli/sessions.py`**

```python
"""`tend sessions ...` — read-only inspection of worker session history."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from tend.cli._shared import (
    ago, print_event, resolve_session, store,
)


app = typer.Typer(
    no_args_is_help=True,
    help="Inspect worker session history (read-only).",
)


@app.command("list")
def list_(
    limit: int = typer.Option(10, "--limit", help="Maximum sessions to show."),
    status: Optional[str] = typer.Option(
        None, "--status",
        help="Filter by status (running, done, failed, killed).",
    ),
    json_: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """List recent sessions, newest first."""
    rows = store().list_recent(limit=limit)
    if status:
        rows = [r for r in rows if r.status == status]
    if json_:
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
            started=ago(r.started_at), sid=r.session_id[:10],
            request=(r.request or "")[:60],
        ))


@app.command("show")
def show(
    session_id: str = typer.Argument(..., help="Session id, or any unique prefix."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON instead of key/value lines."),
) -> None:
    """Print stored metadata for one session."""
    match = resolve_session(store(), session_id)
    if not match:
        print(f"No session matching '{session_id}'", file=sys.stderr)
        raise typer.Exit(code=1)
    if json_:
        print(json.dumps(match.__dict__, indent=2, default=str))
        return
    for k, v in match.__dict__.items():
        print(f"{k:<22} {v}")


@app.command("tail")
def tail(
    session_id: str = typer.Argument(..., help="Session id, or any unique prefix."),
    follow: bool = typer.Option(False, "--follow", help="Stream new events (like `tail -f`)."),
    raw: bool = typer.Option(False, "--raw", help="Print raw JSONL lines."),
) -> None:
    """Pretty-print a session's transcript event by event."""
    match = resolve_session(store(), session_id)
    if not match:
        print(f"No session matching '{session_id}'", file=sys.stderr)
        raise typer.Exit(code=1)
    path = Path(match.transcript_path)
    if not path.exists():
        print(f"Transcript not found: {path}", file=sys.stderr)
        raise typer.Exit(code=1)
    with open(path, "r") as f:
        for line in f:
            print_event(line, raw)
        if not follow:
            return
        while True:
            line = f.readline()
            if line:
                print_event(line, raw)
            else:
                time.sleep(0.2)


@app.command("cat")
def cat(
    session_id: str = typer.Argument(..., help="Session id, or any unique prefix."),
) -> None:
    """Dump a session's transcript file to stdout."""
    match = resolve_session(store(), session_id)
    if not match:
        print(f"No session matching '{session_id}'", file=sys.stderr)
        raise typer.Exit(code=1)
    path = Path(match.transcript_path)
    if not path.exists():
        print(f"Transcript not found: {path}", file=sys.stderr)
        raise typer.Exit(code=1)
    sys.stdout.write(path.read_text())
```

- [ ] **Step 2: Wire the sub-app into the root in `cli/__init__.py`**

Replace `cli/__init__.py` with:

```python
"""tend CLI — Typer-based."""

from __future__ import annotations

import click
import typer

from tend.cli import sessions


app = typer.Typer(
    no_args_is_help=True,
    help=(
        "tend — inspect and manage the on-disk state of the tend voice "
        "assistant. Reads $TEND_HOME directly; does not talk to the running "
        "daemon."
    ),
    add_completion=False,
)
app.add_typer(sessions.app, name="sessions")


def main(argv: list[str] | None = None) -> int:
    """Console-script and test entry point. Returns the rc instead of exiting."""
    try:
        app(args=argv, standalone_mode=False, prog_name="tend")
        return 0
    except click.exceptions.Exit as e:
        return e.exit_code
    except click.exceptions.UsageError as e:
        e.show()
        return e.exit_code or 2
```

- [ ] **Step 3: Smoke-test the new sub-app**

Run:
```bash
TEND_HOME=$(mktemp -d) python -c "from tend.cli import main; raise SystemExit(main(['sessions', 'list']))"
```
Expected: prints `No sessions yet.` and exits 0.

- [ ] **Step 4: Don't migrate tests yet**

The existing `tests/test_cli.py` still imports from `tend._cli_legacy`, so it
exercises the old code path. We migrate tests at the end (Task 11) once all
groups are ported, to keep each commit's diff small and reviewable.

- [ ] **Step 5: Commit**

```bash
git add src/tend/cli/sessions.py src/tend/cli/__init__.py
git commit -m "$(cat <<'EOF'
refactor(cli): port sessions subcommands to Typer

The new tend.cli.sessions sub-app preserves the exact argparse surface
(list, show, tail, cat) with the same flags and exit codes. The legacy
implementation in _cli_legacy.py is still wired to the console script;
the next tasks port the remaining groups before flipping the switch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Implement `tend skills ...` + `tend scan-skill`

**Files:**
- Create: `src/tend/cli/skills.py`
- Modify: `src/tend/cli/__init__.py`

- [ ] **Step 1: Create `cli/skills.py`**

```python
"""`tend skills ...` and top-level `tend scan-skill`."""

from __future__ import annotations

import json
import shutil
import sys

import typer

from tend import paths
from tend.cli._shared import cron_store, validate_skill_name
from tend.skills import enumerate_skills, scan_text


app = typer.Typer(
    no_args_is_help=True,
    help="List, inspect, and remove installed skills.",
)


@app.command("list")
def list_() -> None:
    """List installed skills with their descriptions."""
    skills = enumerate_skills(paths.user_skills_dir())
    if not skills:
        print("No skills installed.")
        return
    for s in skills:
        print(f"{s.name} — {s.description}")


@app.command("show")
def show(name: str = typer.Argument(..., help="Skill name.")) -> None:
    """Print a skill's SKILL.md to stdout."""
    name = validate_skill_name(name)
    skill_md = paths.user_skills_dir() / name / "SKILL.md"
    if not skill_md.is_file():
        print(f"No skill named {name!r}", file=sys.stderr)
        raise typer.Exit(code=2)
    sys.stdout.write(skill_md.read_text(encoding="utf-8"))


@app.command("cat")
def cat(name: str = typer.Argument(..., help="Skill name.")) -> None:
    """Alias for `skills show`."""
    show(name=name)


@app.command("rm")
def rm(name: str = typer.Argument(..., help="Skill name.")) -> None:
    """Delete an installed skill (irreversible)."""
    name = validate_skill_name(name)
    target = paths.user_skills_dir() / name
    if not target.is_dir():
        print(f"No skill named {name!r}", file=sys.stderr)
        raise typer.Exit(code=2)
    shutil.rmtree(target)


@app.command("quarantined")
def quarantined() -> None:
    """List skills the safety scanner blocked."""
    root = paths.skills_quarantine_root()
    entries = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    if not entries:
        print("No quarantined skills.")
        return
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


@app.command("enable-triggers")
def enable_triggers(name: str = typer.Argument(..., help="Skill name.")) -> None:
    """Print the schedules the running daemon would create for this skill."""
    name = validate_skill_name(name)
    skills = [s for s in enumerate_skills(paths.user_skills_dir()) if s.name == name]
    if not skills:
        print(f"No skill named {name!r}", file=sys.stderr)
        raise typer.Exit(code=2)
    info = skills[0]
    if not info.triggers:
        print(f"{name}: no triggers declared in frontmatter")
        return
    print(f"{name}: {len(info.triggers)} trigger(s) — ask the running daemon")
    print("to enable via voice ('enable triggers for x') or by editing")
    print("$TEND_HOME/cron/jobs.json directly while tend is stopped.")
    for t in info.triggers:
        print(f"  - {t}")


@app.command("disable-triggers")
def disable_triggers(name: str = typer.Argument(..., help="Skill name.")) -> None:
    """Remove all schedule entries previously enabled from a skill."""
    name = validate_skill_name(name)
    store = cron_store()
    rows = store.find_by_source(f"skill:{name}")
    if not rows:
        print(f"{name}: no active triggers")
        return
    count = store.remove_by_source(f"skill:{name}")
    print(f"{name}: removed {count} trigger(s)")


# scan-skill is a top-level command, not under `skills`. Define it here so
# all skill-related code lives together, but register it on the root app
# from cli/__init__.py.
def scan_skill_command(
    name: str = typer.Argument(..., help="Skill name (folder under $TEND_HOME/skills/)."),
) -> None:
    """Run the safety scanner over an installed skill.

    Exit codes: 0=clean, 1=warnings only, 2=critical findings, 3=skill not found.
    """
    name = validate_skill_name(name)
    p = paths.user_skills_dir() / name / "SKILL.md"
    if not p.is_file():
        print(f"no skill named {name!r}", file=sys.stderr)
        raise typer.Exit(code=3)
    report = scan_text(p.read_text(encoding="utf-8"))
    if report.is_clean:
        print(f"{name}: clean")
        return
    for f in report.findings:
        print(f"  [{f.severity}] {f.rule} (line {f.line}): {f.snippet}")
    raise typer.Exit(code=2 if report.is_critical else 1)
```

- [ ] **Step 2: Register on the root app**

Edit `src/tend/cli/__init__.py`. Add `from tend.cli import sessions, skills`
at the top, and after the existing `app.add_typer(sessions.app, name="sessions")`
line, add:

```python
app.add_typer(skills.app, name="skills")
app.command("scan-skill")(skills.scan_skill_command)
```

- [ ] **Step 3: Smoke-test `tend skills list` and `tend scan-skill --help`**

Run:
```bash
TEND_HOME=$(mktemp -d) python -c "from tend.cli import main; raise SystemExit(main(['skills', 'list']))"
TEND_HOME=$(mktemp -d) python -c "from tend.cli import main; raise SystemExit(main(['scan-skill', '--help']))"
```
Expected: first prints `No skills installed.` and exits 0; second prints
help text mentioning `name`.

- [ ] **Step 4: Commit**

```bash
git add src/tend/cli/skills.py src/tend/cli/__init__.py
git commit -m "$(cat <<'EOF'
refactor(cli): port skills + scan-skill to Typer

scan-skill stays a top-level command (registered directly on the root app)
rather than moving under `skills`, preserving the legacy invocation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Implement `tend schedule ...`

**Files:**
- Create: `src/tend/cli/schedule.py`
- Modify: `src/tend/cli/__init__.py`

- [ ] **Step 1: Create `cli/schedule.py`**

```python
"""`tend schedule ...` — manage cron jobs at $TEND_HOME/cron/jobs.json."""

from __future__ import annotations

import datetime as _dt
import json
import sys
from typing import Optional
from zoneinfo import ZoneInfo

import typer

from tend.cli._shared import cron_store
from tend.cron_store import JobState
from tend.cron_time import InvalidWhen, next_fire_at, parse_when


app = typer.Typer(
    no_args_is_help=True,
    help="List, add, and remove scheduled jobs.",
)


@app.command("list")
def list_() -> None:
    """List active schedules."""
    store = cron_store()
    jobs = store.load_jobs()
    if not jobs:
        print("No schedules.")
        return
    for j in jobs:
        state = store.get_state(j.id)
        nfa = state.next_run_at or "(unknown)"
        print(
            f"{j.name:<24} {j.kind:<6} {j.schedule:<22} "
            f"source={j.source:<22} next={nfa}"
        )


@app.command("show")
def show(name_or_id: str = typer.Argument(..., help="Job name or id prefix.")) -> None:
    """Show one schedule."""
    store = cron_store()
    match = next(
        (j for j in store.load_jobs()
         if j.name == name_or_id or j.id.startswith(name_or_id)),
        None,
    )
    if match is None:
        print(f"No schedule matching {name_or_id!r}", file=sys.stderr)
        raise typer.Exit(code=2)
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


@app.command("add")
def add(
    when: str = typer.Option(
        ..., "--when",
        help="cron expr / 'in 30m' / 'every 30m' / ISO timestamp",
    ),
    name: str = typer.Option(..., "--name", help="Label for cancel/list later."),
    tz: Optional[str] = typer.Option(None, "--tz", help="Timezone for cron schedules."),
    request: Optional[str] = typer.Option(
        None, "--request",
        help="What the worker should do. Mutually exclusive with --event.",
    ),
    event: Optional[str] = typer.Option(
        None, "--event",
        help="Event kind to dispatch (e.g. lunch.upcoming). Requires --payload.",
    ),
    payload: Optional[str] = typer.Option(
        None, "--payload", help="JSON payload for --event mode.",
    ),
    source: str = typer.Option(
        "cli", "--source",
        help="Source label for the schedule entry (default: cli).",
    ),
) -> None:
    """Add a new schedule."""
    if event and request:
        print("--event and --request are mutually exclusive.", file=sys.stderr)
        raise typer.Exit(code=2)
    if event and not payload:
        print("--event requires --payload (use '{}' for empty).", file=sys.stderr)
        raise typer.Exit(code=2)
    if not event and not request:
        print("either --request or --event is required.", file=sys.stderr)
        raise typer.Exit(code=2)

    event_payload: Optional[dict] = None
    if event:
        try:
            event_payload = json.loads(payload)
        except json.JSONDecodeError as e:
            print(f"--payload is not valid JSON: {e}", file=sys.stderr)
            raise typer.Exit(code=2)
        if not isinstance(event_payload, dict):
            print("--payload must be a JSON object.", file=sys.stderr)
            raise typer.Exit(code=2)

    store = cron_store()
    try:
        kind, schedule = parse_when(when)
    except InvalidWhen as e:
        print(f"invalid --when: {e}", file=sys.stderr)
        raise typer.Exit(code=2)

    payload_dict = {} if event else {"request": request}

    job = store.add_job(
        name=name, kind=kind, schedule=schedule,
        tz=tz or "UTC",
        payload=payload_dict,
        source=source, enabled=True,
        event_kind=event,
        event_payload=event_payload,
    )
    nfa = next_fire_at(
        kind, schedule, tz or "UTC",
        _dt.datetime.now(tz=ZoneInfo("UTC")),
    )
    store.set_state(job.id, JobState(next_run_at=nfa.isoformat()))
    print(f"added {job.id[:8]} {job.name}")


@app.command("rm")
def rm(name_or_id: str = typer.Argument(..., help="Job name or id prefix.")) -> None:
    """Remove a schedule by name or id prefix."""
    store = cron_store()
    match = next(
        (j for j in store.load_jobs()
         if j.name == name_or_id or j.id.startswith(name_or_id)),
        None,
    )
    if match is None:
        print(f"No schedule matching {name_or_id!r}", file=sys.stderr)
        raise typer.Exit(code=2)
    store.remove_job(match.id)
    store.remove_state(match.id)
    print(f"removed {match.name}")
```

- [ ] **Step 2: Register on the root app**

Edit `src/tend/cli/__init__.py`:

Add `schedule` to the import line:
```python
from tend.cli import schedule, sessions, skills
```

Add the registration line near the others:
```python
app.add_typer(schedule.app, name="schedule")
```

- [ ] **Step 3: Smoke-test**

Run:
```bash
TEND_HOME=$(mktemp -d) python -c "from tend.cli import main; raise SystemExit(main(['schedule', 'list']))"
```
Expected: prints `No schedules.` and exits 0.

- [ ] **Step 4: Commit**

```bash
git add src/tend/cli/schedule.py src/tend/cli/__init__.py
git commit -m "$(cat <<'EOF'
refactor(cli): port schedule subcommands to Typer

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Implement `tend webhook test`

**Files:**
- Create: `src/tend/cli/webhook.py`
- Modify: `src/tend/cli/__init__.py`

- [ ] **Step 1: Create `cli/webhook.py`**

```python
"""`tend webhook ...` — probe the local webhook server."""

from __future__ import annotations

import json
import os
import sys
import urllib.request

import typer

from tend.config import settings


app = typer.Typer(no_args_is_help=True, help="Probe the local webhook server.")


@app.command("test")
def test() -> None:
    """POST a smoke message to /say (requires TEND_WEBHOOK_TOKEN)."""
    token = os.environ.get("TEND_WEBHOOK_TOKEN")
    if not token:
        print(
            "TEND_WEBHOOK_TOKEN is not set; cannot test the webhook.",
            file=sys.stderr,
        )
        raise typer.Exit(code=2)
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
    except Exception as e:
        print(f"webhook unreachable: {e}", file=sys.stderr)
        raise typer.Exit(code=1)
```

- [ ] **Step 2: Register on the root app**

Edit `src/tend/cli/__init__.py`:

Add `webhook` to the import:
```python
from tend.cli import schedule, sessions, skills, webhook
```

Add the registration:
```python
app.add_typer(webhook.app, name="webhook")
```

- [ ] **Step 3: Smoke-test the unconfigured-token path**

Run:
```bash
unset TEND_WEBHOOK_TOKEN
python -c "from tend.cli import main; print('rc=', main(['webhook', 'test']))"
```
Expected: prints `TEND_WEBHOOK_TOKEN is not set; cannot test the webhook.` to
stderr and `rc= 2`.

- [ ] **Step 4: Commit**

```bash
git add src/tend/cli/webhook.py src/tend/cli/__init__.py
git commit -m "$(cat <<'EOF'
refactor(cli): port webhook test command to Typer

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Implement `tend snapshot`

**Files:**
- Create: `src/tend/cli/snapshot.py`
- Modify: `src/tend/cli/__init__.py`

- [ ] **Step 1: Create `cli/snapshot.py`**

```python
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
```

- [ ] **Step 2: Register on the root app**

Edit `src/tend/cli/__init__.py`:

Add `snapshot` to the imports:
```python
from tend.cli import schedule, sessions, skills, snapshot, webhook
```

Add the registration as a top-level command (not a sub-app):
```python
app.command("snapshot")(snapshot.snapshot_command)
```

- [ ] **Step 3: Smoke-test top-level help**

Run:
```bash
python -c "from tend.cli import main; raise SystemExit(main(['--help']))"
```
Expected: help text listing all command groups: `scan-skill`, `schedule`,
`sessions`, `skills`, `snapshot`, `webhook`. Exit 0.

- [ ] **Step 4: Commit**

```bash
git add src/tend/cli/snapshot.py src/tend/cli/__init__.py
git commit -m "$(cat <<'EOF'
refactor(cli): port snapshot command to Typer

All command groups now live in the new tend.cli package. The next task
flips the console-script entry point off _cli_legacy and deletes it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Flip console script, delete legacy module

**Files:**
- Modify: `pyproject.toml`
- Delete: `src/tend/_cli_legacy.py`

- [ ] **Step 1: Flip the entry point back to `tend.cli:main`**

Edit `pyproject.toml`:

```toml
[project.scripts]
tend = "tend.cli:main"
```

- [ ] **Step 2: Reinstall the entry point**

Run: `pip install -e .`
Expected: no errors. The installed `tend` script now points at the new
package's `main`.

- [ ] **Step 3: Verify the installed entry works**

Run: `tend --help`
Expected: top-level help listing every command group. Exit 0.

- [ ] **Step 4: Delete the legacy file**

Run: `git rm src/tend/_cli_legacy.py`
Expected: `rm 'src/tend/_cli_legacy.py'`. No errors.

- [ ] **Step 5: Verify nothing imports the legacy module**

Run: `grep -rn "_cli_legacy" src/ scripts/`
Expected: zero matches.

Run: `grep -rn "_cli_legacy" tests/`
Expected: matches in `tests/test_cli.py` and `tests/test_cli_schedule.py`
only. These get fixed in the next task.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml
git rm src/tend/_cli_legacy.py
git commit -m "$(cat <<'EOF'
refactor(cli): switch entry point to the new package and delete legacy

The argparse implementation is gone. `tend = "tend.cli:main"` now resolves
to the new Typer-based package. Tests still import from _cli_legacy and
are fixed in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Migrate CLI tests off the legacy import

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `tests/test_cli_schedule.py`

- [ ] **Step 1: Restore `from tend.cli import main` everywhere**

Run:
```bash
sed -i 's|from tend._cli_legacy import main|from tend.cli import main|g' \
    tests/test_cli.py tests/test_cli_schedule.py
sed -i 's|from tend._cli_legacy import main as cli_main|from tend.cli import main as cli_main|g' \
    tests/test_cli_schedule.py
sed -i 's|from tend import _cli_legacy as cli|from tend.cli import snapshot as snapshot_mod\nfrom tend import cli|g' \
    tests/test_cli.py
```

The third sed handles the `from tend import cli` line in `test_cli.py` that
was rewritten to `_cli_legacy as cli` during Task 3. We need it pointing
back at the new package, but the test's monkeypatches target legacy
private helpers (`_default_root`, `_claude_version`, `_mcp_list_markdown`,
`_claude_home`) that no longer exist. Step 4 below rewrites that one
test's monkeypatches.

- [ ] **Step 2: Run the suite to see what's still broken**

Run: `pytest tests/test_cli.py tests/test_cli_schedule.py -q`
Expected: failures.

Likely failure modes:
- `AttributeError: module 'tend.cli' has no attribute '_default_root'`
  (monkeypatches in `test_cli_snapshot_writes_file` and
  `test_cli_snapshot_handles_missing_claude`).
- Tests that set `TEND_ROOT` or `TEND_SKILLS_ROOT` env vars now produce
  empty workspaces because the CLI looks at `TEND_HOME`.
- The 3 `pytest.raises(SystemExit)` tests fail because `main()` returns rc
  instead of exiting.

Steps 3–6 fix these one fix at a time.

- [ ] **Step 3: Migrate env var `TEND_ROOT` and `TEND_SKILLS_ROOT` → `TEND_HOME`**

In `tests/test_cli_schedule.py`, replace every
`monkeypatch.setenv("TEND_ROOT", str(tmp_path))` with
`monkeypatch.setenv("TEND_HOME", str(tmp_path))`:

```bash
sed -i 's|monkeypatch\.setenv("TEND_ROOT", |monkeypatch.setenv("TEND_HOME", |g' \
    tests/test_cli_schedule.py tests/test_cli.py
```

In `tests/test_cli.py`, the `TEND_SKILLS_ROOT` variable was pointing at a
subdirectory of `tmp_path` (e.g., `tmp_path / "skills"`). Since the new CLI
derives `paths.user_skills_dir()` from `$TEND_HOME / skills`, we replace
the env var **and** drop the trailing `/ "skills"`. Use this exact
multi-step approach for the two patterns:

Open `tests/test_cli.py` and replace every line of the form
`monkeypatch.setenv("TEND_SKILLS_ROOT", str(skills_root))` with
`monkeypatch.setenv("TEND_HOME", str(skills_root.parent))` — where the
test already had `skills_root = tmp_path / "skills"`. Equivalent sed:

```bash
sed -i 's|monkeypatch\.setenv("TEND_SKILLS_ROOT", str(skills_root))|monkeypatch.setenv("TEND_HOME", str(skills_root.parent))|g' \
    tests/test_cli.py
sed -i 's|monkeypatch\.setenv("TEND_SKILLS_ROOT", str(tmp_path / "skills"))|monkeypatch.setenv("TEND_HOME", str(tmp_path))|g' \
    tests/test_cli.py
```

Likewise for `TEND_SKILLS_QUARANTINE_ROOT`:

```bash
sed -i 's|monkeypatch\.setenv("TEND_SKILLS_QUARANTINE_ROOT", str(quarantine_root))|monkeypatch.setenv("TEND_HOME", str(quarantine_root.parent))|g' \
    tests/test_cli.py
sed -i 's|monkeypatch\.setenv("TEND_SKILLS_QUARANTINE_ROOT", str(tmp_path / "skills-quarantined"))|monkeypatch.setenv("TEND_HOME", str(tmp_path))|g' \
    tests/test_cli.py
```

For each test, also rename the local variable `skills_root` in the test
body to be `tmp_path / "skills"` only where the test directly constructs
files under it — that already matches the implicit
`$TEND_HOME/skills/` layout, so the file-creation code does not change.

Run: `pytest tests/test_cli.py::test_skills_list_renders_name_and_description -v`
Expected: PASS.

Run: `pytest tests/test_cli.py::test_skills_quarantined_lists_findings -v`
Expected: PASS.

If any of the sed expressions miss a stray case (e.g., a test you didn't
anticipate), open the file and finish the migration by hand.

- [ ] **Step 4: Rewrite the snapshot test monkeypatches**

The legacy private helpers (`_claude_home`, `_claude_version`,
`_mcp_list_markdown`, `_default_root`) don't exist in the new package.
The new equivalents live in `tend.cli._shared` and are named without the
leading underscore.

Open `tests/test_cli.py`. Find `test_cli_snapshot_writes_file` (around
line 99) and `test_cli_snapshot_handles_missing_claude` (around line
134). Rewrite their monkeypatches as follows.

For `test_cli_snapshot_writes_file`:

```python
def test_cli_snapshot_writes_file(tmp_path, monkeypatch, capsys):
    """`tend snapshot` writes $TEND_HOME/claude-env.md with all expected sections."""
    from tend.cli import _shared
    from tend.cli import main

    root = tmp_path / "tend-home"
    root.mkdir()
    monkeypatch.setenv("TEND_HOME", str(root))

    # Stub out claude_env_data so we don't shell out in tests.
    monkeypatch.setattr(_shared, "claude_version", lambda: "claude 1.2.3")
    monkeypatch.setattr(
        _shared, "mcp_list_markdown",
        lambda: "| sheets | mcp__sheets__ | user | running |",
    )

    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "skills" / "demo").mkdir(parents=True)
    (fake_home / ".claude" / "agents").mkdir(parents=True)
    (fake_home / ".claude" / "commands").mkdir(parents=True)
    (fake_home / ".claude" / "agents" / "agent-x.md").write_text("x")
    monkeypatch.setattr(_shared, "claude_home", lambda: fake_home / ".claude")

    main(["snapshot"])
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
```

Subtle: the snapshot command imports `claude_home`, `claude_version`,
`mcp_list_markdown`, and `list_dir_md` by name from `_shared` AT IMPORT
TIME (`from tend.cli._shared import claude_home, ...`). Once those names
are bound into `snapshot`'s namespace, `monkeypatch.setattr(_shared, ...)`
does NOT replace them. We have to patch the names where they are LOOKED
UP, i.e., on the `tend.cli.snapshot` module:

Replace the three setattr lines above with:

```python
    from tend.cli import snapshot as snapshot_mod
    monkeypatch.setattr(snapshot_mod, "claude_version", lambda: "claude 1.2.3")
    monkeypatch.setattr(
        snapshot_mod, "mcp_list_markdown",
        lambda: "| sheets | mcp__sheets__ | user | running |",
    )
    monkeypatch.setattr(snapshot_mod, "claude_home", lambda: fake_home / ".claude")
```

For `test_cli_snapshot_handles_missing_claude` the monkeypatch on
`shutil.which` is fine; just replace `monkeypatch.setattr(cli, "_default_root", ...)`
with `monkeypatch.setenv("TEND_HOME", str(tmp_path / "tend-home"))`:

```python
def test_cli_snapshot_handles_missing_claude(monkeypatch, tmp_path, capsys):
    from tend.cli import main

    monkeypatch.setenv("TEND_HOME", str(tmp_path / "tend-home"))
    monkeypatch.setattr("shutil.which", lambda _: None)

    rc = main(["snapshot"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "claude" in err.lower()
```

(Note the rewrite from `pytest.raises(SystemExit)` to `rc = main(...)` —
that's part of Step 5.)

Run: `pytest tests/test_cli.py::test_cli_snapshot_writes_file tests/test_cli.py::test_cli_snapshot_handles_missing_claude -v`
Expected: both PASS.

- [ ] **Step 5: Migrate `pytest.raises(SystemExit)` to `rc = main(...)`**

Three tests need updating:

`test_cli_sessions_show_unknown_returns_nonzero`:

```python
def test_cli_sessions_show_unknown_returns_nonzero(store_with_rows):
    from tend.cli import main
    rc = main(["sessions", "show", "zzzz"])
    assert rc == 1
```

`test_skills_invalid_name_rejected`:

```python
def test_skills_invalid_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.cli import main
    rc = main(["skills", "show", "../etc"])
    assert rc == 2
```

`test_scan_skill_invalid_name`:

```python
def test_scan_skill_invalid_name(tmp_path, monkeypatch):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.cli import main
    rc = main(["scan-skill", "../etc"])
    assert rc == 2
```

The `test_cli_snapshot_handles_missing_claude` rewrite in Step 4 already
covers the fourth `pytest.raises(SystemExit)` case.

Run: `pytest tests/test_cli.py -q`
Expected: every test passes.

Run: `pytest tests/test_cli_schedule.py -q`
Expected: every test passes.

- [ ] **Step 6: Run the entire test suite to catch any cross-file fallout**

Run: `pytest -q`
Expected: every test passes (the new package now serves every CLI request).

- [ ] **Step 7: Commit**

```bash
git add tests/test_cli.py tests/test_cli_schedule.py
git commit -m "$(cat <<'EOF'
test(cli): migrate to tend.cli package and TEND_HOME env var

- Replace TEND_ROOT / TEND_SKILLS_ROOT / TEND_SKILLS_QUARANTINE_ROOT
  with TEND_HOME (now the single workspace override).
- Update snapshot-test monkeypatches to target tend.cli.snapshot's
  imported names rather than the gone-away tend.cli private helpers.
- Convert the 4 pytest.raises(SystemExit) tests to assert on the rc
  returned by main(), which is the new contract with standalone_mode=False.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Wheel-install smoke test

**Files:** none (verification only)

- [ ] **Step 1: Build the wheel**

Run: `python -m build --wheel`
Expected: a wheel under `dist/tend-0.1.0-py3-none-any.whl` (or similar).

- [ ] **Step 2: Install it in a throwaway venv**

```bash
python -m venv /tmp/tend-cli-smoke
/tmp/tend-cli-smoke/bin/pip install dist/tend-0.1.0-py3-none-any.whl
```
Expected: clean install, no errors.

- [ ] **Step 3: Smoke-test `tend --help` from the venv**

```bash
TEND_HOME=$(mktemp -d) /tmp/tend-cli-smoke/bin/tend --help
```
Expected: top-level help listing every command group:
`scan-skill`, `schedule`, `sessions`, `skills`, `snapshot`, `webhook`.

- [ ] **Step 4: Smoke-test a real subcommand**

```bash
TEND_HOME=$(mktemp -d) /tmp/tend-cli-smoke/bin/tend schedule list
```
Expected: `No schedules.` Exit 0.

```bash
TEND_HOME=$(mktemp -d) /tmp/tend-cli-smoke/bin/tend sessions list
```
Expected: `No sessions yet.` Exit 0.

- [ ] **Step 5: Clean up the smoke venv**

```bash
rm -rf /tmp/tend-cli-smoke dist/
```

- [ ] **Step 6: No commit needed**

This task is verification only.

---

## Task 12: Manual smoke test on the running Pi workspace

**Files:** none (verification only)

The Pi has a real `~/.tend/` with skills, sessions, schedules. Run a small
read-only battery against it to make sure the new CLI reads the real
on-disk state correctly. None of these commands modify anything.

- [ ] **Step 1: List sessions on the real workspace**

```bash
tend sessions list --limit 5
```
Expected: a table of the 5 most recent sessions, or "No sessions yet."

- [ ] **Step 2: List skills**

```bash
tend skills list
```
Expected: a line per installed skill (e.g., `briefing — Morning briefing`).

- [ ] **Step 3: List schedules**

```bash
tend schedule list
```
Expected: every active schedule, one per line. If
`[scheduler] heartbeat_every = "off"` is in `~/.tend/tend.toml`, the
heartbeat job is absent — which is the expected current state.

- [ ] **Step 4: Top-level help**

```bash
tend --help
tend sessions --help
tend schedule add --help
```
Expected: each prints a Typer-styled help screen with the appropriate
options.

- [ ] **Step 5: No commit needed**

Manual verification only.

---

## Task 13: Update README + conventions doc

**Files:**
- Modify: `README.md`
- Modify: `docs/conventions.md`

- [ ] **Step 1: Grep for old CLI references in docs**

Run:
```bash
grep -nE "TEND_ROOT|TEND_SKILLS_ROOT|TEND_SKILLS_QUARANTINE_ROOT|argparse" \
    README.md docs/conventions.md docs/*.md
```
Expected: a small handful of matches — replace each with `TEND_HOME` or
remove if it's about argparse-specific concerns.

- [ ] **Step 2: README — CLI section**

Open `README.md` and find the section that lists CLI commands. Update any
example invocations that used `TEND_ROOT=… tend …` to use
`TEND_HOME=… tend …` instead. Examples that don't set the env var stay
unchanged.

- [ ] **Step 3: conventions.md — framework note**

The Conventions doc currently says (line ~30):

```
**Framework: Typer.** Argparse is fine for one entrypoint; once we have …
```

Update the sentence after it so the present tense reflects reality:

```
**Framework: Typer.** Each command group lives in its own module under
`src/tend/cli/`. Top-level commands (`snapshot`, `scan-skill`) register
directly on the root app; group commands (`sessions`, `skills`,
`schedule`, `webhook`) use `app.add_typer()`.
```

- [ ] **Step 4: Verify no remaining TEND_ROOT references**

Run:
```bash
grep -rnE "TEND_ROOT\b|TEND_SKILLS_ROOT\b|TEND_SKILLS_QUARANTINE_ROOT\b" \
    README.md docs/ src/ tests/ scripts/
```
Expected: zero matches across `src/`, `tests/`, `scripts/`. Doc matches
should also be zero after the README/conventions edits.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/conventions.md
git commit -m "$(cat <<'EOF'
docs: update CLI references for Typer + TEND_HOME

- README example invocations now use TEND_HOME (the single workspace
  override).
- conventions.md describes the actual cli/ package layout we shipped.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: ROADMAP — mark #3 as shipped

**Files:**
- Modify: `ROADMAP.md`

- [ ] **Step 1: Remove item 3 from the Next list**

Open `ROADMAP.md`. Delete the bullet:

```
3. **Typer CLI migration** — mechanical move from argparse, prerequisite for new subcommands. *~½ day.*
```

Renumber items 4–8 to 3–7 to keep the list contiguous.

- [ ] **Step 2: Commit**

```bash
git add ROADMAP.md
git commit -m "$(cat <<'EOF'
docs(roadmap): mark Typer CLI migration as shipped

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **All tests green:** `pytest -q` → 0 failures.
- [ ] **Wheel installs cleanly:** Task 11 above.
- [ ] **Real Pi workspace reads work:** Task 12 above.
- [ ] **No lingering `TEND_ROOT` / `_cli_legacy` references in source:**
      `grep -rn "TEND_ROOT\|_cli_legacy" src/ tests/` → zero matches.
- [ ] **The `tend.cli` package is the only CLI surface:** `ls src/tend/cli/`
      shows `__init__.py`, `_shared.py`, `sessions.py`, `skills.py`,
      `schedule.py`, `webhook.py`, `snapshot.py`.

After this plan completes, the user-facing surface is identical to the
argparse era; the codebase has a clean foundation for sub-project #4
(First-run UX) to bolt `setup`, `doctor`, `service`, and `workspace`
subcommands onto.

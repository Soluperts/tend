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

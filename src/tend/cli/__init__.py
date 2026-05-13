"""tend CLI — Typer-based."""

from __future__ import annotations

import click
import typer

from tend.cli import schedule, sessions, skills


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
app.add_typer(skills.app, name="skills")
app.add_typer(schedule.app, name="schedule")
app.command("scan-skill")(skills.scan_skill_command)


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

"""`tend doctor` — read-only diagnostic CLI."""

from __future__ import annotations

import json

import typer
from rich.console import Console

from tend import checks


def doctor_command(
    json_: bool = typer.Option(False, "--json", help="Emit JSON instead of human output."),
) -> None:
    """Run every check, print results, exit 0/1/2 based on aggregate status."""
    # Pull keychain/dotenv secrets into env before Settings reads them.
    # The user invoked `tend doctor` deliberately, so a Keychain Access prompt
    # here is expected and welcome — doctor reports backend state for these
    # secrets and needs them visible to do so.
    from tend.config import Settings
    from tend.secrets import load_into_env

    load_into_env()
    results = checks.run_all(Settings())

    if json_:
        body = [
            {"name": r.name, "status": r.status,
             "detail": r.detail, "remediation": r.remediation}
            for r in results
        ]
        print(json.dumps(body, indent=2))
    else:
        _print_human(results)

    rc = checks.aggregate_exit_code(results)
    raise typer.Exit(code=rc)


_ICON = {"ok": "✓", "warn": "⚠", "fail": "✗"}
_STYLE = {"ok": "green", "warn": "yellow", "fail": "red"}


def _print_human(results: list[checks.CheckResult]) -> None:
    console = Console()
    n_warn = sum(1 for r in results if r.status == "warn")
    n_fail = sum(1 for r in results if r.status == "fail")

    for r in results:
        icon = _ICON[r.status]
        style = _STYLE[r.status]
        console.print(
            f"  [{style}]{icon}[/{style}] {r.name:<16} {r.detail}",
        )
        if r.remediation and r.status != "ok":
            console.print(f"                     remediation: {r.remediation}")

    console.print(f"\n{n_warn} warning(s), {n_fail} failure(s).")

# SPDX-License-Identifier: MIT
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

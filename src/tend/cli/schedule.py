# SPDX-License-Identifier: MIT
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

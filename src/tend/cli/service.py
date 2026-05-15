# SPDX-License-Identifier: MIT
"""`tend service install/uninstall/start/stop/status` — service-unit management."""

from __future__ import annotations

import sys

import typer

from tend import service as service_mod


app = typer.Typer(
    no_args_is_help=True,
    help="Install and control the tend service (systemd on Linux, launchd on macOS).",
)


@app.command("install")
def install(
    force: bool = typer.Option(
        False, "--force",
        help="Overwrite an existing unit file without prompting.",
    ),
) -> None:
    """Write the service unit file and register it with the service manager."""
    try:
        path = service_mod.install(force=force)
    except service_mod.ServiceFileExists as e:
        print(f"{e}\nPass --force to overwrite.", file=sys.stderr)
        raise typer.Exit(code=2)
    print(f"Wrote {path}")
    if sys.platform == "darwin":
        print("Next: the LaunchAgent is already loaded; tend will start at login.")
    else:
        print("Next: `systemctl --user enable --now tend`")


@app.command("uninstall")
def uninstall() -> None:
    """Remove the unit file and deregister from the service manager."""
    service_mod.uninstall()
    print("Removed tend service unit file.")


def _handle_control_errors(fn):
    try:
        return fn()
    except service_mod.ServiceNotInstalled as e:
        print(str(e), file=sys.stderr)
        raise typer.Exit(code=2)


@app.command("start")
def start() -> None:
    """Start the tend service."""
    rc = _handle_control_errors(service_mod.start)
    raise typer.Exit(code=rc)


@app.command("stop")
def stop() -> None:
    """Stop the tend service."""
    rc = _handle_control_errors(service_mod.stop)
    raise typer.Exit(code=rc)


@app.command("status")
def status() -> None:
    """Print the current state of the tend service.

    Exits 0 only when the service is active/running, matching service manager conventions.
    """
    from rich.console import Console
    console = Console()
    state, details = _handle_control_errors(service_mod.status)
    color = {
        "active": "green",
        "running": "green",
        "inactive": "yellow",
        "failed": "red",
        "not-installed": "red",
    }.get(state, "white")
    console.print(f"[{color}]{state}[/{color}]")
    if details:
        console.print(details)
    raise typer.Exit(code=0 if state in {"active", "running"} else 1)

"""`tend service install/uninstall/start/stop/status` — systemd user-unit management."""

from __future__ import annotations

import sys

import typer

from tend import service as service_mod


app = typer.Typer(
    no_args_is_help=True,
    help="Install and control the tend systemd service (Linux).",
)


@app.command("install")
def install(
    force: bool = typer.Option(
        False, "--force",
        help="Overwrite an existing unit file without prompting.",
    ),
) -> None:
    """Write ~/.config/systemd/user/tend.service and run daemon-reload."""
    try:
        path = service_mod.install(force=force)
    except service_mod.MacOSNotSupported as e:
        print(str(e), file=sys.stderr)
        raise typer.Exit(code=1)
    except service_mod.ServiceFileExists as e:
        print(f"{e}\nPass --force to overwrite.", file=sys.stderr)
        raise typer.Exit(code=2)
    print(f"Wrote {path}")
    print("Next: `systemctl --user enable --now tend`")


@app.command("uninstall")
def uninstall() -> None:
    """Remove the unit file and run daemon-reload (does not stop a running service)."""
    try:
        service_mod.uninstall()
    except service_mod.MacOSNotSupported as e:
        print(str(e), file=sys.stderr)
        raise typer.Exit(code=1)
    print("Removed tend.service unit file.")


def _handle_control_errors(fn):
    try:
        return fn()
    except service_mod.MacOSNotSupported as e:
        print(str(e), file=sys.stderr)
        raise typer.Exit(code=1)
    except service_mod.ServiceNotInstalled as e:
        print(str(e), file=sys.stderr)
        raise typer.Exit(code=2)


@app.command("start")
def start() -> None:
    """Start the tend systemd service (`systemctl --user start tend`)."""
    rc = _handle_control_errors(service_mod.start)
    raise typer.Exit(code=rc)


@app.command("stop")
def stop() -> None:
    """Stop the tend systemd service (`systemctl --user stop tend`)."""
    rc = _handle_control_errors(service_mod.stop)
    raise typer.Exit(code=rc)


@app.command("status")
def status() -> None:
    """Print the current state of the tend service.

    Exits 0 only when the service is active, matching systemctl conventions.
    """
    from rich.console import Console
    console = Console()
    state, details = _handle_control_errors(service_mod.status)
    color = {
        "active": "green",
        "inactive": "yellow",
        "failed": "red",
        "not-installed": "red",
    }.get(state, "white")
    console.print(f"[{color}]{state}[/{color}]")
    if details:
        console.print(details)
    raise typer.Exit(code=0 if state == "active" else 1)

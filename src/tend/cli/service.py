"""`tend service install/uninstall` — systemd user-unit management."""

from __future__ import annotations

import sys

import typer

from tend import service as service_mod


app = typer.Typer(
    no_args_is_help=True,
    help="Install/uninstall the tend systemd service (Linux).",
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

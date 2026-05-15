# SPDX-License-Identifier: MIT
"""`tend webhook ...` — probe the local webhook server."""

from __future__ import annotations

import json
import os
import sys
import urllib.request

import typer


app = typer.Typer(no_args_is_help=True, help="Probe the local webhook server.")


@app.command("test")
def test() -> None:
    """POST a smoke message to /say (requires TEND_WEBHOOK_TOKEN)."""
    # Import the settings singleton lazily — at module load it would trigger
    # the keyring read via tend.config.__getattr__, and `tend --help` should
    # never touch the OS keychain on macOS.
    from tend.config import settings

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

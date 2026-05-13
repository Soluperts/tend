"""`tend setup` — interactive bootstrap wizard."""

from __future__ import annotations

import secrets as _stdlib_secrets

import questionary
import typer
from rich.console import Console

from tend import paths, secrets, skill_update


def setup_command(
    noninteractive: bool = typer.Option(
        False, "--noninteractive",
        help="Fail fast on missing values instead of prompting.",
    ),
    no_validate: bool = typer.Option(
        False, "--no-validate",
        help="Skip API-key validation calls (offline / test use).",
    ),
) -> None:
    """Walk the user through workspace creation, secret setup, and optional skills."""
    console = Console()

    _ensure_workspace(console)
    stt = _ask_stt(noninteractive)
    if stt == "deepgram":
        _ask_secret("DEEPGRAM_API_KEY", noninteractive=noninteractive, console=console)

    tts = _ask_tts(noninteractive)
    if tts == "elevenlabs":
        _ask_secret("ELEVENLABS_API_KEY", noninteractive=noninteractive, console=console)

    _ask_secret("ANTHROPIC_API_KEY", noninteractive=noninteractive, console=console)

    _ensure_webhook_token(console)

    _maybe_install_optional_skills(noninteractive, console)

    console.print("\n[bold green]Setup complete.[/bold green]")
    console.print(
        "Run `tend doctor` to verify, then "
        "`tend service install` to start tend at boot."
    )


def _ensure_workspace(console: Console) -> None:
    from tend import __version__ as TEND_VERSION
    state = paths.detect_workspace_state()
    if state == paths.WorkspaceState.INITIALIZED:
        console.print(f"  Using existing workspace at {paths.tend_home()}")
        return
    paths.tend_home().mkdir(parents=True, exist_ok=True)
    skill_update.install_soul()
    skill_update.install_workspace_bin()
    paths.write_version_marker(TEND_VERSION)
    console.print(f"  Initialized workspace at {paths.tend_home()}")


def _ask_stt(noninteractive: bool) -> str:
    if noninteractive:
        return "deepgram" if secrets.secret_backend("DEEPGRAM_API_KEY") else "whisper"
    pick = questionary.select(
        "Speech-to-text provider?",
        choices=["Deepgram (cloud)", "Whisper (local)"],
    ).ask()
    if pick is None:
        raise typer.Exit(code=2)
    return "deepgram" if pick.startswith("Deepgram") else "whisper"


def _ask_tts(noninteractive: bool) -> str:
    if noninteractive:
        return "elevenlabs" if secrets.secret_backend("ELEVENLABS_API_KEY") else "piper"
    pick = questionary.select(
        "Text-to-speech provider?",
        choices=["ElevenLabs (cloud)", "Piper (local)"],
    ).ask()
    if pick is None:
        raise typer.Exit(code=2)
    return "elevenlabs" if pick.startswith("ElevenLabs") else "piper"


def _ask_secret(key: str, *, noninteractive: bool, console: Console) -> None:
    """Prompt for `key` if not already set. Stores via secrets.set_secret."""
    if secrets.secret_backend(key) is not None:
        if noninteractive:
            return
        val = questionary.password(
            f"{key} (already set; press Enter to keep)",
            default="",
        ).ask()
        if val is None:
            raise typer.Exit(code=2)
        if val == "":
            return
        backend = secrets.set_secret(key, val)
        _announce_backend(key, backend, console)
        return

    if noninteractive:
        console.print(
            f"[red]{key} is not set; cannot proceed in noninteractive mode.[/red]"
        )
        raise typer.Exit(code=2)

    val = questionary.password(f"{key}").ask()
    if val is None or val == "":
        console.print(f"[red]{key} cannot be empty.[/red]")
        raise typer.Exit(code=2)
    backend = secrets.set_secret(key, val)
    _announce_backend(key, backend, console)


def _announce_backend(key: str, backend: str, console: Console) -> None:
    if backend == "dotenv":
        console.print(
            f"  No system keyring; saved {key} to "
            f"{paths.env_path()} (mode 600)."
        )


def _ensure_webhook_token(console: Console) -> None:
    if secrets.secret_backend("TEND_WEBHOOK_TOKEN") is not None:
        return
    token = _stdlib_secrets.token_urlsafe(32)
    backend = secrets.set_secret("TEND_WEBHOOK_TOKEN", token)
    console.print(
        f"\n  Generated webhook token (give this to external producers):\n"
        f"    {token}"
    )
    _announce_backend("TEND_WEBHOOK_TOKEN", backend, console)


def _maybe_install_optional_skills(noninteractive: bool, console: Console) -> None:
    if noninteractive:
        return
    from tend.skills import enumerate_installable_skills
    candidates = enumerate_installable_skills()
    if not candidates:
        return
    choices = [
        questionary.Choice(title=f"{s.name} — {s.description}", value=s.name)
        for s in candidates
    ]
    picked = questionary.checkbox(
        "Install optional shipped skills?", choices=choices,
    ).ask()
    if picked:
        skill_update.install_optional_skills(picked)
        console.print(f"  Installed: {', '.join(picked)}")

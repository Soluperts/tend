"""`tend setup` — interactive bootstrap wizard."""

from __future__ import annotations

import secrets as _stdlib_secrets
import sys

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
    _persist_tts_provider(tts)

    _ask_secret("ANTHROPIC_API_KEY", noninteractive=noninteractive, console=console)

    _ensure_webhook_token(console)

    _maybe_install_optional_skills(noninteractive, console)

    if sys.platform == "darwin":
        _mac_mic_permission_step(console)
        if tts == "avspeech":
            _mac_premium_voice_nudge(noninteractive, console)

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
    if sys.platform == "darwin":
        return _ask_tts_macos(noninteractive)

    # Linux (existing behavior)
    if noninteractive:
        return "elevenlabs" if secrets.secret_backend("ELEVENLABS_API_KEY") else "piper"
    pick = questionary.select(
        "Text-to-speech provider?",
        choices=["ElevenLabs (cloud)", "Piper (local)"],
    ).ask()
    if pick is None:
        raise typer.Exit(code=2)
    return "elevenlabs" if pick.startswith("ElevenLabs") else "piper"


def _ask_tts_macos(noninteractive: bool) -> str:
    if noninteractive:
        return "elevenlabs" if secrets.secret_backend("ELEVENLABS_API_KEY") else "avspeech"
    pick = questionary.select(
        "How should tend speak?",
        choices=[
            "Apple's built-in voice (free, fast, runs offline)",
            "ElevenLabs (cloud, best quality, paid)",
        ],
    ).ask()
    if pick is None:
        raise typer.Exit(code=2)
    return "avspeech" if pick.startswith("Apple") else "elevenlabs"


def _persist_tts_provider(provider: str, avspeech_voice: str = "") -> None:
    """Write the chosen TTS provider to ~/.tend/tend.toml as top-level keys.

    Settings.tts_provider and Settings.avspeech_voice are flat fields, so
    they must live at the top of tend.toml — not in a `[tts]` section.
    Also migrates any legacy nested keys from older versions of this
    function so existing installs don't need hand-editing.
    """
    from tend import paths
    from tend.cli.voices import _write_toml
    import tomllib

    toml_path = paths.tend_home() / "tend.toml"
    if toml_path.exists():
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    else:
        data = {}

    # Migrate legacy [tts] section from prior versions.
    legacy = data.pop("tts", None) if isinstance(data.get("tts"), dict) else None
    if legacy:
        for k in ("provider", "avspeech_voice"):
            if k in legacy:
                data[("tts_" + k) if k == "provider" else k] = legacy[k]

    data["tts_provider"] = provider
    if avspeech_voice:
        data["avspeech_voice"] = avspeech_voice

    _write_toml(toml_path, data)


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


def _mac_mic_permission_step(console: Console) -> None:
    """Trigger the TCC microphone permission prompt and verify."""
    from tend.checks import probe_microphone_access

    console.print(
        "\n[bold]Microphone access (macOS)[/bold]\n"
        "  macOS will ask to grant tend access to your microphone.\n"
        "  When the prompt appears, click [bold]Allow[/bold]."
    )
    if not questionary.confirm("Continue?", default=True).ask():
        raise typer.Exit(code=2)

    result = probe_microphone_access()
    if result.status == "ok":
        console.print("  [green]✓[/green] Microphone access granted.")
    elif result.status == "warn":
        console.print(
            "  [yellow]⚠[/yellow] Captured silence — confirm the OS prompt "
            "was approved and the mic is unmuted. Run `tend doctor` later to retry."
        )
    else:
        console.print(f"  [red]✗[/red] {result.detail}")
        if result.remediation:
            console.print(f"  remediation: {result.remediation}")


def _mac_premium_voice_nudge(noninteractive: bool, console: Console) -> None:
    """Offer to open VoiceOver Utility for Premium voice install."""
    import subprocess

    console.print(
        "\n[bold]Premium voice (optional, free)[/bold]\n"
        "  Apple's default voice is okay. For a markedly better Premium\n"
        "  voice (~300–600 MB), download one via VoiceOver Utility:\n"
        "    1.  Open VoiceOver Utility (⌃ ⌥ Fn F8)\n"
        "    2.  Speech → Voices → +\n"
        "    3.  Pick a language → pick a voice marked Premium or Enhanced\n"
        "    4.  Click Download → wait for it to finish\n"
        "    5.  Close VoiceOver Utility\n\n"
        "  Note: Siri-quality voices are not accessible to tend; Premium\n"
        "  voices are the highest tier the AVSpeechSynthesizer API exposes."
    )
    if noninteractive or not questionary.confirm(
        "Open VoiceOver Utility now?", default=False,
    ).ask():
        console.print(
            "  Skipped. Later: `open -a 'VoiceOver Utility'`, "
            "then `tend voices set <identifier>`."
        )
        return

    subprocess.run(["open", "-a", "VoiceOver Utility"], check=False)
    console.print(
        "  Opened. After your download finishes, run:\n"
        "    tend voices list\n"
        "    tend voices set <identifier>\n"
        "    tend voices test"
    )

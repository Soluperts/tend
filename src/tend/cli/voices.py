"""`tend voices` — list, select, and test AVSpeechSynthesizer voices."""

from __future__ import annotations

import sys

import typer
from rich.console import Console
from rich.table import Table

voices_app = typer.Typer(help="Manage Apple TTS voices (macOS).")

_QUALITY_NAMES = {1: "Default", 2: "Enhanced", 3: "Premium"}


def _require_macos() -> None:
    if sys.platform != "darwin":
        Console().print(
            "[red]`tend voices` is macOS-only "
            "(uses AVSpeechSynthesizer).[/red]"
        )
        raise typer.Exit(code=1)


def _load_voices() -> list[dict]:
    import AVFoundation
    out = []
    for v in AVFoundation.AVSpeechSynthesisVoice.speechVoices():
        out.append({
            "identifier": v.identifier(),
            "name": v.name(),
            "language": v.language(),
            "quality": int(v.quality()),
        })
    return out


@voices_app.command("list")
def voices_list() -> None:
    """List installed Apple voices, sorted by quality tier."""
    _require_macos()
    voices = _load_voices()
    voices.sort(key=lambda v: (-v["quality"], v["language"], v["name"]))

    console = Console()
    table = Table("Quality", "Language", "Name", "Identifier")
    for v in voices:
        table.add_row(
            _QUALITY_NAMES.get(v["quality"], str(v["quality"])),
            v["language"],
            v["name"],
            v["identifier"],
        )
    console.print(table)


@voices_app.command("set")
def voices_set(
    identifier: str = typer.Argument(..., help="AVSpeechSynthesisVoice identifier."),
) -> None:
    """Write the chosen identifier to [tts] avspeech_voice in tend.toml."""
    _require_macos()
    from tend import paths
    import tomllib

    toml_path = paths.tend_home() / "tend.toml"
    if toml_path.exists():
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    else:
        data = {}
    data.setdefault("tts", {})["avspeech_voice"] = identifier

    # tomllib doesn't write; emit a minimal-but-readable TOML by hand.
    lines = []
    for section, body in data.items():
        lines.append(f"[{section}]")
        for k, val in body.items():
            if isinstance(val, str):
                lines.append(f'{k} = "{val}"')
            else:
                lines.append(f"{k} = {val}")
        lines.append("")
    toml_path.write_text("\n".join(lines), encoding="utf-8")
    Console().print(f"[green]✓[/green] Set tts.avspeech_voice = {identifier}")


@voices_app.command("test")
def voices_test(
    identifier: str = typer.Argument("", help="Identifier (default: configured)."),
) -> None:
    """Speak a short sample with the given (or configured) voice.

    This drives AVSpeechSynthesizer's system-speaker path directly so
    you can hear the voice immediately. It does not exercise the
    pipecat pipeline — for that, run `tend` and wake it normally.
    """
    _require_macos()
    import time

    import AVFoundation

    from tend.config import Settings

    settings = Settings()
    voice_id = identifier or settings.avspeech_voice

    synth = AVFoundation.AVSpeechSynthesizer.new()
    utt = AVFoundation.AVSpeechUtterance.speechUtteranceWithString_(
        "tend is now using this voice. It sounds like this."
    )
    if voice_id:
        voice = AVFoundation.AVSpeechSynthesisVoice.voiceWithIdentifier_(voice_id)
        if voice is not None:
            utt.setVoice_(voice)

    Console().print(
        f"Speaking sample with voice "
        f"[bold]{voice_id or '(system default)'}[/bold]..."
    )
    synth.speakUtterance_(utt)
    # speakUtterance_ is async on a private runloop; poll until it finishes.
    while synth.isSpeaking():
        time.sleep(0.1)
    Console().print("[green]✓[/green] Done.")

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
    """Write the chosen identifier to avspeech_voice in tend.toml.

    Settings.avspeech_voice is a top-level field; we write it that way too.
    Also migrates any legacy `[tts] avspeech_voice` (or `[tts] provider`)
    found in the existing file up to the top level so the user doesn't
    have to hand-edit.
    """
    _require_macos()
    from tend import paths
    import tomllib

    toml_path = paths.tend_home() / "tend.toml"
    if toml_path.exists():
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    else:
        data = {}

    # Migrate legacy nested keys (we used to write under [tts]; Settings reads them flat).
    legacy = data.pop("tts", None) if isinstance(data.get("tts"), dict) else None
    if legacy:
        for k in ("provider", "avspeech_voice"):
            if k in legacy and k not in data:
                data[("tts_" + k) if k == "provider" else k] = legacy[k]

    data["avspeech_voice"] = identifier

    _write_toml(toml_path, data)
    Console().print(f"[green]✓[/green] Set avspeech_voice = {identifier}")


def _write_toml(path, data: dict) -> None:
    """Serialize a flat-then-section dict to TOML by hand.

    Sections (sub-dicts) are written last so top-level scalars stay above
    them — that's the only way pydantic-settings reads top-level fields
    correctly from the same file that also has nested [webhook] / etc.
    """
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    sections = {k: v for k, v in data.items() if isinstance(v, dict)}

    def fmt(val):
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, str):
            return f'"{val}"'
        return str(val)

    lines = [f"{k} = {fmt(v)}" for k, v in scalars.items()]
    for section, body in sections.items():
        lines.append("")
        lines.append(f"[{section}]")
        for k, v in body.items():
            lines.append(f"{k} = {fmt(v)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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

    import objc
    import AVFoundation
    from Foundation import NSDate, NSObject, NSRunLoop

    from tend.config import Settings

    settings = Settings()
    voice_id = identifier or settings.avspeech_voice

    # AVSpeechSynthesizer schedules speech on a private dispatch queue
    # that only runs while the main runloop is pumped. We use a delegate
    # to detect completion + pump the runloop ourselves until done.
    class _SpeechDelegate(NSObject):
        def init(self):
            self = objc.super(_SpeechDelegate, self).init()
            if self is None:
                return None
            self.done = False
            self.failed = None
            return self

        def speechSynthesizer_didFinishSpeechUtterance_(self, _synth, _utt):
            self.done = True

        def speechSynthesizer_didCancelSpeechUtterance_(self, _synth, _utt):
            self.done = True
            self.failed = "cancelled"

    synth = AVFoundation.AVSpeechSynthesizer.new()
    delegate = _SpeechDelegate.alloc().init()
    synth.setDelegate_(delegate)

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

    # Pump the main runloop in 100 ms slices until the delegate fires.
    # Hard cap at 30 s to avoid hanging on a stuck engine.
    loop = NSRunLoop.currentRunLoop()
    deadline = 30.0
    elapsed = 0.0
    slice_ = 0.1
    while not delegate.done and elapsed < deadline:
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(slice_))
        elapsed += slice_

    if not delegate.done:
        Console().print(
            "[yellow]⚠[/yellow] Speech did not complete within 30 s. "
            "Check Sound output, volume, and that the selected voice is installed."
        )
        raise typer.Exit(code=1)

    if delegate.failed:
        Console().print(f"[red]✗[/red] Speech {delegate.failed}.")
        raise typer.Exit(code=1)

    Console().print("[green]✓[/green] Done.")

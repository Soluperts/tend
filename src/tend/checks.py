"""Pure check functions consumed by `tend setup` and `tend doctor`.

Each check returns a CheckResult. The two commands differ only in
side-effects: doctor only reads, setup may prompt the user to fix a fail.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Literal

from tend import paths, secrets
from tend.config import Settings


CheckStatus = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str
    remediation: str | None = None


def check_workspace() -> CheckResult:
    from tend.paths import WorkspaceState, detect_workspace_state
    state = detect_workspace_state()
    if state == WorkspaceState.INITIALIZED:
        marker = paths.read_version_marker()
        return CheckResult(
            "workspace", "ok",
            f"version {marker} at {paths.tend_home()}",
        )
    if state == WorkspaceState.MISSING:
        return CheckResult(
            "workspace", "fail",
            f"no workspace at {paths.tend_home()}",
            remediation="run `tend setup` to create one",
        )
    if state == WorkspaceState.UNCLAIMED:
        return CheckResult(
            "workspace", "fail",
            f"workspace at {paths.tend_home()} has no .tend-version marker",
            remediation="run `tend setup` to claim this workspace",
        )
    return CheckResult(
        "workspace", "fail",
        f"workspace was written by a newer tend ({paths.read_version_marker()})",
        remediation="upgrade tend with `pip install -U tend`",
    )


def check_config(settings: Settings | None = None) -> CheckResult:
    try:
        settings = settings or Settings()
    except Exception as e:
        return CheckResult(
            "config", "fail",
            f"Settings() failed: {e}",
            remediation=f"edit {paths.toml_path()} and re-run",
        )
    n_workers = len(settings.workers)
    return CheckResult(
        "config", "ok",
        f"tend.toml parses; {n_workers} worker override(s)",
    )


def _secret_present_check(
    name: str, key: str, *, warn_only: bool = False,
) -> CheckResult:
    backend = secrets.secret_backend(key)
    if backend is not None:
        return CheckResult(name, "ok", f"{key} present")
    status: CheckStatus = "warn" if warn_only else "fail"
    return CheckResult(
        name, status, f"{key} not set",
        remediation=f"run `tend setup` and provide a value for {key}",
    )


def check_anthropic_key() -> CheckResult:
    return _secret_present_check("anthropic_key", "ANTHROPIC_API_KEY")


def check_webhook_token() -> CheckResult:
    return _secret_present_check(
        "webhook_token", "TEND_WEBHOOK_TOKEN", warn_only=True,
    )


def check_stt(settings: Settings) -> CheckResult:
    if secrets.secret_backend("DEEPGRAM_API_KEY"):
        return CheckResult("stt", "ok", "Deepgram key present")
    return CheckResult(
        "stt", "ok",
        f"local whisper ({settings.whisper_model}) — will download on first run",
    )


def check_tts(settings: Settings) -> CheckResult:
    if secrets.secret_backend("ELEVENLABS_API_KEY"):
        return CheckResult("tts", "ok", "ElevenLabs key present")
    return CheckResult(
        "tts", "ok",
        f"local piper voice ({settings.piper_voice})",
    )


# openwakeword bundles a small catalog of pretrained models in its package
# data; the named model in settings has to be in that catalog or be a path
# to a file on disk.
_BUNDLED_OWW_MODELS = {
    "alexa", "hey_jarvis", "hey_mycroft", "hey_rhasspy",
    "timer", "weather",
}


def check_wake_model(settings: Settings) -> CheckResult:
    name = settings.openwakeword_model
    if name in _BUNDLED_OWW_MODELS:
        return CheckResult(
            "wake_model", "ok", f"bundled openwakeword model: {name}",
        )
    from pathlib import Path as _P
    if _P(name).expanduser().is_file():
        return CheckResult("wake_model", "ok", f"custom model file: {name}")
    return CheckResult(
        "wake_model", "fail",
        f"openwakeword model {name!r} not bundled and file not found",
        remediation=(
            "set openwakeword_model in tend.toml to one of: "
            + ", ".join(sorted(_BUNDLED_OWW_MODELS))
        ),
    )


def check_claude_cli() -> CheckResult:
    from tend.preflight import claude_cli_preflight
    if claude_cli_preflight():
        return CheckResult("claude_cli", "ok", "claude CLI authenticated")
    return CheckResult(
        "claude_cli", "fail",
        "claude CLI not on PATH or not authenticated",
        remediation="install Claude Code, then run `claude auth login`",
    )


def _audio_devices() -> list[dict]:
    """Enumerate input/output audio devices via PyAudio.

    Returns a list of dicts with at least name, maxInputChannels,
    maxOutputChannels. Empty list on import failure.
    """
    try:
        import pyaudio
    except ImportError:
        return []
    pa = pyaudio.PyAudio()
    try:
        return [pa.get_device_info_by_index(i)
                for i in range(pa.get_device_count())]
    finally:
        pa.terminate()


def check_audio() -> CheckResult:
    devs = _audio_devices()
    n_in = sum(1 for d in devs if d.get("maxInputChannels", 0) >= 1)
    n_out = sum(1 for d in devs if d.get("maxOutputChannels", 0) >= 1)
    if n_in >= 1 and n_out >= 1:
        return CheckResult(
            "audio", "ok",
            f"{n_in} input, {n_out} output device(s)",
        )
    return CheckResult(
        "audio", "fail",
        f"need at least one input and one output device "
        f"({n_in} input, {n_out} output found)",
        remediation="plug in a USB mic + speaker; check `arecord -l` and `aplay -l`",
    )


def check_gws_cli() -> CheckResult:
    if shutil.which("gws") is not None:
        return CheckResult("gws_cli", "ok", "gws CLI on PATH")
    return CheckResult(
        "gws_cli", "warn",
        "gws CLI not on PATH — google skills will fail",
        remediation="npm i -g @googleworkspace/cli && gws auth login",
    )


def run_all(settings: Settings | None = None) -> list[CheckResult]:
    s = settings or Settings()
    return [
        check_workspace(),
        check_config(s),
        check_anthropic_key(),
        check_stt(s),
        check_tts(s),
        check_wake_model(s),
        check_claude_cli(),
        check_audio(),
        check_gws_cli(),
        check_webhook_token(),
    ]


def aggregate_exit_code(results: list[CheckResult]) -> int:
    if any(r.status == "fail" for r in results):
        return 2
    if any(r.status == "warn" for r in results):
        return 1
    return 0

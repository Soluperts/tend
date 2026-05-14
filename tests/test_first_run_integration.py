"""End-to-end: setup → doctor → service install, all from CLI."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TEND_HOME", str(tmp_path / "tend-home"))
    import keyring.errors as kerr
    import tend.secrets as sec
    monkeypatch.setattr(
        sec._keyring, "set_password",
        lambda *a, **kw: (_ for _ in ()).throw(kerr.NoKeyringError("x")),
    )
    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: None)

    import tend.preflight as pf
    monkeypatch.setattr(pf, "claude_cli_preflight", lambda: True)
    monkeypatch.setattr(
        "tend.checks._audio_devices",
        lambda: [
            {"name": "mic", "maxInputChannels": 1, "maxOutputChannels": 0},
            {"name": "spkr", "maxInputChannels": 0, "maxOutputChannels": 2},
        ],
    )
    monkeypatch.setattr("shutil.which", lambda c: "/bin/x")

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0))

    from tend.checks import CheckResult
    monkeypatch.setattr(
        "tend.checks.probe_microphone_access",
        lambda: CheckResult("microphone", "ok", "test stub"),
    )
    monkeypatch.setattr(
        "tend.checks.check_aec_engine",
        lambda s: CheckResult("aec_engine", "ok", "AEC: stub (test)"),
    )
    return tmp_path


def test_setup_then_doctor_then_service_install(isolated, monkeypatch):
    """Canned answers for setup, expect doctor to pass, service install to write."""
    answers = iter([
        "Deepgram (cloud)",
        "dg-key",
        "ElevenLabs (cloud)",
        "el-key",
        "sk-anth",
        [],
        True,   # macOS: confirm mic permission step
    ])

    def factory(*a, **kw):
        m = MagicMock()
        m.ask = lambda: next(answers)
        return m

    import questionary
    for fname in ("text", "password", "select", "checkbox", "confirm"):
        monkeypatch.setattr(questionary, fname, factory)

    from tend.cli import main
    assert main(["setup", "--no-validate"]) == 0
    assert main(["doctor"]) == 0
    assert main(["service", "install"]) == 0

    if sys.platform == "darwin":
        assert (isolated / "Library" / "LaunchAgents" / "com.tend.daemon.plist").exists()
    else:
        assert (isolated / ".config" / "systemd" / "user" / "tend.service").exists()

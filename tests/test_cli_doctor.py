"""`tend doctor` — read-only diagnostic CLI."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def all_ok(monkeypatch, tmp_path):
    """Seed an all-green state."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend import paths
    paths.write_version_marker(__import__("tend").__version__)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    monkeypatch.setenv("TEND_WEBHOOK_TOKEN", "x")
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
    # Stub new checks that require hardware/optional deps.
    from tend.checks import CheckResult
    monkeypatch.setattr(
        "tend.checks.probe_microphone_access",
        lambda: CheckResult("microphone", "ok", "mocked ok"),
    )
    monkeypatch.setattr(
        "tend.checks.check_tts_provider",
        lambda s: CheckResult("tts_provider", "ok", "mocked ok"),
    )
    monkeypatch.setattr(
        "tend.checks.check_aec_engine",
        lambda s: CheckResult("aec_engine", "ok", "mocked ok"),
    )
    return tmp_path


def test_doctor_all_ok_exits_zero(capsys, all_ok):
    from tend.cli import main
    rc = main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "workspace" in out
    assert "anthropic_key" in out


def test_doctor_warn_only_exits_one(monkeypatch, all_ok, capsys):
    monkeypatch.delenv("TEND_WEBHOOK_TOKEN", raising=False)
    import tend.secrets as sec
    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: None)
    from tend.cli import main
    rc = main(["doctor"])
    assert rc == 1


def test_doctor_fail_exits_two(monkeypatch, all_ok):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import tend.secrets as sec
    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: None)
    from tend.cli import main
    rc = main(["doctor"])
    assert rc == 2


def test_doctor_json_emits_array(capsys, all_ok):
    from tend.cli import main
    main(["doctor", "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert {r["name"] for r in parsed} >= {"workspace", "anthropic_key"}
    assert all("status" in r for r in parsed)


def test_doctor_never_prints_secret_values(monkeypatch, all_ok, capsys):
    """Raw secret values must never leak to output."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-DO-NOT-PRINT-ME")
    from tend.cli import main
    main(["doctor"])
    out = capsys.readouterr().out
    assert "sk-DO-NOT-PRINT-ME" not in out

# SPDX-License-Identifier: MIT
"""`tend setup` — interactive bootstrap wizard."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    # Stub keyring so .env fallback is exercised deterministically.
    import keyring.errors as kerr
    import tend.secrets as sec
    monkeypatch.setattr(
        sec._keyring, "set_password",
        lambda *a, **kw: (_ for _ in ()).throw(kerr.NoKeyringError("x")),
    )
    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: None)
    return tmp_path


@pytest.fixture(autouse=True)
def mock_probe_microphone(monkeypatch):
    """Prevent real microphone access during tests; return a successful probe."""
    from tend.checks import CheckResult
    monkeypatch.setattr(
        "tend.checks.probe_microphone_access",
        lambda: CheckResult("microphone", "ok", "test stub"),
    )


@pytest.fixture
def canned_answers(monkeypatch):
    """Feed canned answers for the questionary prompts in order."""
    def make(answers: list):
        idx = {"i": 0}

        def _next():
            v = answers[idx["i"]]
            idx["i"] += 1
            return v

        import questionary as q
        # Each prompt factory returns an object whose .ask() returns the next canned answer.
        def factory(*a, **kw):
            m = MagicMock()
            m.ask = lambda: _next()
            return m
        for prompt in ("text", "password", "select", "checkbox", "confirm"):
            monkeypatch.setattr(q, prompt, factory)
        return _next
    return make


def test_setup_creates_workspace_and_writes_secrets(
    isolated, canned_answers,
):
    canned_answers([
        "Deepgram (cloud)",   # STT
        "dg-test-key",         # DEEPGRAM_API_KEY
        "ElevenLabs (cloud)",  # TTS
        "el-test-key",         # ELEVENLABS_API_KEY
        "sk-anthropic-test",   # ANTHROPIC_API_KEY
        [],                    # no optional skills
        True,                  # macOS: confirm mic permission step
    ])
    from tend.cli import main
    rc = main(["setup", "--no-validate"])
    assert rc == 0
    from tend import paths
    assert paths.version_marker_path().exists()
    env = paths.env_path().read_text(encoding="utf-8")
    assert "DEEPGRAM_API_KEY=dg-test-key" in env
    assert "ELEVENLABS_API_KEY=el-test-key" in env
    assert "ANTHROPIC_API_KEY=sk-anthropic-test" in env
    assert "TEND_WEBHOOK_TOKEN=" in env


def test_setup_skips_secret_prompts_when_already_set(
    monkeypatch, isolated, canned_answers,
):
    """When a secret is already in env, the prompt accepts empty → keep."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "preset-key")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "preset-dg")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "preset-el")
    canned_answers([
        "Deepgram (cloud)",
        "",
        "ElevenLabs (cloud)",
        "",
        "",
        [],
        True,                  # macOS: confirm mic permission step
    ])
    from tend.cli import main
    rc = main(["setup", "--no-validate"])
    assert rc == 0
    env = (isolated / ".env").read_text(encoding="utf-8")
    assert "TEND_WEBHOOK_TOKEN=" in env


def test_setup_writes_webhook_token_with_url_safe_chars(
    isolated, canned_answers,
):
    canned_answers([
        "Whisper (local)",
        "ElevenLabs (cloud)",
        "el-test-key",
        "sk-anthropic-test",
        [],
        True,                  # macOS: confirm mic permission step
    ])
    from tend.cli import main
    main(["setup", "--no-validate"])
    from tend import paths
    env = paths.env_path().read_text(encoding="utf-8")
    line = [l for l in env.splitlines() if l.startswith("TEND_WEBHOOK_TOKEN=")][0]
    tok = line.split("=", 1)[1]
    assert len(tok) >= 32
    import string
    assert all(c in string.ascii_letters + string.digits + "-_" for c in tok)


def test_setup_noninteractive_fails_fast_on_missing_secret(
    isolated, monkeypatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from tend.cli import main
    rc = main(["setup", "--noninteractive", "--no-validate"])
    assert rc != 0


def test_setup_never_prints_existing_secret_value(
    monkeypatch, isolated, canned_answers, capsys,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-MUST-NOT-LEAK")
    canned_answers([
        "Whisper (local)",
        "ElevenLabs (cloud)",
        "el-test-key",
        "",
        [],
        True,                  # macOS: confirm mic permission step
    ])
    from tend.cli import main
    main(["setup", "--no-validate"])
    out = capsys.readouterr().out
    assert "sk-MUST-NOT-LEAK" not in out

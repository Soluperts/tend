# SPDX-License-Identifier: MIT
"""tend.secrets — keyring-first with $TEND_HOME/.env fallback."""

from __future__ import annotations

import stat

import pytest


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    return tmp_path


def test_known_secrets_list_is_canonical():
    from tend.secrets import list_known_secrets
    assert list_known_secrets() == [
        "ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY", "TEND_WEBHOOK_TOKEN",
    ]


def test_set_secret_writes_to_keyring_when_available(monkeypatch, isolated_home):
    import tend.secrets as sec
    captured = {}

    def fake_set(service, user, value):
        captured["service"] = service
        captured["user"] = user
        captured["value"] = value

    monkeypatch.setattr(sec._keyring, "set_password", fake_set)
    backend = sec.set_secret("ANTHROPIC_API_KEY", "sk-test")
    assert backend == "keyring"
    assert captured == {"service": "tend", "user": "ANTHROPIC_API_KEY", "value": "sk-test"}


def test_set_secret_falls_back_to_dotenv_when_keyring_unavailable(
    monkeypatch, isolated_home,
):
    import tend.secrets as sec
    import keyring.errors as kerr

    def fake_set(*a, **kw):
        raise kerr.NoKeyringError("no backend")

    monkeypatch.setattr(sec._keyring, "set_password", fake_set)
    backend = sec.set_secret("ANTHROPIC_API_KEY", "sk-test")
    assert backend == "dotenv"

    env_path = isolated_home / ".env"
    assert env_path.exists()
    content = env_path.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-test" in content
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_set_secret_dotenv_replaces_existing_key(monkeypatch, isolated_home):
    import tend.secrets as sec
    import keyring.errors as kerr

    monkeypatch.setattr(
        sec._keyring, "set_password",
        lambda *a, **kw: (_ for _ in ()).throw(kerr.NoKeyringError("x")),
    )

    sec.set_secret("ANTHROPIC_API_KEY", "first")
    sec.set_secret("ANTHROPIC_API_KEY", "second")

    content = (isolated_home / ".env").read_text(encoding="utf-8")
    assert content.count("ANTHROPIC_API_KEY=") == 1
    assert "ANTHROPIC_API_KEY=second" in content


def test_get_secret_prefers_env_var(monkeypatch, isolated_home):
    import tend.secrets as sec
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-val")
    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: "keyring-val")
    (isolated_home / ".env").write_text("ANTHROPIC_API_KEY=dotenv-val\n")
    assert sec.get_secret("ANTHROPIC_API_KEY") == "env-val"


def test_get_secret_falls_back_to_keyring(monkeypatch, isolated_home):
    import tend.secrets as sec
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        sec._keyring, "get_password",
        lambda s, u: "keyring-val" if u == "ANTHROPIC_API_KEY" else None,
    )
    assert sec.get_secret("ANTHROPIC_API_KEY") == "keyring-val"


def test_get_secret_falls_back_to_dotenv(monkeypatch, isolated_home):
    import tend.secrets as sec
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: None)
    (isolated_home / ".env").write_text("ANTHROPIC_API_KEY=dotenv-val\n")
    assert sec.get_secret("ANTHROPIC_API_KEY") == "dotenv-val"


def test_get_secret_none_when_unset(monkeypatch, isolated_home):
    import tend.secrets as sec
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: None)
    assert sec.get_secret("ANTHROPIC_API_KEY") is None


def test_secret_backend_reports_where_a_secret_lives(monkeypatch, isolated_home):
    import tend.secrets as sec
    monkeypatch.setenv("DEEPGRAM_API_KEY", "env-val")
    assert sec.secret_backend("DEEPGRAM_API_KEY") == "env"

    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setattr(
        sec._keyring, "get_password",
        lambda s, u: "x" if u == "DEEPGRAM_API_KEY" else None,
    )
    assert sec.secret_backend("DEEPGRAM_API_KEY") == "keyring"

    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: None)
    (isolated_home / ".env").write_text("DEEPGRAM_API_KEY=x\n")
    assert sec.secret_backend("DEEPGRAM_API_KEY") == "dotenv"

    (isolated_home / ".env").unlink()
    assert sec.secret_backend("DEEPGRAM_API_KEY") is None

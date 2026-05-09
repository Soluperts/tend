"""Tests for tend.config.Settings — TOML, env, and default precedence."""

import os

import pytest


def test_settings_loads_defaults_when_no_toml_no_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k in list(os.environ):
        if k.startswith("TEND_"):
            monkeypatch.delenv(k)

    from tend.config import Settings
    s = Settings()

    assert s.llm_model == "claude-haiku-4-5"
    assert s.openwakeword_model == "hey_jarvis"
    assert s.wake_threshold == 0.5
    assert s.sleep_phrase == "goodbye jarvis"
    assert s.sleep_fuzz_ratio == 0.85
    assert s.awake_timeout_s == 30
    assert s.daily_reset_time == "04:00"
    assert s.anthropic_api_key is None


def test_settings_reads_tend_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tend.toml").write_text(
        'llm_model = "claude-opus-4-7"\n'
        "awake_timeout_s = 60\n"
    )
    for k in list(os.environ):
        if k.startswith("TEND_"):
            monkeypatch.delenv(k)

    from tend.config import Settings
    s = Settings()

    assert s.llm_model == "claude-opus-4-7"
    assert s.awake_timeout_s == 60
    # other fields fall back to defaults
    assert s.openwakeword_model == "hey_jarvis"


def test_env_overrides_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tend.toml").write_text('llm_model = "from-toml"\n')
    monkeypatch.setenv("TEND_LLM_MODEL", "from-env")

    from tend.config import Settings
    s = Settings()

    assert s.llm_model == "from-env"


def test_secrets_loaded_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")

    from tend.config import Settings
    s = Settings()

    assert s.anthropic_api_key == "sk-test-anthropic"
    assert s.deepgram_api_key == "dg-test"
    assert s.elevenlabs_api_key == "el-test"


def test_scheduler_config_defaults_present():
    """The model itself ships sensible defaults; tend.toml may override."""
    from tend.config import SchedulerConfig
    c = SchedulerConfig()
    assert c.heartbeat_every == "30m"
    assert c.missed_at_policy == "run-on-restart"


def test_webhook_config_defaults_present():
    from tend.config import WebhookConfig
    c = WebhookConfig()
    assert c.host == "127.0.0.1"
    assert c.port == 7331


def test_announcer_config_defaults_present():
    from tend.config import AnnouncerConfig
    c = AnnouncerConfig()
    assert c.default_cooldown_s == 300
    assert isinstance(c.category, dict)


def test_webhook_token_from_env(monkeypatch):
    from tend.config import Settings
    monkeypatch.setenv("TEND_WEBHOOK_TOKEN", "shh")
    s = Settings()
    assert s.tend_webhook_token == "shh"

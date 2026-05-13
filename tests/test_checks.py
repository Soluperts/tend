"""tend.checks — pure check functions consumed by setup + doctor."""

from __future__ import annotations

import pytest


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    return tmp_path


def _settings():
    from tend.config import Settings
    return Settings()


def test_check_result_shape():
    from tend.checks import CheckResult
    r = CheckResult(name="x", status="ok", detail="d")
    assert r.name == "x"
    assert r.status == "ok"
    assert r.remediation is None


def test_workspace_check_ok_when_initialized(isolated):
    from tend import paths
    paths.write_version_marker(__import__("tend").__version__)
    from tend.checks import check_workspace
    r = check_workspace()
    assert r.status == "ok"


def test_workspace_check_fail_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path / "missing"))
    from tend.checks import check_workspace
    r = check_workspace()
    assert r.status == "fail"
    assert r.remediation is not None


def test_workspace_check_fail_when_unclaimed(isolated):
    from tend.checks import check_workspace
    r = check_workspace()
    assert r.status == "fail"


def test_config_check_ok_when_settings_construct(isolated):
    from tend.checks import check_config
    r = check_config(_settings())
    assert r.status == "ok"


def test_anthropic_key_check_fail_when_unset(monkeypatch, isolated):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import tend.secrets as sec
    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: None)
    from tend.checks import check_anthropic_key
    assert check_anthropic_key().status == "fail"


def test_anthropic_key_check_ok_when_env_set(monkeypatch, isolated):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from tend.checks import check_anthropic_key
    assert check_anthropic_key().status == "ok"


def test_stt_check_ok_with_deepgram_key(monkeypatch, isolated):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    from tend.checks import check_stt
    assert check_stt(_settings()).status == "ok"


def test_stt_check_ok_or_warn_without_deepgram(monkeypatch, isolated):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    import tend.secrets as sec
    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: None)
    from tend.checks import check_stt
    assert check_stt(_settings()).status in {"ok", "warn"}


def test_tts_check_ok_with_elevenlabs_key(monkeypatch, isolated):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    from tend.checks import check_tts
    assert check_tts(_settings()).status == "ok"


def test_wake_model_check_ok_with_default_model(isolated):
    from tend.checks import check_wake_model
    r = check_wake_model(_settings())
    assert r.status == "ok"


def test_claude_cli_check_uses_preflight(monkeypatch, isolated):
    import tend.preflight as pf
    monkeypatch.setattr(pf, "claude_cli_preflight", lambda: True)
    from tend.checks import check_claude_cli
    assert check_claude_cli().status == "ok"

    monkeypatch.setattr(pf, "claude_cli_preflight", lambda: False)
    assert check_claude_cli().status == "fail"


def test_audio_check_ok_with_both_devices(monkeypatch, isolated):
    fake_devices = [
        {"name": "mic", "maxInputChannels": 1, "maxOutputChannels": 0},
        {"name": "spkr", "maxInputChannels": 0, "maxOutputChannels": 2},
    ]
    monkeypatch.setattr("tend.checks._audio_devices", lambda: fake_devices)
    from tend.checks import check_audio
    assert check_audio().status == "ok"


def test_audio_check_fail_when_no_input(monkeypatch, isolated):
    fake_devices = [
        {"name": "spkr", "maxInputChannels": 0, "maxOutputChannels": 2},
    ]
    monkeypatch.setattr("tend.checks._audio_devices", lambda: fake_devices)
    from tend.checks import check_audio
    assert check_audio().status == "fail"


def test_gws_cli_check_warns_when_missing(monkeypatch, isolated):
    monkeypatch.setattr("shutil.which", lambda c: None if c == "gws" else "/bin/x")
    from tend.checks import check_gws_cli
    assert check_gws_cli().status == "warn"


def test_webhook_token_check_warns_when_missing(monkeypatch, isolated):
    monkeypatch.delenv("TEND_WEBHOOK_TOKEN", raising=False)
    import tend.secrets as sec
    monkeypatch.setattr(sec._keyring, "get_password", lambda *a: None)
    from tend.checks import check_webhook_token
    assert check_webhook_token().status == "warn"


def test_run_all_returns_one_result_per_check(monkeypatch, isolated):
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
    from tend import paths
    paths.write_version_marker(__import__("tend").__version__)
    from tend.checks import run_all
    results = run_all(_settings())
    assert {r.name for r in results} == {
        "workspace", "config", "anthropic_key", "stt", "tts",
        "wake_model", "claude_cli", "audio", "gws_cli", "webhook_token",
    }


def test_aggregate_exit_code_ok():
    from tend.checks import CheckResult, aggregate_exit_code
    rs = [CheckResult(name=str(i), status="ok", detail="d") for i in range(3)]
    assert aggregate_exit_code(rs) == 0


def test_aggregate_exit_code_warn_only():
    from tend.checks import CheckResult, aggregate_exit_code
    rs = [
        CheckResult(name="a", status="ok", detail="d"),
        CheckResult(name="b", status="warn", detail="d", remediation="r"),
    ]
    assert aggregate_exit_code(rs) == 1


def test_aggregate_exit_code_with_fail():
    from tend.checks import CheckResult, aggregate_exit_code
    rs = [
        CheckResult(name="a", status="warn", detail="d", remediation="r"),
        CheckResult(name="b", status="fail", detail="d", remediation="r"),
    ]
    assert aggregate_exit_code(rs) == 2

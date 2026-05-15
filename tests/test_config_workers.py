# SPDX-License-Identifier: MIT
"""WorkerConfig is loaded per-worker from tend.toml."""

from tend.config import Settings, WorkerConfig


def test_worker_config_defaults():
    cfg = WorkerConfig()
    assert cfg.model is None
    assert cfg.setting_sources == "user"
    assert cfg.allowed_tools == []
    # bypassPermissions is the default — daemon has no human at the terminal
    # to approve permission prompts. Tightening is opt-in via tend.toml.
    assert cfg.permission_mode == "bypassPermissions"
    assert cfg.mcp_config_path is None
    assert cfg.workspace_dir is None


def test_worker_config_permission_mode_override(tmp_path, monkeypatch):
    """User can opt into a stricter permission mode in tend.toml."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    toml = tmp_path / "tend.toml"
    toml.write_text(
        """
[workers.general]
permission_mode = "default"
allowed_tools = ["Read", "Edit"]
"""
    )
    s = Settings()
    assert s.workers["general"].permission_mode == "default"
    assert s.workers["general"].allowed_tools == ["Read", "Edit"]


def test_settings_loads_workers_block_from_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    toml = tmp_path / "tend.toml"
    toml.write_text(
        """
[workers.general]
model = "claude-opus-4-7"
setting_sources = "user,project,local"
allowed_tools = ["Read", "Edit", "Bash"]

[workers.meal_plan]
model = "claude-sonnet-4-6"
allowed_tools = ["mcp__google_calendar__*"]
"""
    )
    s = Settings()
    assert "general" in s.workers
    assert s.workers["general"].model == "claude-opus-4-7"
    assert s.workers["general"].setting_sources == "user,project,local"
    assert s.workers["general"].allowed_tools == ["Read", "Edit", "Bash"]
    assert s.workers["meal_plan"].setting_sources == "user"
    assert s.workers["meal_plan"].allowed_tools == ["mcp__google_calendar__*"]


def test_settings_workers_default_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    s = Settings()
    assert s.workers == {}

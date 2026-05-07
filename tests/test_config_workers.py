"""WorkerConfig is loaded per-worker from tend.toml."""

from tend.config import Settings, WorkerConfig


def test_worker_config_defaults():
    cfg = WorkerConfig()
    assert cfg.model is None
    assert cfg.setting_sources == "user"
    assert cfg.allowed_tools == []
    assert cfg.mcp_config_path is None


def test_settings_loads_workers_block_from_toml(tmp_path, monkeypatch):
    toml = tmp_path / "tend.toml"
    toml.write_text(
        """
[workers.coding]
model = "claude-opus-4-7"
setting_sources = "user,project,local"
allowed_tools = ["Read", "Edit", "Bash"]

[workers.meal_plan]
model = "claude-sonnet-4-6"
allowed_tools = ["mcp__google_calendar__*"]
"""
    )
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert "coding" in s.workers
    assert s.workers["coding"].model == "claude-opus-4-7"
    assert s.workers["coding"].setting_sources == "user,project,local"
    assert s.workers["coding"].allowed_tools == ["Read", "Edit", "Bash"]
    assert s.workers["meal_plan"].setting_sources == "user"  # default kicks in
    assert s.workers["meal_plan"].allowed_tools == ["mcp__google_calendar__*"]


def test_settings_workers_default_empty(tmp_path, monkeypatch):
    # Pin to a directory with no tend.toml so this test can't be silently
    # broken when Task 10 adds [workers.coding] to the project root tend.toml.
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.workers == {}

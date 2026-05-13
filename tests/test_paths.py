"""Tests for tend.paths — single source of truth for filesystem paths."""

from __future__ import annotations

from pathlib import Path


def test_tend_home_default(monkeypatch):
    monkeypatch.delenv("TEND_HOME", raising=False)
    from tend.paths import tend_home
    assert tend_home() == Path.home() / ".config" / "tend"


def test_tend_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import tend_home
    assert tend_home() == tmp_path


def test_tend_home_env_not_expanded(monkeypatch):
    """$TEND_HOME with a leading ~ stays literal — we don't expand."""
    monkeypatch.setenv("TEND_HOME", "~/custom")
    from tend.paths import tend_home
    assert tend_home() == Path("~/custom")


def test_user_side_paths_anchored(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import (
        user_skills_dir, workspace_bin_dir, soul_path, env_path, toml_path,
        log_path, fault_log_path, cron_root, sessions_dir,
        skills_backup_root, version_marker_path,
    )
    assert user_skills_dir() == tmp_path / "skills"
    assert workspace_bin_dir() == tmp_path / "workspace" / "bin"
    assert soul_path() == tmp_path / "soul.md"
    assert env_path() == tmp_path / ".env"
    assert toml_path() == tmp_path / "tend.toml"
    assert log_path() == tmp_path / "logs" / "tend.log"
    assert fault_log_path() == tmp_path / "logs" / "tend.faults.log"
    assert cron_root() == tmp_path / "cron"
    assert sessions_dir() == tmp_path / "sessions"
    assert skills_backup_root() == tmp_path / "skills-backup"
    assert version_marker_path() == tmp_path / ".tend-version"

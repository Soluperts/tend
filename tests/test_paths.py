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


def test_wheel_side_paths_return_real_paths():
    """importlib.resources.files() must resolve to real Paths the runtime can use."""
    from tend.paths import (
        critical_skills_dir, shipped_skills_dir,
        shipped_soul_md, shipped_workspace_bin,
    )
    assert isinstance(critical_skills_dir(), Path)
    assert isinstance(shipped_skills_dir(), Path)
    assert isinstance(shipped_soul_md(), Path)
    assert isinstance(shipped_workspace_bin(), Path)

    import tend
    pkg_root = Path(tend.__file__).resolve().parent
    # Each wheel-side path lives under the package's _defaults/.
    assert (pkg_root / "_defaults") in critical_skills_dir().parents


def test_critical_skills_dir_caches():
    """Repeated calls return the same path (process-lifetime cache)."""
    from tend.paths import critical_skills_dir
    assert critical_skills_dir() == critical_skills_dir()


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


def test_read_soul_user_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    (tmp_path / "soul.md").write_text("MY CUSTOM PERSONA", encoding="utf-8")
    from tend.paths import read_soul
    assert read_soul() == "MY CUSTOM PERSONA"


def test_read_soul_shipped_fallback(monkeypatch, tmp_path):
    """No user soul.md → falls back to the shipped one (the real one in _defaults)."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import read_soul
    text = read_soul()
    assert "Tend" in text  # Shipped soul.md starts with "You are Tend, …"


def test_read_soul_hardcoded_fallback(monkeypatch, tmp_path):
    """If both user and shipped are missing, return the hardcoded fallback."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend import paths
    from tend.paths import read_soul, DEFAULT_SOUL_FALLBACK
    monkeypatch.setattr(paths, "shipped_soul_md", lambda: tmp_path / "does-not-exist.md")
    assert read_soul() == DEFAULT_SOUL_FALLBACK

# SPDX-License-Identifier: MIT
"""Tests for the skill-update flow primitives.

The Typer CLI wiring (`tend skill update`) lands in sub-project #4. This
module implements the underlying logic the wiring will call, plus the
setup-wizard install_* helpers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def user_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    return tmp_path


def _write_skill(root: Path, name: str, body: str = "minimal body") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {body}\n---\n\n{body}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# compute_skill_updates
# ---------------------------------------------------------------------------

def test_unchanged_skill_yields_no_update(user_home, monkeypatch, tmp_path):
    """Current user dir bytes equal shipped bytes → not in diff list."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "v1")
    _write_skill(user_home / "skills", "briefing", "v1")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import compute_skill_updates
    updates, new = compute_skill_updates()
    assert updates == []
    assert new == []


def test_modified_skill_yields_update(user_home, monkeypatch, tmp_path):
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "shipped-v2")
    _write_skill(user_home / "skills", "briefing", "user-edit")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import compute_skill_updates
    updates, new = compute_skill_updates()
    assert updates == ["briefing"]
    assert new == []


def test_new_shipped_skill_yields_new(user_home, monkeypatch, tmp_path):
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "weather", "v1")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import compute_skill_updates
    updates, new = compute_skill_updates()
    assert updates == []
    assert new == ["weather"]


def test_user_skill_not_in_shipped_untouched(user_home, monkeypatch, tmp_path):
    """A user-authored skill (e.g., meal-plan) not in shipped catalog → never in diff."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "v1")
    _write_skill(user_home / "skills", "briefing", "v1")
    _write_skill(user_home / "skills", "meal-plan", "user-authored")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import compute_skill_updates
    updates, new = compute_skill_updates()
    assert updates == []
    assert new == []


# ---------------------------------------------------------------------------
# apply_skill_updates
# ---------------------------------------------------------------------------

def test_apply_updates_backs_up_then_overwrites(user_home, monkeypatch, tmp_path):
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "shipped-v2")
    _write_skill(user_home / "skills", "briefing", "user-edit")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import apply_skill_updates
    apply_skill_updates(updates=["briefing"], new_to_install=[])

    user_skill_md = user_home / "skills" / "briefing" / "SKILL.md"
    assert "shipped-v2" in user_skill_md.read_text()

    backup_md = user_home / "skills-backup" / "briefing" / "SKILL.md"
    assert "user-edit" in backup_md.read_text()

    manifest = user_home / "skills-backup" / ".restore-manifest.json"
    data = json.loads(manifest.read_text())
    assert "briefing" in data["skills"]
    assert "backed_up_at" in data


def test_apply_updates_replaces_previous_backup(user_home, monkeypatch, tmp_path):
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "shipped-v2")
    _write_skill(user_home / "skills", "briefing", "user-edit-1")

    # Pre-existing stale backup from a "previous" update.
    (user_home / "skills-backup").mkdir()
    (user_home / "skills-backup" / "stale-thing").mkdir()
    (user_home / "skills-backup" / "stale-thing" / "old.txt").write_text("ancient")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import apply_skill_updates
    apply_skill_updates(updates=["briefing"], new_to_install=[])

    assert (user_home / "skills-backup" / "briefing" / "SKILL.md").exists()
    assert not (user_home / "skills-backup" / "stale-thing").exists()


def test_apply_updates_installs_new(user_home, monkeypatch, tmp_path):
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "weather", "shipped-v1")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import apply_skill_updates
    apply_skill_updates(updates=[], new_to_install=["weather"])
    assert (user_home / "skills" / "weather" / "SKILL.md").exists()
    # No backup created — nothing was overwritten.
    assert not (user_home / "skills-backup").exists()


def test_apply_updates_empty_is_noop(user_home, monkeypatch, tmp_path):
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import apply_skill_updates
    apply_skill_updates(updates=[], new_to_install=[])
    assert not (user_home / "skills-backup").exists()


def test_no_changes_preserves_previous_backup(user_home, monkeypatch, tmp_path):
    """Re-running with empty updates leaves an existing backup untouched."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()

    # Pre-existing backup from a prior update.
    backup = user_home / "skills-backup"
    backup.mkdir()
    (backup / "old-skill").mkdir()
    (backup / "old-skill" / "marker").write_text("preserve me")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import apply_skill_updates
    apply_skill_updates(updates=[], new_to_install=[])

    assert (backup / "old-skill" / "marker").read_text() == "preserve me"


# ---------------------------------------------------------------------------
# install_optional_skills / install_workspace_bin / install_soul
# ---------------------------------------------------------------------------

def test_install_optional_skills(user_home, monkeypatch, tmp_path):
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "v1")
    _write_skill(fake_shipped, "mail-triage", "v1")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import install_optional_skills
    install_optional_skills(["briefing"])
    assert (user_home / "skills" / "briefing" / "SKILL.md").exists()
    assert not (user_home / "skills" / "mail-triage").exists()


def test_install_optional_skills_skips_existing(user_home, monkeypatch, tmp_path):
    """If the user already has a skill installed, install_optional_skills does not overwrite."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "shipped")
    _write_skill(user_home / "skills", "briefing", "user-edited")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import install_optional_skills
    install_optional_skills(["briefing"])

    # User's existing version untouched.
    assert "user-edited" in (user_home / "skills" / "briefing" / "SKILL.md").read_text()


def test_install_workspace_bin(user_home, monkeypatch, tmp_path):
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    (fake_bin / "tool.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (fake_bin / "tool.sh").chmod(0o755)

    from tend import paths
    monkeypatch.setattr(paths, "shipped_workspace_bin", lambda: fake_bin)

    from tend.skill_update import install_workspace_bin
    install_workspace_bin()
    target = user_home / "workspace" / "bin" / "tool.sh"
    assert target.exists()
    assert target.stat().st_mode & 0o111  # executable bit preserved


def test_install_soul(user_home, monkeypatch, tmp_path):
    fake_soul = tmp_path / "fake_soul.md"
    fake_soul.write_text("SHIPPED PERSONA", encoding="utf-8")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_soul_md", lambda: fake_soul)

    from tend.skill_update import install_soul
    install_soul()
    assert (user_home / "soul.md").read_text() == "SHIPPED PERSONA"


def test_install_soul_preserves_existing(user_home, monkeypatch, tmp_path):
    fake_soul = tmp_path / "fake_soul.md"
    fake_soul.write_text("SHIPPED", encoding="utf-8")
    (user_home / "soul.md").write_text("USER PERSONA", encoding="utf-8")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_soul_md", lambda: fake_soul)

    from tend.skill_update import install_soul
    install_soul()
    assert (user_home / "soul.md").read_text() == "USER PERSONA"

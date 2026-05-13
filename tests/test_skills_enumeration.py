"""Tests for skill enumeration across the critical (wheel) + user roots."""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_skill(root: Path, name: str, description: str = "test skill") -> None:
    """Helper: write a minimal SKILL.md at root/name/."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _write_skill_with_events(root: Path, name: str, events: list[str]) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    events_yaml = "\n".join(f"  - {e}" for e in events)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\nevents:\n{events_yaml}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def user_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    return tmp_path


def test_critical_only(user_home, monkeypatch, tmp_path):
    """Empty user dir → runtime catalog is just the critical skills."""
    fake_critical = tmp_path / "fake_critical"
    fake_critical.mkdir()
    _write_skill(fake_critical, "heartbeat")
    _write_skill(fake_critical, "schedule-watcher")

    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: fake_critical)

    from tend.skills import enumerate_all_skills
    result = enumerate_all_skills()
    assert sorted(s.name for s in result) == ["heartbeat", "schedule-watcher"]


def test_user_alongside_critical(user_home, monkeypatch, tmp_path):
    """Disjoint names → result is the union."""
    fake_critical = tmp_path / "fake_critical"
    fake_critical.mkdir()
    _write_skill(fake_critical, "heartbeat")
    _write_skill(user_home / "skills", "briefing")
    _write_skill(user_home / "skills", "meal-plan")

    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: fake_critical)

    from tend.skills import enumerate_all_skills
    result = enumerate_all_skills()
    assert sorted(s.name for s in result) == ["briefing", "heartbeat", "meal-plan"]


def test_user_shadows_critical(user_home, monkeypatch, tmp_path):
    """Same-name dir in both → user wins; warning logged."""
    fake_critical = tmp_path / "fake_critical"
    fake_critical.mkdir()
    _write_skill(fake_critical, "heartbeat", description="shipped heartbeat")
    _write_skill(user_home / "skills", "heartbeat", description="user override")

    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: fake_critical)

    from tend.skills import enumerate_all_skills
    result = enumerate_all_skills()
    assert len(result) == 1
    assert result[0].name == "heartbeat"
    assert result[0].description == "user override"


def test_enumerate_all_skills_sorted(user_home, monkeypatch, tmp_path):
    fake_critical = tmp_path / "fake_critical"
    fake_critical.mkdir()
    _write_skill(fake_critical, "zebra")
    _write_skill(user_home / "skills", "alpha")

    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: fake_critical)

    from tend.skills import enumerate_all_skills
    result = enumerate_all_skills()
    assert [s.name for s in result] == ["alpha", "zebra"]


def test_find_event_subscribers_all(user_home, monkeypatch, tmp_path):
    fake_critical = tmp_path / "fake_critical"
    fake_critical.mkdir()
    _write_skill_with_events(fake_critical, "heartbeat", events=["tick"])
    _write_skill_with_events(user_home / "skills", "lunch-prep", events=["lunch"])

    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: fake_critical)

    from tend.skills import find_event_subscribers_all
    tick_subs = find_event_subscribers_all("tick")
    lunch_subs = find_event_subscribers_all("lunch")
    unknown_subs = find_event_subscribers_all("nope")
    assert [s.name for s in tick_subs] == ["heartbeat"]
    assert [s.name for s in lunch_subs] == ["lunch-prep"]
    assert unknown_subs == []


def test_enumerate_installable_excludes_installed(user_home, monkeypatch, tmp_path):
    """Shipped optional catalog minus what the user already has."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing")
    _write_skill(fake_shipped, "mail-triage")
    _write_skill(fake_shipped, "weather")
    _write_skill(user_home / "skills", "briefing")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skills import enumerate_installable_skills
    result = enumerate_installable_skills()
    assert sorted(s.name for s in result) == ["mail-triage", "weather"]

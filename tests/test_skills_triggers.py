"""Coverage for triggers + events + silent_default frontmatter."""

from __future__ import annotations

from pathlib import Path

import pytest

from tend.skills import (
    SkillFrontmatterError,
    enumerate_skills,
    find_event_subscribers,
    parse_frontmatter,
)


def _seed(root: Path, name: str, body: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(body, encoding="utf-8")


def test_parse_frontmatter_with_triggers():
    text = (
        "---\n"
        "name: meal-plan\n"
        "description: noon meal plan\n"
        "triggers:\n"
        "  - cron: \"0 12 * * *\"\n"
        "    tz: America/Los_Angeles\n"
        "    request: make the meal plan\n"
        "---\n"
        "body\n"
    )
    fm = parse_frontmatter(text)
    assert fm.name == "meal-plan"
    assert fm.description == "noon meal plan"
    assert len(fm.triggers) == 1
    t = fm.triggers[0]
    assert t["cron"] == "0 12 * * *"
    assert t["tz"] == "America/Los_Angeles"
    assert t["request"] == "make the meal plan"


def test_parse_frontmatter_with_events_and_silent_default():
    text = (
        "---\n"
        "name: heartbeat\n"
        "description: silent check-in\n"
        "silent_default: true\n"
        "events:\n"
        "  - posture.slumped\n"
        "  - hydration.lapse\n"
        "---\n"
        "body\n"
    )
    fm = parse_frontmatter(text)
    assert fm.silent_default is True
    assert fm.events == ("posture.slumped", "hydration.lapse")


def test_parse_frontmatter_defaults():
    text = (
        "---\n"
        "name: simple\n"
        "description: a skill with nothing fancy\n"
        "---\n"
        "body\n"
    )
    fm = parse_frontmatter(text)
    assert fm.triggers == ()
    assert fm.events == ()
    assert fm.silent_default is False


def test_enumerate_includes_triggers_events(tmp_path):
    _seed(tmp_path, "meal-plan",
          "---\n"
          "name: meal-plan\n"
          "description: x\n"
          "triggers:\n"
          "  - cron: \"0 12 * * *\"\n"
          "    request: lunch\n"
          "events:\n"
          "  - hunger.detected\n"
          "---\n"
          "body\n")
    skills = enumerate_skills(tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert len(s.triggers) == 1
    assert s.events == ("hunger.detected",)


def test_find_event_subscribers(tmp_path):
    _seed(tmp_path, "a",
          "---\nname: a\ndescription: x\nevents:\n  - foo\n---\nbody\n")
    _seed(tmp_path, "b",
          "---\nname: b\ndescription: y\nevents:\n  - bar\n  - foo\n---\nbody\n")
    _seed(tmp_path, "c",
          "---\nname: c\ndescription: z\n---\nbody\n")
    matches = find_event_subscribers(tmp_path, "foo")
    names = sorted(m.name for m in matches)
    assert names == ["a", "b"]


def test_malformed_triggers_logs_and_skips(tmp_path):
    """A malformed `triggers:` block doesn't break the rest of the catalog."""
    _seed(tmp_path, "good",
          "---\nname: good\ndescription: ok\n---\nbody\n")
    _seed(tmp_path, "bad",
          "---\nname: bad\ndescription: ok\n"
          "triggers:\n"
          "  - this is not a mapping\n"
          "---\nbody\n")
    skills = enumerate_skills(tmp_path)
    names = sorted(s.name for s in skills)
    # The 'bad' skill is still enumerable; its triggers just come back empty.
    assert names == ["bad", "good"]
    bad = next(s for s in skills if s.name == "bad")
    assert bad.triggers == ()

# SPDX-License-Identifier: MIT
"""`tend skills new` — scaffold a new SKILL.md from the template."""

from __future__ import annotations

import pytest


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    return tmp_path


def test_skills_new_creates_skill_dir(isolated):
    from tend.cli import main
    rc = main(["skills", "new", "my-skill", "--description", "Test"])
    assert rc == 0
    p = isolated / "skills" / "my-skill" / "SKILL.md"
    assert p.exists()
    body = p.read_text(encoding="utf-8")
    assert "name: my-skill" in body
    assert "description: Test" in body
    assert "{{NAME}}" not in body
    assert "{{DESCRIPTION}}" not in body


def test_skills_new_refuses_to_overwrite(isolated):
    (isolated / "skills" / "x").mkdir(parents=True)
    (isolated / "skills" / "x" / "SKILL.md").write_text("preexisting")
    from tend.cli import main
    rc = main(["skills", "new", "x", "--description", "y"])
    assert rc == 2
    assert (isolated / "skills" / "x" / "SKILL.md").read_text() == "preexisting"


def test_skills_new_rejects_bad_name(isolated):
    from tend.cli import main
    rc = main(["skills", "new", "../etc", "--description", "y"])
    assert rc == 2

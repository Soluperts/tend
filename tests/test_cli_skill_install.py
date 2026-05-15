# SPDX-License-Identifier: MIT
"""`tend skills install` — interactive picker for optional shipped skills."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    return tmp_path


def test_skills_install_all_copies_every_shipped_skill(isolated):
    from tend.cli import main
    rc = main(["skills", "install", "--all"])
    assert rc == 0
    # at least the briefing skill is shipped optional
    assert (isolated / "skills" / "briefing").is_dir()


def test_skills_install_interactive_uses_questionary(monkeypatch, isolated):
    """When neither --all nor names are given, questionary.checkbox is called."""
    asked = {}
    fake_q = MagicMock()
    fake_q.ask.return_value = ["briefing"]

    def fake_checkbox(msg, choices):
        asked["msg"] = msg
        asked["n_choices"] = len(choices)
        return fake_q

    import questionary
    monkeypatch.setattr(questionary, "checkbox", fake_checkbox)

    from tend.cli import main
    rc = main(["skills", "install"])
    assert rc == 0
    assert (isolated / "skills" / "briefing").is_dir()
    assert asked["n_choices"] >= 1


def test_skills_install_specific_name(isolated):
    from tend.cli import main
    rc = main(["skills", "install", "--name", "briefing"])
    assert rc == 0
    assert (isolated / "skills" / "briefing").is_dir()

# SPDX-License-Identifier: MIT
"""Tests for tend.dispatch.dispatch_event — shared event fan-out helper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def _seed_skill(root: Path, name: str, events: list[str]) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    body_events = "\n".join(f"  - {e}" for e in events)
    (d / "SKILL.md").write_text(
        f"---\n"
        f"name: {name}\n"
        f"description: test skill\n"
        f"events:\n{body_events}\n"
        f"---\n# {name}\n"
    )


@pytest.fixture
def isolated_workspace(monkeypatch, tmp_path):
    """Redirect $TEND_HOME to tmp_path and empty out critical-skills."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    empty_critical = tmp_path / "_empty_critical"
    empty_critical.mkdir()
    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: empty_critical)
    return tmp_path


async def test_dispatch_event_routes_to_each_subscriber(isolated_workspace):
    from tend.dispatch import dispatch_event
    skills_root = isolated_workspace / "skills"
    _seed_skill(skills_root, "skill-a", ["lunch.upcoming"])
    _seed_skill(skills_root, "skill-b", ["lunch.upcoming", "deep-work.ended"])
    _seed_skill(skills_root, "skill-c", ["other.event"])
    dispatch = AsyncMock()

    names = await dispatch_event(
        kind="lunch.upcoming",
        payload={"event_id": "x"},
        dispatch=dispatch,
    )

    assert sorted(names) == ["skill-a", "skill-b"]
    assert dispatch.await_count == 2
    targets = [c.args[0] for c in dispatch.await_args_list]
    assert all(t == "general" for t in targets)
    payloads = [c.args[1] for c in dispatch.await_args_list]
    assert all(p["event"]["kind"] == "lunch.upcoming" for p in payloads)
    assert all(p["event"]["event_id"] == "x" for p in payloads)
    assert {p["skill"] for p in payloads} == {"skill-a", "skill-b"}


async def test_dispatch_event_returns_empty_when_no_subscribers(isolated_workspace):
    from tend.dispatch import dispatch_event
    skills_root = isolated_workspace / "skills"
    _seed_skill(skills_root, "skill-a", ["other.event"])
    dispatch = AsyncMock()

    names = await dispatch_event(
        kind="lunch.upcoming",
        payload={},
        dispatch=dispatch,
    )

    assert names == []
    dispatch.assert_not_awaited()

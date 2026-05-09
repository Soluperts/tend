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


async def test_dispatch_event_routes_to_each_subscriber(tmp_path):
    from tend.dispatch import dispatch_event
    _seed_skill(tmp_path, "skill-a", ["lunch.upcoming"])
    _seed_skill(tmp_path, "skill-b", ["lunch.upcoming", "deep-work.ended"])
    _seed_skill(tmp_path, "skill-c", ["other.event"])
    dispatch = AsyncMock()

    names = await dispatch_event(
        kind="lunch.upcoming",
        payload={"event_id": "x"},
        dispatch=dispatch,
        skills_root=tmp_path,
    )

    assert sorted(names) == ["skill-a", "skill-b"]
    assert dispatch.await_count == 2
    targets = [c.args[0] for c in dispatch.await_args_list]
    assert all(t == "general" for t in targets)
    payloads = [c.args[1] for c in dispatch.await_args_list]
    assert all(p["event"]["kind"] == "lunch.upcoming" for p in payloads)
    assert all(p["event"]["event_id"] == "x" for p in payloads)
    assert {p["skill"] for p in payloads} == {"skill-a", "skill-b"}


async def test_dispatch_event_returns_empty_when_no_subscribers(tmp_path):
    from tend.dispatch import dispatch_event
    _seed_skill(tmp_path, "skill-a", ["other.event"])
    dispatch = AsyncMock()

    names = await dispatch_event(
        kind="lunch.upcoming",
        payload={},
        dispatch=dispatch,
        skills_root=tmp_path,
    )

    assert names == []
    dispatch.assert_not_awaited()

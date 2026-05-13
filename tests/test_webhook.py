"""Tests for the aiohttp webhook receiver."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tend.webhook import build_app


def _make_app(*, announcer, dispatch, token: str = "TKN"):
    return build_app(
        token=token,
        announcer=announcer,
        dispatch=dispatch,
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


@pytest.fixture
async def client(isolated_workspace):
    announcer = MagicMock()
    announcer.announce = AsyncMock(return_value=True)
    dispatch = AsyncMock()
    app = _make_app(announcer=announcer, dispatch=dispatch)
    async with TestClient(TestServer(app)) as c:
        c.announcer = announcer
        c.dispatch = dispatch
        yield c


async def test_say_unauth_returns_401(client):
    r = await client.post("/say", json={"text": "hi"})
    assert r.status == 401


async def test_say_authed_calls_announcer(client):
    r = await client.post(
        "/say",
        headers={"Authorization": "Bearer TKN"},
        json={"text": "Sit up.", "category": "posture"},
    )
    assert r.status == 200
    body = await r.json()
    assert body["delivered"] is True
    client.announcer.announce.assert_awaited_once()
    kwargs = client.announcer.announce.call_args.kwargs
    assert kwargs["text"] == "Sit up."
    assert kwargs["category"] == "posture"
    assert kwargs["urgent"] is False


async def test_say_urgent_passed_through(client):
    await client.post(
        "/say",
        headers={"Authorization": "Bearer TKN"},
        json={"text": "FIRE", "urgent": True},
    )
    kwargs = client.announcer.announce.call_args.kwargs
    assert kwargs["urgent"] is True


async def test_say_missing_text_400(client):
    r = await client.post(
        "/say", headers={"Authorization": "Bearer TKN"}, json={},
    )
    assert r.status == 400


async def test_event_dispatches_per_subscriber(isolated_workspace):
    # Seed two subscribing skills under $TEND_HOME/skills/
    skills_root = isolated_workspace / "skills"
    skills_root.mkdir()
    for name in ("a", "b"):
        (skills_root / name).mkdir()
        (skills_root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: x\n"
            f"events:\n  - posture.slumped\n---\nbody\n",
            encoding="utf-8",
        )
    announcer = MagicMock()
    announcer.announce = AsyncMock(return_value=True)
    dispatch = AsyncMock()
    app = _make_app(announcer=announcer, dispatch=dispatch)
    async with TestClient(TestServer(app)) as c:
        r = await c.post(
            "/event",
            headers={"Authorization": "Bearer TKN"},
            json={"kind": "posture.slumped", "duration_s": 600},
        )
        assert r.status == 200
        body = await r.json()
        assert sorted(body["dispatched"]) == ["a", "b"]
    assert dispatch.await_count == 2
    payloads = [c.args[1] for c in dispatch.call_args_list]
    for p in payloads:
        assert p["event"]["kind"] == "posture.slumped"
        assert p["skill"] in ("a", "b")


async def test_event_no_subscribers_returns_empty_list(client):
    r = await client.post(
        "/event",
        headers={"Authorization": "Bearer TKN"},
        json={"kind": "nothing.matches"},
    )
    assert r.status == 200
    body = await r.json()
    assert body["dispatched"] == []


async def test_event_unauth_returns_401(client):
    r = await client.post("/event", json={"kind": "x"})
    assert r.status == 401

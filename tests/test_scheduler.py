# SPDX-License-Identifier: MIT
"""Tests for Scheduler — load, dispatch loop, missed-fire policy, API."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from tend.cron_store import CronStore
from tend.scheduler import Scheduler


UTC = ZoneInfo("UTC")


@pytest.fixture
def store(tmp_path: Path) -> CronStore:
    return CronStore(root=tmp_path)


@pytest.fixture
def dispatch():
    return AsyncMock()


def _new_scheduler(store, dispatch, **overrides):
    bus = MagicMock()
    bus.publish = AsyncMock()
    return Scheduler(
        "scheduler",
        bus=bus,
        store=store,
        dispatch=dispatch,
        default_tz=overrides.get("default_tz", "UTC"),
        missed_at_policy=overrides.get("missed_at_policy", "run-on-restart"),
    )


def test_add_job_persists_and_computes_next_run_at(store, dispatch):
    s = _new_scheduler(store, dispatch)
    job = s.add_job(
        when="every 30m", request="ping", name="every-half-hour",
        source="cli",
    )
    assert job.kind == "every"
    assert job.schedule == "30m"
    state = store.get_state(job.id)
    assert state.next_run_at is not None


def test_list_jobs_returns_persisted(store, dispatch):
    s = _new_scheduler(store, dispatch)
    s.add_job(when="every 1m", request="x", name="a", source="cli")
    s.add_job(when="every 5m", request="y", name="b", source="cli")
    rows = s.list_jobs()
    names = sorted(r.name for r in rows)
    assert names == ["a", "b"]


def test_cancel_job_by_name(store, dispatch):
    s = _new_scheduler(store, dispatch)
    j = s.add_job(when="every 1m", request="x", name="a", source="cli")
    assert s.cancel_job("a") is True
    assert store.load_jobs() == []
    # state row also cleared
    assert store.get_state(j.id).next_run_at is None


def test_cancel_job_by_id_prefix(store, dispatch):
    s = _new_scheduler(store, dispatch)
    j = s.add_job(when="every 1m", request="x", name="a", source="cli")
    assert s.cancel_job(j.id[:6]) is True


def test_cancel_job_unknown_returns_false(store, dispatch):
    s = _new_scheduler(store, dispatch)
    assert s.cancel_job("nope") is False


def test_find_by_source(store, dispatch):
    s = _new_scheduler(store, dispatch)
    s.add_job(when="every 1m", request="x", name="a",
              source="skill:meal-plan")
    s.add_job(when="every 1m", request="y", name="b",
              source="voice")
    rows = s.find_by_source("skill:meal-plan")
    assert len(rows) == 1 and rows[0].name == "a"


async def test_fire_dispatches_payload(store, dispatch):
    s = _new_scheduler(store, dispatch)
    j = s.add_job(when="every 1m", request="hi", name="a",
                  source="voice", payload_extras={"skill": "test"})
    await s._fire_job(j)
    dispatch.assert_awaited_once()
    target, payload = dispatch.call_args.args
    assert target == "general"
    assert payload["request"] == "hi"
    assert payload["skill"] == "test"


async def test_fire_records_state(store, dispatch):
    s = _new_scheduler(store, dispatch)
    j = s.add_job(when="every 1m", request="hi", name="a", source="voice")
    await s._fire_job(j)
    state = store.get_state(j.id)
    assert state.last_run_status == "succeeded"
    assert state.last_run_at is not None
    assert state.next_run_at is not None


async def test_at_job_deletes_after_fire(store, dispatch):
    s = _new_scheduler(store, dispatch)
    target = (dt.datetime.now(tz=UTC) + dt.timedelta(seconds=60)).isoformat()
    j = s.add_job(when=target, request="one-shot", name="z", source="voice")
    await s._fire_job(j)
    assert store.load_jobs() == []


async def test_dispatch_failure_records_error(store, dispatch):
    dispatch.side_effect = RuntimeError("boom")
    s = _new_scheduler(store, dispatch)
    j = s.add_job(when="every 1m", request="hi", name="a", source="voice")
    await s._fire_job(j)
    state = store.get_state(j.id)
    assert state.last_run_status == "failed"
    assert "boom" in (state.last_error or "")
    assert state.consecutive_errors == 1


async def test_missed_at_run_on_restart_fires(store, dispatch):
    s = _new_scheduler(store, dispatch, missed_at_policy="run-on-restart")
    past = (dt.datetime.now(tz=UTC) - dt.timedelta(minutes=5)).isoformat()
    j = s.add_job(when=past, request="missed", name="m", source="voice")
    await s.fire_missed_now()
    dispatch.assert_awaited_once()
    assert store.load_jobs() == []  # at job auto-deletes


async def test_missed_at_skip_does_not_fire(store, dispatch):
    s = _new_scheduler(store, dispatch, missed_at_policy="skip")
    past = (dt.datetime.now(tz=UTC) - dt.timedelta(minutes=5)).isoformat()
    s.add_job(when=past, request="missed", name="m", source="voice")
    await s.fire_missed_now()
    dispatch.assert_not_awaited()
    assert store.load_jobs() == []  # skipped, deleted


def test_add_job_invalid_tz_does_not_persist(store, dispatch):
    """A bad tz must not leave an orphaned row in jobs.json."""
    from tend.cron_time import InvalidWhen

    s = _new_scheduler(store, dispatch)
    with pytest.raises(InvalidWhen):
        s.add_job(
            when="0 12 * * *", request="x", name="bad",
            source="cli", tz="Mars/Capital",
        )
    assert store.load_jobs() == []


def test_compute_wait_s_caps_far_future_jobs(store, dispatch):
    """When the next due job is far in the future, the loop's nap is
    bounded by _MAX_NAP_S so external CLI writes get noticed within
    that cap."""
    s = _new_scheduler(store, dispatch)
    job = s.add_job(when="every 1m", request="x", name="ping",
                    source="voice")
    now = dt.datetime.now(tz=UTC)
    far_future = (job, now + dt.timedelta(hours=1))
    wait_s = s._compute_wait_s(far_future, now)
    assert wait_s == s._MAX_NAP_S


def test_compute_wait_s_no_jobs_caps_at_max(store, dispatch):
    s = _new_scheduler(store, dispatch)
    now = dt.datetime.now(tz=UTC)
    wait_s = s._compute_wait_s(None, now)
    assert wait_s == s._MAX_NAP_S


def test_compute_wait_s_returns_zero_for_due_job(store, dispatch):
    s = _new_scheduler(store, dispatch)
    job = s.add_job(when="every 1m", request="x", name="ping",
                    source="voice")
    now = dt.datetime.now(tz=UTC)
    past = (job, now - dt.timedelta(seconds=10))
    wait_s = s._compute_wait_s(past, now)
    assert wait_s == 0.0


def test_compute_wait_s_short_horizon_pass_through(store, dispatch):
    """If the next job is within the cap window, return that exact value."""
    s = _new_scheduler(store, dispatch)
    job = s.add_job(when="every 30m", request="x", name="ping",
                    source="voice")
    now = dt.datetime.now(tz=UTC)
    soon = (job, now + dt.timedelta(seconds=10))
    wait_s = s._compute_wait_s(soon, now)
    assert 9.0 < wait_s < 11.0


def _seed_skill(root, name, events):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    body_events = "\n".join(f"  - {e}" for e in events)
    (d / "SKILL.md").write_text(
        f"---\n"
        f"name: {name}\n"
        f"description: test\n"
        f"events:\n{body_events}\n"
        f"---\n# {name}\n"
    )


async def test_event_mode_job_routes_via_dispatch_event(
    store, dispatch, tmp_path, monkeypatch,
):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    empty_critical = tmp_path / "_empty_critical"
    empty_critical.mkdir()
    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: empty_critical)

    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _seed_skill(skills_root, "lunch-prep", ["lunch.upcoming"])

    bus = MagicMock()
    bus.publish = AsyncMock()
    s = Scheduler(
        "scheduler",
        bus=bus,
        store=store,
        dispatch=dispatch,
        default_tz="UTC",
        missed_at_policy="run-on-restart",
    )
    j = s.add_job(
        when=(dt.datetime.now(tz=UTC) + dt.timedelta(seconds=60)).isoformat(),
        request="(unused for event jobs)",
        name="lunch-fire", source="schedule-watcher",
        event_kind="lunch.upcoming",
        event_payload={"event_id": "x"},
    )
    await s._fire_job(j)

    # The event-mode job should fan out to lunch-prep via dispatch (the
    # same callable, target="general", with event payload structure).
    dispatch.assert_awaited_once()
    target, payload = dispatch.call_args.args
    assert target == "general"
    assert payload["skill"] == "lunch-prep"
    assert payload["event"]["kind"] == "lunch.upcoming"
    assert payload["event"]["event_id"] == "x"


async def test_legacy_job_still_dispatches_to_general(store, dispatch, tmp_path):
    bus = MagicMock()
    bus.publish = AsyncMock()
    s = Scheduler(
        "scheduler",
        bus=bus,
        store=store,
        dispatch=dispatch,
        default_tz="UTC",
        missed_at_policy="run-on-restart",
    )
    j = s.add_job(
        when="every 1m", request="hi",
        name="legacy", source="voice",
    )  # no event_kind
    await s._fire_job(j)

    dispatch.assert_awaited_once()
    target, payload = dispatch.call_args.args
    assert target == "general"
    assert payload == {"request": "hi"}

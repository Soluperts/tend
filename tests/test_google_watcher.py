# SPDX-License-Identifier: MIT
"""Tests for tend.google_watcher — pure logic for tag parsing and phase fires."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest


UTC = ZoneInfo("UTC")


def test_parse_tag_returns_tag_when_present():
    from tend.google_watcher import parse_tag
    assert parse_tag("[deep-work] Plan Q2 docs") == "deep-work"
    assert parse_tag("[lunch] with kids") == "lunch"
    assert parse_tag("[exercise]") == "exercise"


def test_parse_tag_returns_none_for_untagged():
    from tend.google_watcher import parse_tag
    assert parse_tag("Plan Q2 docs") is None
    assert parse_tag("") is None
    assert parse_tag("Lunch") is None


def test_parse_tag_rejects_malformed_brackets():
    from tend.google_watcher import parse_tag
    assert parse_tag("[]") is None
    assert parse_tag("[bad spaces] x") is None
    assert parse_tag(" [leading-space] x") is None
    assert parse_tag("[tag] [other] x") == "tag"  # only the leading one matters


def test_parse_tag_handles_multibyte_chars():
    from tend.google_watcher import parse_tag
    assert parse_tag("[déjà-vu] x") is None  # \w doesn't include accents in default mode
    assert parse_tag("[multi-word-tag] x") == "multi-word-tag"


def test_compute_phases_basic():
    from tend.google_watcher import compute_phases
    start = dt.datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    end = dt.datetime(2026, 5, 9, 13, 0, tzinfo=UTC)
    fires = compute_phases(
        start=start, end=end,
        upcoming_lead_s=3600,
        emit=["upcoming", "starting", "ended"],
    )
    assert fires == {
        "upcoming": dt.datetime(2026, 5, 9, 11, 0, tzinfo=UTC),
        "starting": dt.datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        "ended": dt.datetime(2026, 5, 9, 13, 0, tzinfo=UTC),
    }


def test_compute_phases_only_emits_listed():
    from tend.google_watcher import compute_phases
    start = dt.datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    end = dt.datetime(2026, 5, 9, 13, 0, tzinfo=UTC)
    fires = compute_phases(
        start=start, end=end,
        upcoming_lead_s=0,
        emit=["starting"],
    )
    assert fires == {
        "starting": dt.datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    }


def test_parse_lead_seconds():
    from tend.google_watcher import parse_lead_seconds
    assert parse_lead_seconds("5m") == 300
    assert parse_lead_seconds("60m") == 3600
    assert parse_lead_seconds("0m") == 0
    assert parse_lead_seconds("2h") == 7200
    assert parse_lead_seconds("30s") == 30


def test_parse_lead_seconds_rejects_garbage():
    from tend.google_watcher import parse_lead_seconds
    with pytest.raises(ValueError):
        parse_lead_seconds("")
    with pytest.raises(ValueError):
        parse_lead_seconds("five")
    with pytest.raises(ValueError):
        parse_lead_seconds("5")  # missing unit


def _load_fixture(name: str) -> str:
    from pathlib import Path
    return (Path(__file__).parent / "fixtures" / "gws" / name).read_text()


def test_load_state_returns_empty_when_missing(tmp_path):
    from tend.google_watcher import load_state
    out = load_state(tmp_path / "missing.json")
    assert out == {}


def test_load_state_round_trip(tmp_path):
    from tend.google_watcher import load_state, save_state
    p = tmp_path / "state.json"
    save_state(p, {
        "evt-1": {"updated": "u1", "fired_phases": ["upcoming"]},
    })
    assert load_state(p) == {
        "evt-1": {"updated": "u1", "fired_phases": ["upcoming"]},
    }


def test_load_state_treats_corrupt_file_as_empty(tmp_path):
    from tend.google_watcher import load_state
    p = tmp_path / "state.json"
    p.write_text("{not valid json")
    assert load_state(p) == {}


def test_prune_state_drops_old_events(tmp_path):
    from tend.google_watcher import prune_state
    now = dt.datetime(2026, 5, 9, 18, 0, tzinfo=UTC)
    state = {
        "old-evt": {
            "updated": "x",
            "fired_phases": ["ended"],
            "end_at": "2026-05-08T10:00:00+00:00",  # >24h old
        },
        "fresh-evt": {
            "updated": "x",
            "fired_phases": ["upcoming"],
            "end_at": "2026-05-09T19:00:00+00:00",  # future
        },
    }
    pruned = prune_state(state, now=now)
    assert "old-evt" not in pruned
    assert "fresh-evt" in pruned


async def test_run_tick_schedules_unfired_phases(tmp_path, monkeypatch):
    """Watcher reads agenda, parses tags, computes phases, schedules
    one-shot jobs via `tend schedule add` for future fires."""
    import json
    from unittest.mock import MagicMock
    from tend.google_watcher import run_tick

    state_path = tmp_path / "state.json"
    schedule_calls: list[list[str]] = []

    def fake_run_subprocess(cmd: list[str], **kwargs) -> object:
        # Two cases: gws calls return JSON; tend schedule add returns success.
        result = MagicMock()
        result.returncode = 0
        if cmd[:4] == ["gws", "calendar", "calendarList", "list"]:
            result.stdout = _load_fixture("calendar-list.json")
        elif cmd[:4] == ["gws", "calendar", "events", "list"]:
            # The calendarId is inside the --params JSON blob.
            params_idx = cmd.index("--params") + 1
            params = json.loads(cmd[params_idx])
            if params.get("calendarId") == "tend-cal-id-001":
                result.stdout = _load_fixture("agenda.json")
            else:
                result.stdout = _load_fixture("empty-agenda.json")
        elif cmd[:2] == ["tend", "schedule"] and cmd[2] == "add":
            schedule_calls.append(cmd)
            result.stdout = "added abcdef job"
        else:
            result.stdout = ""
        return result

    monkeypatch.setattr(
        "tend.google_watcher.subprocess.run", fake_run_subprocess,
    )

    # Pretend "now" is well before the events so all phases are future.
    now = dt.datetime(2026, 5, 9, 6, 0, tzinfo=UTC)

    summary = await run_tick(
        watched_names=["tend"],
        events_config={
            "lunch": {"upcoming_lead_s": 3600, "emit": ["upcoming", "starting"]},
            "deep-work": {"upcoming_lead_s": 300, "emit": ["upcoming", "starting", "ended"]},
        },
        state_path=state_path,
        webhook_token="test-token",
        webhook_url="http://127.0.0.1:7331/event",
        now=now,
    )

    # 2 phases for lunch + 3 phases for deep-work = 5 schedule add calls.
    assert len(schedule_calls) == 5
    # Verify event_kinds passed to schedule add include all expected.
    kinds = []
    for cmd in schedule_calls:
        idx = cmd.index("--event")
        kinds.append(cmd[idx + 1])
    assert sorted(kinds) == sorted([
        "lunch.upcoming", "lunch.starting",
        "deep-work.upcoming", "deep-work.starting", "deep-work.ended",
    ])

    # State file must record fired phases per event_id.
    state = json.loads(state_path.read_text())
    assert "evt-1" in state
    assert sorted(state["evt-1"]["fired_phases"]) == ["starting", "upcoming"]
    assert "evt-3" not in state  # untagged ignored

    assert summary["scheduled"] == 5
    assert summary["seen_tagged"] == 2


async def test_run_tick_dedups_already_fired_phases(tmp_path, monkeypatch):
    """Re-running with a populated state file should skip phases already
    fired."""
    import json
    from unittest.mock import MagicMock
    from tend.google_watcher import run_tick

    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "evt-1": {
            "updated": "2026-05-08T20:00:00+00:00",
            "fired_phases": ["upcoming", "starting"],
            "end_at": "2026-05-09T13:00:00+00:00",
        },
    }))
    schedule_calls: list[list[str]] = []

    def fake_run_subprocess(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        if cmd[:4] == ["gws", "calendar", "calendarList", "list"]:
            result.stdout = _load_fixture("calendar-list.json")
        elif cmd[:4] == ["gws", "calendar", "events", "list"]:
            params_idx = cmd.index("--params") + 1
            params = json.loads(cmd[params_idx])
            if params.get("calendarId") == "tend-cal-id-001":
                result.stdout = json.dumps({"items": [{
                    "id": "evt-1",
                    "summary": "[lunch] with kids",
                    "start": {"dateTime": "2026-05-09T12:00:00+00:00"},
                    "end": {"dateTime": "2026-05-09T13:00:00+00:00"},
                    "updated": "2026-05-08T20:00:00+00:00",
                }]})
            else:
                result.stdout = '{"items": []}'
        elif cmd[:2] == ["tend", "schedule"]:
            schedule_calls.append(cmd)
            result.stdout = "added"
        return result

    monkeypatch.setattr(
        "tend.google_watcher.subprocess.run", fake_run_subprocess,
    )
    now = dt.datetime(2026, 5, 9, 6, 0, tzinfo=UTC)

    summary = await run_tick(
        watched_names=["tend"],
        events_config={
            "lunch": {"upcoming_lead_s": 3600, "emit": ["upcoming", "starting"]},
        },
        state_path=state_path,
        webhook_token="t",
        webhook_url="http://127.0.0.1:7331/event",
        now=now,
    )
    assert len(schedule_calls) == 0  # all phases already fired
    assert summary["scheduled"] == 0


async def test_run_tick_resets_fired_phases_when_event_was_edited(
    tmp_path, monkeypatch,
):
    """If the event's `updated` field changes, fired_phases must reset
    so the user gets phase-firings again on the new schedule."""
    import json
    from unittest.mock import MagicMock
    from tend.google_watcher import run_tick

    state_path = tmp_path / "state.json"
    # Pre-populate with the OLD updated value and both phases fired.
    state_path.write_text(json.dumps({
        "evt-1": {
            "updated": "OLD-UPDATED-TIMESTAMP",
            "fired_phases": ["upcoming", "starting"],
            "end_at": "2026-05-09T13:00:00+00:00",
        },
    }))
    schedule_calls: list[list[str]] = []

    def fake_run_subprocess(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        if cmd[:4] == ["gws", "calendar", "calendarList", "list"]:
            result.stdout = _load_fixture("calendar-list.json")
        elif cmd[:4] == ["gws", "calendar", "events", "list"]:
            params_idx = cmd.index("--params") + 1
            params = json.loads(cmd[params_idx])
            if params.get("calendarId") == "tend-cal-id-001":
                # Return the event with a NEW updated value, simulating an edit.
                result.stdout = json.dumps({"items": [{
                    "id": "evt-1",
                    "summary": "[lunch] with kids",
                    "start": {"dateTime": "2026-05-09T12:00:00+00:00"},
                    "end": {"dateTime": "2026-05-09T13:00:00+00:00"},
                    "updated": "NEW-UPDATED-TIMESTAMP",
                }]})
            else:
                result.stdout = '{"items": []}'
        elif cmd[:2] == ["tend", "schedule"]:
            schedule_calls.append(cmd)
            result.stdout = "added"
        return result

    monkeypatch.setattr(
        "tend.google_watcher.subprocess.run", fake_run_subprocess,
    )
    now = dt.datetime(2026, 5, 9, 6, 0, tzinfo=UTC)

    summary = await run_tick(
        watched_names=["tend"],
        events_config={
            "lunch": {"upcoming_lead_s": 3600, "emit": ["upcoming", "starting"]},
        },
        state_path=state_path,
        webhook_token="t",
        webhook_url="http://127.0.0.1:7331/event",
        now=now,
    )

    # Both phases should re-schedule since fired_phases was reset.
    assert len(schedule_calls) == 2
    assert summary["scheduled"] == 2
    # State file should now have the NEW updated timestamp + the re-fired phases.
    state = json.loads(state_path.read_text())
    assert state["evt-1"]["updated"] == "NEW-UPDATED-TIMESTAMP"
    assert sorted(state["evt-1"]["fired_phases"]) == ["starting", "upcoming"]

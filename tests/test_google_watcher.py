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

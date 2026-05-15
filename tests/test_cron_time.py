# SPDX-License-Identifier: MIT
"""Pure-function tests for cron-time parsing and next-fire computation."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tend.cron_time import (
    InvalidWhen,
    next_fire_at,
    parse_when,
)


UTC = ZoneInfo("UTC")
LA = ZoneInfo("America/Los_Angeles")


def test_parse_when_cron_5_field():
    kind, schedule = parse_when("0 12 * * *")
    assert kind == "cron"
    assert schedule == "0 12 * * *"


def test_parse_when_relative_offset_seconds():
    kind, schedule = parse_when("in 90s")
    assert kind == "at"
    # schedule is an ISO timestamp; just check shape, not value.
    parsed = dt.datetime.fromisoformat(schedule)
    assert parsed.tzinfo is not None


def test_parse_when_relative_offset_minutes():
    kind, schedule = parse_when("in 5m")
    parsed = dt.datetime.fromisoformat(schedule)
    delta = parsed - dt.datetime.now(tz=UTC)
    assert 4 * 60 < delta.total_seconds() < 6 * 60


def test_parse_when_relative_offset_hours():
    kind, schedule = parse_when("in 2h")
    parsed = dt.datetime.fromisoformat(schedule)
    delta = parsed - dt.datetime.now(tz=UTC)
    assert 1.9 * 3600 < delta.total_seconds() < 2.1 * 3600


def test_parse_when_iso_timestamp():
    kind, schedule = parse_when("2030-01-01T12:00:00+00:00")
    assert kind == "at"
    assert schedule == "2030-01-01T12:00:00+00:00"


def test_parse_when_every_minutes():
    kind, schedule = parse_when("every 30m")
    assert kind == "every"
    assert schedule == "30m"


def test_parse_when_every_hours():
    kind, schedule = parse_when("every 6h")
    assert kind == "every"
    assert schedule == "6h"


def test_parse_when_invalid_nonsense():
    with pytest.raises(InvalidWhen):
        parse_when("nonsense")


def test_parse_when_invalid_lone_in():
    with pytest.raises(InvalidWhen):
        parse_when("in")


def test_parse_when_invalid_every_banana():
    with pytest.raises(InvalidWhen):
        parse_when("every banana")


def test_next_fire_at_cron_basic():
    now = dt.datetime(2026, 5, 8, 11, 0, 0, tzinfo=LA)
    fire = next_fire_at("cron", "0 12 * * *", "America/Los_Angeles", now)
    assert fire == dt.datetime(2026, 5, 8, 12, 0, 0, tzinfo=LA)


def test_next_fire_at_cron_rolls_over_day():
    now = dt.datetime(2026, 5, 8, 13, 0, 0, tzinfo=LA)
    fire = next_fire_at("cron", "0 12 * * *", "America/Los_Angeles", now)
    assert fire == dt.datetime(2026, 5, 9, 12, 0, 0, tzinfo=LA)


def test_next_fire_at_every_relative():
    now = dt.datetime(2026, 5, 8, 11, 0, 0, tzinfo=UTC)
    fire = next_fire_at("every", "30m", None, now)
    assert fire == dt.datetime(2026, 5, 8, 11, 30, 0, tzinfo=UTC)


def test_next_fire_at_at_returns_schedule():
    now = dt.datetime(2026, 5, 8, 11, 0, 0, tzinfo=UTC)
    fire = next_fire_at("at", "2026-05-08T12:00:00+00:00", None, now)
    assert fire == dt.datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)


def test_parse_when_zero_duration_relative_rejected():
    with pytest.raises(InvalidWhen):
        parse_when("in 0s")
    with pytest.raises(InvalidWhen):
        parse_when("in 0m")


def test_parse_when_zero_duration_every_rejected():
    with pytest.raises(InvalidWhen):
        parse_when("every 0s")
    with pytest.raises(InvalidWhen):
        parse_when("every 0h")


def test_next_fire_at_cron_requires_aware_time():
    naive = dt.datetime(2026, 5, 8, 11, 0, 0)
    with pytest.raises(InvalidWhen):
        next_fire_at("cron", "0 12 * * *", None, naive)


def test_next_fire_at_unknown_tz_rejected():
    now = dt.datetime(2026, 5, 8, 11, 0, 0, tzinfo=UTC)
    with pytest.raises(InvalidWhen):
        next_fire_at("cron", "0 12 * * *", "America/Typo", now)

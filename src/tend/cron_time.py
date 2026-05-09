"""Pure helpers for parsing schedule strings and computing next-fire times.

No I/O. Used by Scheduler, Brain.schedule, and CLI alike.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter, CroniterError


Kind = Literal["at", "cron", "every"]


class InvalidWhen(ValueError):
    """The `when` string did not parse into any supported kind."""


_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_RELATIVE_RE = re.compile(r"^in\s+(\d+)([smhd])$", re.IGNORECASE)
_EVERY_RE = re.compile(r"^every\s+(\d+[smhd])$", re.IGNORECASE)
_DURATION_TO_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _duration_seconds(s: str) -> int:
    m = _DURATION_RE.match(s)
    if not m:
        raise InvalidWhen(f"invalid duration: {s!r}")
    duration = int(m.group(1))
    if duration == 0:
        raise InvalidWhen(f"duration must be > 0: {s!r}")
    return duration * _DURATION_TO_SECONDS[m.group(2)]


def parse_when(when: str) -> tuple[Kind, str]:
    """Classify a `when` string into one of: cron, every, at.

    Returns (kind, schedule). For `at`, schedule is an ISO timestamp string;
    for `cron`, the original cron expression; for `every`, the duration
    string (e.g. "30m"). Raises InvalidWhen on unparseable input.
    """
    s = when.strip()
    if not s:
        raise InvalidWhen("empty")

    m = _RELATIVE_RE.match(s)
    if m:
        duration = int(m.group(1))
        if duration == 0:
            raise InvalidWhen(f"relative offset must be > 0: {when!r}")
        seconds = duration * _DURATION_TO_SECONDS[m.group(2).lower()]
        target = dt.datetime.now(tz=ZoneInfo("UTC")) + dt.timedelta(seconds=seconds)
        return "at", target.isoformat()

    m = _EVERY_RE.match(s)
    if m:
        # Validate the duration parses
        _duration_seconds(m.group(1).lower())
        return "every", m.group(1).lower()

    # Try ISO timestamp
    try:
        parsed = dt.datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return "at", parsed.isoformat()
    except ValueError:
        pass

    # Try cron expression
    try:
        croniter(s)  # raises if invalid
        return "cron", s
    except (ValueError, KeyError, TypeError, CroniterError) as e:
        raise InvalidWhen(f"unrecognised when string: {when!r}") from e


def next_fire_at(
    kind: Kind, schedule: str, tz: str | None, after: dt.datetime,
) -> dt.datetime:
    """Compute the next fire time on or after `after` for a job.

    `tz` is required for kind == "cron" (callers should supply a default
    tz from settings if the user omitted one). For "every" and "at" the
    returned datetime is in `after`'s tz when tz is None.
    """
    if kind == "at":
        return dt.datetime.fromisoformat(schedule)
    if kind == "every":
        seconds = _duration_seconds(schedule)
        return after + dt.timedelta(seconds=seconds)
    if kind == "cron":
        if tz is None and after.tzinfo is None:
            raise InvalidWhen("cron schedule needs an explicit tz or an aware 'after' datetime")
        try:
            zone = ZoneInfo(tz) if tz else after.tzinfo
        except ZoneInfoNotFoundError:
            raise InvalidWhen(f"unknown timezone: {tz!r}")
        base = after.astimezone(zone) if zone else after
        c = croniter(schedule, base)
        return c.get_next(dt.datetime)
    raise InvalidWhen(f"unknown kind: {kind}")

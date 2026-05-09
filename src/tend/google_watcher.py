"""Schedule-watcher: pure logic for parsing event titles and computing
phase fire times. The orchestrator (run_tick) is in the same module
but separated from these helpers for clean unit testing.
"""

from __future__ import annotations

import datetime as dt
import re


_TAG_RE = re.compile(r"^\[([\w-]+)\]", re.ASCII)


def parse_tag(title: str) -> str | None:
    """Return the leading bracket tag (e.g. 'deep-work' for
    '[deep-work] Plan Q2 docs') or None if the title is untagged or the
    bracket isn't a clean leading prefix.
    """
    if not title:
        return None
    m = _TAG_RE.match(title)
    if not m:
        return None
    return m.group(1)


_LEAD_RE = re.compile(r"^(\d+)([smh])$")


def parse_lead_seconds(s: str) -> int:
    """Parse a duration string like '60m', '2h', '30s', '0m' into seconds.

    Raises ValueError on malformed input.
    """
    m = _LEAD_RE.match(s.strip())
    if not m:
        raise ValueError(f"invalid lead duration: {s!r}")
    n = int(m.group(1))
    unit = m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def compute_phases(
    *,
    start: dt.datetime,
    end: dt.datetime,
    upcoming_lead_s: int,
    emit: list[str],
) -> dict[str, dt.datetime]:
    """Compute fire times for each emitted phase.

    Returns a dict keyed by phase name ('upcoming' / 'starting' / 'ended')
    with their respective absolute fire times. Phases not in `emit` are
    omitted from the result.
    """
    candidates = {
        "upcoming": start - dt.timedelta(seconds=upcoming_lead_s),
        "starting": start,
        "ended": end,
    }
    return {p: candidates[p] for p in emit if p in candidates}

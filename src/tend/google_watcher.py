"""Schedule-watcher: pure logic for parsing event titles and computing
phase fire times. The orchestrator (run_tick) is in the same module
but separated from these helpers for clean unit testing.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import urllib.request
from pathlib import Path

from loguru import logger


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


def load_state(path: Path) -> dict:
    """Load the watcher state file. Returns empty dict if missing or
    corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def prune_state(state: dict, *, now: dt.datetime) -> dict:
    """Drop entries whose `end_at` is more than 24h before `now`."""
    cutoff = now - dt.timedelta(hours=24)
    pruned = {}
    for k, v in state.items():
        end_str = v.get("end_at")
        if not end_str:
            # No end recorded — keep, the next tick will fix it.
            pruned[k] = v
            continue
        try:
            end_at = dt.datetime.fromisoformat(end_str)
        except ValueError:
            pruned[k] = v
            continue
        if end_at >= cutoff:
            pruned[k] = v
    return pruned


def _gws_calendar_list() -> list[dict]:
    r = subprocess.run(
        ["gws", "calendar", "list", "--json"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if r.returncode != 0:
        logger.warning(f"gws calendar list failed: {r.stderr}")
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return []


def _gws_agenda(calendar_id: str, time_min: str, time_max: str) -> list[dict]:
    r = subprocess.run(
        ["gws", "calendar", "+agenda",
         "--calendar", calendar_id,
         "--time-min", time_min,
         "--time-max", time_max,
         "--json"],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if r.returncode != 0:
        logger.warning(
            f"gws agenda failed for {calendar_id}: {r.stderr}",
        )
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return []


def _resolve_watched_ids(names: list[str]) -> list[tuple[str, str]]:
    """Resolve watched calendar names to (name, id) tuples. Skips names
    that don't resolve."""
    cals = _gws_calendar_list()
    by_summary = {c.get("summary", "").lower(): c.get("id") for c in cals}
    out: list[tuple[str, str]] = []
    for name in names:
        cid = by_summary.get(name.lower())
        if cid is None:
            logger.warning(f"watched_calendar {name!r} not found in gws list")
            continue
        out.append((name, cid))
    return out


def _schedule_add_event(
    *,
    when: dt.datetime,
    event_kind: str,
    event_payload: dict,
    job_name: str,
) -> None:
    """Shell out to `tend schedule add` to create a one-shot event-mode
    job. Logs and silently swallows failures (state file will retry on
    next tick)."""
    cmd = [
        "tend", "schedule", "add",
        "--when", when.isoformat(),
        "--event", event_kind,
        "--payload", json.dumps(event_payload),
        "--name", job_name,
        "--source", "schedule-watcher",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    if r.returncode != 0:
        logger.warning(
            f"tend schedule add failed ({event_kind}): {r.stderr}",
        )


def _post_event(
    *, kind: str, payload: dict, webhook_url: str, webhook_token: str,
) -> None:
    body = json.dumps({"kind": kind, **payload}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, method="POST", data=body,
        headers={
            "Authorization": f"Bearer {webhook_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as _resp:
            pass
    except Exception as e:
        logger.warning(f"webhook POST {kind} failed: {e}")


async def run_tick(
    *,
    watched_names: list[str],
    events_config: dict[str, dict],
    state_path: Path,
    webhook_token: str,
    webhook_url: str,
    now: dt.datetime,
    horizon_hours: int = 2,
) -> dict:
    """One watcher tick.

    Args:
      watched_names: calendar names from `[google] watched_calendars`.
      events_config: `{tag: {"upcoming_lead_s": int, "emit": [str]}}`.
      state_path: path to ~/.tend/cache/schedule-watcher-state.json.
      webhook_token, webhook_url: for posting missed phases.
      now: caller-supplied so tests can pin the clock.
      horizon_hours: how far ahead to enumerate events.

    Returns a summary dict for the skill prompt to inspect.
    """
    state = prune_state(load_state(state_path), now=now)

    watched = _resolve_watched_ids(watched_names)
    if not watched:
        save_state(state_path, state)
        return {"scheduled": 0, "seen_tagged": 0, "missed_fired": 0}

    time_min = now.isoformat()
    time_max = (now + dt.timedelta(hours=horizon_hours)).isoformat()

    seen_tagged = 0
    scheduled = 0
    missed_fired = 0

    for _name, cal_id in watched:
        events = _gws_agenda(cal_id, time_min, time_max)
        for ev in events:
            tag = parse_tag(ev.get("summary", ""))
            if tag is None:
                continue
            cfg = events_config.get(tag)
            if cfg is None:
                logger.debug(f"unknown tag in event: {tag!r}")
                continue
            seen_tagged += 1
            ev_id = ev.get("id")
            if not ev_id:
                continue
            try:
                start = dt.datetime.fromisoformat(
                    ev["start"]["dateTime"]
                )
                end = dt.datetime.fromisoformat(ev["end"]["dateTime"])
            except (KeyError, ValueError, TypeError):
                continue

            phase_fires = compute_phases(
                start=start, end=end,
                upcoming_lead_s=cfg["upcoming_lead_s"],
                emit=cfg["emit"],
            )

            ev_state = state.get(ev_id) or {}
            if ev_state.get("updated") != ev.get("updated"):
                # Event was edited (or new) — reset fired_phases.
                ev_state = {
                    "updated": ev.get("updated", ""),
                    "fired_phases": [],
                    "end_at": end.isoformat(),
                }
            fired = set(ev_state.get("fired_phases", []))

            for phase, fire_at in phase_fires.items():
                if phase in fired:
                    continue
                event_kind = f"{tag}.{phase}"
                payload = {
                    "event_id": ev_id,
                    "summary": ev.get("summary", ""),
                    "start": ev.get("start", {}),
                    "end": ev.get("end", {}),
                }
                if fire_at <= now:
                    _post_event(
                        kind=event_kind, payload=payload,
                        webhook_url=webhook_url,
                        webhook_token=webhook_token,
                    )
                    missed_fired += 1
                else:
                    _schedule_add_event(
                        when=fire_at,
                        event_kind=event_kind,
                        event_payload=payload,
                        job_name=f"watcher-{ev_id}-{phase}",
                    )
                    scheduled += 1
                fired.add(phase)

            ev_state["fired_phases"] = sorted(fired)
            ev_state["end_at"] = end.isoformat()
            state[ev_id] = ev_state

    save_state(state_path, state)
    return {
        "scheduled": scheduled,
        "seen_tagged": seen_tagged,
        "missed_fired": missed_fired,
    }

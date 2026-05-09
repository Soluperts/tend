# tend × Google + Daily Schedule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `gws` (Google Workspace CLI) into tend so it can read calendar/inbox state, write daily-routine blocks into a tend-owned calendar, and dispatch reactive skills off bracket-tagged calendar events through the proactive-trigger bus.

**Architecture:** Skills shell out to `gws` directly — no Python wrapper around it. The only `src/tend/` change is a small extension to the scheduler so jobs can carry `event_kind`/`event_payload` and dispatch via `/event` semantics. A schedule-watcher skill (heartbeat-fired, every 15m) reads watched calendars, parses bracket-tag prefixes, and creates one-shot scheduler jobs at exact phase fire times. Reactive skills subscribe to those events through the existing `events:` SKILL.md frontmatter.

**Tech Stack:** Python 3.13, pytest, pydantic-settings, aiohttp, loguru, the existing pipecat/pipecat-subagents stack, and `gws` (npm package `@googleworkspace/cli`) installed on the Pi.

**Spec:** `docs/superpowers/specs/2026-05-09-tend-google-and-daily-schedule-design.md`

---

## File map

**Source changes (small):**
- Modify: `src/tend/cron_store.py` — `CronJob` gets `event_kind`/`event_payload` optional fields
- Create: `src/tend/dispatch.py` — `dispatch_event` helper extracted from webhook
- Modify: `src/tend/webhook.py` — `/event` handler now calls `dispatch_event`
- Modify: `src/tend/scheduler.py` — `_fire_job` branches on `event_kind`; new `skills_root` constructor kwarg
- Modify: `src/tend/cli.py` — `tend schedule add` gains `--event KIND --payload JSON`
- Modify: `src/tend/config.py` — `GoogleConfig` / `GoogleEventConfig` Pydantic models
- Create: `src/tend/google_watcher.py` — pure logic for tag parsing, phase computation, state, gws calls, `run_tick()`
- Modify: `src/tend/main.py` — seed new skills (`schedule-watcher`, `briefing`, etc.)

**Tests:**
- Modify: `tests/test_cron_store.py` — event-mode round-trip
- Modify: `tests/test_scheduler.py` — event-mode dispatch
- Modify: `tests/test_webhook.py` — refactor uses `dispatch_event`
- Modify: `tests/test_cli_schedule.py` — `--event`/`--payload` flag
- Modify: `tests/test_config.py` — `GoogleConfig` defaults
- Create: `tests/test_dispatch.py` — `dispatch_event` helper
- Create: `tests/test_google_watcher.py` — watcher logic
- Create: `tests/fixtures/gws/agenda.json`, `gws-calendar-list.json`, `gws-empty-agenda.json`

**Skills (committed to repo at `skills/<name>/`, seeded into `~/.tend/skills/` from `main.py`):**
- Create: `skills/schedule-watcher/SKILL.md` + `skills/schedule-watcher/bin/tick.py`
- Create: `skills/briefing/SKILL.md`
- Create: `skills/meeting-prep/SKILL.md`
- Create: `skills/mail-triage/SKILL.md`
- Create: `skills/routine-setup/SKILL.md`
- Create: `skills/schedule-block/SKILL.md`
- Create: `skills/lunch-prep/SKILL.md`
- Create: `skills/post-deep-work/SKILL.md`

**Workspace bin (committed to repo at `workspace/bin/`, seeded into `~/.tend/workspace/bin/`):**
- Create: `workspace/bin/gws-agenda.sh`
- Create: `workspace/bin/gws-recent-mail.sh`
- Create: `workspace/bin/gws-find-slot.sh`
- Create: `workspace/bin/tend-schedule-event.sh`

**Config / docs:**
- Modify: `tend.toml` — `[google]` and `[google.events.<tag>]` blocks
- Modify: `deploy/tend.service` — `Environment=GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=...`
- Create: `docs/google-setup.md`
- Modify: `CLAUDE.md` — Google integration overview
- Modify: `README.md` — module layout updates

---

## Setup

Before any task, ensure the dev venv is healthy:

```bash
cd /home/pi/hasat
/home/pi/hasat/.venv/bin/pip install -e '.[dev]'
/home/pi/hasat/.venv/bin/pytest -q   # baseline; should be green
```

All `pytest` commands below use the venv binary at `/home/pi/hasat/.venv/bin/pytest` to avoid PATH ambiguity.

---

## Task 1: CronJob gains event_kind / event_payload

**Files:**
- Modify: `src/tend/cron_store.py:34-44`
- Test: `tests/test_cron_store.py`

**Why now:** Foundation. Layer C creates one-shot scheduler jobs that dispatch via `/event` semantics; this requires the data model to carry the event kind + payload alongside the existing skill-request payload. `_row_to_cronjob` already filters unknown keys, so legacy jobs.json files load fine after this change.

- [ ] **Step 1: Add a failing test for event-mode round-trip**

Append to `tests/test_cron_store.py`:

```python
def test_add_job_persists_event_fields(tmp_path):
    store = CronStore(root=tmp_path)
    job = store.add_job(
        name="lunch-fire", kind="at",
        schedule="2026-12-31T11:00:00+00:00",
        tz="UTC", payload={}, source="schedule-watcher",
        enabled=True,
        event_kind="lunch.upcoming",
        event_payload={"event_id": "abc123"},
    )
    assert job.event_kind == "lunch.upcoming"
    assert job.event_payload == {"event_id": "abc123"}
    reloaded = store.load_jobs()
    assert len(reloaded) == 1
    assert reloaded[0].event_kind == "lunch.upcoming"
    assert reloaded[0].event_payload == {"event_id": "abc123"}


def test_legacy_job_without_event_fields_loads_with_defaults(tmp_path):
    """A jobs.json predating event-mode must still load, with both fields None."""
    import json
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / "jobs.json").write_text(json.dumps({
        "version": 1,
        "jobs": [{
            "id": "deadbeef", "name": "old", "kind": "every",
            "schedule": "30m", "tz": "UTC", "payload": {"request": "x"},
            "source": "cli", "enabled": True,
            "created_at": "2026-01-01T00:00:00+00:00",
        }],
    }))
    store = CronStore(root=tmp_path)
    rows = store.load_jobs()
    assert len(rows) == 1
    assert rows[0].event_kind is None
    assert rows[0].event_payload is None
```

- [ ] **Step 2: Run tests — confirm both fail**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_cron_store.py::test_add_job_persists_event_fields tests/test_cron_store.py::test_legacy_job_without_event_fields_loads_with_defaults -v
```

Expected: FAIL — `add_job` rejects unknown kwargs `event_kind`/`event_payload`; `CronJob` has no such fields.

- [ ] **Step 3: Add the fields to `CronJob`**

In `src/tend/cron_store.py`, replace the `CronJob` dataclass:

```python
@dataclass(frozen=True)
class CronJob:
    id: str
    name: str
    kind: str       # "at" | "cron" | "every"
    schedule: str   # cron expr, ISO timestamp, or duration like "30m"
    tz: str | None
    payload: dict
    source: str     # "voice" | "cli" | "skill:<name>"
    enabled: bool
    created_at: str
    event_kind: str | None = None
    event_payload: dict | None = None
```

- [ ] **Step 4: Extend `CronStore.add_job` to accept the new fields**

In `src/tend/cron_store.py`, replace `add_job`:

```python
def add_job(
    self, *,
    name: str, kind: str, schedule: str, tz: str | None,
    payload: dict, source: str, enabled: bool,
    id: str | None = None, created_at: str | None = None,
    event_kind: str | None = None,
    event_payload: dict | None = None,
) -> CronJob:
    import datetime as dt
    job = CronJob(
        id=id or uuid.uuid4().hex,
        name=name, kind=kind, schedule=schedule, tz=tz,
        payload=payload, source=source, enabled=enabled,
        created_at=created_at or dt.datetime.now(
            tz=dt.timezone.utc
        ).isoformat(),
        event_kind=event_kind,
        event_payload=event_payload,
    )
    jobs = self.load_jobs()
    jobs.append(job)
    self.save_jobs(jobs)
    return job
```

- [ ] **Step 5: Run tests — confirm both pass + nothing else broke**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_cron_store.py -v
```

Expected: All tests in `test_cron_store.py` PASS, including the two new ones.

- [ ] **Step 6: Commit**

```bash
git add src/tend/cron_store.py tests/test_cron_store.py
git commit -m "feat(cron_store): CronJob.event_kind / event_payload (back-compat)"
```

---

## Task 2: Extract `dispatch_event` helper

**Files:**
- Create: `src/tend/dispatch.py`
- Modify: `src/tend/webhook.py:59-82`
- Test: `tests/test_dispatch.py` (create), `tests/test_webhook.py` (verify still green)

**Why now:** The scheduler will dispatch event-mode jobs via the same path the webhook uses for `POST /event`. Putting the loop in a shared helper avoids duplicating `find_event_subscribers` + dispatch fan-out logic across two files.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dispatch.py`:

```python
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
```

- [ ] **Step 2: Run — confirm fail**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_dispatch.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'tend.dispatch'`.

- [ ] **Step 3: Implement `tend.dispatch`**

Create `src/tend/dispatch.py`:

```python
"""Shared event fan-out helper.

Used by both `webhook.py` (`POST /event`) and `scheduler.py` (event-mode
scheduled jobs) to find skills subscribed to an event kind and dispatch
each via the GeneralWorker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger

from tend.skills import find_event_subscribers


async def dispatch_event(
    *,
    kind: str,
    payload: dict,
    dispatch: Callable[[str, dict], Awaitable[None]],
    skills_root: Path,
) -> list[str]:
    """Find skills subscribed to `kind` and dispatch them via `dispatch`.

    Returns the list of skill names that were dispatched. Empty list is
    not an error — it means no skill subscribes to this kind.
    """
    try:
        matches = find_event_subscribers(skills_root, kind)
    except Exception:
        logger.exception("event dispatch failed listing subscribers")
        matches = []
    for skill in matches:
        await dispatch("general", {
            "request": f"Handle event {kind}",
            "skill": skill.name,
            "event": {"kind": kind, **payload},
        })
    return [s.name for s in matches]
```

- [ ] **Step 4: Run new tests — confirm pass**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_dispatch.py -v
```

Expected: both PASS.

- [ ] **Step 5: Refactor `webhook.py` to use `dispatch_event`**

In `src/tend/webhook.py`, replace lines 19 and 59-82 (`handle_event`):

Replace the import:
```python
from tend.dispatch import dispatch_event
```
(remove the `from tend.skills import find_event_subscribers` line)

Replace `handle_event`:
```python
async def handle_event(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid-json"}, status=400)
    kind = body.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        return web.json_response(
            {"error": "missing 'kind' field"}, status=400,
        )
    payload = {k: v for k, v in body.items() if k != "kind"}
    names = await dispatch_event(
        kind=kind, payload=payload,
        dispatch=dispatch, skills_root=skills_root,
    )
    return web.json_response({"dispatched": names})
```

- [ ] **Step 6: Run webhook tests — confirm still green**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_webhook.py tests/test_dispatch.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tend/dispatch.py src/tend/webhook.py tests/test_dispatch.py
git commit -m "refactor: extract dispatch_event helper for /event + scheduler"
```

---

## Task 3: Scheduler dispatches event-mode jobs

**Files:**
- Modify: `src/tend/scheduler.py:35-46, 226-265`
- Modify: `src/tend/main.py:142-151, 196-197`
- Test: `tests/test_scheduler.py`

**Why now:** With `event_kind` on `CronJob` and `dispatch_event` extracted, the scheduler can route event-mode jobs through the shared event-fan-out path while leaving legacy jobs (no `event_kind`) on the existing GeneralWorker dispatch.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_scheduler.py`:

```python
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
    store, dispatch, tmp_path,
):
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
        skills_root=skills_root,
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
    skills_root = tmp_path / "skills"
    skills_root.mkdir()

    bus = MagicMock()
    bus.publish = AsyncMock()
    s = Scheduler(
        "scheduler",
        bus=bus,
        store=store,
        dispatch=dispatch,
        skills_root=skills_root,
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
```

Also update the local helper `_new_scheduler` (lines ~30-40 of `tests/test_scheduler.py`) to pass `skills_root`:

```python
def _new_scheduler(store, dispatch, **overrides):
    bus = MagicMock()
    bus.publish = AsyncMock()
    return Scheduler(
        "scheduler",
        bus=bus,
        store=store,
        dispatch=dispatch,
        skills_root=overrides.get("skills_root"),
        default_tz=overrides.get("default_tz", "UTC"),
        missed_at_policy=overrides.get("missed_at_policy", "run-on-restart"),
    )
```

- [ ] **Step 2: Run — confirm fail**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_scheduler.py::test_event_mode_job_routes_via_dispatch_event tests/test_scheduler.py::test_legacy_job_still_dispatches_to_general -v
```

Expected: FAIL — `Scheduler.__init__` rejects `skills_root`; `add_job` rejects `event_kind`.

- [ ] **Step 3: Add `skills_root` to `Scheduler.__init__` and `add_job`**

In `src/tend/scheduler.py`, replace the `Scheduler.__init__`:

```python
def __init__(
    self,
    name: str,
    *,
    bus: AgentBus,
    store: CronStore,
    dispatch: Callable[[str, dict], Awaitable[None]],
    skills_root: Path | None = None,
    default_tz: str = "UTC",
    missed_at_policy: MissedPolicy = "run-on-restart",
):
    super().__init__(name, bus=bus)
    self._store = store
    self._dispatch = dispatch
    self._skills_root = skills_root
    self._default_tz = default_tz
    self._missed_policy = missed_at_policy
    self._loop_task: asyncio.Task | None = None
    self._wakeup = asyncio.Event()
```

Add `from pathlib import Path` to the imports if not already present.

Replace `add_job`:

```python
def add_job(
    self,
    *,
    when: str,
    request: str,
    name: str,
    source: str,
    tz: str | None = None,
    payload_extras: dict | None = None,
    event_kind: str | None = None,
    event_payload: dict | None = None,
) -> CronJob:
    # Validate everything before persisting — a bad tz would otherwise
    # leave a half-written row in jobs.json.
    kind, schedule = parse_when(when)
    effective_tz = tz or self._default_tz
    nfa = next_fire_at(
        kind, schedule, effective_tz, dt.datetime.now(tz=ZoneInfo("UTC")),
    )
    payload: dict = {"request": request}
    if payload_extras:
        payload.update(payload_extras)
    job = self._store.add_job(
        name=name, kind=kind, schedule=schedule, tz=effective_tz,
        payload=payload, source=source, enabled=True,
        event_kind=event_kind, event_payload=event_payload,
    )
    self._store.set_state(
        job.id,
        JobState(next_run_at=nfa.isoformat()),
    )
    self._wakeup.set()
    return job
```

- [ ] **Step 4: Branch `_fire_job` on `event_kind`**

In `src/tend/scheduler.py`, replace `_fire_job`:

```python
async def _fire_job(self, job: CronJob) -> None:
    st = self._store.get_state(job.id)
    try:
        if job.event_kind:
            from tend.dispatch import dispatch_event
            await dispatch_event(
                kind=job.event_kind,
                payload=job.event_payload or {},
                dispatch=self._dispatch,
                skills_root=self._skills_root,
            )
        else:
            await self._dispatch("general", job.payload)
        new_state = JobState(
            last_run_at=dt.datetime.now(tz=ZoneInfo("UTC")).isoformat(),
            last_run_status="succeeded",
            last_error=None,
            next_run_at=None,
            consecutive_errors=0,
        )
    except Exception as e:
        logger.exception(f"scheduler dispatch failed for {job.name!r}")
        new_state = JobState(
            last_run_at=dt.datetime.now(tz=ZoneInfo("UTC")).isoformat(),
            last_run_status="failed",
            last_error=str(e),
            next_run_at=None,
            consecutive_errors=st.consecutive_errors + 1,
        )

    if job.kind == "at":
        self._store.remove_job(job.id)
        self._store.remove_state(job.id)
        return

    nfa = next_fire_at(
        job.kind, job.schedule, job.tz or self._default_tz,
        dt.datetime.now(tz=ZoneInfo("UTC")),
    )
    self._store.set_state(
        job.id,
        JobState(
            last_run_at=new_state.last_run_at,
            last_run_status=new_state.last_run_status,
            last_error=new_state.last_error,
            next_run_at=nfa.isoformat(),
            consecutive_errors=new_state.consecutive_errors,
        ),
    )
```

- [ ] **Step 5: Wire `skills_root` through `main.py`**

In `src/tend/main.py`, replace the Scheduler construction (currently at lines 142-151):

```python
scheduler = Scheduler(
    "scheduler",
    bus=runner.bus,
    store=cron_store,
    dispatch=lambda target, payload: scheduler.request_task(
        target, payload=payload,
    ),
    skills_root=Path.home() / ".tend" / "skills",
    default_tz=settings.timezone or "UTC",
    missed_at_policy=settings.scheduler.missed_at_policy,
)
```

- [ ] **Step 6: Run scheduler tests — confirm all pass**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_scheduler.py -v
```

Expected: all PASS, including the two new ones. The `_new_scheduler` helper change should already make existing tests still pass.

- [ ] **Step 7: Run the whole test suite to catch any regressions**

```bash
/home/pi/hasat/.venv/bin/pytest -q
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/tend/scheduler.py src/tend/main.py tests/test_scheduler.py
git commit -m "feat(scheduler): event-mode jobs dispatch via dispatch_event"
```

---

## Task 4: `tend schedule add` accepts `--event` / `--payload`

**Files:**
- Modify: `src/tend/cli.py:495-518, 769-775`
- Test: `tests/test_cli_schedule.py`

**Why now:** The schedule-watcher (Task 8) shells out to `tend schedule add` to create one-shot jobs from a skill subprocess. It needs to express "fire `lunch.upcoming` with this payload" through the CLI.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_cli_schedule.py`:

```python
def test_schedule_add_event_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEND_ROOT", str(tmp_path))
    from tend.cli import main as cli_main
    from tend.cron_store import CronStore

    rc = cli_main([
        "schedule", "add",
        "--when", "2026-12-31T11:00:00+00:00",
        "--event", "lunch.upcoming",
        "--payload", '{"event_id":"abc","cal":"primary"}',
        "--name", "lunch-fire",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "added" in captured.out
    store = CronStore(root=tmp_path)
    rows = store.load_jobs()
    assert len(rows) == 1
    assert rows[0].event_kind == "lunch.upcoming"
    assert rows[0].event_payload == {"event_id": "abc", "cal": "primary"}


def test_schedule_add_event_requires_payload(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEND_ROOT", str(tmp_path))
    from tend.cli import main as cli_main
    rc = cli_main([
        "schedule", "add",
        "--when", "2026-12-31T11:00:00+00:00",
        "--event", "lunch.upcoming",
        "--name", "lunch-fire",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "payload" in err.lower()


def test_schedule_add_event_and_request_mutually_exclusive(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("TEND_ROOT", str(tmp_path))
    from tend.cli import main as cli_main
    rc = cli_main([
        "schedule", "add",
        "--when", "in 1m",
        "--event", "x.y",
        "--payload", "{}",
        "--request", "do something",
        "--name", "weird",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "mutually exclusive" in err.lower() or "not allowed" in err.lower()
```

- [ ] **Step 2: Run — confirm fail**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_cli_schedule.py -v
```

Expected: FAIL — argparse rejects `--event`/`--payload`.

- [ ] **Step 3: Update parser + handler**

In `src/tend/cli.py`, replace the `sa = sched.add_parser(...)` block (around lines 769-775):

```python
sa = sched.add_parser("add", help="Add a new schedule.")
sa.add_argument("--when", required=True,
                help="cron expr / 'in 30m' / 'every 30m' / ISO timestamp")
sa.add_argument("--name", required=True, help="Label for cancel/list later.")
sa.add_argument("--tz", default=None, help="Timezone for cron schedules.")
sa.add_argument("--request", default=None,
                help="What the worker should do. Mutually exclusive with --event.")
sa.add_argument("--event", default=None,
                help="Event kind to dispatch (e.g. lunch.upcoming). Requires --payload.")
sa.add_argument("--payload", default=None,
                help="JSON payload for --event mode.")
sa.set_defaults(func=cmd_schedule_add)
```

Replace `cmd_schedule_add`:

```python
def cmd_schedule_add(args) -> int:
    from tend.cron_time import InvalidWhen, parse_when, next_fire_at
    import datetime as _dt
    from zoneinfo import ZoneInfo

    if args.event and args.request:
        print(
            "--event and --request are mutually exclusive.",
            file=sys.stderr,
        )
        return 2
    if args.event and not args.payload:
        print("--event requires --payload (use '{}' for empty).", file=sys.stderr)
        return 2
    if not args.event and not args.request:
        print("either --request or --event is required.", file=sys.stderr)
        return 2

    event_payload: dict | None = None
    if args.event:
        try:
            event_payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"--payload is not valid JSON: {e}", file=sys.stderr)
            return 2
        if not isinstance(event_payload, dict):
            print("--payload must be a JSON object.", file=sys.stderr)
            return 2

    store = _cron_store()
    try:
        kind, schedule = parse_when(args.when)
    except InvalidWhen as e:
        print(f"invalid --when: {e}", file=sys.stderr)
        return 2

    if args.event:
        payload = {}
    else:
        payload = {"request": args.request}

    job = store.add_job(
        name=args.name, kind=kind, schedule=schedule,
        tz=args.tz or "UTC",
        payload=payload,
        source="cli", enabled=True,
        event_kind=args.event,
        event_payload=event_payload,
    )
    nfa = next_fire_at(
        kind, schedule, args.tz or "UTC",
        _dt.datetime.now(tz=ZoneInfo("UTC")),
    )
    from tend.cron_store import JobState
    store.set_state(job.id, JobState(next_run_at=nfa.isoformat()))
    print(f"added {job.id[:8]} {job.name}")
    return 0
```

- [ ] **Step 4: Run tests — confirm new pass + old still pass**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_cli_schedule.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/cli.py tests/test_cli_schedule.py
git commit -m "feat(cli): tend schedule add --event/--payload"
```

---

## Task 5: `GoogleConfig` Pydantic models

**Files:**
- Modify: `src/tend/config.py:42-109`
- Test: `tests/test_config.py`

**Why now:** Layer A and C skills read from `[google.events.<tag>]` and `[google].watched_calendars`; the watcher (Task 7) iterates these. Centralising in `config.py` follows the existing `SchedulerConfig`/`WebhookConfig`/`AnnouncerConfig` pattern.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_config.py`:

```python
def test_google_config_defaults_present():
    from tend.config import GoogleConfig
    c = GoogleConfig()
    assert c.watched_calendars == []
    assert c.events == {}


def test_google_event_config_defaults():
    from tend.config import GoogleEventConfig
    e = GoogleEventConfig()
    assert e.upcoming_lead == "0m"
    assert e.emit == ["starting"]


def test_google_config_loads_from_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tend.toml").write_text("""
[google]
watched_calendars = ["tend", "primary"]

[google.events.lunch]
upcoming_lead = "60m"
emit = ["upcoming", "starting"]

[google.events.deep-work]
upcoming_lead = "5m"
emit = ["upcoming", "starting", "ended"]
""")
    for k in list(os.environ):
        if k.startswith("TEND_"):
            monkeypatch.delenv(k)

    from tend.config import Settings
    s = Settings()
    assert s.google.watched_calendars == ["tend", "primary"]
    assert s.google.events["lunch"].upcoming_lead == "60m"
    assert s.google.events["lunch"].emit == ["upcoming", "starting"]
    assert s.google.events["deep-work"].emit == ["upcoming", "starting", "ended"]
```

- [ ] **Step 2: Run — confirm fail**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_config.py::test_google_config_defaults_present tests/test_config.py::test_google_event_config_defaults tests/test_config.py::test_google_config_loads_from_toml -v
```

Expected: FAIL — `GoogleConfig` does not exist.

- [ ] **Step 3: Add the models**

In `src/tend/config.py`, after `AnnouncerConfig` (around line 60) add:

```python
class GoogleEventConfig(BaseModel):
    """Per-tag config in `[google.events.<tag>]` blocks."""

    upcoming_lead: str = "0m"
    emit: list[str] = ["starting"]


class GoogleConfig(BaseModel):
    """Google integration config from `[google]` block."""

    watched_calendars: list[str] = []
    events: dict[str, GoogleEventConfig] = {}
```

In the `Settings` class body, near the other config blocks (after `announcer:`):

```python
google: GoogleConfig = GoogleConfig()
```

- [ ] **Step 4: Run — confirm pass**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_config.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/config.py tests/test_config.py
git commit -m "feat(config): GoogleConfig + GoogleEventConfig models"
```

---

## Task 6: Watcher — tag parsing + phase computation

**Files:**
- Create: `src/tend/google_watcher.py`
- Test: `tests/test_google_watcher.py`

**Why now:** Pure logic with no I/O — start here so the rest of the watcher can lean on tested primitives. Following the `cron_time.py` precedent (pure helpers separated from `scheduler.py`).

- [ ] **Step 1: Write failing tests**

Create `tests/test_google_watcher.py`:

```python
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
```

- [ ] **Step 2: Run — confirm fail**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_google_watcher.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the helpers**

Create `src/tend/google_watcher.py`:

```python
"""Schedule-watcher: pure logic for parsing event titles and computing
phase fire times. The orchestrator (run_tick) is in the same module
but separated from these helpers for clean unit testing.
"""

from __future__ import annotations

import datetime as dt
import re


_TAG_RE = re.compile(r"^\[([\w-]+)\]")


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
```

- [ ] **Step 4: Run — confirm pass**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_google_watcher.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tend/google_watcher.py tests/test_google_watcher.py
git commit -m "feat(google_watcher): tag parser + phase fire-time helpers"
```

---

## Task 7: Watcher — state file + `run_tick` orchestrator

**Files:**
- Modify: `src/tend/google_watcher.py`
- Modify: `tests/test_google_watcher.py`
- Create: `tests/fixtures/gws/agenda.json`
- Create: `tests/fixtures/gws/calendar-list.json`
- Create: `tests/fixtures/gws/empty-agenda.json`

**Why now:** With pure helpers in place, wire them up with subprocess calls (mockable) and a JSON state file. The result is a `run_tick(...)` function that's testable end-to-end with mocks for `gws` calls and `subprocess.run` for `tend schedule add`.

- [ ] **Step 1: Create fixture files**

Create `tests/fixtures/gws/calendar-list.json`:

```json
[
  {"id": "tend-cal-id-001", "summary": "tend"},
  {"id": "primary-cal-id", "summary": "primary"},
  {"id": "personal-cal-id", "summary": "Personal"}
]
```

Create `tests/fixtures/gws/agenda.json`:

```json
[
  {
    "id": "evt-1",
    "summary": "[lunch] with kids",
    "start": {"dateTime": "2026-05-09T12:00:00+00:00"},
    "end": {"dateTime": "2026-05-09T13:00:00+00:00"},
    "updated": "2026-05-08T20:00:00+00:00"
  },
  {
    "id": "evt-2",
    "summary": "[deep-work] write the plan",
    "start": {"dateTime": "2026-05-09T09:00:00+00:00"},
    "end": {"dateTime": "2026-05-09T11:00:00+00:00"},
    "updated": "2026-05-08T20:00:00+00:00"
  },
  {
    "id": "evt-3",
    "summary": "Random meeting",
    "start": {"dateTime": "2026-05-09T14:00:00+00:00"},
    "end": {"dateTime": "2026-05-09T15:00:00+00:00"},
    "updated": "2026-05-08T20:00:00+00:00"
  }
]
```

Create `tests/fixtures/gws/empty-agenda.json`:

```json
[]
```

- [ ] **Step 2: Write failing tests for state + run_tick**

Append to `tests/test_google_watcher.py`:

```python
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
        if cmd[:2] == ["gws", "calendar"] and "list" in cmd:
            result.stdout = _load_fixture("calendar-list.json")
        elif cmd[:2] == ["gws", "calendar"] and "+agenda" in cmd:
            # First watched calendar: return the agenda fixture.
            # Second (and further) watched calendars: return empty.
            cal_arg_idx = cmd.index("--calendar") + 1
            cal_id = cmd[cal_arg_idx]
            if cal_id == "tend-cal-id-001":
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
        if cmd[:2] == ["gws", "calendar"] and "list" in cmd:
            result.stdout = _load_fixture("calendar-list.json")
        elif cmd[:2] == ["gws", "calendar"] and "+agenda" in cmd:
            cal_arg_idx = cmd.index("--calendar") + 1
            if cmd[cal_arg_idx] == "tend-cal-id-001":
                result.stdout = json.dumps([{
                    "id": "evt-1",
                    "summary": "[lunch] with kids",
                    "start": {"dateTime": "2026-05-09T12:00:00+00:00"},
                    "end": {"dateTime": "2026-05-09T13:00:00+00:00"},
                    "updated": "2026-05-08T20:00:00+00:00",
                }])
            else:
                result.stdout = "[]"
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
```

- [ ] **Step 3: Run — confirm fail**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_google_watcher.py -v
```

Expected: new tests FAIL — `load_state`, `save_state`, `prune_state`, `run_tick` don't exist.

- [ ] **Step 4: Implement state helpers + `run_tick`**

Append to `src/tend/google_watcher.py`:

```python
import json
import subprocess
import urllib.request
from pathlib import Path

from loguru import logger


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
```

Note: `run_tick` is `async def` for future-proofing (HTTP could become async via aiohttp), but in v1 it does only sync I/O. The tests still need `pytest-asyncio` enabled (the project already uses it via `tests/conftest.py`).

- [ ] **Step 5: Run — confirm pass**

```bash
/home/pi/hasat/.venv/bin/pytest tests/test_google_watcher.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tend/google_watcher.py tests/test_google_watcher.py tests/fixtures/gws/
git commit -m "feat(google_watcher): state file + run_tick orchestrator"
```

---

## Task 8: schedule-watcher skill + workspace tick script

**Files:**
- Create: `skills/schedule-watcher/SKILL.md`
- Create: `skills/schedule-watcher/bin/tick.py`
- Modify: `src/tend/main.py:54-83, 186-187`

**Why now:** With the watcher Python module complete, ship a SKILL.md that the heartbeat-fired GeneralWorker can run, plus a tiny entry script that imports `tend.google_watcher.run_tick`. Seed it from main.py so first boot installs it automatically.

- [ ] **Step 1: Create the workspace tick script**

Create `skills/schedule-watcher/bin/tick.py`:

```python
#!/usr/bin/env python3
"""Schedule-watcher tick — invoked by the SKILL.md.

Reads google config + tend.toml, runs run_tick() against the live
~/.tend/, prints a one-line JSON summary to stdout. Stderr is the loguru
log stream (handled by the parent process if redirected).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from tend.config import settings
from tend.google_watcher import parse_lead_seconds, run_tick


def main() -> int:
    google = settings.google
    if not google.watched_calendars:
        print(json.dumps({"skipped": "no_watched_calendars"}))
        return 0

    events_config: dict[str, dict] = {}
    for tag, cfg in google.events.items():
        try:
            lead_s = parse_lead_seconds(cfg.upcoming_lead)
        except ValueError as e:
            print(
                f"google.events.{tag}.upcoming_lead invalid: {e}",
                file=sys.stderr,
            )
            continue
        events_config[tag] = {
            "upcoming_lead_s": lead_s,
            "emit": list(cfg.emit),
        }

    state_path = (
        Path.home() / ".tend" / "cache" / "schedule-watcher-state.json"
    )
    webhook_token = os.environ.get("TEND_WEBHOOK_TOKEN", "")
    webhook_url = (
        f"http://{settings.webhook.host}:{settings.webhook.port}/event"
    )
    now = dt.datetime.now(tz=ZoneInfo("UTC"))

    summary = asyncio.run(run_tick(
        watched_names=list(google.watched_calendars),
        events_config=events_config,
        state_path=state_path,
        webhook_token=webhook_token,
        webhook_url=webhook_url,
        now=now,
    ))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Create the SKILL.md**

Create `skills/schedule-watcher/SKILL.md`:

```markdown
---
name: schedule-watcher
description: Periodic check of watched calendars. Reads bracket-tagged events ([deep-work] X, [lunch] Y) on the tend and primary calendars and schedules one-shot phase events (tag.upcoming, tag.starting, tag.ended) so reactive skills can run on time. Silent unless something noteworthy comes up.
silent_default: true
triggers:
  - when: "every 15m"
---

# schedule-watcher

You are the schedule-watcher tick. Run the tick script and act on what
it returns.

## Procedure

1. Run the tick:

   ```bash
   python3 ~/.tend/skills/schedule-watcher/bin/tick.py
   ```

   The output is a single JSON line like
   `{"scheduled": 3, "seen_tagged": 2, "missed_fired": 0}` or
   `{"skipped": "no_watched_calendars"}`.

2. Exit silently. End your run with the literal phrase
   `(nothing to surface)` as your last assistant message.

## Notes

- This skill must NEVER speak unprompted. The tick has its own log
  channel; if errors happen, the user can read them in `/tmp/tend.log`.
- If the tick prints `skipped: no_watched_calendars`, the user hasn't
  set up the Google integration yet. Stay silent.
```

- [ ] **Step 3: Add seeding to main.py**

In `src/tend/main.py`, after `_seed_heartbeat_skill()` (around line 83), add:

```python
SCHEDULE_WATCHER_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "schedule-watcher"


def _seed_skill_from_repo(skill_dir: Path) -> None:
    """Copy a repo-shipped skill into ~/.tend/skills/<name>/ if not
    already present. Idempotent: never overwrites a skill the user has
    edited."""
    target = Path.home() / ".tend" / "skills" / skill_dir.name
    if target.exists():
        return
    import shutil
    shutil.copytree(skill_dir, target)
    logger.info(f"seeded skill {skill_dir.name}")
```

After the existing `_seed_heartbeat_skill()` call (around line 187), add:

```python
_seed_skill_from_repo(SCHEDULE_WATCHER_SKILL_DIR)
```

- [ ] **Step 4: Verify schedule-watcher seeds and runs end-to-end (manual smoke)**

```bash
# Start a clean tend root
TEND_ROOT=/tmp/tend-smoke /home/pi/hasat/.venv/bin/python -c "
from pathlib import Path
import shutil; shutil.rmtree('/tmp/tend-smoke', ignore_errors=True)
from tend.main import _seed_skill_from_repo, SCHEDULE_WATCHER_SKILL_DIR
target_root = Path('/tmp/tend-smoke')
import os; os.environ['HOME'] = '/tmp/tend-smoke-home'
import shutil; shutil.rmtree('/tmp/tend-smoke-home', ignore_errors=True)
Path('/tmp/tend-smoke-home/.tend/skills').mkdir(parents=True)
_seed_skill_from_repo(SCHEDULE_WATCHER_SKILL_DIR)
print('seeded:', list((Path('/tmp/tend-smoke-home/.tend/skills/schedule-watcher')).iterdir()))
"
```

Expected: prints a list including `SKILL.md` and `bin/`.

(This is a manual sanity check, not an automated test — the seeder is one `shutil.copytree` call which is well-tested elsewhere.)

- [ ] **Step 5: Commit**

```bash
git add skills/schedule-watcher/ src/tend/main.py
git commit -m "feat: schedule-watcher skill + tick script + repo-skill seeder"
```

---

## Task 9: Read skills (briefing, meeting-prep, mail-triage)

**Files:**
- Create: `skills/briefing/SKILL.md`
- Create: `skills/meeting-prep/SKILL.md`
- Create: `skills/mail-triage/SKILL.md`
- Modify: `src/tend/main.py` — seed each via `_seed_skill_from_repo`

**Why now:** With the seeding mechanism in place from Task 8, layer-A skills are pure SKILL.md files plus a one-line seed call.

- [ ] **Step 1: Create `skills/briefing/SKILL.md`**

```markdown
---
name: briefing
description: Morning briefing. Pulls today's events from watched calendars and unread Gmail summary, speaks one or two sentences. Use when the user asks "morning briefing", "what's my day", "brief me", "what's going on".
---

# briefing

Surface today's calendar + inbox state in one or two TTS-friendly
sentences.

## Procedure

1. **Calendar agenda:** for each name in `google.watched_calendars`
   from tend.toml, run:

   ```bash
   bash ~/.tend/workspace/bin/gws-agenda.sh --calendar "<name>" --today --json
   ```

   The script outputs JSON. Parse and combine across calendars. Sort by
   start time.

2. **Inbox:** run:

   ```bash
   bash ~/.tend/workspace/bin/gws-recent-mail.sh --triage --json
   ```

   This wraps `gws gmail +triage --json`.

3. **Speak:** one or two sentences, conversational. Lead with the next
   meeting (if any in the next 6h). Mention unread count + top sender
   if material. If both are empty, say "Nothing on your calendar
   today, inbox is quiet."

## Output style

- One or two sentences. No lists, no markdown.
- Dates as "this morning / this afternoon / tomorrow"; not ISO times.
- If a meeting is in <30min, lead with that.
```

- [ ] **Step 2: Create `skills/meeting-prep/SKILL.md`**

```markdown
---
name: meeting-prep
description: Surfaces context for the user's next meeting. Reads the next event with attendees and grabs recent Gmail threads with each attendee. Use when the user says "prep my next meeting", "who am I meeting next", "what's my next meeting about".
---

# meeting-prep

Find the next meeting on watched calendars and surface the most recent
email context with the attendees.

## Procedure

1. **Find next meeting with attendees:** run

   ```bash
   bash ~/.tend/workspace/bin/gws-agenda.sh --next 1 --json
   ```

   Inspect the result. If the event has no `attendees` field, fall back
   to "no meeting prep available — your next event has no attendees."

2. **For each attendee** (up to 3), pull their recent threads:

   ```bash
   bash ~/.tend/workspace/bin/gws-recent-mail.sh --from <attendee-email> --limit 3 --json
   ```

3. **Speak the prep:**
   - Lead with the meeting time + topic.
   - One short summary per attendee: "Recent thread with Sarah was
     about the Q2 plan."
   - Total: 2-3 sentences.

## Output style

- Conversational TTS. No quoting subject lines verbatim if they're long.
- If recent threads have no clear connection, just name the attendees
  ("with Sarah and Tom").
```

- [ ] **Step 3: Create `skills/mail-triage/SKILL.md`**

```markdown
---
name: mail-triage
description: Summarises unread Gmail. Calls `gws gmail +triage` and groups by sender priority. Use when the user asks "what's in my inbox", "any new mail", "any urgent emails".
---

# mail-triage

Surface the unread inbox in a TTS-friendly summary.

## Procedure

1. Run:

   ```bash
   bash ~/.tend/workspace/bin/gws-recent-mail.sh --triage --json
   ```

2. Pick the 2-3 most material threads (recency + sender importance).
   Rough heuristic: a known correspondent (replied to within last 7
   days) is more important than newsletter-style senders.

3. Speak in 1-3 sentences:
   - Total unread count first.
   - Top 1-2 senders by name.
   - If nothing actionable, say "Inbox is mostly newsletters."

## Output style

- One to three sentences. No reading subject lines verbatim.
```

- [ ] **Step 4: Add the seeders to `main.py`**

In `src/tend/main.py`, near `SCHEDULE_WATCHER_SKILL_DIR`, add:

```python
_REPO_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

REPO_SKILL_NAMES = (
    "schedule-watcher",
    "briefing",
    "meeting-prep",
    "mail-triage",
)
```

Replace the standalone `_seed_skill_from_repo(SCHEDULE_WATCHER_SKILL_DIR)` call with a loop:

```python
for name in REPO_SKILL_NAMES:
    _seed_skill_from_repo(_REPO_SKILLS_DIR / name)
```

Remove the now-unused `SCHEDULE_WATCHER_SKILL_DIR` constant.

- [ ] **Step 5: Quick sanity-check that frontmatter parses**

```bash
/home/pi/hasat/.venv/bin/python -c "
from tend.skills import enumerate_skills
from pathlib import Path
import shutil, os
target = Path('/tmp/tend-skill-check/.tend/skills')
shutil.rmtree('/tmp/tend-skill-check', ignore_errors=True)
target.mkdir(parents=True)
for n in ('briefing', 'meeting-prep', 'mail-triage'):
    shutil.copytree(Path('skills') / n, target / n)
infos = enumerate_skills(target)
for i in infos:
    print(i.name, '|', i.description[:60])
assert {i.name for i in infos} == {'briefing', 'meeting-prep', 'mail-triage'}
print('OK')
"
```

Expected: prints three names; ends with `OK`.

- [ ] **Step 6: Commit**

```bash
git add skills/briefing skills/meeting-prep skills/mail-triage src/tend/main.py
git commit -m "feat: read skills (briefing, meeting-prep, mail-triage)"
```

---

## Task 10: Write skills (routine-setup, schedule-block)

**Files:**
- Create: `skills/routine-setup/SKILL.md`
- Create: `skills/schedule-block/SKILL.md`
- Modify: `src/tend/main.py` — add to `REPO_SKILL_NAMES`

**Why now:** Layer B. These skills exercise the write scope (`calendar.app.created`) by inserting events into the tend calendar.

- [ ] **Step 1: Create `skills/routine-setup/SKILL.md`**

```markdown
---
name: routine-setup
description: Conversational walk-through to set up the user's daily routine as recurring events in the tend calendar. Use when the user says "set up my daily routine", "redo my schedule", "configure my day".
---

# routine-setup

Walk the user through their daily routine and create recurring events
in the tend calendar.

## Procedure

1. **Read the tend calendar id** from `~/.tend/google/tend-calendar-id`.
   If the file is missing, tell the user "tend calendar isn't set up
   yet — run the install step first" and stop.

2. **Walk through blocks** one at a time, asking for time + days
   + duration. Defaults:
   - Exercise: weekday mornings, 30 min
   - Lunch: every day, noon, 60 min
   - Deep work: weekdays, 9am-noon
   - Break: every day, 3pm, 15 min

   For each, ask: "What time do you want exercise? (or skip)" — accept
   ranges like "7-7:30" or "7:00 for 30 minutes".

3. **For each block the user accepts:**
   - Build the event title with the appropriate bracket tag prefix
     (e.g. `[exercise]`, `[lunch]`, `[deep-work]`, `[break]`).
   - Build an RRULE based on the requested days.
   - **Check conflicts:** for each day in the next two weeks where this
     block would land, run:

     ```bash
     bash ~/.tend/workspace/bin/gws-agenda.sh \
       --time-min <day-start> --time-max <day-end> --json
     ```

     Filter to events on the user's primary calendar that overlap. If
     any, surface them: "Tuesday morning has a 9am meeting; skip that
     day or pick another time?"

   - On confirm, insert the recurring event:

     ```bash
     gws calendar +insert \
       --calendar "$(cat ~/.tend/google/tend-calendar-id)" \
       --summary "[exercise]" \
       --start "<first-occurrence-iso>" \
       --end "<first-occurrence-end-iso>" \
       --recurrence "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR" \
       --json
     ```

4. **Spoken summary** at the end (one sentence):
   "Set up your daily routine — exercise, lunch, deep work, and a
   break. You can change anything in Google Calendar directly."

## Output style

- Conversational, ask one question at a time.
- Always confirm before writing: "Should I add this?" — wait for "yes".
- TTS-friendly: spell out times in plain English.
```

- [ ] **Step 2: Create `skills/schedule-block/SKILL.md`**

```markdown
---
name: schedule-block
description: Schedule a single block of time into the tend calendar. Handles approximate times by finding free slots, exact times go directly. Use when the user says "schedule deep work tomorrow morning", "block 2-4pm Wednesday for X", "put a [tag] on my calendar".
---

# schedule-block

Add a single event to the tend calendar.

## Procedure

1. **Read the tend calendar id** from `~/.tend/google/tend-calendar-id`.
   If missing, error out with "tend calendar isn't set up — run the
   install step."

2. **Parse the user's request** for: tag (e.g. `deep-work`, `exercise`),
   day, and time window. The day might be relative ("tomorrow",
   "Friday"). The time might be exact ("2-4pm") or approximate
   ("morning", "evening").

3. **If approximate time, find a free slot:**

   ```bash
   bash ~/.tend/workspace/bin/gws-find-slot.sh \
     --day <YYYY-MM-DD> --window morning|afternoon|evening \
     --duration <minutes> --json
   ```

   Pick the largest free slot returned. Propose to the user:
   "Tomorrow 9-11am is open, sound good?"

4. **Conflict-check exact times** before writing:

   ```bash
   bash ~/.tend/workspace/bin/gws-agenda.sh \
     --time-min <iso-start> --time-max <iso-end> --json
   ```

   If the user's primary calendar has any overlap, surface it and ask
   if they want to proceed anyway.

5. **On user confirm**, insert the event:

   ```bash
   gws calendar +insert \
     --calendar "$(cat ~/.tend/google/tend-calendar-id)" \
     --summary "[<tag>]" \
     --start "<iso-start>" \
     --end "<iso-end>" \
     --json
   ```

6. **Spoken summary**: "Added [tag] from <start> to <end>."

## Output style

- One question at a time.
- Always confirm before writing.
- If the request is fully exact and unambiguous (specific tag, specific
  day, specific time), you can confirm in one turn.
```

- [ ] **Step 3: Add to `REPO_SKILL_NAMES`**

In `src/tend/main.py`:

```python
REPO_SKILL_NAMES = (
    "schedule-watcher",
    "briefing",
    "meeting-prep",
    "mail-triage",
    "routine-setup",
    "schedule-block",
)
```

- [ ] **Step 4: Frontmatter sanity check**

```bash
/home/pi/hasat/.venv/bin/python -c "
from tend.skills import enumerate_skills
from pathlib import Path
import shutil
target = Path('/tmp/tend-skill-check2/.tend/skills')
shutil.rmtree('/tmp/tend-skill-check2', ignore_errors=True)
target.mkdir(parents=True)
for n in ('routine-setup', 'schedule-block'):
    shutil.copytree(Path('skills') / n, target / n)
infos = enumerate_skills(target)
print([i.name for i in infos])
assert {i.name for i in infos} == {'routine-setup', 'schedule-block'}
print('OK')
"
```

Expected: prints both names; ends with `OK`.

- [ ] **Step 5: Commit**

```bash
git add skills/routine-setup skills/schedule-block src/tend/main.py
git commit -m "feat: write skills (routine-setup, schedule-block)"
```

---

## Task 11: Reactive skills (lunch-prep, post-deep-work)

**Files:**
- Create: `skills/lunch-prep/SKILL.md`
- Create: `skills/post-deep-work/SKILL.md`
- Modify: `src/tend/main.py` — add to `REPO_SKILL_NAMES`

**Why now:** Demonstrates the C-layer end-to-end: a watcher tick schedules a `lunch.upcoming` event-mode job, the scheduler fires it, dispatch_event finds these skills via their `events:` frontmatter and dispatches.

- [ ] **Step 1: Create `skills/lunch-prep/SKILL.md`**

```markdown
---
name: lunch-prep
description: Suggest a meal an hour before a calendar event tagged [lunch]. If a meal-plan was generated for today, mention it; otherwise offer to plan one. Subscribed to lunch.upcoming via the schedule-watcher.
events:
  - lunch.upcoming
---

# lunch-prep

You were dispatched because a `lunch.upcoming` event fired. The user
has lunch coming up.

## Procedure

1. **Look for an existing meal plan** for today in
   `~/.tend/workspace/plans/`:

   ```bash
   ls -1t ~/.tend/workspace/plans/*.md 2>/dev/null | head -5
   ```

   If a plan exists with today's date in the filename or front of the
   file, read its top section.

2. **Speak one short sentence:**
   - With existing plan: "Lunch in an hour — your plan is the chicken
     stir-fry with broccoli."
   - Without: "Lunch in an hour — want me to suggest something?"

## Output style

- One sentence. Conversational.
- Don't read out a recipe — this is just a heads-up.
```

- [ ] **Step 2: Create `skills/post-deep-work/SKILL.md`**

```markdown
---
name: post-deep-work
description: Suggests a stretch, water, or short walk after a [deep-work] block ends. Silent unless the block was at least 45 minutes long.
silent_default: true
events:
  - deep-work.ended
---

# post-deep-work

You were dispatched because a `deep-work.ended` event fired. The user
just finished a deep-work block.

## Procedure

1. The dispatch payload includes `event` with `start` and `end`
   timestamps. Compute the duration in minutes.

2. **If the block was less than 45 minutes**, exit silently with the
   literal phrase `(nothing to surface)`.

3. **Otherwise**, pick one short suggestion at random:
   - "Deep work block done — stretch?"
   - "That was a long stretch — drink some water."
   - "Block ended — walk around?"

   Speak it as one sentence.

## Output style

- One sentence. Don't be preachy.
- If you have nothing to say (block too short), end silently.
```

- [ ] **Step 3: Add to `REPO_SKILL_NAMES`**

In `src/tend/main.py`:

```python
REPO_SKILL_NAMES = (
    "schedule-watcher",
    "briefing",
    "meeting-prep",
    "mail-triage",
    "routine-setup",
    "schedule-block",
    "lunch-prep",
    "post-deep-work",
)
```

- [ ] **Step 4: Verify event subscription is parsed correctly**

```bash
/home/pi/hasat/.venv/bin/python -c "
from tend.skills import find_event_subscribers, enumerate_skills
from pathlib import Path
import shutil
target = Path('/tmp/tend-skill-check3/.tend/skills')
shutil.rmtree('/tmp/tend-skill-check3', ignore_errors=True)
target.mkdir(parents=True)
for n in ('lunch-prep', 'post-deep-work'):
    shutil.copytree(Path('skills') / n, target / n)
sl = find_event_subscribers(target, 'lunch.upcoming')
sd = find_event_subscribers(target, 'deep-work.ended')
print('lunch.upcoming:', [s.name for s in sl])
print('deep-work.ended:', [s.name for s in sd])
assert sl[0].name == 'lunch-prep'
assert sd[0].name == 'post-deep-work'
print('OK')
"
```

Expected: prints both subscriptions; ends with `OK`.

- [ ] **Step 5: Commit**

```bash
git add skills/lunch-prep skills/post-deep-work src/tend/main.py
git commit -m "feat: reactive skills (lunch-prep, post-deep-work)"
```

---

## Task 12: Workspace shared scripts

**Files:**
- Create: `workspace/bin/gws-agenda.sh`
- Create: `workspace/bin/gws-recent-mail.sh`
- Create: `workspace/bin/gws-find-slot.sh`
- Create: `workspace/bin/tend-schedule-event.sh`
- Modify: `src/tend/main.py` — seeder for workspace bin

**Why now:** Skills (Tasks 9-11) reference these scripts in their procedures. **Order note:** if you prefer for each commit to be runtime-functional in isolation, do this task before Tasks 9-11. The numbered order is fine for plan-level review since runtime end-to-end happens only at the final smoke check.

- [ ] **Step 1: Create `workspace/bin/gws-agenda.sh`**

```bash
#!/usr/bin/env bash
# gws-agenda.sh — wrapper around `gws calendar +agenda` with sane defaults.
#
# Usage:
#   gws-agenda.sh --calendar <name> --today --json
#   gws-agenda.sh --calendar <name> --time-min <iso> --time-max <iso> --json
#   gws-agenda.sh --next 1 --json
#
# Reads tend.toml watched_calendars when --watched is passed.

set -euo pipefail
exec gws calendar +agenda "$@"
```

`chmod +x` after creating.

- [ ] **Step 2: Create `workspace/bin/gws-recent-mail.sh`**

```bash
#!/usr/bin/env bash
# gws-recent-mail.sh — wrapper for gmail reads.
#
# Usage:
#   gws-recent-mail.sh --triage --json
#   gws-recent-mail.sh --from sender@example.com --limit 3 --json

set -euo pipefail

mode=""
if [[ "${1:-}" == "--triage" ]]; then
    shift
    exec gws gmail +triage "$@"
elif [[ "${1:-}" == "--from" ]]; then
    shift
    sender="$1"; shift
    exec gws gmail list --query "from:$sender" "$@"
else
    exec gws gmail list "$@"
fi
```

- [ ] **Step 3: Create `workspace/bin/gws-find-slot.sh`**

```bash
#!/usr/bin/env bash
# gws-find-slot.sh — find the largest free slot on a given day in a window.
#
# Usage:
#   gws-find-slot.sh --day 2026-05-09 --window morning --duration 60 --json
#
# Reads watched calendars from tend.toml; returns largest contiguous free slot.

set -euo pipefail

day=""
window=""
duration=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --day) day="$2"; shift 2 ;;
        --window) window="$2"; shift 2 ;;
        --duration) duration="$2"; shift 2 ;;
        --json) shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

case "$window" in
    morning) start_h=7; end_h=12 ;;
    afternoon) start_h=12; end_h=17 ;;
    evening) start_h=17; end_h=21 ;;
    *) echo "window must be morning|afternoon|evening" >&2; exit 2 ;;
esac

# Pull events for the day from the primary calendar (gws default).
events_json="$(gws calendar +agenda \
    --time-min "${day}T${start_h}:00:00+00:00" \
    --time-max "${day}T${end_h}:00:00+00:00" \
    --json)"

# Largest free slot calculation: emit the day window minus any overlaps.
# For v1, use a simple python helper. (jq alone gets unwieldy.)
python3 - "$events_json" "$day" "$start_h" "$end_h" "$duration" <<'PY'
import json, sys
events = json.loads(sys.argv[1])
day = sys.argv[2]
sh = int(sys.argv[3])
eh = int(sys.argv[4])
need_min = int(sys.argv[5])
import datetime as dt
start = dt.datetime.fromisoformat(f"{day}T{sh:02d}:00:00+00:00")
end = dt.datetime.fromisoformat(f"{day}T{eh:02d}:00:00+00:00")
busy = []
for ev in events:
    try:
        s = dt.datetime.fromisoformat(ev["start"]["dateTime"])
        e = dt.datetime.fromisoformat(ev["end"]["dateTime"])
        busy.append((max(s, start), min(e, end)))
    except (KeyError, ValueError):
        continue
busy.sort()
free = []
cursor = start
for s, e in busy:
    if s > cursor:
        free.append((cursor, s))
    cursor = max(cursor, e)
if cursor < end:
    free.append((cursor, end))
free.sort(key=lambda t: -(t[1] - t[0]).total_seconds())
out = [{"start": s.isoformat(), "end": e.isoformat(),
        "minutes": int((e - s).total_seconds() / 60)} for s, e in free]
out = [r for r in out if r["minutes"] >= need_min]
print(json.dumps(out[:3]))  # top three by length
PY
```

- [ ] **Step 4: Create `workspace/bin/tend-schedule-event.sh`**

```bash
#!/usr/bin/env bash
# tend-schedule-event.sh — convenience wrapper for `tend schedule add --event`.
#
# Usage:
#   tend-schedule-event.sh --when <iso> --event <kind> --payload <json> --name <unique>

set -euo pipefail
exec tend schedule add "$@"
```

- [ ] **Step 5: Add a workspace-bin seeder to main.py**

In `src/tend/main.py`, near `_seed_skill_from_repo`:

```python
_REPO_WORKSPACE_BIN = (
    Path(__file__).resolve().parents[2] / "workspace" / "bin"
)


def _seed_workspace_bin() -> None:
    """Copy repo workspace/bin/ scripts into ~/.tend/workspace/bin/.
    Per-file: never overwrite existing scripts (user may have edited).
    """
    target = Path.home() / ".tend" / "workspace" / "bin"
    target.mkdir(parents=True, exist_ok=True)
    if not _REPO_WORKSPACE_BIN.exists():
        return
    import shutil
    for src in _REPO_WORKSPACE_BIN.iterdir():
        if not src.is_file():
            continue
        dst = target / src.name
        if dst.exists():
            continue
        shutil.copy2(src, dst)
        dst.chmod(0o755)
```

Call it after the skills loop:

```python
for name in REPO_SKILL_NAMES:
    _seed_skill_from_repo(_REPO_SKILLS_DIR / name)
_seed_workspace_bin()
```

- [ ] **Step 6: Make scripts executable in repo**

```bash
chmod +x workspace/bin/*.sh
```

- [ ] **Step 7: Commit**

```bash
git add workspace/bin/ src/tend/main.py
git commit -m "feat: workspace shared gws helper scripts"
```

---

## Task 13: tend.toml + systemd config

**Files:**
- Modify: `tend.toml`
- Modify: `deploy/tend.service`

**Why now:** Defaults the watcher reads from settings need to be in `tend.toml`; the `gws` env var needs to be set in the unit so subprocess calls can find the credentials.

- [ ] **Step 1: Add `[google]` blocks to `tend.toml`**

Append to `tend.toml`:

```toml
[google]
watched_calendars = ["tend", "primary"]

[google.events.deep-work]
upcoming_lead = "5m"
emit = ["upcoming", "starting", "ended"]

[google.events.lunch]
upcoming_lead = "60m"
emit = ["upcoming", "starting"]

[google.events.exercise]
upcoming_lead = "15m"
emit = ["upcoming", "starting", "ended"]

[google.events.break]
upcoming_lead = "0m"
emit = ["starting"]
```

- [ ] **Step 2: Add the env var to systemd**

In `deploy/tend.service`, in the `[Service]` block (where existing `Environment=...` lines live), add:

```
Environment=GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=%h/.tend/secrets/google-creds.json
```

- [ ] **Step 3: Smoke-test settings load**

```bash
/home/pi/hasat/.venv/bin/python -c "
from tend.config import settings
print('watched:', settings.google.watched_calendars)
print('events:', list(settings.google.events.keys()))
print('lunch lead:', settings.google.events['lunch'].upcoming_lead)
"
```

Expected:

```
watched: ['tend', 'primary']
events: ['deep-work', 'lunch', 'exercise', 'break']
lunch lead: 60m
```

- [ ] **Step 4: Commit**

```bash
git add tend.toml deploy/tend.service
git commit -m "config: tend.toml [google] defaults + systemd env var"
```

---

## Task 14: Setup documentation

**Files:**
- Create: `docs/google-setup.md`

**Why now:** The whole setup procedure is one-time and external to tend itself; a single doc page captures it for future re-installs.

- [ ] **Step 1: Create `docs/google-setup.md`**

```markdown
# Google integration setup

One-time procedure to wire `gws` (Google Workspace CLI) into tend so it
can read your calendar/inbox and write to a tend-owned calendar.

## Prerequisites

- A Google account (any flavour — personal `@gmail.com` or Workspace).
- Node.js available on the Pi (`gws` installs via npm).
- A second machine with a real browser (your laptop) for the OAuth
  consent step. The Pi has no browser.

## 1. Install gws on the Pi

```bash
sudo npm install -g @googleworkspace/cli
gws --version
```

## 2. Create an OAuth client in Google Cloud

1. Open [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials).
2. Create or select a project (any name).
3. Enable: Google Calendar API, Gmail API.
4. **Create credentials** → **OAuth client ID** → Application type
   "Desktop app" → name "tend".
5. Download the JSON. Keep it; you'll point `gws` at it on your laptop.

## 3. Login on your laptop

On your laptop (not the Pi):

```bash
npm install -g @googleworkspace/cli
gws auth setup       # paste the OAuth client JSON when prompted
gws auth login \
  --scope https://www.googleapis.com/auth/gmail.readonly \
  --scope https://www.googleapis.com/auth/calendar.readonly \
  --scope https://www.googleapis.com/auth/calendar.app.created
```

A browser window opens. Approve all three scopes. The CLI stores an
encrypted token locally.

## 4. Export the token to the Pi

On your laptop:

```bash
gws auth export --unmasked > google-creds.json
scp google-creds.json pi:~/.tend/secrets/google-creds.json
```

On the Pi:

```bash
chmod 600 ~/.tend/secrets/google-creds.json
```

## 5. Verify on the Pi

```bash
GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=$HOME/.tend/secrets/google-creds.json \
  gws auth status
```

You should see all three scopes listed.

## 6. Bootstrap the tend calendar

Once on the Pi:

```bash
GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=$HOME/.tend/secrets/google-creds.json \
  gws calendar +insert \
    --summary "tend" \
    --description "Schedule blocks managed by tend" \
    --json \
  | jq -r '.id' \
  > ~/.tend/google/tend-calendar-id

mkdir -p ~/.tend/google
cat ~/.tend/google/tend-calendar-id   # sanity-check the ID is there
```

## 7. Restart tend

```bash
systemctl --user daemon-reload
systemctl --user restart tend
```

## 8. Try it

Speak the wake word, then:

- "Morning briefing." — should describe today's events + inbox state.
- "Set up my daily routine." — walks you through routine-setup.

## Smoke checks

```bash
# Calendar reads work?
gws calendar +agenda --json | head -20

# Watcher finds a tagged event?
python3 ~/.tend/skills/schedule-watcher/bin/tick.py
# expected: {"scheduled": N, "seen_tagged": M, ...}

# A schedule entry was created?
tend schedule list
```

## If a refresh fails (rare)

If `gws auth status` on the Pi later starts reporting "token expired"
(typically only happens if you revoke consent), repeat steps 3-4.
```

- [ ] **Step 2: Commit**

```bash
git add docs/google-setup.md
git commit -m "docs: google-setup.md for one-time install procedure"
```

---

## Task 15: CLAUDE.md + README updates

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

**Why now:** Both reference the module layout + non-choice list; adding the Google integration without updating them leaves the docs lying.

- [ ] **Step 1: Add a "Google integration" section to CLAUDE.md**

In `CLAUDE.md`, after the "Proactive triggers" section (around the line that ends `"off"`), add:

```markdown
## Google integration

`gws` (the Google Workspace CLI, npm `@googleworkspace/cli`) handles
all Google API access. tend wraps nothing — skills shell out to `gws`
directly. See `docs/google-setup.md` for the one-time setup procedure
and `docs/superpowers/specs/2026-05-09-tend-google-and-daily-schedule-design.md`
for the design.

Three layers, all skills:

- **Reads:** `briefing`, `meeting-prep`, `mail-triage` skills.
- **Writes:** `routine-setup` (recurring events) and `schedule-block`
  (one-off) skills, scoped to a tend-owned calendar via
  `calendar.app.created` OAuth scope.
- **Triggers:** `schedule-watcher` skill (heartbeat-fired every 15m)
  parses bracket-tagged event titles (`[deep-work] Foo`,
  `[lunch] Bar`) and creates one-shot scheduler jobs at exact phase
  fire times. Reactive skills like `lunch-prep` and `post-deep-work`
  subscribe via `events:` frontmatter.

The scheduler supports two job dispatch modes:

- Legacy: `payload.request` is sent to the GeneralWorker as a skill
  request (the existing `do_task` path).
- Event-mode: `event_kind` + `event_payload` set; dispatched via
  `dispatch_event` (the same path as `POST /event`), fanning out to
  all skills with matching `events:` frontmatter.
```

In the module layout block, add:

```
src/tend/
  ...
  dispatch.py        Shared event fan-out helper used by webhook + scheduler.
  google_watcher.py  Schedule-watcher pure logic + run_tick orchestrator.
```

And add a non-choice (in the deliberate-non-choices list):

```
- **No Python wrapper around gws.** Skills shell out to `gws` directly.
  Wrapping it is rejected as premature abstraction.
- **Brain has no @tool methods for Google reads.** Calendar / Gmail
  queries always go through GeneralWorker via `do_task`. ~3-5s extra
  latency vs. ~300ms cached, traded for architectural coherence.
```

- [ ] **Step 2: Update `README.md` module layout**

Wherever the module layout is documented in `README.md`, add `dispatch.py` and `google_watcher.py` to the list (location next to `webhook.py` makes sense for `dispatch.py`; `google_watcher.py` next to `scheduler.py`).

- [ ] **Step 3: Final test sweep**

```bash
/home/pi/hasat/.venv/bin/pytest -q
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: CLAUDE.md / README updates for Google integration"
```

---

## Final smoke check

After all tasks land, run end-to-end on the Pi (manual; not part of the
CI suite):

1. Run through `docs/google-setup.md` to install `gws` + OAuth.
2. Restart tend: `systemctl --user restart tend`.
3. Speak: "morning briefing." Expect a TTS response describing today's
   events + inbox state.
4. Speak: "set up my daily routine." Expect a conversational
   walk-through that creates events on the tend calendar.
5. Manually create a `[lunch] test` event 1h from now in your primary
   calendar (or the tend calendar).
6. Wait up to 15 minutes for the watcher to tick. Check
   `tend schedule list` — there should be a `watcher-<id>-upcoming`
   one-shot job at T-60min.
7. When it fires, the `lunch-prep` skill should announce.

---

## Known follow-ups (out of plan scope)

- **24h cooldown on `google-auth` category for OAuth-failure announcements.**
  The spec section 11 promises "first skill to hit gws on an expired
  token announces 'I lost access to your Google account, please
  re-authorize' with 24h cooldown." Implementing it cleanly requires
  skills to surface auth failures via the announcer (which has the
  cooldown machinery) rather than via normal TTS. The simplest path is
  a small `workspace/bin/tend-announce.sh` that POSTs to `/say` with a
  category, and a one-line guidance in each gws-using skill: "if `gws`
  exit code indicates auth failure (e.g., stderr contains
  `Reauthentication`), call this script and stop." Add when the user
  hits this in real life — not blocking v1.

- **`gws gmail +watch` → `/event` real-time hook.** Mentioned in spec
  section 16 as future work. The seam exists today (the watcher already
  posts to `/event` for missed phases), so a separate small daemon (or
  a `gws gmail +watch` long-running subprocess parsed in tend's main
  process) could feed `mail.received` events. Defer until a real
  email-driven skill is designed.

- **Conflict re-balancing.** Spec section 16. If a tend-scheduled
  deep-work block ends up overlapping a meeting added later, tend
  flags but doesn't move. Auto-move logic is a UX call we should make
  after living with manual handling for a few weeks.

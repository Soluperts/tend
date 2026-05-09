# tend Proactive Triggers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cron-style scheduled jobs, a worker-side heartbeat tick, and an HTTP receiver for external producers (vision daemon today, future webhooks later) — all converging through a single ProactiveAnnouncer that handles cooldown, active-Brain deferral, urgency override, and LLMContext logging. Retire `ReminderWorker` as redundant.

**Architecture:** A new `Scheduler` BaseAgent (peer to Brain in the runner) owns wall-clock dispatch via an asyncio loop, reading job state from JSON files under `~/.tend/cron/`. A new `ProactiveAnnouncer` is the single entry point any proactive source uses to speak; it publishes `TTSSpeakFrame` + `LLMMessagesAppendFrame` to the bus. A new `webhook.py` runs an aiohttp server on `127.0.0.1` exposing `POST /say` (direct TTS) and `POST /event` (skill-mediated). `Brain` gains 5 tools for voice authoring; `Brain.remind_in` becomes a thin wrapper over the new scheduler. `GeneralWorker` registers eagerly at boot and accepts `silent_default` / `event` / `skill` in the dispatch payload so the heartbeat skill can run silently.

**Tech Stack:** Python 3.11+, pipecat-ai-subagents (BaseAgent + AgentBus + bus messages), aiohttp (already transitively present via pipecat), pydantic-settings, croniter (new dep), zoneinfo (stdlib). Tests use pytest + pytest-asyncio + freezegun (already in dev deps).

**Reference spec:** `docs/superpowers/specs/2026-05-08-tend-proactive-triggers-design.md`.

---

## File map

Lock the responsibility for each file before any code is written.

**New files:**
- `src/tend/cron_store.py` — load/save `jobs.json` + `jobs-state.json` atomically; `CronJob` and `JobState` dataclasses; CRUD helpers (add, remove, find_by_source, get_state, set_state).
- `src/tend/cron_time.py` — `parse_when(when)` and `next_fire_at(kind, schedule, tz, after)`. Pure functions, no I/O. The `tz` argument is consumed at fire-time, not parse-time.
- `src/tend/announcer.py` — `ProactiveAnnouncer` class: cooldown registry, deferral queue, drain, urgency override, publishes both `TTSSpeakFrame` and `LLMMessagesAppendFrame`.
- `src/tend/scheduler.py` — `Scheduler` BaseAgent: load on init, asyncio dispatch loop, missed-fire policy, public API (`add_job`, `cancel_job`, `list_jobs`, `find_by_source`).
- `src/tend/webhook.py` — aiohttp app, bearer-token auth middleware, `POST /say`, `POST /event`, lifecycle (`start_server`, `stop_server`).

**Modified files:**
- `src/tend/skills.py` — extend `parse_frontmatter` to read `triggers:`, `events:`, `silent_default:`; add `find_event_subscribers(root, kind)`.
- `src/tend/brain.py` — add 5 tools, drop `_ensure_reminder_worker`, rewrite `remind_in` as a `schedule(...)` wrapper, hold a `Scheduler` reference.
- `src/tend/workers/general.py` — accept `silent_default` / `event` / `skill` payload fields; route announcements through the announcer instead of publishing TTS frames directly.
- `src/tend/audio/hub.py` — hold a `ProactiveAnnouncer` reference; on Brain deactivation, call `announcer.drain_pending()`.
- `src/tend/config.py` — add `SchedulerConfig`, `WebhookConfig`, `AnnouncerConfig` Pydantic models; add `tend_webhook_token` secret.
- `src/tend/main.py` — construct cron_store, announcer, scheduler, webhook; register scheduler + webhook with the runner; eager-register the GeneralWorker; seed the heartbeat skill on first boot.
- `src/tend/cli.py` — add `tend schedule list/show/add/rm`, `tend skills enable-triggers/disable-triggers`, `tend webhook test`.
- `tend.toml` — `[scheduler]`, `[webhook]`, `[announcer]`, `[announcer.category]` sections.
- `pyproject.toml` — add `croniter>=1.4` and pin a recent `aiohttp` minimum.
- `CLAUDE.md` — add "Proactive triggers" section, drop ReminderWorker mention, reference the integration doc.

**New tests:**
- `tests/test_cron_store.py`
- `tests/test_cron_time.py`
- `tests/test_announcer.py`
- `tests/test_scheduler.py`
- `tests/test_webhook.py`
- `tests/test_brain_schedule_tools.py`
- `tests/test_skills_triggers.py`
- `tests/test_cli_schedule.py`

**Modified tests:**
- `tests/workers/test_general.py` — silent-default / event payload coverage.
- `tests/test_brain.py` — `remind_in` now goes through scheduler.
- `tests/test_config.py` — new config sections parse.

**Deleted files:**
- `src/tend/workers/reminder.py`
- `tests/workers/test_reminder.py`

**New docs:**
- `docs/integrating-with-tend.md` — outward-facing integration contract.

---

## Task ordering rationale

The order is foundation → composition → integration → docs:
- Tasks 1–4 build pure data/time helpers and the announcer with no external dependencies.
- Tasks 5–7 wire the scheduler and webhook on top of those primitives.
- Tasks 8–11 connect the new components to skills, brain, and the worker.
- Tasks 12–14 finalise configuration, hub wiring, and main-process construction.
- Tasks 15–17 add CLI surfaces.
- Tasks 18–21 retire ReminderWorker, write the integration doc, update CLAUDE.md, and run the full smoke pass.

Each task's tests fail-first against the previous green state, so the suite stays green at every commit boundary.

---

### Task 1: Add croniter dep and cron-time helpers

**Files:**
- Modify: `pyproject.toml` — add `croniter>=1.4` to runtime deps.
- Create: `src/tend/cron_time.py`
- Create: `tests/test_cron_time.py`

- [ ] **Step 1: Add the dependency**

Edit `pyproject.toml`:

```toml
dependencies = [
    "anthropic>=0.40",
    "croniter>=1.4",
    "loguru>=0.7",
    "openwakeword>=0.4",
    "pipecat-ai[whisper,piper,silero,local,deepgram,elevenlabs]>=1.1",
    "pipecat-ai-subagents==0.4.0",
    "rapidfuzz>=3.0",
    "pydantic-settings>=2.2",
]
```

Then install: `pip install -e '.[dev]'`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_cron_time.py`:

```python
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


def test_parse_when_invalid_raises():
    with pytest.raises(InvalidWhen):
        parse_when("nonsense")
    with pytest.raises(InvalidWhen):
        parse_when("in")
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
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_cron_time.py -v
```

Expected: every test fails with `ModuleNotFoundError: tend.cron_time`.

- [ ] **Step 4: Implement the helpers**

Create `src/tend/cron_time.py`:

```python
"""Pure helpers for parsing schedule strings and computing next-fire times.

No I/O. Used by Scheduler, Brain.schedule, and CLI alike.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal
from zoneinfo import ZoneInfo

from croniter import croniter


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
    return int(m.group(1)) * _DURATION_TO_SECONDS[m.group(2)]


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
        seconds = int(m.group(1)) * _DURATION_TO_SECONDS[m.group(2).lower()]
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
    except Exception as e:
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
        zone = ZoneInfo(tz) if tz else after.tzinfo
        base = after.astimezone(zone) if zone else after
        c = croniter(schedule, base)
        return c.get_next(dt.datetime)
    raise InvalidWhen(f"unknown kind: {kind}")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_cron_time.py -v
```

Expected: 11 passes.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/tend/cron_time.py tests/test_cron_time.py
git commit -m "feat: cron-time helpers (parse_when, next_fire_at)"
```

---

### Task 2: cron_store module — durable JSON job state

**Files:**
- Create: `src/tend/cron_store.py`
- Create: `tests/test_cron_store.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cron_store.py`:

```python
"""Tests for cron_store.py — atomic JSON read/write + CRUD."""

from __future__ import annotations

import json
import datetime as dt
from pathlib import Path

import pytest

from tend.cron_store import (
    CronJob,
    CronStore,
    JobState,
)


@pytest.fixture
def store(tmp_path: Path) -> CronStore:
    return CronStore(root=tmp_path)


def test_load_jobs_empty_returns_empty_list(store):
    assert store.load_jobs() == []


def test_save_then_load_roundtrip(store):
    job = CronJob(
        id="abc", name="test", kind="cron", schedule="0 12 * * *",
        tz="America/Los_Angeles",
        payload={"request": "say hi"},
        source="voice", enabled=True,
        created_at="2026-05-08T18:00:00+00:00",
    )
    store.save_jobs([job])
    loaded = store.load_jobs()
    assert len(loaded) == 1
    assert loaded[0] == job


def test_add_job_assigns_id_when_missing(store):
    j = store.add_job(
        name="x", kind="every", schedule="30m",
        tz=None, payload={"request": "y"}, source="cli", enabled=True,
    )
    assert j.id  # non-empty
    assert store.load_jobs()[0].id == j.id


def test_remove_job_by_id(store):
    j = store.add_job(name="a", kind="every", schedule="1m", tz=None,
                      payload={}, source="cli", enabled=True)
    assert store.remove_job(j.id) is True
    assert store.load_jobs() == []


def test_remove_job_unknown_id_returns_false(store):
    assert store.remove_job("nope") is False


def test_find_by_source(store):
    a = store.add_job(name="a", kind="every", schedule="1m", tz=None,
                      payload={}, source="skill:meal-plan", enabled=True)
    b = store.add_job(name="b", kind="every", schedule="1m", tz=None,
                      payload={}, source="skill:meal-plan", enabled=True)
    store.add_job(name="c", kind="every", schedule="1m", tz=None,
                  payload={}, source="voice", enabled=True)
    matches = store.find_by_source("skill:meal-plan")
    ids = sorted(m.id for m in matches)
    assert ids == sorted([a.id, b.id])


def test_remove_by_source_returns_count(store):
    store.add_job(name="a", kind="every", schedule="1m", tz=None,
                  payload={}, source="skill:meal-plan", enabled=True)
    store.add_job(name="b", kind="every", schedule="1m", tz=None,
                  payload={}, source="skill:meal-plan", enabled=True)
    assert store.remove_by_source("skill:meal-plan") == 2
    assert store.load_jobs() == []


def test_state_get_default_empty(store):
    assert store.get_state("any-id") == JobState()


def test_state_set_and_get(store):
    s = JobState(
        last_run_at="2026-05-08T12:00:00+00:00",
        last_run_status="succeeded",
        last_error=None,
        next_run_at="2026-05-09T12:00:00+00:00",
        consecutive_errors=0,
    )
    store.set_state("job-1", s)
    assert store.get_state("job-1") == s


def test_jobs_file_layout(store, tmp_path):
    # add a job, then inspect what was written
    store.add_job(name="x", kind="every", schedule="30m", tz=None,
                  payload={"request": "y"}, source="voice", enabled=True)
    raw = (tmp_path / "cron" / "jobs.json").read_text()
    data = json.loads(raw)
    assert data["version"] == 1
    assert isinstance(data["jobs"], list)
    assert len(data["jobs"]) == 1


def test_atomic_write_uses_temp_file(store, tmp_path, monkeypatch):
    """If os.replace is mocked to fail, the original file is not corrupted."""
    store.add_job(name="x", kind="every", schedule="30m", tz=None,
                  payload={"request": "before"}, source="voice", enabled=True)
    original = (tmp_path / "cron" / "jobs.json").read_text()

    import os
    real_replace = os.replace
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        store.add_job(name="y", kind="every", schedule="1m", tz=None,
                      payload={"request": "after"}, source="voice",
                      enabled=True)
    monkeypatch.setattr(os, "replace", real_replace)
    assert (tmp_path / "cron" / "jobs.json").read_text() == original
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_cron_store.py -v
```

Expected: every test fails with `ModuleNotFoundError: tend.cron_store`.

- [ ] **Step 3: Implement the store**

Create `src/tend/cron_store.py`:

```python
"""Durable JSON-backed storage for scheduled jobs.

Two files under <root>/cron/:
  jobs.json         — definitions, hand-editable, may be git-tracked.
  jobs-state.json   — runtime fields (last_run_at, next_run_at, errors).
                      Gitignored; a crash mid-write leaves it best-effort.

Atomic writes via temp-file-rename mirror SessionStore's pattern so a
partial write never produces a corrupt file.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 1


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


@dataclass(frozen=True)
class JobState:
    last_run_at: str | None = None
    last_run_status: str | None = None
    last_error: str | None = None
    next_run_at: str | None = None
    consecutive_errors: int = 0


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class CronStore:
    """JSON-backed scheduled-job store."""

    def __init__(self, *, root: Path):
        self._root = Path(root)
        self._jobs_path = self._root / "cron" / "jobs.json"
        self._state_path = self._root / "cron" / "jobs-state.json"

    # ---- jobs.json ----

    def load_jobs(self) -> list[CronJob]:
        if not self._jobs_path.exists():
            return []
        data = json.loads(self._jobs_path.read_text(encoding="utf-8"))
        return [CronJob(**row) for row in data.get("jobs", [])]

    def save_jobs(self, jobs: Iterable[CronJob]) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "jobs": [asdict(j) for j in jobs],
        }
        _atomic_write_text(
            self._jobs_path,
            json.dumps(payload, indent=2, sort_keys=True),
        )

    def add_job(
        self, *,
        name: str, kind: str, schedule: str, tz: str | None,
        payload: dict, source: str, enabled: bool,
        id: str | None = None, created_at: str | None = None,
    ) -> CronJob:
        import datetime as dt
        job = CronJob(
            id=id or uuid.uuid4().hex,
            name=name, kind=kind, schedule=schedule, tz=tz,
            payload=payload, source=source, enabled=enabled,
            created_at=created_at or dt.datetime.now(
                tz=dt.timezone.utc
            ).isoformat(),
        )
        jobs = self.load_jobs()
        jobs.append(job)
        self.save_jobs(jobs)
        return job

    def remove_job(self, job_id: str) -> bool:
        jobs = self.load_jobs()
        keep = [j for j in jobs if j.id != job_id]
        if len(keep) == len(jobs):
            return False
        self.save_jobs(keep)
        return True

    def find_by_source(self, source: str) -> list[CronJob]:
        return [j for j in self.load_jobs() if j.source == source]

    def remove_by_source(self, source: str) -> int:
        jobs = self.load_jobs()
        keep = [j for j in jobs if j.source != source]
        removed = len(jobs) - len(keep)
        if removed:
            self.save_jobs(keep)
        return removed

    # ---- jobs-state.json ----

    def _load_all_state(self) -> dict[str, JobState]:
        if not self._state_path.exists():
            return {}
        data = json.loads(self._state_path.read_text(encoding="utf-8"))
        states = data.get("states", {})
        return {k: JobState(**v) for k, v in states.items()}

    def _save_all_state(self, all_state: dict[str, JobState]) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "states": {k: asdict(v) for k, v in all_state.items()},
        }
        _atomic_write_text(
            self._state_path,
            json.dumps(payload, indent=2, sort_keys=True),
        )

    def get_state(self, job_id: str) -> JobState:
        return self._load_all_state().get(job_id, JobState())

    def set_state(self, job_id: str, state: JobState) -> None:
        all_state = self._load_all_state()
        all_state[job_id] = state
        self._save_all_state(all_state)

    def remove_state(self, job_id: str) -> None:
        all_state = self._load_all_state()
        if job_id in all_state:
            del all_state[job_id]
            self._save_all_state(all_state)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_cron_store.py -v
```

Expected: 11 passes.

- [ ] **Step 5: Commit**

```bash
git add src/tend/cron_store.py tests/test_cron_store.py
git commit -m "feat: cron_store — durable JSON job state with atomic writes"
```

---

### Task 3: ProactiveAnnouncer

**Files:**
- Create: `src/tend/announcer.py`
- Create: `tests/test_announcer.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_announcer.py`:

```python
"""Tests for ProactiveAnnouncer — cooldown, deferral, urgency, drain."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat_subagents.bus.messages import BusFrameMessage

from tend.announcer import ProactiveAnnouncer


@pytest.fixture
def bus():
    b = MagicMock()
    b.publish = AsyncMock()
    return b


@pytest.fixture
def is_active():
    state = {"value": False}
    def f():
        return state["value"]
    f.set = lambda v: state.update(value=v)
    return f


def _texts_published(bus) -> list[str]:
    out = []
    for call in bus.publish.call_args_list:
        msg = call.args[0]
        if isinstance(msg.frame, TTSSpeakFrame):
            out.append(msg.frame.text)
    return out


async def test_announce_when_idle_publishes_tts_and_context(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    delivered = await a.announce(
        "Hello.", source="test", category="general", urgent=False,
    )
    assert delivered is True
    # Two publishes: TTSSpeakFrame and LLMMessagesAppendFrame
    assert bus.publish.call_count == 2
    frames = [c.args[0].frame for c in bus.publish.call_args_list]
    assert isinstance(frames[0], TTSSpeakFrame)
    assert frames[0].text == "Hello."
    assert isinstance(frames[1], LLMMessagesAppendFrame)
    msg = frames[1].messages[0]
    assert msg["role"] == "system"
    assert "test/general" in msg["content"]
    assert "Hello." in msg["content"]


async def test_cooldown_drops_repeat_in_same_category(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={"posture": 600},
    )
    await a.announce("Sit up.", source="vision", category="posture")
    bus.publish.reset_mock()
    delivered = await a.announce(
        "Sit up.", source="vision", category="posture",
    )
    assert delivered is False
    assert bus.publish.call_count == 0


async def test_different_categories_independent(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    await a.announce("a", source="x", category="alpha")
    await a.announce("b", source="x", category="beta")
    assert _texts_published(bus) == ["a", "b"]


async def test_urgent_bypasses_cooldown(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    await a.announce("first", source="x", category="alarm")
    bus.publish.reset_mock()
    await a.announce(
        "second", source="x", category="alarm", urgent=True,
    )
    assert "second" in _texts_published(bus)


async def test_defer_when_brain_active(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    is_active.set(True)
    delivered = await a.announce(
        "wait", source="x", category="alpha", urgent=False,
    )
    assert delivered is True  # queued counts as delivered
    assert bus.publish.call_count == 0


async def test_drain_publishes_pending(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    is_active.set(True)
    await a.announce("first", source="x", category="alpha")
    await a.announce("second", source="x", category="beta")
    bus.publish.reset_mock()

    is_active.set(False)
    await a.drain_pending()
    texts = _texts_published(bus)
    assert "first" in texts
    assert "second" in texts


async def test_urgent_speaks_immediately_even_if_brain_active(bus, is_active):
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    is_active.set(True)
    await a.announce(
        "URGENT", source="alarm", category="fire", urgent=True,
    )
    assert "URGENT" in _texts_published(bus)


async def test_drain_respects_cooldown(bus, is_active):
    """If a queued announcement's category went on cooldown via urgent
    delivery while pending, drain still respects that."""
    a = ProactiveAnnouncer(
        bus=bus, is_brain_active=is_active,
        default_cooldown_s=300, category_cooldowns={},
    )
    is_active.set(True)
    # Queue one normal announcement
    await a.announce("queued", source="x", category="alpha")
    # Urgent of same category fires immediately (sets cooldown)
    await a.announce("urgent", source="x", category="alpha", urgent=True)
    bus.publish.reset_mock()

    is_active.set(False)
    await a.drain_pending()
    # The queued one should be dropped because cooldown is now active.
    assert "queued" not in _texts_published(bus)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_announcer.py -v
```

Expected: every test fails with `ModuleNotFoundError: tend.announcer`.

- [ ] **Step 3: Implement the announcer**

Create `src/tend/announcer.py`:

```python
"""ProactiveAnnouncer — single entry point for proactive TTS announcements.

Used by Scheduler, webhook /say, and GeneralWorker. Enforces:
- Per-category cooldown (drops repeat announcements within the window).
- Active-Brain deferral (non-urgent calls queue while Brain.active=True).
- Urgency override (urgent calls bypass both).
- LLMContext logging (every published announcement also lands in Hub's
  context as a system message so Brain knows what was said).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat_subagents.bus import AgentBus
from pipecat_subagents.bus.messages import BusFrameMessage


@dataclass(frozen=True)
class _Pending:
    text: str
    source: str
    category: str


class ProactiveAnnouncer:
    def __init__(
        self,
        *,
        bus: AgentBus,
        is_brain_active: Callable[[], bool],
        default_cooldown_s: int,
        category_cooldowns: dict[str, int],
        agent_name: str = "announcer",
    ):
        self._bus = bus
        self._is_brain_active = is_brain_active
        self._default = float(default_cooldown_s)
        self._per_category = {k: float(v) for k, v in category_cooldowns.items()}
        self._last_fired: dict[str, float] = {}
        self._pending: deque[_Pending] = deque()
        self._name = agent_name

    def _cooldown_for(self, category: str) -> float:
        return self._per_category.get(category, self._default)

    def _on_cooldown(self, category: str) -> bool:
        last = self._last_fired.get(category)
        if last is None:
            return False
        return (time.monotonic() - last) < self._cooldown_for(category)

    async def announce(
        self, text: str, *, source: str, category: str, urgent: bool = False,
    ) -> bool:
        """Speak `text` (subject to cooldown / deferral). Returns True if
        delivered or queued; False if dropped on cooldown."""
        if not urgent and self._on_cooldown(category):
            logger.debug(
                f"announce dropped (cooldown {category}): {text!r}"
            )
            return False
        if not urgent and self._is_brain_active():
            self._pending.append(_Pending(text, source, category))
            logger.info(
                f"announce queued (brain active, {category}): {text!r}"
            )
            return True
        await self._publish(text, source, category)
        self._last_fired[category] = time.monotonic()
        return True

    async def drain_pending(self) -> None:
        """Called when Brain transitions to inactive. Publishes queued
        announcements still inside their cooldown window."""
        while self._pending:
            p = self._pending.popleft()
            if self._on_cooldown(p.category):
                logger.debug(
                    f"drain dropped (cooldown {p.category}): {p.text!r}"
                )
                continue
            await self._publish(p.text, p.source, p.category)
            self._last_fired[p.category] = time.monotonic()

    async def _publish(self, text: str, source: str, category: str) -> None:
        await self._bus.publish(BusFrameMessage(
            source=self._name,
            frame=TTSSpeakFrame(text),
            direction=FrameDirection.DOWNSTREAM,
        ))
        await self._bus.publish(BusFrameMessage(
            source=self._name,
            frame=LLMMessagesAppendFrame(messages=[{
                "role": "system",
                "content": (
                    f"You announced (from {source}/{category}): {text!r}"
                ),
            }]),
            direction=FrameDirection.DOWNSTREAM,
        ))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_announcer.py -v
```

Expected: 8 passes.

- [ ] **Step 5: Commit**

```bash
git add src/tend/announcer.py tests/test_announcer.py
git commit -m "feat: ProactiveAnnouncer (cooldown, deferral, urgency, drain)"
```

---

### Task 4: Scheduler BaseAgent

**Files:**
- Create: `src/tend/scheduler.py`
- Create: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scheduler.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_scheduler.py -v
```

Expected: every test fails with `ModuleNotFoundError: tend.scheduler`.

- [ ] **Step 3: Implement the scheduler**

Create `src/tend/scheduler.py`:

```python
"""Scheduler — proactive job dispatch agent.

Owns wall-clock time. Reads cron job definitions and runtime state from
CronStore on init. Runs an asyncio loop that sleeps until the next
fire time, dispatches the job, recomputes next-fire, and repeats. One-
shot `at` jobs auto-delete after fire (regardless of outcome).

Public API:
- add_job(when, request, name, source, ...) -> CronJob
- cancel_job(name_or_id) -> bool
- list_jobs() -> list[CronJob]
- find_by_source(source) -> list[CronJob]
- fire_missed_now() — call once at boot for at-jobs whose target time
  has already passed (subject to missed_at_policy).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from loguru import logger
from pipecat_subagents.agents import BaseAgent
from pipecat_subagents.bus import AgentBus

from tend.cron_store import CronJob, CronStore, JobState
from tend.cron_time import next_fire_at, parse_when


MissedPolicy = Literal["run-on-restart", "skip"]


class Scheduler(BaseAgent):
    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        store: CronStore,
        dispatch: Callable[[str, dict], Awaitable[None]],
        default_tz: str = "UTC",
        missed_at_policy: MissedPolicy = "run-on-restart",
    ):
        super().__init__(name, bus=bus)
        self._store = store
        self._dispatch = dispatch
        self._default_tz = default_tz
        self._missed_policy = missed_at_policy
        self._loop_task: asyncio.Task | None = None
        self._wakeup = asyncio.Event()

    # ---- public API ----

    def add_job(
        self,
        *,
        when: str,
        request: str,
        name: str,
        source: str,
        tz: str | None = None,
        payload_extras: dict | None = None,
    ) -> CronJob:
        kind, schedule = parse_when(when)
        effective_tz = tz or self._default_tz
        payload: dict = {"request": request}
        if payload_extras:
            payload.update(payload_extras)
        job = self._store.add_job(
            name=name, kind=kind, schedule=schedule, tz=effective_tz,
            payload=payload, source=source, enabled=True,
        )
        nfa = next_fire_at(
            kind, schedule, effective_tz, dt.datetime.now(tz=ZoneInfo("UTC")),
        )
        st = self._store.get_state(job.id)
        self._store.set_state(
            job.id,
            JobState(
                last_run_at=st.last_run_at,
                last_run_status=st.last_run_status,
                last_error=st.last_error,
                next_run_at=nfa.isoformat(),
                consecutive_errors=st.consecutive_errors,
            ),
        )
        self._wakeup.set()
        return job

    def cancel_job(self, name_or_id: str) -> bool:
        jobs = self._store.load_jobs()
        match = next(
            (j for j in jobs
             if j.name == name_or_id or j.id.startswith(name_or_id)),
            None,
        )
        if match is None:
            return False
        ok = self._store.remove_job(match.id)
        if ok:
            self._store.remove_state(match.id)
            self._wakeup.set()
        return ok

    def list_jobs(self) -> list[CronJob]:
        return self._store.load_jobs()

    def find_by_source(self, source: str) -> list[CronJob]:
        return self._store.find_by_source(source)

    # ---- lifecycle ----

    async def on_ready(self) -> None:
        await super().on_ready()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def on_stopped(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        await super().on_stopped()

    async def fire_missed_now(self) -> None:
        """Run once at startup. For each enabled `at` job whose target
        time is already past, either fire-and-delete or skip-and-delete
        based on missed_at_policy. Recurring jobs simply have their
        next_run_at recomputed; no catch-up firings."""
        now = dt.datetime.now(tz=ZoneInfo("UTC"))
        for job in self._store.load_jobs():
            if not job.enabled:
                continue
            if job.kind != "at":
                # Refresh next_run_at for recurring jobs.
                nfa = next_fire_at(job.kind, job.schedule, job.tz or self._default_tz, now)
                st = self._store.get_state(job.id)
                self._store.set_state(
                    job.id,
                    JobState(
                        last_run_at=st.last_run_at,
                        last_run_status=st.last_run_status,
                        last_error=st.last_error,
                        next_run_at=nfa.isoformat(),
                        consecutive_errors=st.consecutive_errors,
                    ),
                )
                continue
            target = dt.datetime.fromisoformat(job.schedule)
            if target > now:
                continue
            if self._missed_policy == "run-on-restart":
                logger.info(f"firing missed at-job {job.name!r}")
                await self._fire_job(job)
            else:
                logger.info(f"skipping missed at-job {job.name!r}")
                self._store.remove_job(job.id)
                self._store.remove_state(job.id)

    # ---- internals ----

    async def _run_loop(self) -> None:
        while True:
            now = dt.datetime.now(tz=ZoneInfo("UTC"))
            jobs = [j for j in self._store.load_jobs() if j.enabled]
            soonest = self._next_due_after(jobs, now)
            if soonest is None:
                # No jobs; sleep up to a minute then re-check.
                wait_s = 60.0
            else:
                job, when = soonest
                wait_s = max(0.0, (when - now).total_seconds())
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=wait_s)
                # Wakeup signal — re-evaluate.
                self._wakeup.clear()
                continue
            except asyncio.TimeoutError:
                pass
            # Timed out — soonest is due.
            if soonest is None:
                continue
            job, _ = soonest
            await self._fire_job(job)

    def _next_due_after(
        self, jobs: list[CronJob], now: dt.datetime,
    ) -> tuple[CronJob, dt.datetime] | None:
        candidates: list[tuple[dt.datetime, CronJob]] = []
        for j in jobs:
            st = self._store.get_state(j.id)
            if st.next_run_at:
                candidates.append((dt.datetime.fromisoformat(st.next_run_at), j))
            else:
                nfa = next_fire_at(
                    j.kind, j.schedule, j.tz or self._default_tz, now,
                )
                candidates.append((nfa, j))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0])
        when, job = candidates[0]
        return job, when

    async def _fire_job(self, job: CronJob) -> None:
        st = self._store.get_state(job.id)
        try:
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

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_scheduler.py -v
```

Expected: 12 passes.

- [ ] **Step 5: Commit**

```bash
git add src/tend/scheduler.py tests/test_scheduler.py
git commit -m "feat: Scheduler BaseAgent — load, dispatch loop, missed-fire policy"
```

---

### Task 5: Skills frontmatter — triggers / events / silent_default

**Files:**
- Modify: `src/tend/skills.py:48-89` (parse_frontmatter)
- Modify: `src/tend/skills.py:99-127` (enumerate_skills) — return new fields
- Create: `tests/test_skills_triggers.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_skills_triggers.py`:

```python
"""Coverage for triggers + events + silent_default frontmatter."""

from __future__ import annotations

from pathlib import Path

import pytest

from tend.skills import (
    SkillFrontmatterError,
    enumerate_skills,
    find_event_subscribers,
    parse_frontmatter,
)


def _seed(root: Path, name: str, body: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(body, encoding="utf-8")


def test_parse_frontmatter_with_triggers():
    text = (
        "---\n"
        "name: meal-plan\n"
        "description: noon meal plan\n"
        "triggers:\n"
        "  - cron: \"0 12 * * *\"\n"
        "    tz: America/Los_Angeles\n"
        "    request: make the meal plan\n"
        "---\n"
        "body\n"
    )
    fm = parse_frontmatter(text)
    assert fm.name == "meal-plan"
    assert fm.description == "noon meal plan"
    assert len(fm.triggers) == 1
    t = fm.triggers[0]
    assert t["cron"] == "0 12 * * *"
    assert t["tz"] == "America/Los_Angeles"
    assert t["request"] == "make the meal plan"


def test_parse_frontmatter_with_events_and_silent_default():
    text = (
        "---\n"
        "name: heartbeat\n"
        "description: silent check-in\n"
        "silent_default: true\n"
        "events:\n"
        "  - posture.slumped\n"
        "  - hydration.lapse\n"
        "---\n"
        "body\n"
    )
    fm = parse_frontmatter(text)
    assert fm.silent_default is True
    assert fm.events == ("posture.slumped", "hydration.lapse")


def test_parse_frontmatter_defaults():
    text = (
        "---\n"
        "name: simple\n"
        "description: a skill with nothing fancy\n"
        "---\n"
        "body\n"
    )
    fm = parse_frontmatter(text)
    assert fm.triggers == ()
    assert fm.events == ()
    assert fm.silent_default is False


def test_enumerate_includes_triggers_events(tmp_path):
    _seed(tmp_path, "meal-plan",
          "---\n"
          "name: meal-plan\n"
          "description: x\n"
          "triggers:\n"
          "  - cron: \"0 12 * * *\"\n"
          "    request: lunch\n"
          "events:\n"
          "  - hunger.detected\n"
          "---\n"
          "body\n")
    skills = enumerate_skills(tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert len(s.triggers) == 1
    assert s.events == ("hunger.detected",)


def test_find_event_subscribers(tmp_path):
    _seed(tmp_path, "a",
          "---\nname: a\ndescription: x\nevents:\n  - foo\n---\nbody\n")
    _seed(tmp_path, "b",
          "---\nname: b\ndescription: y\nevents:\n  - bar\n  - foo\n---\nbody\n")
    _seed(tmp_path, "c",
          "---\nname: c\ndescription: z\n---\nbody\n")
    matches = find_event_subscribers(tmp_path, "foo")
    names = sorted(m.name for m in matches)
    assert names == ["a", "b"]


def test_malformed_triggers_logs_and_skips(tmp_path):
    """A malformed `triggers:` block doesn't break the rest of the catalog."""
    _seed(tmp_path, "good",
          "---\nname: good\ndescription: ok\n---\nbody\n")
    _seed(tmp_path, "bad",
          "---\nname: bad\ndescription: ok\n"
          "triggers:\n"
          "  - this is not a mapping\n"
          "---\nbody\n")
    skills = enumerate_skills(tmp_path)
    names = sorted(s.name for s in skills)
    # The 'bad' skill is still enumerable; its triggers just come back empty.
    assert names == ["bad", "good"]
    bad = next(s for s in skills if s.name == "bad")
    assert bad.triggers == ()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_skills_triggers.py -v
```

Expected: every test fails (`triggers`/`events`/`silent_default` attrs don't exist; `find_event_subscribers` not defined).

- [ ] **Step 3: Extend skills.py**

Update `src/tend/skills.py`. Replace the `SkillFrontmatter`, `parse_frontmatter`, `SkillInfo`, and `enumerate_skills` definitions, and append `find_event_subscribers`. Keep all existing exports working (back-compat for tests/test_skills.py).

```python
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape as _xml_escape

from loguru import logger


class SkillFrontmatterError(ValueError):
    """The SKILL.md frontmatter block is missing or malformed."""


@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str
    triggers: tuple[dict, ...] = ()
    events: tuple[str, ...] = ()
    silent_default: bool = False


_FENCE = "---"


_QUOT_MAP = {'"': "&quot;"}


def _esc(s: str) -> str:
    return _xml_escape(s, _QUOT_MAP)


def _parse_yaml_frontmatter_block(text: str) -> dict:
    """Minimal yaml-ish parser for our frontmatter dialect.

    Supports:
      key: value
      key:
        - scalar
        - scalar
      key:
        - nested_key: nested_value
          nested_key2: nested_value2
    Quotes are stripped on scalars. Anything that doesn't fit the shape
    is silently dropped from the parsed result so a malformed block
    doesn't kill the rest of the catalog.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise SkillFrontmatterError("no frontmatter fence at start of file")
    closed = False
    body_lines: list[str] = []
    for raw in lines[1:]:
        if raw.strip() == _FENCE:
            closed = True
            break
        body_lines.append(raw)
    if not closed:
        raise SkillFrontmatterError("unterminated frontmatter (no closing ---)")

    out: dict = {}
    i = 0
    while i < len(body_lines):
        line = body_lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if line.startswith(" "):
            # stray indented line at top level — skip
            i += 1
            continue
        if ":" not in stripped:
            raise SkillFrontmatterError(f"unparseable frontmatter line: {line!r}")
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if value:
            # Scalar
            out[key] = _strip_quotes(value)
            i += 1
            continue
        # No inline value → consume an indented block
        items: list = []
        i += 1
        cur_map: dict | None = None
        while i < len(body_lines):
            l2 = body_lines[i]
            if not l2.strip():
                i += 1
                continue
            if not (l2.startswith("  ") or l2.startswith("\t")):
                break
            stripped2 = l2.strip()
            if stripped2.startswith("- "):
                # New list item
                if cur_map is not None:
                    items.append(cur_map)
                first_kv = stripped2[2:].strip()
                if ":" in first_kv:
                    k, _, v = first_kv.partition(":")
                    cur_map = {k.strip(): _strip_quotes(v.strip())}
                else:
                    items.append(_strip_quotes(first_kv))
                    cur_map = None
                i += 1
                continue
            # Continuation of current map
            if cur_map is not None and ":" in stripped2:
                k, _, v = stripped2.partition(":")
                cur_map[k.strip()] = _strip_quotes(v.strip())
                i += 1
                continue
            i += 1  # skip lines we don't understand
        if cur_map is not None:
            items.append(cur_map)
        out[key] = items
    return out


def _strip_quotes(s: str) -> str:
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1]
    return s


def parse_frontmatter(text: str) -> SkillFrontmatter:
    fields = _parse_yaml_frontmatter_block(text)
    name = fields.get("name", "")
    description = fields.get("description", "")
    if not name:
        raise SkillFrontmatterError("frontmatter missing required key: name")
    if not description:
        raise SkillFrontmatterError(
            "frontmatter missing required key: description"
        )

    triggers_raw = fields.get("triggers") or []
    triggers: list[dict] = []
    if isinstance(triggers_raw, list):
        for item in triggers_raw:
            if isinstance(item, dict):
                triggers.append(item)
            # skip non-dict entries silently

    events_raw = fields.get("events") or []
    events: list[str] = []
    if isinstance(events_raw, list):
        for item in events_raw:
            if isinstance(item, str):
                events.append(item)

    silent = str(fields.get("silent_default", "")).strip().lower() == "true"

    return SkillFrontmatter(
        name=str(name),
        description=str(description),
        triggers=tuple(triggers),
        events=tuple(events),
        silent_default=silent,
    )


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path
    triggers: tuple[dict, ...] = ()
    events: tuple[str, ...] = ()
    silent_default: bool = False


def enumerate_skills(root: Path) -> list[SkillInfo]:
    if not root.exists():
        return []
    out: list[SkillInfo] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text()
            fm = parse_frontmatter(text)
        except (OSError, SkillFrontmatterError) as e:
            logger.warning(f"skipping malformed skill at {skill_md}: {e}")
            continue
        out.append(SkillInfo(
            name=fm.name,
            description=fm.description,
            path=skill_md.resolve(),
            triggers=fm.triggers,
            events=fm.events,
            silent_default=fm.silent_default,
        ))
    out.sort(key=lambda s: s.name)
    return out


def find_event_subscribers(root: Path, kind: str) -> list[SkillInfo]:
    """Return all skills under <root> whose `events:` lists `kind`."""
    return [s for s in enumerate_skills(root) if kind in s.events]


# format_catalog_xml, ScanFinding, ScanReport, scan_text,
# atomic_write_text, quarantine_skill — keep existing definitions below
# unchanged.
```

Keep the existing `format_catalog_xml`, `ScanFinding`, `ScanReport`, `_RULES`, `scan_text`, `atomic_write_text`, and `quarantine_skill` definitions intact at the bottom of the file — they continue to work as-is.

- [ ] **Step 4: Run tests to verify both old + new pass**

```bash
pytest tests/test_skills.py tests/test_skills_triggers.py -v
```

Expected: all existing test_skills tests still pass, plus the 6 new ones.

- [ ] **Step 5: Commit**

```bash
git add src/tend/skills.py tests/test_skills_triggers.py
git commit -m "feat: SKILL.md frontmatter learns triggers/events/silent_default"
```

---

### Task 6: Webhook receiver

**Files:**
- Create: `src/tend/webhook.py`
- Create: `tests/test_webhook.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_webhook.py`:

```python
"""Tests for the aiohttp webhook receiver."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tend.webhook import build_app


def _make_app(*, announcer, dispatch, skills_root: Path, token: str = "TKN"):
    return build_app(
        token=token,
        announcer=announcer,
        dispatch=dispatch,
        skills_root=skills_root,
    )


@pytest.fixture
async def client(tmp_path):
    announcer = MagicMock()
    announcer.announce = AsyncMock(return_value=True)
    dispatch = AsyncMock()
    app = _make_app(
        announcer=announcer, dispatch=dispatch, skills_root=tmp_path,
    )
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


async def test_event_dispatches_per_subscriber(tmp_path):
    # Seed two subscribing skills
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: x\n"
            f"events:\n  - posture.slumped\n---\nbody\n",
            encoding="utf-8",
        )
    announcer = MagicMock()
    announcer.announce = AsyncMock(return_value=True)
    dispatch = AsyncMock()
    app = _make_app(
        announcer=announcer, dispatch=dispatch, skills_root=tmp_path,
    )
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_webhook.py -v
```

Expected: every test fails with `ModuleNotFoundError: tend.webhook`.

- [ ] **Step 3: Implement the receiver**

Create `src/tend/webhook.py`:

```python
"""HTTP receiver for external producers.

Exposes:
  POST /say   — direct TTS (no LLM round-trip); body: {text, category?, urgent?}
  POST /event — structured event for skill mediation; body: {kind, ...}

Authenticated via `Authorization: Bearer <token>`. Bound to loopback only;
the caller is responsible for keeping the network surface narrow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web
from loguru import logger

from tend.skills import find_event_subscribers


def build_app(
    *,
    token: str,
    announcer,
    dispatch: Callable[[str, dict], Awaitable[None]],
    skills_root: Path,
) -> web.Application:
    """Construct the aiohttp Application. Pure construction; the caller
    owns lifecycle (start/stop)."""

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if header != expected:
            return web.json_response(
                {"error": "unauthorized"}, status=401,
            )
        return await handler(request)

    async def handle_say(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid-json"}, status=400)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return web.json_response(
                {"error": "missing 'text' field"}, status=400,
            )
        category = str(body.get("category", "general"))
        urgent = bool(body.get("urgent", False))
        delivered = await announcer.announce(
            text=text, source="webhook", category=category, urgent=urgent,
        )
        return web.json_response({"delivered": bool(delivered)})

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
        try:
            matches = find_event_subscribers(skills_root, kind)
        except Exception:
            logger.exception("event dispatch failed listing subscribers")
            matches = []
        for skill in matches:
            await dispatch("general", {
                "request": f"Handle event {kind}",
                "skill": skill.name,
                "event": body,
            })
        return web.json_response(
            {"dispatched": [s.name for s in matches]},
        )

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_post("/say", handle_say)
    app.router.add_post("/event", handle_event)
    return app


class WebhookServer:
    """Lifecycle wrapper. main.py constructs one and calls start/stop."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        app: web.Application,
    ):
        self._host = host
        self._port = port
        self._app = app
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info(f"webhook server listening on {self._host}:{self._port}")

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_webhook.py -v
```

Expected: 7 passes.

- [ ] **Step 5: Commit**

```bash
git add src/tend/webhook.py tests/test_webhook.py
git commit -m "feat: webhook receiver — /say and /event with token auth"
```

---

### Task 7: Brain — schedule / list_schedules / cancel_schedule tools

**Files:**
- Modify: `src/tend/brain.py:32-44` (constructor) — accept Scheduler reference
- Modify: `src/tend/brain.py` — add 3 new tool methods
- Create: `tests/test_brain_schedule_tools.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_brain_schedule_tools.py`:

```python
"""Tests for Brain's scheduling tools (schedule / list / cancel)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tend.brain import Brain


def _params():
    p = MagicMock()
    p.result_callback = AsyncMock()
    return p


@pytest.fixture
def scheduler():
    s = MagicMock()
    s.add_job = MagicMock()
    s.cancel_job = MagicMock(return_value=True)
    s.list_jobs = MagicMock(return_value=[])
    return s


@pytest.fixture
def brain(scheduler):
    bus = MagicMock()
    return Brain(
        "brain", bus=bus, llm_service=MagicMock(),
        store=MagicMock(), scheduler=scheduler,
    )


async def test_schedule_calls_scheduler(brain, scheduler):
    p = _params()
    job = MagicMock(id="abcdef0123", name="lunch")
    scheduler.add_job.return_value = job
    await brain.schedule(p, when="0 12 * * *", request="lunch plan",
                         name="lunch")
    scheduler.add_job.assert_called_once()
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["when"] == "0 12 * * *"
    assert kwargs["request"] == "lunch plan"
    assert kwargs["name"] == "lunch"
    assert kwargs["source"] == "voice"
    p.result_callback.assert_awaited_once()
    msg = p.result_callback.call_args.args[0]
    assert "lunch" in msg


async def test_schedule_invalid_when_returns_error_message(brain, scheduler):
    from tend.cron_time import InvalidWhen
    scheduler.add_job.side_effect = InvalidWhen("bad")
    p = _params()
    await brain.schedule(p, when="nonsense", request="x")
    msg = p.result_callback.call_args.args[0]
    assert "couldn't parse" in msg.lower() or "didn't" in msg.lower()


async def test_list_schedules_empty(brain, scheduler):
    scheduler.list_jobs.return_value = []
    p = _params()
    await brain.list_schedules(p)
    msg = p.result_callback.call_args.args[0]
    assert "no" in msg.lower() and ("schedule" in msg.lower())


async def test_list_schedules_summarises(brain, scheduler):
    scheduler.list_jobs.return_value = [
        MagicMock(name="lunch", kind="cron", schedule="0 12 * * *",
                  source="voice"),
        MagicMock(name="ping", kind="every", schedule="30m",
                  source="cli"),
    ]
    p = _params()
    await brain.list_schedules(p)
    msg = p.result_callback.call_args.args[0]
    assert "lunch" in msg
    assert "ping" in msg


async def test_cancel_schedule_known(brain, scheduler):
    scheduler.cancel_job.return_value = True
    p = _params()
    await brain.cancel_schedule(p, name_or_id="lunch")
    scheduler.cancel_job.assert_called_once_with("lunch")
    msg = p.result_callback.call_args.args[0]
    assert "cancelled" in msg.lower() or "removed" in msg.lower()


async def test_cancel_schedule_unknown(brain, scheduler):
    scheduler.cancel_job.return_value = False
    p = _params()
    await brain.cancel_schedule(p, name_or_id="nope")
    msg = p.result_callback.call_args.args[0]
    assert "find" in msg.lower() or "no" in msg.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_brain_schedule_tools.py -v
```

Expected: all fail (Brain has no `scheduler` kwarg, no `schedule`/`list_schedules`/`cancel_schedule` tools).

- [ ] **Step 3: Update Brain**

Edit `src/tend/brain.py`. Change the constructor to accept `scheduler`, and add the 3 tools. Keep the existing tool methods intact:

```python
def __init__(
    self,
    name: str,
    *,
    bus: AgentBus,
    llm_service: LLMService | None,
    session_manager=None,
    store: SessionStore | None = None,
    scheduler=None,
):
    super().__init__(name, bus=bus, bridged=())
    self._llm_service = llm_service
    self._session_manager = session_manager
    self._store = store
    self._scheduler = scheduler
```

Add (anywhere among the existing `@tool` methods):

```python
@tool
async def schedule(
    self,
    params: FunctionCallParams,
    when: str,
    request: str,
    name: str | None = None,
):
    """Schedule a recurring or one-shot job. The worker fires when the
    schedule matches and announces the result.

    Args:
        when: cron expression like '0 12 * * *', a relative offset like
              'in 3 hours', an ISO timestamp, or 'every 30m'.
        request: what should happen, in plain English.
        name: optional human label for cancel/list later.
    """
    if self._scheduler is None:
        await params.result_callback("Scheduler not configured.")
        return
    from tend.cron_time import InvalidWhen
    try:
        job = self._scheduler.add_job(
            when=when, request=request,
            name=name or f"job-{int(__import__('time').time())}",
            source="voice",
        )
    except InvalidWhen as e:
        await params.result_callback(
            f"I couldn't parse that schedule: {e}. Try '0 12 * * *' "
            f"for cron, 'in 30 minutes' for one-shot, or 'every 30m'."
        )
        return
    await params.result_callback(
        f"Scheduled '{job.name}'."
    )

@tool
async def list_schedules(self, params: FunctionCallParams):
    """List currently active schedules and what they'll do."""
    if self._scheduler is None:
        await params.result_callback("Scheduler not configured.")
        return
    jobs = self._scheduler.list_jobs()
    if not jobs:
        await params.result_callback("You have no active schedules.")
        return
    lines = []
    for j in jobs:
        kind = j.kind
        sched = j.schedule
        lines.append(f"{j.name}: {kind} {sched} ({j.source})")
    await params.result_callback("\n".join(lines))

@tool
async def cancel_schedule(
    self, params: FunctionCallParams, name_or_id: str,
):
    """Cancel a scheduled job by name or id prefix."""
    if self._scheduler is None:
        await params.result_callback("Scheduler not configured.")
        return
    ok = self._scheduler.cancel_job(name_or_id)
    if ok:
        await params.result_callback(f"Cancelled '{name_or_id}'.")
    else:
        await params.result_callback(
            f"Couldn't find a schedule matching '{name_or_id}'."
        )
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_brain_schedule_tools.py tests/test_brain.py -v
```

Expected: new tests pass; existing test_brain.py tests still pass (the new `scheduler` kwarg defaults to None).

- [ ] **Step 5: Commit**

```bash
git add src/tend/brain.py tests/test_brain_schedule_tools.py
git commit -m "feat: Brain.schedule / list_schedules / cancel_schedule tools"
```

---

### Task 8: Brain — enable_skill_triggers / disable_skill_triggers

**Files:**
- Modify: `src/tend/brain.py` — add 2 more tools
- Modify: `tests/test_brain_schedule_tools.py` — add coverage

- [ ] **Step 1: Add failing tests**

Append to `tests/test_brain_schedule_tools.py`:

```python
async def test_enable_skill_triggers_copies_into_scheduler(brain, scheduler, tmp_path, monkeypatch):
    # Seed a skill with two triggers.
    skill_dir = tmp_path / "meal-plan"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meal-plan\n"
        "description: x\n"
        "triggers:\n"
        "  - cron: \"0 12 * * *\"\n"
        "    tz: UTC\n"
        "    request: lunch\n"
        "  - cron: \"0 19 * * *\"\n"
        "    tz: UTC\n"
        "    request: dinner\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEND_SKILLS_ROOT", str(tmp_path))

    # find_by_source returns nothing the first time (no prior copies)
    scheduler.find_by_source = MagicMock(return_value=[])
    scheduler.cancel_job = MagicMock(return_value=True)

    p = _params()
    await brain.enable_skill_triggers(p, skill="meal-plan")
    assert scheduler.add_job.call_count == 2
    sources = {c.kwargs["source"] for c in scheduler.add_job.call_args_list}
    assert sources == {"skill:meal-plan"}


async def test_enable_skill_triggers_idempotent_replaces_existing(brain, scheduler, tmp_path, monkeypatch):
    skill_dir = tmp_path / "x"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: x\ndescription: y\n"
        "triggers:\n  - cron: \"0 12 * * *\"\n    request: hi\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEND_SKILLS_ROOT", str(tmp_path))

    existing = [MagicMock(id="old1"), MagicMock(id="old2")]
    scheduler.find_by_source = MagicMock(return_value=existing)
    scheduler.cancel_job = MagicMock(return_value=True)

    p = _params()
    await brain.enable_skill_triggers(p, skill="x")
    assert scheduler.cancel_job.call_count == 2
    assert scheduler.add_job.call_count == 1


async def test_disable_skill_triggers_removes_only_matching_source(brain, scheduler):
    scheduler.find_by_source = MagicMock(
        return_value=[MagicMock(id="one"), MagicMock(id="two")],
    )
    scheduler.cancel_job = MagicMock(return_value=True)
    p = _params()
    await brain.disable_skill_triggers(p, skill="meal-plan")
    assert scheduler.cancel_job.call_count == 2


async def test_enable_skill_triggers_no_triggers_returns_message(brain, scheduler, tmp_path, monkeypatch):
    skill_dir = tmp_path / "bare"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bare\ndescription: y\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEND_SKILLS_ROOT", str(tmp_path))
    scheduler.find_by_source = MagicMock(return_value=[])
    p = _params()
    await brain.enable_skill_triggers(p, skill="bare")
    msg = p.result_callback.call_args.args[0]
    assert "no triggers" in msg.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_brain_schedule_tools.py::test_enable_skill_triggers_copies_into_scheduler -v
```

Expected: AttributeError (tool not defined).

- [ ] **Step 3: Add the tools**

Append to Brain in `src/tend/brain.py`:

```python
@tool
async def enable_skill_triggers(
    self, params: FunctionCallParams, skill: str,
):
    """Activate the schedule triggers declared in a skill's frontmatter.

    Idempotent: re-running replaces any previously copied triggers for
    this skill.
    """
    if self._scheduler is None:
        await params.result_callback("Scheduler not configured.")
        return
    import os as _os
    from pathlib import Path as _Path

    from tend.skills import enumerate_skills

    root = _Path(
        _os.environ.get("TEND_SKILLS_ROOT")
        or _Path.home() / ".tend" / "skills"
    )
    matching = [s for s in enumerate_skills(root) if s.name == skill]
    if not matching:
        await params.result_callback(f"No skill named '{skill}'.")
        return
    info = matching[0]
    if not info.triggers:
        await params.result_callback(
            f"'{skill}' has no triggers declared in its frontmatter."
        )
        return
    # Wipe any prior copies of this source first.
    source = f"skill:{skill}"
    for old in self._scheduler.find_by_source(source):
        self._scheduler.cancel_job(old.id)
    added = 0
    for t in info.triggers:
        when = t.get("cron") or t.get("every") or t.get("at")
        if not when:
            continue
        request = t.get("request") or f"Run {skill}."
        self._scheduler.add_job(
            when=when, request=request,
            name=f"{skill}-{added+1}",
            source=source,
            tz=t.get("tz"),
            payload_extras={"skill": skill},
        )
        added += 1
    await params.result_callback(
        f"Enabled {added} trigger(s) for '{skill}'."
    )

@tool
async def disable_skill_triggers(
    self, params: FunctionCallParams, skill: str,
):
    """Remove all schedule triggers previously enabled from a skill."""
    if self._scheduler is None:
        await params.result_callback("Scheduler not configured.")
        return
    source = f"skill:{skill}"
    rows = self._scheduler.find_by_source(source)
    for r in rows:
        self._scheduler.cancel_job(r.id)
    await params.result_callback(
        f"Disabled {len(rows)} trigger(s) for '{skill}'."
    )
```

`scheduler.add_job` here calls into the new method signature (with `tz=` and `payload_extras=`); the implementation in Task 4 already accepts both.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_brain_schedule_tools.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/tend/brain.py tests/test_brain_schedule_tools.py
git commit -m "feat: Brain.enable_skill_triggers / disable_skill_triggers"
```

---

### Task 9: Brain — rewrite remind_in, drop ReminderWorker construction

**Files:**
- Modify: `src/tend/brain.py:98-106` — drop `_ensure_reminder_worker`
- Modify: `src/tend/brain.py:127-136` — rewrite `remind_in`
- Modify: `tests/test_brain.py` — update remind_in expectations

- [ ] **Step 1: Write the failing test**

Update or add to `tests/test_brain.py`:

```python
async def test_remind_in_uses_scheduler(monkeypatch):
    """remind_in should add a one-shot at-job via scheduler.add_job."""
    from tend.brain import Brain
    from unittest.mock import AsyncMock, MagicMock
    bus = MagicMock()
    scheduler = MagicMock()
    scheduler.add_job = MagicMock(
        return_value=MagicMock(id="x", name="reminder"),
    )
    brain = Brain(
        "brain", bus=bus, llm_service=MagicMock(),
        store=MagicMock(), scheduler=scheduler,
    )
    p = MagicMock()
    p.result_callback = AsyncMock()
    await brain.remind_in(p, seconds=120, what="oven")
    scheduler.add_job.assert_called_once()
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["when"].lower().startswith("in ")
    assert "120s" in kwargs["when"]
    assert kwargs["request"] == "oven"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_brain.py::test_remind_in_uses_scheduler -v
```

Expected: fail (current `remind_in` calls `request_task("reminder", ...)`, not the scheduler).

- [ ] **Step 3: Update Brain**

Edit `src/tend/brain.py`:

1. Remove the entire `_ensure_reminder_worker` method (lines ~98–106).
2. Replace the `remind_in` tool body:

```python
@tool
async def remind_in(self, params: FunctionCallParams, seconds: int, what: str):
    """Ask the assistant to remind you about something after a delay.

    Args:
        seconds (int): How long to wait (in seconds) before the reminder fires.
        what (str): The thing to remind about. Plain text, will be spoken aloud.
    """
    if self._scheduler is None:
        await params.result_callback("Scheduler not configured.")
        return
    self._scheduler.add_job(
        when=f"in {int(seconds)}s",
        request=what,
        name=f"reminder-{int(__import__('time').time())}",
        source="voice",
    )
    await params.result_callback(
        f"Got it. I'll remind you in {int(seconds)} seconds."
    )
```

3. Remove the `from tend.workers.reminder import ReminderWorker` import if any other places reference it; replace with nothing.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_brain.py tests/test_brain_schedule_tools.py -v
```

Expected: pass. (Existing tests for `remind_in` that asserted `request_task("reminder", ...)` need updating — replace those expectations with the scheduler.add_job assertion above.)

- [ ] **Step 5: Commit**

```bash
git add src/tend/brain.py tests/test_brain.py
git commit -m "refactor: remind_in now wraps scheduler; drop ReminderWorker construction"
```

---

### Task 10: GeneralWorker — silent_default / event / skill payload + announcer integration

**Files:**
- Modify: `src/tend/workers/general.py:101-115` (constructor)
- Modify: `src/tend/workers/general.py:125-184` (`do_task`, `_announce`, preamble)
- Modify: `tests/workers/test_general.py` — add coverage

- [ ] **Step 1: Add failing tests**

Append to `tests/workers/test_general.py`:

```python
async def test_silent_default_skips_announcement_when_summary_marks_silent(tmp_path, monkeypatch):
    """If silent_default=True and the spoken summary is the conventional
    silent marker, the worker should NOT publish a TTSSpeakFrame."""
    from unittest.mock import AsyncMock, MagicMock
    from tend.config import WorkerConfig
    from tend.workers.general import GeneralWorker

    bus = MagicMock()
    bus.publish = AsyncMock()
    announcer = MagicMock()
    announcer.announce = AsyncMock(return_value=True)
    store = MagicMock()
    cfg = WorkerConfig()

    worker = GeneralWorker(
        "general", bus=bus, store=store, config=cfg,
        announcer=announcer,
    )

    msg = MagicMock()
    msg.task_id = "t1"
    msg.payload = {"request": "tick", "silent_default": True}

    async def fake_run_claude(spec):
        entry = MagicMock()
        entry.spoken_summary = "(nothing to surface)"
        entry.session_id = "s"
        entry.cwd = str(tmp_path)
        entry.request = "tick"
        return entry

    worker.run_claude = fake_run_claude
    worker.send_task_update = AsyncMock()
    worker.send_task_response = AsyncMock()
    monkeypatch.setattr(worker, "_post_run_scan", lambda *a, **kw: ([], []))

    await worker.do_task(msg)

    announcer.announce.assert_not_awaited()


async def test_silent_default_announces_when_summary_has_content(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from tend.config import WorkerConfig
    from tend.workers.general import GeneralWorker

    bus = MagicMock()
    bus.publish = AsyncMock()
    announcer = MagicMock()
    announcer.announce = AsyncMock(return_value=True)
    store = MagicMock()
    cfg = WorkerConfig()
    worker = GeneralWorker(
        "general", bus=bus, store=store, config=cfg, announcer=announcer,
    )

    msg = MagicMock()
    msg.task_id = "t1"
    msg.payload = {"request": "tick", "silent_default": True}

    async def fake_run(spec):
        entry = MagicMock()
        entry.spoken_summary = "Your meeting starts in five minutes."
        entry.session_id = "s"
        entry.cwd = str(tmp_path)
        entry.request = "tick"
        return entry

    worker.run_claude = fake_run
    worker.send_task_update = AsyncMock()
    worker.send_task_response = AsyncMock()
    monkeypatch.setattr(worker, "_post_run_scan", lambda *a, **kw: ([], []))

    await worker.do_task(msg)
    announcer.announce.assert_awaited_once()


async def test_normal_request_uses_announcer(tmp_path, monkeypatch):
    """In normal (non-silent) mode the worker still goes through announcer."""
    from unittest.mock import AsyncMock, MagicMock
    from tend.config import WorkerConfig
    from tend.workers.general import GeneralWorker

    bus = MagicMock()
    bus.publish = AsyncMock()
    announcer = MagicMock()
    announcer.announce = AsyncMock(return_value=True)
    store = MagicMock()
    cfg = WorkerConfig()
    worker = GeneralWorker(
        "general", bus=bus, store=store, config=cfg, announcer=announcer,
    )

    msg = MagicMock()
    msg.task_id = "t1"
    msg.payload = {"request": "x"}

    async def fake_run(spec):
        entry = MagicMock()
        entry.spoken_summary = "Done."
        entry.session_id = "s"
        entry.cwd = str(tmp_path)
        entry.request = "x"
        return entry

    worker.run_claude = fake_run
    worker.send_task_update = AsyncMock()
    worker.send_task_response = AsyncMock()
    monkeypatch.setattr(worker, "_post_run_scan", lambda *a, **kw: ([], []))

    await worker.do_task(msg)
    announcer.announce.assert_awaited_once()
```

- [ ] **Step 2: Run to verify fails**

```bash
pytest tests/workers/test_general.py::test_silent_default_skips_announcement_when_summary_marks_silent -v
```

Expected: fail (`GeneralWorker.__init__` rejects `announcer`).

- [ ] **Step 3: Update GeneralWorker**

Edit `src/tend/workers/general.py`. Constructor change:

```python
def __init__(
    self,
    name: str,
    *,
    bus: AgentBus,
    store: SessionStore,
    config: WorkerConfig,
    subprocess_factory=None,
    workspace_dir: Path | None = None,
    skills_dir: Path | None = None,
    announcer=None,
):
    super().__init__(
        name, bus=bus, store=store, subprocess_factory=subprocess_factory,
    )
    self._config = config
    self._workspace_dir = workspace_dir or _resolve_workspace(config)
    self._workspace_skills_dir = skills_dir or _resolve_skills_dir(config)
    self._announcer = announcer
```

Update the preamble to include silent-mode + event hints:

```python
_GENERAL_PREAMBLE = """\
You are tend's general-purpose worker. The user is a desk worker who talks
to tend through a smart speaker; you are their hands. You run inside a
persistent workspace at ~/.tend/workspace/ where everything you build
accumulates.

Two paths for any incoming request:

1. SKILL MATCH. If <available-skills> below contains a skill matching the
   request, Read its SKILL.md and follow the procedure exactly. Skills
   typically tell you which scripts in ~/.tend/workspace/bin/ to invoke.

2. NO SKILL MATCH. If the request is a *recurring workflow* (named inputs,
   plausibly repeatable), build a skill for it before executing:
   a. Author any scripts you need under ~/.tend/workspace/bin/.
   b. Author ~/.tend/skills/<name>/SKILL.md describing the workflow.
   c. Run `tend scan-skill <name>` via Bash. Exit 0 = run it. Exit 1 = warn,
      review the warnings then run only if they're acceptable. Exit 2 = move
      the skill to ~/.tend/skills-quarantined/<name>/ and announce a graceful
      fallback to the user instead of running it.
   d. If the scan was clean, execute the skill end-to-end.

   If the request is a *one-shot* (chitchat, "what's 17x19", "tell me a
   joke"), just answer inline; do not author a skill.

If the dispatch payload sets silent_default=true, you are running on the
heartbeat tick. Default to NOT speaking: only produce a non-empty spoken
summary if there is something the user genuinely needs to hear right
now. If nothing is worth surfacing, end your run with the literal phrase
"(nothing to surface)" as your last assistant message.

If the dispatch payload includes an `event` block, the user did not ask
for this directly — a trigger fired (vision daemon, calendar, etc.). Be
brief; the user did not invite a long answer.

Skill naming: hyphen-case lowercase, descriptive (`meal-plan`, not `mp`).
Same-name conflicts: prefer appending or replacing a section over silent
overwrite.

Scripts must be self-contained and idempotent where reasonable. Never write
secrets or tokens into scripts; read from environment variables.

When you finish, your last assistant message becomes the spoken summary.
Keep it short — one or two sentences for TTS. Save the long-form artifact
to ~/.tend/workspace/plans/, ~/.tend/workspace/data/, or wherever the skill
directs.
"""
```

Update `do_task` to read silent_default + skill + event from payload, pass them into the spec only if relevant, and route announcement through the announcer:

```python
@task
async def do_task(self, message) -> None:
    request = str(message.payload["request"])
    resume_id = message.payload.get("resume_session_id")
    silent_default = bool(message.payload.get("silent_default", False))
    pinned_skill = message.payload.get("skill")
    event = message.payload.get("event")

    task_start = time.time() - 1.0
    skills_dir = self._workspace_skills_dir
    try:
        workspace = await asyncio.to_thread(self._ensure_workspace)
        skills = await asyncio.to_thread(enumerate_skills, skills_dir)
        system_prompt = _GENERAL_PREAMBLE
        catalog = format_catalog_xml(skills)
        if catalog:
            system_prompt = system_prompt + "\n" + catalog
        # Inject context blocks for the worker preamble to read.
        if pinned_skill:
            system_prompt += (
                f"\n\n<pinned-skill>{pinned_skill}</pinned-skill>"
            )
        if event:
            system_prompt += (
                f"\n\n<event>{event}</event>"
            )
        if silent_default:
            system_prompt += "\n\n<silent_default>true</silent_default>"
        spec = ClaudeRunSpec(
            prompt=request,
            system_prompt=system_prompt,
            resume_session_id=resume_id,
            allowed_tools=self._config.allowed_tools,
            setting_sources=self._config.setting_sources,
            model=self._config.model,
            cwd=workspace,
        )
        entry = await self.run_claude(spec)
    except Exception as e:
        logger.exception("GeneralWorker failed")
        await self._announce_error(message.task_id, request, e)
        return

    try:
        created, quarantined = await asyncio.to_thread(
            self._post_run_scan, skills_dir, task_start,
        )
    except Exception:
        logger.exception("post-hoc scan failed; treating run as clean")
        created, quarantined = [], []

    try:
        await self._announce(
            message.task_id, entry,
            created=created, quarantined=quarantined,
            silent_default=silent_default,
            category=pinned_skill or "general",
        )
    except Exception as e:
        logger.exception("GeneralWorker _announce failed after successful run")
        await self._announce_error(message.task_id, request, e)
```

Replace `_announce` body:

```python
SILENT_MARKER = "(nothing to surface)"


async def _announce(
    self, task_id, entry, *,
    created=None, quarantined=None,
    silent_default: bool = False,
    category: str = "general",
):
    spoken = (entry.spoken_summary or "").strip()
    is_silent = silent_default and (
        spoken == "" or spoken.lower() == SILENT_MARKER.lower()
    )
    if not is_silent:
        text = spoken or "Task complete."
        if self._announcer is not None:
            await self._announcer.announce(
                text=text,
                source=f"worker:{self.name}",
                category=category,
                urgent=False,
            )
        else:
            # Back-compat for the period where main.py hasn't wired the
            # announcer yet — fall through to publishing TTS directly.
            await self.bus.publish(BusFrameMessage(
                source=self.name,
                frame=TTSSpeakFrame(text),
                direction=FrameDirection.DOWNSTREAM,
            ))
    await self.send_task_update(task_id, {
        "kind": "announcement",
        "spoken": "" if is_silent else (spoken or "Task complete."),
        "context": {
            "session_id": entry.session_id,
            "workspace": entry.cwd,
            "request": entry.request,
            "skills_created": list(created or []),
            "skills_quarantined": list(quarantined or []),
            "silent": is_silent,
        },
    })
    await self.send_task_response(task_id, {"delivered": not is_silent})
```

Hoist `SILENT_MARKER` to module scope (above `class GeneralWorker`).

- [ ] **Step 4: Run tests**

```bash
pytest tests/workers/test_general.py -v
```

Expected: existing tests still pass (since announcer defaults to None and falls back to direct TTS), plus the 3 new ones.

- [ ] **Step 5: Commit**

```bash
git add src/tend/workers/general.py tests/workers/test_general.py
git commit -m "feat: GeneralWorker silent_default mode + announcer integration"
```

---

### Task 11: Config — SchedulerConfig, WebhookConfig, AnnouncerConfig

**Files:**
- Modify: `src/tend/config.py`
- Modify: `tests/test_config.py` — add new-section coverage

- [ ] **Step 1: Add failing test**

Append to `tests/test_config.py`:

```python
def test_scheduler_config_defaults_present(monkeypatch, tmp_path):
    from tend.config import Settings
    s = Settings()
    assert s.scheduler.heartbeat_every == "30m"
    assert s.scheduler.missed_at_policy == "run-on-restart"


def test_webhook_config_defaults_present():
    from tend.config import Settings
    s = Settings()
    assert s.webhook.host == "127.0.0.1"
    assert s.webhook.port == 7331


def test_announcer_config_defaults_present():
    from tend.config import Settings
    s = Settings()
    assert s.announcer.default_cooldown_s == 300
    # Per-category defaults can be empty.
    assert isinstance(s.announcer.category, dict)


def test_webhook_token_from_env(monkeypatch):
    from tend.config import Settings
    monkeypatch.setenv("TEND_WEBHOOK_TOKEN", "shh")
    s = Settings()
    assert s.tend_webhook_token == "shh"
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_config.py::test_scheduler_config_defaults_present -v
```

Expected: AttributeError (no `scheduler` attr).

- [ ] **Step 3: Add the models**

Edit `src/tend/config.py`. Add at module level:

```python
class SchedulerConfig(BaseModel):
    heartbeat_every: str = "30m"  # or "off"
    missed_at_policy: str = "run-on-restart"  # or "skip"


class WebhookConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7331


class AnnouncerConfig(BaseModel):
    default_cooldown_s: int = 300
    category: dict[str, int] = {}
```

In the `Settings` class, add fields before the secrets:

```python
scheduler: SchedulerConfig = SchedulerConfig()
webhook: WebhookConfig = WebhookConfig()
announcer: AnnouncerConfig = AnnouncerConfig()

tend_webhook_token: str | None = Field(
    default=None,
    validation_alias=AliasChoices("TEND_WEBHOOK_TOKEN", "tend_webhook_token"),
)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_config.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/tend/config.py tests/test_config.py
git commit -m "feat: SchedulerConfig / WebhookConfig / AnnouncerConfig"
```

---

### Task 12: Hub — drain announcer on Brain deactivation

**Files:**
- Modify: `src/tend/audio/hub.py:48-72` (constructor + drain hook)
- Modify: `tests/audio/...` (add coverage if your existing test scaffolding allows; otherwise skip and rely on integration smoke)

- [ ] **Step 1: Add failing test**

Create `tests/audio/test_hub_announcer.py`:

```python
"""Hub drains the announcer when Brain transitions inactive."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tend.audio.hub import Hub
from tend.config import Settings


async def test_on_brain_deactivated_drains_announcer():
    bus = MagicMock()
    bus.publish = AsyncMock()
    announcer = MagicMock()
    announcer.drain_pending = AsyncMock()
    settings = Settings()
    stt = MagicMock()
    tts = MagicMock()
    brain = MagicMock()
    hub = Hub(
        "hub", bus=bus, settings=settings,
        stt=stt, tts=tts, tts_sample_rate=16000,
        brain=brain, announcer=announcer,
    )
    await hub.on_brain_deactivated()
    announcer.drain_pending.assert_awaited_once()


async def test_on_brain_deactivated_no_announcer_safe():
    bus = MagicMock()
    settings = Settings()
    hub = Hub(
        "hub", bus=bus, settings=settings,
        stt=MagicMock(), tts=MagicMock(), tts_sample_rate=16000,
        brain=MagicMock(),
    )
    # Should not raise even without announcer.
    await hub.on_brain_deactivated()
```

- [ ] **Step 2: Run to verify fails**

```bash
pytest tests/audio/test_hub_announcer.py -v
```

Expected: fail (Hub.__init__ rejects announcer; no on_brain_deactivated method).

- [ ] **Step 3: Update Hub**

Edit `src/tend/audio/hub.py`:

```python
def __init__(
    self,
    name: str,
    *,
    bus: AgentBus,
    settings: Settings,
    stt: STTService,
    tts: TTSService,
    tts_sample_rate: int,
    brain: BaseAgent,
    announcer=None,
):
    super().__init__(name, bus=bus)
    self._settings = settings
    self._stt = stt
    self._tts = tts
    self._tts_sample_rate = tts_sample_rate
    self._brain = brain
    self._context = LLMContext()
    self._announcer = announcer

async def on_brain_deactivated(self) -> None:
    if self._announcer is not None:
        await self._announcer.drain_pending()
```

The existing gates that flip Brain to inactive are responsible for calling `await self._hub.on_brain_deactivated()` — check `src/tend/audio/gates.py` (Sleep gate) and add the call there if it's not already present. The existing SessionManager hooks into Brain's deactivation through a separate `Brain.on_deactivated` override — leave that path alone; this is a sibling hook for announcer drain.

In `src/tend/audio/gates.py`, in whichever place the sleep transition happens, add:

```python
# After flipping brain.active to False:
await self._hub.on_brain_deactivated()
```

If the gates already call a single method that fans out (such as `hub.handle_brain_deactivated`), reuse that.

- [ ] **Step 4: Run tests**

```bash
pytest tests/audio/test_hub_announcer.py tests/audio/ -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/tend/audio/hub.py src/tend/audio/gates.py tests/audio/test_hub_announcer.py
git commit -m "feat: Hub drains ProactiveAnnouncer on brain deactivation"
```

---

### Task 13: tend.toml + main.py wiring

**Files:**
- Modify: `tend.toml`
- Modify: `src/tend/main.py`

- [ ] **Step 1: Update tend.toml**

Add to `tend.toml` after the existing `[workers.general]` section:

```toml
[scheduler]
heartbeat_every = "30m"
missed_at_policy = "run-on-restart"

[webhook]
host = "127.0.0.1"
port = 7331

[announcer]
default_cooldown_s = 300

[announcer.category]
posture = 600
hydration = 900
```

Add a placeholder line in `.env.example` (create if missing) to document the token:

```
TEND_WEBHOOK_TOKEN=replace-me
```

- [ ] **Step 2: Update main.py**

Edit `src/tend/main.py` to construct and register the new components. Replace `_run` with:

```python
async def _run() -> None:
    runner = AgentRunner()

    claude_cli_preflight()  # logs warning on failure; non-fatal
    store = SessionStore(root=Path.home() / ".tend")
    cron_store = CronStore(root=Path.home() / ".tend")

    llm_service = _make_brain_llm(settings)
    if llm_service is None:
        logger.error(
            "Brain LLM unavailable. Tend boots but the brain will not respond. "
            "Set ANTHROPIC_API_KEY or fix the preflight."
        )

    # Build the announcer (will get its is_brain_active hook after Brain exists).
    announcer_holder: dict = {}
    def _is_brain_active() -> bool:
        b = announcer_holder.get("brain")
        return bool(b and getattr(b, "active", False))
    announcer = ProactiveAnnouncer(
        bus=runner.bus,
        is_brain_active=_is_brain_active,
        default_cooldown_s=settings.announcer.default_cooldown_s,
        category_cooldowns=dict(settings.announcer.category),
    )

    # Scheduler dispatches to the general worker via the bus.
    async def _dispatch(target: str, payload: dict) -> None:
        # Use the runner's bus directly via a request-task message.
        # The Scheduler is itself a BaseAgent, so we use its request_task.
        await scheduler.request_task(target, payload=payload)

    scheduler = Scheduler(
        "scheduler",
        bus=runner.bus,
        store=cron_store,
        dispatch=_dispatch,
        default_tz=settings.timezone or "UTC",
        missed_at_policy=settings.scheduler.missed_at_policy,
    )

    brain = Brain(
        "brain", bus=runner.bus, llm_service=llm_service, store=store,
        scheduler=scheduler,
    )
    announcer_holder["brain"] = brain

    stt = _make_stt(settings)
    tts, tts_rate = _make_tts(settings)

    hub = Hub(
        "hub", bus=runner.bus, settings=settings,
        stt=stt, tts=tts, tts_sample_rate=tts_rate,
        brain=brain, announcer=announcer,
    )

    session_manager = SessionManager(
        brain=brain,
        hub=hub,
        soul_path=settings.soul_path,
        reset_time=settings.daily_reset_time,
        timezone=settings.timezone,
    )
    brain.attach_session_manager(session_manager)
    await session_manager.start()

    # Eagerly register the GeneralWorker so scheduler/webhook fires can
    # land even before the user has spoken.
    cfg = settings.workers.get("general") or WorkerConfig()
    general = GeneralWorker(
        "general", bus=runner.bus, store=store, config=cfg,
        announcer=announcer,
    )
    await brain.add_agent(general)

    # Seed the heartbeat skill on first boot if missing.
    _seed_heartbeat_skill()

    # Seed the heartbeat job too if not already present.
    _seed_heartbeat_job(scheduler, settings.scheduler.heartbeat_every)

    # Webhook server.
    webhook_app = build_app(
        token=settings.tend_webhook_token or "",
        announcer=announcer,
        dispatch=_dispatch,
        skills_root=Path.home() / ".tend" / "skills",
    )
    webhook_server = WebhookServer(
        host=settings.webhook.host,
        port=settings.webhook.port,
        app=webhook_app,
    )
    await webhook_server.start()
    try:
        await runner.add_agent(hub)
        await runner.add_agent(scheduler)
        await scheduler.fire_missed_now()
        await runner.run()
    finally:
        await webhook_server.stop()
```

Add the helper functions and imports at the top of `main.py`:

```python
from tend.announcer import ProactiveAnnouncer
from tend.config import WorkerConfig
from tend.cron_store import CronStore
from tend.scheduler import Scheduler
from tend.webhook import WebhookServer, build_app
from tend.workers.general import GeneralWorker


HEARTBEAT_SKILL_BODY = """\
---
name: heartbeat
description: Periodic silent check-in. Review pending work and announce only when something genuinely needs the user's attention.
silent_default: true
---

# Heartbeat

You are running on the heartbeat tick. By default, exit silently.

Only announce if there is something the user genuinely wants to know
right now and would not have heard otherwise. Examples that justify an
announcement: a follow-up deadline arrived, a long-running task you
started earlier finished while the user was away.

If you have nothing worth surfacing, end your run with the literal
phrase "(nothing to surface)" as your last assistant message.
"""


def _seed_heartbeat_skill() -> None:
    target = Path.home() / ".tend" / "skills" / "heartbeat" / "SKILL.md"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(HEARTBEAT_SKILL_BODY, encoding="utf-8")


def _seed_heartbeat_job(scheduler: Scheduler, every: str) -> None:
    if (every or "").lower() == "off":
        # Remove existing heartbeat job if disabled.
        for job in scheduler.find_by_source("system:heartbeat"):
            scheduler.cancel_job(job.id)
        return
    if scheduler.find_by_source("system:heartbeat"):
        return
    scheduler.add_job(
        when=f"every {every}",
        request="Heartbeat tick — review and surface anything worth saying.",
        name="heartbeat",
        source="system:heartbeat",
        payload_extras={"skill": "heartbeat", "silent_default": True},
    )
```

- [ ] **Step 3: Smoke-run main**

```bash
TEND_WEBHOOK_TOKEN=test python -c "from tend.main import _run; import asyncio; asyncio.run(_run())" &
sleep 3
curl -s -H "Authorization: Bearer test" -X POST http://127.0.0.1:7331/say -H 'Content-Type: application/json' -d '{"text":"smoke check","category":"smoke","urgent":true}'
```

Expected: `{"delivered": true}` and tend speaks "smoke check" (or, if no speaker, the bus log shows `TTSSpeakFrame` published). `Ctrl+C` to stop.

- [ ] **Step 4: Run unit suite**

```bash
pytest -x
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add tend.toml src/tend/main.py .env.example
git commit -m "feat: wire scheduler + webhook + announcer in main process"
```

---

### Task 14: CLI — `tend schedule list/show/add/rm`

**Files:**
- Modify: `src/tend/cli.py`
- Create: `tests/test_cli_schedule.py`

- [ ] **Step 1: Add failing tests**

Create `tests/test_cli_schedule.py`:

```python
"""End-to-end CLI tests for `tend schedule ...`."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from tend.cli import main
from tend.cron_store import CronStore


@pytest.fixture
def tend_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TEND_ROOT", str(tmp_path))
    return tmp_path


def _run(argv) -> tuple[int, str]:
    buf = io.StringIO()
    sys_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = main(argv)
    finally:
        sys.stdout = sys_stdout
    return rc, buf.getvalue()


def test_schedule_list_empty(tend_root):
    rc, out = _run(["schedule", "list"])
    assert rc == 0
    assert "no" in out.lower()


def test_schedule_add_and_list(tend_root):
    rc, _ = _run([
        "schedule", "add",
        "--when", "every 1m",
        "--request", "ping",
        "--name", "ping-job",
    ])
    assert rc == 0
    rc, out = _run(["schedule", "list"])
    assert rc == 0
    assert "ping-job" in out


def test_schedule_show(tend_root):
    _run(["schedule", "add", "--when", "every 5m",
          "--request", "x", "--name", "show-me"])
    rc, out = _run(["schedule", "show", "show-me"])
    assert rc == 0
    assert "show-me" in out
    assert "every" in out


def test_schedule_rm(tend_root):
    _run(["schedule", "add", "--when", "every 5m",
          "--request", "x", "--name", "tmp"])
    rc, _ = _run(["schedule", "rm", "tmp"])
    assert rc == 0
    rc, out = _run(["schedule", "list"])
    assert "tmp" not in out
```

- [ ] **Step 2: Run to verify fails**

```bash
pytest tests/test_cli_schedule.py -v
```

Expected: fail (`schedule` subcommand not registered; `TEND_ROOT` not yet read by the CLI helpers).

- [ ] **Step 3: Implement the subcommand**

Edit `src/tend/cli.py`. First, change `_default_root` to honor `TEND_ROOT`:

```python
def _default_root() -> Path:
    override = os.environ.get("TEND_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".tend"
```

Add the cron_store helper:

```python
def _cron_store() -> "CronStore":
    from tend.cron_store import CronStore
    return CronStore(root=_default_root())
```

Add the command functions:

```python
def cmd_schedule_list(args) -> int:
    from tend.cron_store import CronStore  # local import keeps cold start fast
    store = _cron_store()
    jobs = store.load_jobs()
    if not jobs:
        print("No schedules.")
        return 0
    for j in jobs:
        state = store.get_state(j.id)
        nfa = state.next_run_at or "(unknown)"
        print(
            f"{j.name:<24} {j.kind:<6} {j.schedule:<22} "
            f"source={j.source:<22} next={nfa}"
        )
    return 0


def cmd_schedule_show(args) -> int:
    store = _cron_store()
    name_or_id = args.name_or_id
    match = next(
        (j for j in store.load_jobs()
         if j.name == name_or_id or j.id.startswith(name_or_id)),
        None,
    )
    if match is None:
        print(f"No schedule matching {name_or_id!r}", file=sys.stderr)
        return 2
    print(f"id          {match.id}")
    print(f"name        {match.name}")
    print(f"kind        {match.kind}")
    print(f"schedule    {match.schedule}")
    print(f"tz          {match.tz}")
    print(f"source      {match.source}")
    print(f"enabled     {match.enabled}")
    print(f"created_at  {match.created_at}")
    print(f"payload     {json.dumps(match.payload)}")
    state = store.get_state(match.id)
    print(f"last_run    {state.last_run_at} ({state.last_run_status})")
    print(f"next_run    {state.next_run_at}")
    if state.last_error:
        print(f"last_error  {state.last_error}")
    return 0


def cmd_schedule_add(args) -> int:
    from tend.cron_time import InvalidWhen, parse_when, next_fire_at
    import datetime as _dt
    from zoneinfo import ZoneInfo
    store = _cron_store()
    try:
        kind, schedule = parse_when(args.when)
    except InvalidWhen as e:
        print(f"invalid --when: {e}", file=sys.stderr)
        return 2
    job = store.add_job(
        name=args.name, kind=kind, schedule=schedule,
        tz=args.tz or "UTC",
        payload={"request": args.request},
        source="cli", enabled=True,
    )
    nfa = next_fire_at(
        kind, schedule, args.tz or "UTC",
        _dt.datetime.now(tz=ZoneInfo("UTC")),
    )
    from tend.cron_store import JobState
    store.set_state(job.id, JobState(next_run_at=nfa.isoformat()))
    print(f"added {job.id[:8]} {job.name}")
    return 0


def cmd_schedule_rm(args) -> int:
    store = _cron_store()
    name_or_id = args.name_or_id
    match = next(
        (j for j in store.load_jobs()
         if j.name == name_or_id or j.id.startswith(name_or_id)),
        None,
    )
    if match is None:
        print(f"No schedule matching {name_or_id!r}", file=sys.stderr)
        return 2
    store.remove_job(match.id)
    store.remove_state(match.id)
    print(f"removed {match.name}")
    return 0
```

Register the subparser inside `build_parser()` (place after the `skills` block):

```python
sched_p = sub.add_parser(
    "schedule",
    help="List, add, and remove scheduled jobs.",
    description=(
        "Manage scheduled jobs at ~/.tend/cron/jobs.json. The running tend "
        "daemon picks up changes; add/rm here is equivalent to using the "
        "voice tools."
    ),
)
sched = sched_p.add_subparsers(
    dest="action", required=True, title="actions", metavar="<action>",
)

sl = sched.add_parser("list", help="List active schedules.")
sl.set_defaults(func=cmd_schedule_list)

ss = sched.add_parser("show", help="Show one schedule.")
ss.add_argument("name_or_id")
ss.set_defaults(func=cmd_schedule_show)

sa = sched.add_parser("add", help="Add a new schedule.")
sa.add_argument("--when", required=True,
                help="cron expr / 'in 30m' / 'every 30m' / ISO timestamp")
sa.add_argument("--request", required=True, help="What the worker should do.")
sa.add_argument("--name", required=True, help="Label for cancel/list later.")
sa.add_argument("--tz", default=None, help="Timezone for cron schedules.")
sa.set_defaults(func=cmd_schedule_add)

sr = sched.add_parser("rm", help="Remove a schedule by name or id prefix.")
sr.add_argument("name_or_id")
sr.set_defaults(func=cmd_schedule_rm)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_cli_schedule.py tests/test_cli.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/tend/cli.py tests/test_cli_schedule.py
git commit -m "feat: tend schedule list/show/add/rm CLI"
```

---

### Task 15: CLI — skills enable-triggers / disable-triggers, webhook test

**Files:**
- Modify: `src/tend/cli.py`
- Modify: `tests/test_cli.py` — add coverage

- [ ] **Step 1: Add failing tests**

Append to `tests/test_cli.py`:

```python
def test_skills_enable_triggers_no_running_daemon(tmp_path, monkeypatch, capsys):
    """enable-triggers without a running daemon is a no-op that explains
    itself, since the CLI cannot dispatch onto the bus."""
    from tend.cli import main
    monkeypatch.setenv("TEND_ROOT", str(tmp_path))
    skill_dir = tmp_path / "skills" / "x"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: x\ndescription: y\n"
        "triggers:\n  - cron: \"0 12 * * *\"\n    request: hi\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEND_SKILLS_ROOT", str(tmp_path / "skills"))

    rc = main(["skills", "enable-triggers", "x"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "x" in captured.out


def test_webhook_test_unconfigured_token(monkeypatch, capsys):
    monkeypatch.delenv("TEND_WEBHOOK_TOKEN", raising=False)
    from tend.cli import main
    rc = main(["webhook", "test"])
    assert rc == 2  # Misconfigured
    err = capsys.readouterr().err
    assert "TEND_WEBHOOK_TOKEN" in err
```

- [ ] **Step 2: Run to verify fails**

```bash
pytest tests/test_cli.py::test_skills_enable_triggers_no_running_daemon tests/test_cli.py::test_webhook_test_unconfigured_token -v
```

Expected: fail (subcommands not defined).

- [ ] **Step 3: Add the commands**

In `src/tend/cli.py`, add functions:

```python
def cmd_skills_enable_triggers(args) -> int:
    """Print the schedules the running daemon would create. The daemon
    itself owns the schedules; the CLI is a planning aid (no IPC)."""
    name = _validate_skill_name(args.name)
    from tend.skills import enumerate_skills
    skills = [s for s in enumerate_skills(_skills_root()) if s.name == name]
    if not skills:
        print(f"No skill named {name!r}", file=sys.stderr)
        return 2
    info = skills[0]
    if not info.triggers:
        print(f"{name}: no triggers declared in frontmatter")
        return 0
    print(f"{name}: {len(info.triggers)} trigger(s) — ask the running daemon")
    print("to enable via voice ('enable triggers for x') or by editing")
    print("~/.tend/cron/jobs.json directly while tend is stopped.")
    for t in info.triggers:
        print(f"  - {t}")
    return 0


def cmd_skills_disable_triggers(args) -> int:
    name = _validate_skill_name(args.name)
    store = _cron_store()
    rows = store.find_by_source(f"skill:{name}")
    if not rows:
        print(f"{name}: no active triggers")
        return 0
    count = store.remove_by_source(f"skill:{name}")
    print(f"{name}: removed {count} trigger(s)")
    return 0


def cmd_webhook_test(args) -> int:
    import urllib.request
    token = os.environ.get("TEND_WEBHOOK_TOKEN")
    if not token:
        print(
            "TEND_WEBHOOK_TOKEN is not set; cannot test the webhook.",
            file=sys.stderr,
        )
        return 2
    from tend.config import settings
    url = f"http://{settings.webhook.host}:{settings.webhook.port}/say"
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps({
            "text": "tend webhook test",
            "category": "test",
            "urgent": True,
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            print(resp.read().decode())
        return 0
    except Exception as e:
        print(f"webhook unreachable: {e}", file=sys.stderr)
        return 1
```

Register in `build_parser()`:

```python
# Inside the skills subparser block, alongside `quarantined`:
sk_en = skills.add_parser(
    "enable-triggers",
    help="Show triggers a skill will activate (informational; the running "
         "daemon owns the schedule).",
)
sk_en.add_argument("name")
sk_en.set_defaults(func=cmd_skills_enable_triggers)

sk_dis = skills.add_parser(
    "disable-triggers",
    help="Remove all schedule entries previously enabled from a skill.",
)
sk_dis.add_argument("name")
sk_dis.set_defaults(func=cmd_skills_disable_triggers)

# New top-level group:
wh_p = sub.add_parser(
    "webhook",
    help="Probe the local webhook server.",
)
wh_actions = wh_p.add_subparsers(
    dest="action", required=True, title="actions", metavar="<action>",
)
wt = wh_actions.add_parser(
    "test",
    help="POST a smoke message to /say (requires TEND_WEBHOOK_TOKEN).",
)
wt.set_defaults(func=cmd_webhook_test)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_cli.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/tend/cli.py tests/test_cli.py
git commit -m "feat: tend skills enable/disable-triggers + tend webhook test"
```

---

### Task 16: Delete ReminderWorker + tests

**Files:**
- Delete: `src/tend/workers/reminder.py`
- Delete: `tests/workers/test_reminder.py`

- [ ] **Step 1: Delete the files**

```bash
git rm src/tend/workers/reminder.py tests/workers/test_reminder.py
```

- [ ] **Step 2: Run the full suite**

```bash
pytest -x
```

Expected: pass. (If anything still imports `tend.workers.reminder`, fix it inline.)

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: drop ReminderWorker — superseded by scheduler + announcer"
```

---

### Task 17: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Edit CLAUDE.md**

Find the `Architecture (current)` section and update the diagram:

```
AgentRunner (in-process AsyncQueueBus)
└── Hub (BaseAgent, always running) — owns LLMContext
      pipeline:
        transport.in → VAD → OpenWakeWordGate → STT
          → InputLatencyLogger → SleepPhraseGate
          → user_aggregator → BusBridgeProcessor (unnamed bridge)
          → TTS → OutputLatencyLogger → transport.out
          → assistant_aggregator
      └── Brain (LLMAgent, bridged=(), starts inactive)
            pipeline: [LLM]
            └── GeneralWorker (eager-registered)
└── Scheduler (BaseAgent, peer to Hub) — owns wall-clock + jobs.json
WebhookServer (aiohttp on 127.0.0.1) — POST /say, /event
ProactiveAnnouncer — single TTS funnel; cooldown / deferral / urgency
```

Find the `Module layout` section. Add new modules and remove `reminder.py`:

```
src/tend/
  audio/
    hub.py         Hub agent — audio + STT/TTS + LLMContext + aggregators + announcer drain
    gates.py       OpenWakeWordGate (pre-STT, with cooldown), SleepPhraseGate (post-STT)
    logging.py     Input/OutputLatencyLogger (pass-through, debug only)
  workers/
    claude_cli.py  ClaudeCliWorker (base — runs `claude` CLI subprocess)
    general.py     GeneralWorker (skill-driven; silent_default heartbeat mode; uses announcer)
  announcer.py     ProactiveAnnouncer — single TTS funnel
  brain.py         Brain (thin LLMAgent — build_llm + tools; remind_in wraps schedule)
  cron_store.py    JSON job store (jobs.json + jobs-state.json)
  cron_time.py     Pure helpers: parse_when, next_fire_at
  scheduler.py     Scheduler BaseAgent — wall-clock dispatch loop
  webhook.py       aiohttp /say + /event receiver, token-authed, loopback-only
  session.py       SessionManager (soul.md, daily reset → Hub.reset_session)
  sessions.py      SessionStore (per-worker persistent session metadata)
  services.py      STT / TTS / brain LLM factories with preflight + fallback
  preflight.py     `claude` CLI preflight check
  config.py        Settings (pydantic-settings, TOML + env)
  cli.py           `tend` CLI — sessions / skills / scan-skill / schedule / webhook test
  main.py          AgentRunner setup; entry point
```

Append a new section at the bottom (above "When in doubt"):

```markdown
## Proactive triggers

The scheduler + announcer + webhook stack lets tend act without being
asked. Read `docs/superpowers/specs/2026-05-08-tend-proactive-triggers-design.md`
before editing any of `src/tend/{scheduler,announcer,webhook,cron_store,cron_time}.py`.

- **Voice authoring:** `Brain.schedule(when, request, name)`. `when`
  accepts cron expressions, `in 30m` style relatives, ISO timestamps,
  or `every 30m`.
- **Skill defaults:** `triggers:` entries in `SKILL.md` frontmatter are
  inert until the user runs `enable_skill_triggers <skill>` (voice) or
  edits `~/.tend/cron/jobs.json` while tend is stopped.
- **External producers:** see `docs/integrating-with-tend.md`. Vision
  daemon, Gmail webhooks, etc. POST to `127.0.0.1:7331/say` or
  `/event` with a Bearer token from `TEND_WEBHOOK_TOKEN`.
- **Cooldown / deferral:** all proactive announcements go through
  `ProactiveAnnouncer.announce` so they share one cooldown registry
  (per-category) and queue while Brain is mid-conversation. `urgent=True`
  bypasses both.
- **Heartbeat:** an `every 30m` job seeded at first boot dispatches the
  `heartbeat` skill silently. The skill announces only when there is
  something genuinely worth saying. Disable with
  `[scheduler] heartbeat_every = "off"`.
```

In the `Worker pattern (canonical)` section, replace the example or add a paragraph noting that workers should call `self._announcer.announce(...)` instead of publishing `TTSSpeakFrame` directly.

In the `Deliberate non-choices for v1` list, change the line about "no new worker classes per capability" to keep its meaning, and add:

```markdown
- **No retry/backoff on failed scheduled jobs.** Recurring jobs wait
  for next scheduled fire; one-shot jobs delete after a single attempt.
- **No outbound channel routing.** Scheduler/webhook announcements only
  go to local TTS in v1; Telegram/SMS delivery is a separate workstream.
```

- [ ] **Step 2: No tests, just commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md updated for scheduler / announcer / webhook"
```

---

### Task 18: Write the integration document

**Files:**
- Create: `docs/integrating-with-tend.md`

- [ ] **Step 1: Write the document**

Create `docs/integrating-with-tend.md`:

````markdown
# Integrating with tend

This page is the contract for any external process that wants to make
tend speak (via `POST /say`) or hand it a structured event (via
`POST /event`). You can wire up an integration from this page alone —
no need to read tend's source.

> tend is a single-user voice assistant running on a Raspberry Pi. The
> webhook surface exists so co-located processes (a vision daemon, a
> Gmail watcher, etc.) can produce announcements without becoming part
> of tend's main process.

## Setup

- Server: `aiohttp` on `127.0.0.1:7331` by default. Configurable via
  `tend.toml`:
  ```toml
  [webhook]
  host = "127.0.0.1"
  port = 7331
  ```
- Auth token: `TEND_WEBHOOK_TOKEN` in `.env` (gitignored). Share it with
  your producer process out-of-band (env var, secrets file, etc.).
- Smoke test: `tend webhook test` will `POST /say` a smoke message
  using the token from your environment. A `200` with
  `{"delivered": true}` confirms the server is up and reachable.

## Authentication

Every request must include:

```
Authorization: Bearer <TEND_WEBHOOK_TOKEN>
```

Missing or wrong token → `401 {"error": "unauthorized"}`.

Token rotation is manual: edit `.env`, restart `tend` (`systemctl --user
restart tend`), update your producer config.

## `POST /say`

Direct TTS — tend speaks the text you provide. No LLM round-trip;
lowest latency.

**Request body**

| field      | type    | required | description                                   |
|------------|---------|----------|-----------------------------------------------|
| `text`     | string  | yes      | What to say. Goes straight to TTS.            |
| `category` | string  | no       | Cooldown bucket (default `"general"`).        |
| `urgent`   | boolean | no       | Bypass cooldown + active-Brain deferral.      |

**Response**

```json
{ "delivered": true }
```

`delivered: true` means the announcement was either spoken immediately or
queued for delivery once Brain stops conversing with the user.
`delivered: false` means the call was dropped on cooldown (the same
category fired too recently; see "Cooldown semantics" below).

**Status codes**

- `200` — handled (delivered or dropped on cooldown).
- `400` — body missing `text` or invalid JSON.
- `401` — missing or wrong token.

**Examples**

```bash
curl -sX POST http://127.0.0.1:7331/say \
  -H "Authorization: Bearer $TEND_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Sit up straight.","category":"posture"}'
```

```python
import os, requests
TOKEN = os.environ["TEND_WEBHOOK_TOKEN"]

def say(text, category="general", urgent=False):
    r = requests.post(
        "http://127.0.0.1:7331/say",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"text": text, "category": category, "urgent": urgent},
        timeout=2,
    )
    r.raise_for_status()
    return r.json()["delivered"]
```

## `POST /event`

Structured event — tend matches the event kind against installed skills'
`events:` frontmatter and dispatches to each subscribing skill via the
GeneralWorker. Each skill sees the full body in its dispatch payload.

**Request body**

| field  | type   | required | description                                   |
|--------|--------|----------|-----------------------------------------------|
| `kind` | string | yes      | Event identifier — `dotted.snake_case`.       |
| ...    | any    | no       | Free-form fields, passed through verbatim.    |

**Response**

```json
{ "dispatched": ["skill-a", "skill-b"] }
```

`dispatched` lists the skills that were handed the event. An empty list
is **not** an error — it means no installed skill subscribes to that
kind. Delivery is fire-and-forget: the response does not wait for the
worker to finish.

**Status codes**

- `200` — handled (zero or more skills dispatched).
- `400` — body missing `kind` or invalid JSON.
- `401` — missing or wrong token.

**Examples**

```bash
curl -sX POST http://127.0.0.1:7331/event \
  -H "Authorization: Bearer $TEND_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"kind":"posture.slumped","duration_s":1200}'
```

## Cooldown semantics

`/say` shares one cooldown registry with all other proactive sources
(scheduler fires, internal worker announcements). The default cooldown
is 5 minutes per category; specific categories can have their own
cooldown set in `tend.toml [announcer.category]`.

If you POST a `category="posture"` message and the same category was
spoken less than the cooldown window ago, your message is **dropped**
and the response is `{"delivered": false}`. Don't retry — that defeats
the purpose. Either pick a different category if the messages are
genuinely unrelated, or accept the drop.

`urgent: true` bypasses cooldown. Reserve for safety-class events
(smoke alarm, doorbell, glass-break detection). Routine nags should
always be `urgent: false`.

## Best practices

- **Pick a stable, meaningful category.** `posture`, `hydration`,
  `meal-reminder` — not `general` for everything. Distinct categories
  throttle independently.
- **Keep `text` short.** It goes straight to TTS. One sentence is
  ideal; two is the upper bound. Long announcements interrupt the user
  for too long.
- **Reserve `urgent: true`.** Default to `false`. The user is the
  arbiter of what's actually urgent; if everything is urgent, nothing
  is.
- **Retry on `ConnectionRefusedError`.** tend may be restarting. Wait
  a few seconds, try again. Don't loop forever — fall back to logging
  if tend is down for an extended period.
- **Don't pre-rate-limit on the producer side based on TTS timing.**
  Let the cooldown drop happen naturally. The response tells you
  whether the message landed.

## Limitations

- **Loopback only.** The HTTP server binds to `127.0.0.1`; it is not
  reachable from outside the host. Use SSH tunnelling or a reverse
  proxy if you need cross-host access.
- **No streaming.** Each request is one announcement. There is no
  long-lived connection for progressive output.
- **No delivery acknowledgement beyond the synchronous response.** The
  producer cannot tell whether a queued (`delivered: true`) message
  actually got spoken later — it might be dropped on a subsequent
  cooldown when Brain finally goes inactive.
- **No inbound channel.** tend cannot push messages to your producer
  in v1. If your producer needs to know what tend is saying, read the
  daemon logs.

## Versioning

This contract follows `tend`'s normal release cadence. Breaking changes
will be called out in `CHANGELOG.md` (when one exists) or on the
release notes for the affected commit. Until then, pin to a known-good
commit if your producer needs strict stability.

## Worked example: a posture-nag daemon

A minimal vision daemon that:
- nags posture via `/say` (rate-limited by tend's cooldown);
- emits a structured `/event` for long sitting periods that a skill
  might react to.

```python
"""posture_daemon.py — example tend integration."""

from __future__ import annotations

import os
import time
from typing import Optional

import requests


TEND_BASE = "http://127.0.0.1:7331"
TOKEN = os.environ["TEND_WEBHOOK_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _post(path: str, body: dict) -> Optional[dict]:
    try:
        r = requests.post(TEND_BASE + path, headers=HEADERS,
                          json=body, timeout=2)
        if r.ok:
            return r.json()
        print(f"tend {path} responded {r.status_code}: {r.text}")
    except requests.ConnectionError:
        print("tend not reachable; will retry next tick")
    return None


def say(text: str, category: str = "general", urgent: bool = False) -> None:
    _post("/say", {"text": text, "category": category, "urgent": urgent})


def event(kind: str, **payload) -> None:
    _post("/event", {"kind": kind, **payload})


def main_loop() -> None:
    last_seated_at = time.monotonic()
    while True:
        bad_posture = detect_bad_posture()    # your CV pipeline
        if bad_posture:
            say("Sit up straight.", category="posture")
        seated_seconds = time.monotonic() - last_seated_at
        if seated_seconds > 90 * 60:
            event("user_idle.long_sit",
                  duration_s=int(seated_seconds))
            last_seated_at = time.monotonic()
        time.sleep(2.0)


if __name__ == "__main__":
    main_loop()
```

The vision daemon owns wording and timing of `/say` calls; the cooldown
in tend prevents posture nags from machine-gunning. The `/event` call
gives skills a chance to react with richer behaviour (a meal-plan
adjustment, a calendar block, a one-line nudge with context); if no
skill subscribes, the event is harmlessly logged.
````

- [ ] **Step 2: Commit**

```bash
git add docs/integrating-with-tend.md
git commit -m "docs: integration contract for /say and /event"
```

---

### Task 19: Final smoke + full-suite + branch hand-off

**Files:** none (validation only)

- [ ] **Step 1: Run the full suite**

```bash
pytest -v
```

Expected: all green. Investigate and fix any reds before moving on.

- [ ] **Step 2: Manual smoke test of scheduler firing**

```bash
TEND_WEBHOOK_TOKEN=test python -m tend &
TEND_PID=$!
sleep 5
# Add a one-shot 5-second-from-now job via CLI:
tend schedule add --when "in 5s" --request "smoke test" --name "smoke"
sleep 8
# Check that it fired and was deleted:
tend schedule list
# Should print "No schedules." (the at-job auto-deleted)
kill $TEND_PID
```

Expected: tend logs the dispatch in `/tmp/tend.log`; the GeneralWorker spawns claude (or fails open if no claude binary in this env, which is acceptable for the smoke); the schedule is gone.

- [ ] **Step 3: Manual smoke test of webhook**

```bash
TEND_WEBHOOK_TOKEN=test python -m tend &
TEND_PID=$!
sleep 5
TEND_WEBHOOK_TOKEN=test tend webhook test
# Expected: {"delivered": true}
kill $TEND_PID
```

- [ ] **Step 4: Verify branch is clean and documented**

```bash
git status
git log --oneline | head -25
```

Expected: working tree clean; commits present for tasks 1–18; the diff vs `main` covers the file map at the top of this plan.

- [ ] **Step 5: Hand off**

Open a PR (or hand the branch off):

```bash
git push -u origin <feature-branch>
gh pr create --title "Proactive triggers: scheduler + announcer + webhook" --body "$(cat <<'EOF'
## Summary
- Adds Scheduler BaseAgent, ProactiveAnnouncer, and aiohttp webhook receiver
- Brain gets schedule / list_schedules / cancel_schedule / enable_skill_triggers / disable_skill_triggers tools
- GeneralWorker learns silent_default heartbeat mode + event-driven dispatch
- ReminderWorker removed; Brain.remind_in now wraps the scheduler
- New docs/integrating-with-tend.md is the outward-facing webhook contract

Spec: docs/superpowers/specs/2026-05-08-tend-proactive-triggers-design.md

## Test plan
- [ ] pytest -v passes
- [ ] tend daemon starts cleanly with TEND_WEBHOOK_TOKEN set
- [ ] tend schedule add --when "in 5s" → job fires and auto-deletes
- [ ] tend webhook test → {"delivered": true} and audible "tend webhook test"
- [ ] tend schedule add --when "0 12 * * *" → noon dispatches GeneralWorker (skip if no audio in dev env)
EOF
)"
```

---

## Risks the implementer should know about

- **croniter vs zoneinfo behaviour around DST transitions.** croniter's
  default uses `next` semantics that may produce surprising next-fire
  times during the spring-forward / fall-back hour. Tests don't cover
  this — if you see anomalies on DST days, audit `next_fire_at` and
  consider passing the timezone-aware `now` consistently.
- **aiohttp aiohttp test fixtures.** `tests/test_webhook.py` uses
  `TestClient(TestServer(app))`. If your aiohttp version is newer than
  what pipecat brings in, the import paths may shift; use
  `aiohttp.test_utils` consistently.
- **Bus dispatch ordering.** Scheduler calls `request_task("general", ...)`
  which goes through the bus. If `runner.add_agent(scheduler)` runs
  before the GeneralWorker is registered, the very first fire after
  startup may land on an unregistered agent and silently drop. Task 13's
  ordering (`brain.add_agent(general)` before `runner.add_agent(scheduler)`)
  prevents this — preserve that order if you refactor `_run`.
- **Time fixtures in tests.** `freezegun` is in dev deps but the plan
  tests use real `datetime.now()`. If you find flakiness, swap to
  `freeze_time(...)` rather than tightening time tolerances.
- **Skill-trigger frontmatter parser.** The hand-rolled YAML-ish parser
  in Task 5 is intentional (no PyYAML dep) but fragile against tabs and
  CRLF line endings. If a skill author runs into trouble, lean toward
  fixing the SKILL.md rather than expanding the parser; a real YAML
  dependency would be a separate proposal.
- **Hub gate hook for announcer drain.** Task 12 says "if the gates
  already call a single fan-out method, reuse that." Read
  `src/tend/audio/gates.py` end-to-end before adding the
  `await self._hub.on_brain_deactivated()` call so you don't double-fire
  it from both gates' transition paths.

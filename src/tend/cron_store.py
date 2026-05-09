"""Durable JSON-backed storage for scheduled jobs.

Two files under <root>/cron/:
  jobs.json         — definitions, hand-editable, may be git-tracked.
  jobs-state.json   — runtime fields (last_run_at, next_run_at, errors).
                      Gitignored; a crash mid-write leaves it best-effort.

Atomic writes via temp-file-rename mirror SessionStore's pattern so a
partial write never produces a corrupt file.

Concurrency model: this module is designed for a single writer. The
running tend daemon owns jobs.json and jobs-state.json while it is
alive. The `tend schedule ...` CLI is a planning aid that should only
be used while the daemon is stopped (or with the understanding that
edits can race with the daemon's own writes).
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from loguru import logger


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
    event_kind: str | None = None
    event_payload: dict | None = None


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


def _row_to_cronjob(row: dict) -> CronJob:
    """Construct a CronJob from a dict, ignoring unknown keys for
    forward-compat with hand-edited or newer-schema files."""
    fields = {f.name for f in CronJob.__dataclass_fields__.values()}
    return CronJob(**{k: v for k, v in row.items() if k in fields})


def _row_to_jobstate(row: dict) -> JobState:
    """Construct a JobState from a dict, ignoring unknown keys for
    forward-compat with hand-edited or newer-schema files."""
    fields = {f.name for f in JobState.__dataclass_fields__.values()}
    return JobState(**{k: v for k, v in row.items() if k in fields})


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
        try:
            data = json.loads(self._jobs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"jobs.json unreadable ({e!r}); treating as empty")
            return []
        rows = data.get("jobs") or []
        out: list[CronJob] = []
        for row in rows:
            try:
                out.append(_row_to_cronjob(row))
            except (TypeError, KeyError) as e:
                logger.warning(f"skipping malformed job row {row!r}: {e}")
        return out

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
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"jobs-state.json unreadable ({e!r}); treating as empty")
            return {}
        states = data.get("states") or {}
        out: dict[str, JobState] = {}
        for k, v in states.items():
            try:
                out[k] = _row_to_jobstate(v)
            except (TypeError, AttributeError) as e:
                logger.warning(f"skipping malformed state row {k!r}: {e}")
        return out

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

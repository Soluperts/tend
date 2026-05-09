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

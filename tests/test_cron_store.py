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


def test_load_jobs_handles_corrupt_file(store, tmp_path):
    """Garbage in jobs.json must not crash the loader."""
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text("{ this is not json", encoding="utf-8")
    assert store.load_jobs() == []


def test_load_state_handles_corrupt_file(store, tmp_path):
    """Garbage in jobs-state.json must not crash the loader."""
    state_path = tmp_path / "cron" / "jobs-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("garbage", encoding="utf-8")
    # Defaults still readable for any id
    assert store.get_state("anything") == JobState()


def test_load_jobs_skips_malformed_row(store, tmp_path):
    """A bad row entry shouldn't kill the rest of the load."""
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(
        '{"version": 1, "jobs": ['
        '{"missing": "everything"},'
        '{"id": "x", "name": "good", "kind": "every", "schedule": "1m",'
        ' "tz": null, "payload": {}, "source": "cli", "enabled": true,'
        ' "created_at": "2026-05-08T00:00:00+00:00"}]}',
        encoding="utf-8",
    )
    jobs = store.load_jobs()
    assert len(jobs) == 1
    assert jobs[0].name == "good"


def test_load_jobs_ignores_unknown_fields(store, tmp_path):
    """Forward-compat: extra fields in jobs.json don't crash the loader."""
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(
        '{"version": 1, "jobs": ['
        '{"id": "x", "name": "good", "kind": "every", "schedule": "1m",'
        ' "tz": null, "payload": {}, "source": "cli", "enabled": true,'
        ' "created_at": "2026-05-08T00:00:00+00:00",'
        ' "future_field": "tend-of-tomorrow"}]}',
        encoding="utf-8",
    )
    jobs = store.load_jobs()
    assert len(jobs) == 1
    assert jobs[0].name == "good"

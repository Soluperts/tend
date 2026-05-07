"""Tests for SessionStore — atomic JSON index + per-session transcripts."""

import json
import pytest

from tend.sessions import SessionEntry, SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path)


def test_start_writes_running_record_to_index(store):
    entry = store.start(
        session_id="abc123",
        worker="coding",
        request="fix the auth bug",
        cwd="/home/pi/hasat",
        task_id="t-1",
    )
    assert entry.session_id == "abc123"
    assert entry.status == "running"
    assert entry.started_at > 0
    assert entry.last_interaction_at == entry.started_at
    assert entry.ended_at is None
    assert entry.transcript_path.endswith("abc123.jsonl")

    # Round-trip from disk
    rows = store.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].session_id == "abc123"
    assert rows[0].status == "running"


def test_complete_updates_status_and_summary(store):
    store.start(session_id="x", worker="w", request="r", cwd=None)
    updated = store.complete(
        "x", status="done",
        spoken_summary="all done",
        usage={"total_cost_usd": 0.0123},
    )
    assert updated.status == "done"
    assert updated.spoken_summary == "all done"
    assert updated.cost_usd == pytest.approx(0.0123)
    assert updated.ended_at is not None


def test_complete_failure_records_error(store):
    store.start(session_id="x", worker="w", request="r", cwd=None)
    updated = store.complete("x", status="failed", error="boom" * 200)
    assert updated.status == "failed"
    # Index file holds the truncated error string
    raw = json.loads((store.root / "sessions.json").read_text())
    assert len(raw["x"]["error"]) <= 500


def test_complete_unknown_session_id_raises(store):
    with pytest.raises(KeyError):
        store.complete("never-started", status="done")
    # Index must remain empty — no phantom row written.
    rows = store.list_recent(limit=10)
    assert rows == []


def test_list_recent_sorts_descending_by_started_at(store):
    from unittest.mock import patch
    with patch("tend.sessions._now_ms", side_effect=[1000, 1000, 2000, 2000]):
        store.start(session_id="old", worker="w", request="r", cwd=None)
        store.start(session_id="new", worker="w", request="r", cwd=None)
    rows = store.list_recent(limit=10)
    assert [r.session_id for r in rows] == ["new", "old"]


def test_list_recent_respects_limit(store):
    for i in range(5):
        store.start(session_id=f"s{i}", worker="w", request="r", cwd=None)
    rows = store.list_recent(limit=2)
    assert len(rows) == 2


def test_get_returns_entry_or_none(store):
    store.start(session_id="z", worker="w", request="r", cwd=None)
    assert store.get("z") is not None
    assert store.get("missing") is None


def test_write_system_prompt_creates_file(store):
    p = store.write_system_prompt("abc", "You are helpful.")
    assert p.read_text() == "You are helpful."
    assert p.name == "abc.txt"


def test_atomic_write_recovers_from_corrupt_index(store, tmp_path):
    # Simulate truncation
    (tmp_path / "sessions.json").write_text("")
    # Should be treated as empty index, not crash
    rows = store.list_recent(limit=10)
    assert rows == []


def test_index_round_trip_via_disk(tmp_path):
    s1 = SessionStore(root=tmp_path)
    s1.start(session_id="x", worker="w", request="r", cwd=None)
    s1.complete("x", status="done", spoken_summary="ok")

    # Fresh instance reads same disk
    s2 = SessionStore(root=tmp_path)
    rows = s2.list_recent(limit=10)
    assert len(rows) == 1 and rows[0].status == "done" and rows[0].spoken_summary == "ok"

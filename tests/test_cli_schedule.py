"""End-to-end CLI tests for `tend schedule ...`."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from tend._cli_legacy import main


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


def test_schedule_add_event_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEND_ROOT", str(tmp_path))
    from tend._cli_legacy import main as cli_main
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
    from tend._cli_legacy import main as cli_main
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
    from tend._cli_legacy import main as cli_main
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


def test_schedule_add_custom_source(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEND_ROOT", str(tmp_path))
    from tend._cli_legacy import main as cli_main
    from tend.cron_store import CronStore

    rc = cli_main([
        "schedule", "add",
        "--when", "in 1m",
        "--request", "test",
        "--name", "x",
        "--source", "schedule-watcher",
    ])
    assert rc == 0
    rows = CronStore(root=tmp_path).load_jobs()
    assert len(rows) == 1
    assert rows[0].source == "schedule-watcher"

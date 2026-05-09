"""End-to-end CLI tests for `tend schedule ...`."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from tend.cli import main


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

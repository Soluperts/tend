# SPDX-License-Identifier: MIT
"""`tend service install/uninstall` CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Isolated HOME/TEND_HOME with subprocess.run stubbed out."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TEND_HOME", str(tmp_path / "tend-home"))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0))
    return tmp_path


def _seed_unit(tmp_path):
    """Create a fake unit file at the current platform's unit_path location."""
    from tend import service as svc
    p = svc.unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[Unit]\n" if sys.platform != "darwin" else "<plist/>\n")
    return p


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

def test_service_install_writes_unit_file(isolated, capsys):
    from tend.cli import main
    rc = main(["service", "install"])
    assert rc == 0
    from tend import service as svc
    assert svc.unit_path().exists()
    out = capsys.readouterr().out
    # File name is either "tend.service" (Linux) or "com.tend.daemon.plist" (macOS)
    assert svc.unit_path().name in out


def test_service_install_force_overwrites(isolated):
    from tend import service as svc
    p = svc.unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("OLD")
    from tend.cli import main
    rc = main(["service", "install", "--force"])
    assert rc == 0
    assert "OLD" not in p.read_text(encoding="utf-8")


def test_service_install_refuses_without_force_when_exists(isolated, capsys):
    from tend import service as svc
    p = svc.unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("OLD")
    from tend.cli import main
    rc = main(["service", "install"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "force" in err.lower()


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------

def test_service_uninstall_removes_unit(isolated):
    from tend import service as svc
    p = svc.unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[Unit]")
    from tend.cli import main
    rc = main(["service", "uninstall"])
    assert rc == 0
    assert not p.exists()


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------

def test_service_start_runs_service_manager(monkeypatch, isolated):
    _seed_unit(isolated)
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    from tend.cli import main
    rc = main(["service", "start"])
    assert rc == 0
    # Either systemctl (Linux) or launchctl kickstart (macOS)
    assert any(
        c[:2] in (["systemctl", "--user"], ["launchctl", "kickstart"])
        for c in calls
    )


def test_service_stop_runs_service_manager(monkeypatch, isolated):
    _seed_unit(isolated)
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    from tend.cli import main
    rc = main(["service", "stop"])
    assert rc == 0
    assert any(
        c[:2] in (["systemctl", "--user"], ["launchctl", "kill"])
        for c in calls
    )


def test_service_start_when_not_installed_exits_two(isolated, capsys):
    from tend.cli import main
    rc = main(["service", "start"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "tend service install" in err


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_service_status_active_exits_zero(monkeypatch, isolated, capsys):
    _seed_unit(isolated)

    if sys.platform == "darwin":
        sample = "gui/501/com.tend.daemon = {\n    state = running\n}\n"
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: MagicMock(returncode=0, stdout=sample, stderr=""),
        )
        expected_state = "running"
    else:
        def fake_run(cmd, *a, **kw):
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return MagicMock(returncode=0, stdout="active\n", stderr="")
            return MagicMock(returncode=0, stdout="● tend.service\n   Active: active", stderr="")
        monkeypatch.setattr("subprocess.run", fake_run)
        expected_state = "active"

    from tend.cli import main
    rc = main(["service", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert expected_state in out


def test_service_status_inactive_exits_nonzero(monkeypatch, isolated, capsys):
    _seed_unit(isolated)

    if sys.platform == "darwin":
        sample = "gui/501/com.tend.daemon = {\n    state = stopped\n}\n"
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: MagicMock(returncode=0, stdout=sample, stderr=""),
        )
        expected_state = "stopped"
    else:
        def fake_run(cmd, *a, **kw):
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return MagicMock(returncode=3, stdout="inactive\n", stderr="")
            return MagicMock(returncode=3, stdout="", stderr="")
        monkeypatch.setattr("subprocess.run", fake_run)
        expected_state = "inactive"

    from tend.cli import main
    rc = main(["service", "status"])
    assert rc == 1
    out = capsys.readouterr().out
    assert expected_state in out


def test_service_status_not_installed_exits_nonzero(isolated, capsys):
    from tend.cli import main
    rc = main(["service", "status"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "not-installed" in out

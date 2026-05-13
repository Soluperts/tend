"""`tend service install/uninstall` CLI."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TEND_HOME", str(tmp_path / "tend-home"))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0))
    return tmp_path


def test_service_install_writes_unit_file(isolated, capsys):
    from tend.cli import main
    rc = main(["service", "install"])
    assert rc == 0
    assert (isolated / ".config" / "systemd" / "user" / "tend.service").exists()
    out = capsys.readouterr().out
    assert "tend.service" in out


def test_service_install_force_overwrites(isolated):
    p = isolated / ".config" / "systemd" / "user" / "tend.service"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("OLD")
    from tend.cli import main
    rc = main(["service", "install", "--force"])
    assert rc == 0
    assert "OLD" not in p.read_text(encoding="utf-8")


def test_service_install_refuses_without_force_when_exists(isolated, capsys):
    p = isolated / ".config" / "systemd" / "user" / "tend.service"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("OLD")
    from tend.cli import main
    rc = main(["service", "install"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "force" in err.lower()


def test_service_uninstall_removes_unit(isolated):
    p = isolated / ".config" / "systemd" / "user" / "tend.service"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[Unit]")
    from tend.cli import main
    rc = main(["service", "uninstall"])
    assert rc == 0
    assert not p.exists()


def test_service_install_macos_exits_one(monkeypatch, isolated, capsys):
    monkeypatch.setattr("sys.platform", "darwin")
    from tend.cli import main
    rc = main(["service", "install"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "macOS" in err
    assert "sub-project #3" in err


# start / stop / status -----------------------------------------------------


def _seed_unit(tmp_path):
    p = tmp_path / ".config" / "systemd" / "user" / "tend.service"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[Unit]\n")
    return p


def test_service_start_runs_systemctl(monkeypatch, isolated):
    _seed_unit(isolated)
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    from tend.cli import main
    rc = main(["service", "start"])
    assert rc == 0
    assert ["systemctl", "--user", "start", "tend"] in calls


def test_service_stop_runs_systemctl(monkeypatch, isolated):
    _seed_unit(isolated)
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    from tend.cli import main
    rc = main(["service", "stop"])
    assert rc == 0
    assert ["systemctl", "--user", "stop", "tend"] in calls


def test_service_start_when_not_installed_exits_two(isolated, capsys):
    from tend.cli import main
    rc = main(["service", "start"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "tend service install" in err


def test_service_status_active_exits_zero(monkeypatch, isolated, capsys):
    _seed_unit(isolated)

    def fake_run(cmd, *a, **kw):
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return MagicMock(returncode=0, stdout="active\n", stderr="")
        return MagicMock(returncode=0, stdout="● tend.service\n   Active: active", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    from tend.cli import main
    rc = main(["service", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "active" in out


def test_service_status_inactive_exits_nonzero(monkeypatch, isolated, capsys):
    _seed_unit(isolated)

    def fake_run(cmd, *a, **kw):
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return MagicMock(returncode=3, stdout="inactive\n", stderr="")
        return MagicMock(returncode=3, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    from tend.cli import main
    rc = main(["service", "status"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "inactive" in out


def test_service_status_not_installed_exits_nonzero(isolated, capsys):
    from tend.cli import main
    rc = main(["service", "status"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "not-installed" in out


def test_service_start_macos_exits_one(monkeypatch, isolated, capsys):
    monkeypatch.setattr("sys.platform", "darwin")
    from tend.cli import main
    rc = main(["service", "start"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "macOS" in err


def test_service_status_macos_exits_one(monkeypatch, isolated, capsys):
    monkeypatch.setattr("sys.platform", "darwin")
    from tend.cli import main
    rc = main(["service", "status"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "macOS" in err

"""tend.service — systemd user-unit install/uninstall (Linux only)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


def test_render_substitutes_python_and_tend_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.service import render_unit
    body = render_unit(python="/opt/x/python", tend_home=tmp_path)
    assert "ExecStart=/opt/x/python -m tend" in body
    assert f"WorkingDirectory={tmp_path}" in body
    assert f"Environment=TEND_HOME={tmp_path}" in body


def test_unit_path_under_user_systemd(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tend.service import unit_path
    assert unit_path() == tmp_path / ".config" / "systemd" / "user" / "tend.service"


def test_install_writes_unit_and_calls_daemon_reload(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TEND_HOME", str(tmp_path / "tend-home"))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0))

    from tend.service import install
    written = install()
    assert written.exists()
    body = written.read_text(encoding="utf-8")
    assert "[Unit]" in body
    assert sys.executable in body


def test_install_force_overwrites_existing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0))

    from tend.service import install, unit_path
    p = unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("OLD")

    written = install(force=True)
    assert "OLD" not in written.read_text(encoding="utf-8")


def test_install_refuses_to_overwrite_without_force(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0))

    from tend.service import install, ServiceFileExists, unit_path
    p = unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("OLD")

    with pytest.raises(ServiceFileExists):
        install(force=False)


def test_uninstall_removes_unit_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0))

    from tend.service import install, uninstall, unit_path
    install(force=True)
    assert unit_path().exists()
    uninstall()
    assert not unit_path().exists()


def test_uninstall_when_not_installed_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0))
    from tend.service import uninstall
    uninstall()


def test_install_on_macos_raises_not_implemented(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "darwin")
    from tend.service import install, MacOSNotSupported
    with pytest.raises(MacOSNotSupported):
        install()


# start / stop / status -----------------------------------------------------


def _seed_unit(tmp_path):
    p = tmp_path / ".config" / "systemd" / "user" / "tend.service"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[Unit]\n")
    return p


def test_start_invokes_systemctl_start(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_unit(tmp_path)
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    from tend.service import start
    assert start() == 0
    assert calls == [["systemctl", "--user", "start", "tend"]]


def test_stop_invokes_systemctl_stop(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_unit(tmp_path)
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    from tend.service import stop
    assert stop() == 0
    assert calls == [["systemctl", "--user", "stop", "tend"]]


def test_start_raises_when_unit_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tend.service import start, ServiceNotInstalled
    with pytest.raises(ServiceNotInstalled):
        start()


def test_stop_raises_when_unit_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tend.service import stop, ServiceNotInstalled
    with pytest.raises(ServiceNotInstalled):
        stop()


def test_status_active(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_unit(tmp_path)

    def fake_run(cmd, *a, **kw):
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return MagicMock(returncode=0, stdout="active\n", stderr="")
        return MagicMock(returncode=0, stdout="● tend.service\n   Active: active", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    from tend.service import status
    state, details = status()
    assert state == "active"
    assert "Active: active" in details


def test_status_inactive(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_unit(tmp_path)

    def fake_run(cmd, *a, **kw):
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return MagicMock(returncode=3, stdout="inactive\n", stderr="")
        return MagicMock(returncode=3, stdout="● tend.service\n   Active: inactive (dead)", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    from tend.service import status
    state, _ = status()
    assert state == "inactive"


def test_status_not_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tend.service import status
    state, details = status()
    assert state == "not-installed"
    assert "no unit at" in details


def test_status_unknown_when_stdout_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_unit(tmp_path)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: MagicMock(returncode=4, stdout="", stderr=""),
    )
    from tend.service import status
    state, _ = status()
    assert state == "unknown"


def test_start_on_macos_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_unit(tmp_path)
    monkeypatch.setattr("sys.platform", "darwin")
    from tend.service import start, MacOSNotSupported
    with pytest.raises(MacOSNotSupported):
        start()


def test_status_on_macos_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "darwin")
    from tend.service import status, MacOSNotSupported
    with pytest.raises(MacOSNotSupported):
        status()

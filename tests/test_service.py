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

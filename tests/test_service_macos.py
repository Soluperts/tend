# SPDX-License-Identifier: MIT
"""Tests for tend.service macOS (launchd) branches."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tend import service


def test_unit_path_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    p = service.unit_path()
    assert p.name == "com.tend.daemon.plist"
    assert "LaunchAgents" in p.parts


def test_render_unit_macos_substitutes_placeholders(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    body = service.render_unit(python="/usr/bin/python3", tend_home=tmp_path)
    assert "/usr/bin/python3" in body
    assert str(tmp_path) in body
    assert "{{PYTHON}}" not in body
    assert "{{TEND_HOME}}" not in body


def test_install_macos_calls_launchctl_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    monkeypatch.setattr(service.paths, "tend_home", lambda: tmp_path)
    target = tmp_path / "LaunchAgents" / "com.tend.daemon.plist"
    monkeypatch.setattr(service, "unit_path", lambda: target)

    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        return MagicMock(returncode=0)
    monkeypatch.setattr(service.subprocess, "run", fake_run)

    out = service.install()

    assert out == target
    assert target.exists()
    # First call should be launchctl bootstrap.
    assert calls[0][0] == "launchctl"
    assert calls[0][1] == "bootstrap"
    assert calls[0][2] == f"gui/{os.getuid()}"


def test_uninstall_macos_calls_bootout_and_deletes(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    target = tmp_path / "com.tend.daemon.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("<plist/>")
    monkeypatch.setattr(service, "unit_path", lambda: target)

    calls = []
    monkeypatch.setattr(
        service.subprocess, "run",
        lambda args, **kw: (calls.append(args), MagicMock(returncode=0))[1],
    )

    service.uninstall()

    assert not target.exists()
    assert calls[0][0] == "launchctl"
    assert calls[0][1] == "bootout"


def test_start_macos_kickstart(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    target = tmp_path / "com.tend.daemon.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    monkeypatch.setattr(service, "unit_path", lambda: target)

    calls = []
    monkeypatch.setattr(
        service.subprocess, "run",
        lambda args, **kw: (calls.append(args), MagicMock(returncode=0))[1],
    )

    rc = service.start()

    assert rc == 0
    assert calls[0][:2] == ["launchctl", "kickstart"]


def test_stop_macos_kill_sigterm(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    target = tmp_path / "com.tend.daemon.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    monkeypatch.setattr(service, "unit_path", lambda: target)

    calls = []
    monkeypatch.setattr(
        service.subprocess, "run",
        lambda args, **kw: (calls.append(args), MagicMock(returncode=0))[1],
    )

    rc = service.stop()

    assert rc == 0
    assert calls[0][:3] == ["launchctl", "kill", "SIGTERM"]


def test_status_macos_parses_launchctl_print(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    target = tmp_path / "com.tend.daemon.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    monkeypatch.setattr(service, "unit_path", lambda: target)

    sample_print = """\
gui/501/com.tend.daemon = {
    active count = 1
    state = running
    program = /usr/local/bin/python3.11
}
"""
    monkeypatch.setattr(
        service.subprocess, "run",
        lambda args, **kw: MagicMock(returncode=0, stdout=sample_print, stderr=""),
    )

    state, details = service.status()
    assert state == "running"
    assert "com.tend.daemon" in details

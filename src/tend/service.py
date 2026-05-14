"""service-unit install/uninstall for tend.

Writes either a systemd user unit (Linux) or a launchd LaunchAgent
plist (macOS), and wraps `systemctl`/`launchctl` for start/stop/status.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from tend import paths


class ServiceFileExists(Exception):
    """Raised when install() would overwrite an existing unit file without force."""


class ServiceNotInstalled(Exception):
    """Raised when start/stop is called and no unit file exists."""


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _label() -> str:
    return "com.tend.daemon"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def unit_path() -> Path:
    if _is_macos():
        return Path.home() / "Library" / "LaunchAgents" / f"{_label()}.plist"
    return Path.home() / ".config" / "systemd" / "user" / "tend.service"


def render_unit(*, python: str, tend_home: Path) -> str:
    """Render the unit template with the two placeholder substitutions."""
    name = (
        "launchd/com.tend.daemon.plist.tmpl"
        if _is_macos()
        else "systemd/tend.service.tmpl"
    )
    body = files("tend._defaults").joinpath(name).read_text(encoding="utf-8")
    return (
        body
        .replace("{{PYTHON}}", python)
        .replace("{{TEND_HOME}}", str(tend_home))
    )


def install(*, force: bool = False) -> Path:
    """Write the unit file and register it with the service manager. Returns the path written."""
    target = unit_path()
    new_body = render_unit(python=sys.executable, tend_home=paths.tend_home())

    if (
        target.exists()
        and target.read_text(encoding="utf-8") != new_body
        and not force
    ):
        raise ServiceFileExists(
            f"{target} already exists; pass force=True to overwrite"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_body, encoding="utf-8")

    if _is_macos():
        # `bootstrap` is idempotent on most macOS versions; if it fails because
        # the agent is already loaded, fall back to `load`.
        r = subprocess.run(
            ["launchctl", "bootstrap", _domain(), str(target)],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            subprocess.run(
                ["launchctl", "load", str(target)],
                check=False,
            )
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    return target


def uninstall() -> None:
    """Remove the unit file (if present) and deregister from the service manager."""
    target = unit_path()
    if _is_macos():
        subprocess.run(
            ["launchctl", "bootout", f"{_domain()}/{_label()}"],
            check=False,
        )
        if target.exists():
            target.unlink()
    else:
        if target.exists():
            target.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


def _require_installed() -> None:
    if not unit_path().exists():
        raise ServiceNotInstalled(
            f"no unit at {unit_path()}; run `tend service install` first"
        )


def start() -> int:
    """Start the tend service. Returns the service manager's return code."""
    _require_installed()
    if _is_macos():
        return subprocess.run(
            ["launchctl", "kickstart", f"{_domain()}/{_label()}"],
            check=False,
        ).returncode
    return subprocess.run(
        ["systemctl", "--user", "start", "tend"], check=False,
    ).returncode


def stop() -> int:
    """Stop the tend service. Returns the service manager's return code."""
    _require_installed()
    if _is_macos():
        return subprocess.run(
            ["launchctl", "kill", "SIGTERM", f"{_domain()}/{_label()}"],
            check=False,
        ).returncode
    return subprocess.run(
        ["systemctl", "--user", "stop", "tend"], check=False,
    ).returncode


def status() -> tuple[str, str]:
    """Return (state, details).

    state ∈ {active, inactive, failed, activating, deactivating,
              running, not-installed, unknown}.
    details is a short multi-line summary from the service manager, or empty.
    """
    if not unit_path().exists():
        return ("not-installed", f"no unit at {unit_path()}")

    if _is_macos():
        r = subprocess.run(
            ["launchctl", "print", f"{_domain()}/{_label()}"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            return ("unknown", (r.stderr or r.stdout or "").strip())
        text = r.stdout or ""
        state = "unknown"
        for line in text.splitlines():
            if "state =" in line:
                state = line.split("=", 1)[1].strip()
                break
        return (state, text.strip())

    r = subprocess.run(
        ["systemctl", "--user", "is-active", "tend"],
        capture_output=True,
        text=True,
        check=False,
    )
    state = (r.stdout or "").strip() or "unknown"
    r2 = subprocess.run(
        ["systemctl", "--user", "status", "tend", "--no-pager", "-n", "0"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (state, (r2.stdout or "").strip())

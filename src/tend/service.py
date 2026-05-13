"""systemd user-unit install/uninstall for tend.

Linux only — macOS launchd support is sub-project #3 (macOS port).
"""

from __future__ import annotations

import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from tend import paths


class ServiceFileExists(Exception):
    """Raised when install() would overwrite an existing unit file without force."""


class ServiceNotInstalled(Exception):
    """Raised when start/stop is called and no unit file exists."""


class MacOSNotSupported(NotImplementedError):
    """Raised when install/uninstall/start/stop/status is called on macOS."""


def _is_macos() -> bool:
    return sys.platform == "darwin"


def unit_path() -> Path:
    """`~/.config/systemd/user/tend.service`."""
    return Path.home() / ".config" / "systemd" / "user" / "tend.service"


def render_unit(*, python: str, tend_home: Path) -> str:
    """Render the systemd unit template with the two placeholder substitutions."""
    tmpl_path = files("tend._defaults").joinpath("systemd/tend.service.tmpl")
    body = tmpl_path.read_text(encoding="utf-8")
    return (
        body
        .replace("{{PYTHON}}", python)
        .replace("{{TEND_HOME}}", str(tend_home))
    )


def install(*, force: bool = False) -> Path:
    """Write the unit file and run daemon-reload. Returns the path written."""
    if _is_macos():
        raise MacOSNotSupported(
            "macOS service install is not yet implemented (sub-project #3)"
        )

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
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        check=False,
    )
    return target


def uninstall() -> None:
    """Remove the unit file (if present) and run daemon-reload."""
    if _is_macos():
        raise MacOSNotSupported(
            "macOS service install is not yet implemented (sub-project #3)"
        )

    target = unit_path()
    if target.exists():
        target.unlink()
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        check=False,
    )


def _require_installed_linux() -> None:
    if _is_macos():
        raise MacOSNotSupported(
            "macOS service control is not yet implemented (sub-project #3)"
        )
    if not unit_path().exists():
        raise ServiceNotInstalled(
            f"no unit at {unit_path()}; run `tend service install` first"
        )


def start() -> int:
    """`systemctl --user start tend`. Returns systemctl's return code."""
    _require_installed_linux()
    return subprocess.run(
        ["systemctl", "--user", "start", "tend"],
        check=False,
    ).returncode


def stop() -> int:
    """`systemctl --user stop tend`. Returns systemctl's return code."""
    _require_installed_linux()
    return subprocess.run(
        ["systemctl", "--user", "stop", "tend"],
        check=False,
    ).returncode


def status() -> tuple[str, str]:
    """Return (state, details).

    state ∈ {active, inactive, failed, activating, deactivating, not-installed, unknown}.
    details is a short multi-line summary from `systemctl status`, or empty.
    """
    if _is_macos():
        raise MacOSNotSupported(
            "macOS service control is not yet implemented (sub-project #3)"
        )
    if not unit_path().exists():
        return ("not-installed", f"no unit at {unit_path()}")
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

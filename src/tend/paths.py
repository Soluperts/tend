"""Single source of truth for tend's filesystem paths.

Resolution order for the workspace root:
1. ``$TEND_HOME`` env var (taken literally — no ~ expansion).
2. ``~/.tend/`` (cross-platform default; macOS too).

This is a single-folder workspace — config + persona + skills + state +
logs all under one dotdir. Same shape as ``~/.ollama/`` or ``~/.docker/``.
We deliberately don't use ``~/.config/tend/``: XDG reserves ``~/.config/``
for config files, but tend has a real workspace (skills, scripts, state)
that would mis-fit there.

User-side functions are anchored at :func:`tend_home`. Wheel-side functions
read from the package's ``_defaults/`` tree via ``importlib.resources``.
"""

from __future__ import annotations

import atexit
import os
from contextlib import ExitStack
from functools import lru_cache
from importlib.resources import as_file, files
from pathlib import Path


_cleanup_stack = ExitStack()
atexit.register(_cleanup_stack.close)


@lru_cache(maxsize=1)
def _materialize_defaults_root() -> Path:
    """Return the on-disk path of the shipped ``_defaults`` tree.

    For unzipped wheels (pip/pipx/uv common case) this is a no-op —
    ``files()`` already points at a real directory. For zipped-wheel
    installs we materialize via ``as_file()`` into a tempdir, cached
    for the process lifetime; ``atexit`` cleans it up.
    """
    traversable = files("tend._defaults")
    return Path(_cleanup_stack.enter_context(as_file(traversable)))


def tend_home() -> Path:
    """Resolve the workspace root at every call so env overrides take effect."""
    override = os.environ.get("TEND_HOME")
    if override:
        return Path(override)
    return Path.home() / ".tend"


def user_skills_dir() -> Path:
    return tend_home() / "skills"


def workspace_bin_dir() -> Path:
    return tend_home() / "workspace" / "bin"


def soul_path() -> Path:
    return tend_home() / "soul.md"


def env_path() -> Path:
    return tend_home() / ".env"


def toml_path() -> Path:
    return tend_home() / "tend.toml"


def log_path() -> Path:
    return tend_home() / "logs" / "tend.log"


def fault_log_path() -> Path:
    return tend_home() / "logs" / "tend.faults.log"


def cron_root() -> Path:
    return tend_home() / "cron"


def sessions_dir() -> Path:
    return tend_home() / "sessions"


def skills_backup_root() -> Path:
    return tend_home() / "skills-backup"


def skills_quarantine_root() -> Path:
    return tend_home() / "skills-quarantined"


def version_marker_path() -> Path:
    return tend_home() / ".tend-version"


def critical_skills_dir() -> Path:
    return _materialize_defaults_root() / "critical-skills"


def shipped_skills_dir() -> Path:
    return _materialize_defaults_root() / "skills"


def shipped_soul_md() -> Path:
    return _materialize_defaults_root() / "soul.md"


def shipped_workspace_bin() -> Path:
    return _materialize_defaults_root() / "workspace" / "bin"


DEFAULT_SOUL_FALLBACK = (
    "You are a helpful voice assistant. Keep replies brief, "
    "conversational, plain prose."
)


def read_soul() -> str:
    """Resolve the persona text: user soul.md → shipped soul.md → hardcoded fallback."""
    user = soul_path()
    if user.exists():
        return user.read_text(encoding="utf-8")
    shipped = shipped_soul_md()
    try:
        return shipped.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return DEFAULT_SOUL_FALLBACK


def read_version_marker() -> str | None:
    """Return the workspace's .tend-version contents, or None if absent."""
    p = version_marker_path()
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()


def write_version_marker(version: str) -> None:
    """Write .tend-version, creating $TEND_HOME if missing."""
    p = version_marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(version, encoding="utf-8")


from enum import Enum
from packaging.version import InvalidVersion, Version


class WorkspaceState(Enum):
    MISSING = "missing"
    UNCLAIMED = "unclaimed"
    INITIALIZED = "initialized"
    FUTURE_VERSION = "future_version"


def _current_tend_version() -> Version:
    from tend import __version__
    return Version(__version__)


def detect_workspace_state() -> WorkspaceState:
    """Classify the current $TEND_HOME for boot-time gating."""
    home = tend_home()
    if not home.exists():
        return WorkspaceState.MISSING
    marker = read_version_marker()
    if marker is None:
        return WorkspaceState.UNCLAIMED
    try:
        marker_version = Version(marker)
    except InvalidVersion:
        return WorkspaceState.UNCLAIMED
    if marker_version > _current_tend_version():
        return WorkspaceState.FUTURE_VERSION
    return WorkspaceState.INITIALIZED


def migrate_workspace(from_version: str, to_version: str) -> None:
    """Apply any required workspace-layout migrations between two tend versions.

    For v0.1 this is a no-op. When v0.2 ships a layout change, branch on
    from_version here and apply the migration. Called from boot when
    .tend-version < current.
    """
    return

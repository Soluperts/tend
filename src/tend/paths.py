"""Single source of truth for tend's filesystem paths.

Resolution order for the workspace root:
1. ``$TEND_HOME`` env var (taken literally — no ~ expansion).
2. ``~/.config/tend/`` (cross-platform default; macOS too).

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
    return Path.home() / ".config" / "tend"


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

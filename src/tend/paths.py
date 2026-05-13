"""Single source of truth for tend's filesystem paths.

Resolution order for the workspace root:
1. ``$TEND_HOME`` env var (taken literally — no ~ expansion).
2. ``~/.config/tend/`` (cross-platform default; macOS too).

User-side functions are anchored at :func:`tend_home`. Wheel-side functions
read from the package's ``_defaults/`` tree via ``importlib.resources``.
"""

from __future__ import annotations

import os
from pathlib import Path


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

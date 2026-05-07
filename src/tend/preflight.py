"""Boot-time checks for optional dependencies. Each preflight returns a bool;
the caller decides whether to log/proceed/abort based on its own policy."""

from __future__ import annotations

import shutil
import subprocess

from loguru import logger


def claude_cli_preflight() -> bool:
    """Verify `claude` is installed and authenticated. Never raises.

    Returns True only if both `claude --version` and `claude auth status`
    return zero. Otherwise logs a warning and returns False — claude-cli
    workers will still error cleanly on first dispatch in that case.
    """
    if not shutil.which("claude"):
        logger.warning("preflight: `claude` CLI not on PATH; claude-cli workers will fail.")
        return False
    try:
        v = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if v.returncode != 0:
            logger.warning(f"preflight: `claude --version` failed: {v.stderr.strip()}")
            return False
        logger.info(f"preflight: claude version → {v.stdout.strip()}")
        a = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if a.returncode != 0:
            logger.warning(
                "preflight: `claude auth status` failed; run `claude auth login`."
            )
            return False
        return True
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"preflight: claude check raised {e!r}")
        return False

"""Boot-state integration tests: tend's behavior when $TEND_HOME is missing/unclaimed/future."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run_tend(tend_home: Path, extra_env=None) -> subprocess.CompletedProcess:
    """Run `python -m tend` with a controlled TEND_HOME; capture stdout/stderr/exit."""
    full_env = os.environ.copy()
    full_env["TEND_HOME"] = str(tend_home)
    # Strip API keys so we never accidentally hit cloud services from a test.
    for k in ("ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        full_env.pop(k, None)
    if extra_env:
        full_env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "tend"],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_tend_no_workspace_exits_with_message(tmp_path):
    """Running tend with a missing $TEND_HOME prints a setup hint and exits non-zero."""
    target = tmp_path / "missing"
    result = _run_tend(target)
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "tend setup" in output


def test_tend_populated_no_marker_prompts_adopt(tmp_path):
    """A populated $TEND_HOME without .tend-version → asks user to run tend setup."""
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "briefing").mkdir()
    result = _run_tend(tmp_path)
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "tend setup" in output
    assert "adopt" in output or "isn't initialized" in output


def test_tend_future_version_refuses(tmp_path):
    """A .tend-version > current refuses to boot."""
    (tmp_path / ".tend-version").write_text("999.0.0", encoding="utf-8")
    result = _run_tend(tmp_path)
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    # Either "future" or version-mismatch messaging — match on the
    # "upgrade tend" hint and the version number we wrote.
    assert "upgrade tend" in output
    assert "999.0.0" in output

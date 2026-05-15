# SPDX-License-Identifier: MIT
"""Smoke test that the built wheel contains every shipped default.

Run on every PR. Catches the common bug of adding a default skill and
forgetting to update package-data in pyproject.toml.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


EXPECTED_PATHS = [
    "tend/_defaults/soul.md",
    "tend/_defaults/critical-skills/heartbeat/SKILL.md",
    "tend/_defaults/critical-skills/schedule-watcher/SKILL.md",
    "tend/_defaults/critical-skills/schedule-watcher/bin/tick.py",
    "tend/_defaults/skills/briefing/SKILL.md",
    "tend/_defaults/skills/lunch-prep/SKILL.md",
    "tend/_defaults/skills/mail-triage/SKILL.md",
    "tend/_defaults/skills/meeting-prep/SKILL.md",
    "tend/_defaults/skills/post-deep-work/SKILL.md",
    "tend/_defaults/skills/routine-setup/SKILL.md",
    "tend/_defaults/skills/schedule-block/SKILL.md",
    "tend/_defaults/workspace/bin/gws-agenda.sh",
    "tend/_defaults/workspace/bin/gws-events-window.sh",
    "tend/_defaults/workspace/bin/gws-find-slot.sh",
    "tend/_defaults/workspace/bin/gws-recent-mail.sh",
    "tend/_defaults/workspace/bin/tend-schedule-event.sh",
]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    """Build a wheel into a temp dir and return its path."""
    repo_root = Path(__file__).resolve().parents[1]
    dist = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    wheels = list(dist.glob("tend-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def test_wheel_contains_all_expected_defaults(built_wheel):
    with zipfile.ZipFile(built_wheel) as zf:
        names = set(zf.namelist())
    missing = [p for p in EXPECTED_PATHS if p not in names]
    assert not missing, f"missing from wheel: {missing}"

# tend Workspace Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move tend from "runs only from a git clone" to "fully pip-installable." User state moves into `$TEND_HOME` (default `~/.tend/`); shipped defaults move into the wheel at `src/tend/_defaults/`; a `.tend-version` marker indicates an initialized workspace.

**Architecture:** A new `src/tend/paths.py` module centralizes every filesystem path. Shipped content (`heartbeat` + `schedule-watcher` as critical, plus 7 optional skills, `workspace/bin/` helpers, and `soul.md`) moves under `src/tend/_defaults/`. Critical skills stay in the wheel and are runtime-enumerated alongside `$TEND_HOME/skills/`, with user dir winning on name collision. Optional skills are copied to `$TEND_HOME/skills/` during `tend setup` (the wizard UX is sub-project #4 — this plan only implements the underlying primitives).

**Tech stack:** Python 3.11+, `pydantic-settings` v2 (`TomlConfigSettingsSource` + `DotEnvSettingsSource`), `importlib.resources.files()` for wheel-side reads, `shutil.copytree` for seeding, `pytest` + `monkeypatch` for tests.

**Prerequisites:**
- Read the spec at `docs/superpowers/specs/2026-05-12-tend-workspace-migration-design.md` end-to-end before starting.
- Read the existing `src/tend/config.py`, `src/tend/main.py`, `src/tend/session.py`, `src/tend/skills.py` so you understand current behavior.
- Be on a feature branch, not `main`. The execution skill creates a worktree for this; if not, run `git checkout -b workspace-migration` first.

**Project conventions** (from `docs/conventions.md` and `CLAUDE.md`):
- Constructor injection — no module-level singletons (Settings is the one allowed exception).
- TDD: write failing test → implement → watch pass → commit.
- Frequent commits per Conventional Commits style: `feat:`, `fix:`, `refactor:`, `test:`, `chore:`.
- Default to no comments; only when WHY is non-obvious.

**File structure changes:**

New files (created in tasks below):
- `src/tend/paths.py`
- `src/tend/skill_update.py`
- `src/tend/_defaults/critical-skills/heartbeat/SKILL.md`
- `src/tend/_defaults/critical-skills/schedule-watcher/` (moved from `skills/schedule-watcher/`)
- `src/tend/_defaults/skills/{briefing,lunch-prep,mail-triage,meeting-prep,post-deep-work,routine-setup,schedule-block}/` (moved)
- `src/tend/_defaults/workspace/bin/` (moved from `workspace/bin/`)
- `src/tend/_defaults/soul.md` (moved from `soul.md`)
- `tests/test_paths.py`
- `tests/test_workspace_lifecycle.py`
- `tests/test_skills_enumeration.py`
- `tests/test_skill_update.py`
- `tests/test_packaging.py`

Modified files:
- `src/tend/config.py`
- `src/tend/session.py`
- `src/tend/main.py`
- `src/tend/skills.py`
- `src/tend/audio/hub.py`
- `src/tend/scheduler.py`
- `src/tend/webhook.py`
- `src/tend/workers/general.py`
- `pyproject.toml`
- `.gitignore`

Deleted at end:
- `tend.toml` (repo root)
- `soul.md` (repo root)
- `skills/` (repo root)
- `workspace/` (repo root)

---

## Task 1: Add `paths.tend_home()` with env override

**Files:**
- Create: `src/tend/paths.py`
- Test: `tests/test_paths.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_paths.py`:

```python
"""Tests for tend.paths — single source of truth for filesystem paths."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_tend_home_default(monkeypatch):
    monkeypatch.delenv("TEND_HOME", raising=False)
    from tend.paths import tend_home
    assert tend_home() == Path.home() / ".tend"


def test_tend_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import tend_home
    assert tend_home() == tmp_path


def test_tend_home_env_expanded(monkeypatch):
    """$TEND_HOME with a leading ~ stays literal — we don't expand."""
    monkeypatch.setenv("TEND_HOME", "~/custom")
    from tend.paths import tend_home
    assert tend_home() == Path("~/custom")  # no auto-expansion; that's the user's job
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/pi/hasat
pytest tests/test_paths.py -v
```

Expected: `ModuleNotFoundError: No module named 'tend.paths'`.

- [ ] **Step 3: Implement `paths.py`**

Create `src/tend/paths.py`:

```python
"""Single source of truth for tend's filesystem paths.

Resolution order for the workspace root:
1. `$TEND_HOME` env var (no expansion of ~ — taken literally).
2. `~/.tend/` (cross-platform default; macOS too).

All other path functions are anchored at `tend_home()` (user-side) or at the
wheel's `_defaults/` directory (shipped-side) via `importlib.resources`.
"""

from __future__ import annotations

import os
from pathlib import Path


def tend_home() -> Path:
    """The workspace root. Resolved at every call so env-var overrides take effect."""
    override = os.environ.get("TEND_HOME")
    if override:
        return Path(override)
    return Path.home() / ".tend"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_paths.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/paths.py tests/test_paths.py
git commit -m "feat(paths): introduce paths.tend_home() with env override"
```

---

## Task 2: User-side path functions anchored at `tend_home()`

**Files:**
- Modify: `src/tend/paths.py`
- Modify: `tests/test_paths.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_paths.py`:

```python
def test_user_side_paths_anchored(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import (
        user_skills_dir, workspace_bin_dir, soul_path, env_path, toml_path,
        log_path, fault_log_path, cron_root, sessions_dir,
        skills_backup_root, version_marker_path,
    )
    assert user_skills_dir() == tmp_path / "skills"
    assert workspace_bin_dir() == tmp_path / "workspace" / "bin"
    assert soul_path() == tmp_path / "soul.md"
    assert env_path() == tmp_path / ".env"
    assert toml_path() == tmp_path / "tend.toml"
    assert log_path() == tmp_path / "logs" / "tend.log"
    assert fault_log_path() == tmp_path / "logs" / "tend.faults.log"
    assert cron_root() == tmp_path / "cron"
    assert sessions_dir() == tmp_path / "sessions"
    assert skills_backup_root() == tmp_path / "skills-backup"
    assert version_marker_path() == tmp_path / ".tend-version"
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_paths.py::test_user_side_paths_anchored -v
```

Expected: `ImportError: cannot import name 'user_skills_dir' from 'tend.paths'`.

- [ ] **Step 3: Add path functions to `paths.py`**

Append to `src/tend/paths.py`:

```python
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
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_paths.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/paths.py tests/test_paths.py
git commit -m "feat(paths): add user-side path functions anchored at TEND_HOME"
```

---

## Task 3: Wheel-side path functions via `importlib.resources`

**Files:**
- Modify: `src/tend/paths.py`
- Modify: `tests/test_paths.py`
- Create empty: `src/tend/_defaults/__init__.py` (so the package is importable for tests)

- [ ] **Step 1: Write failing test**

Append to `tests/test_paths.py`:

```python
def test_wheel_side_paths_return_real_paths():
    """importlib.resources.files() must resolve to real Paths the runtime can use."""
    from tend.paths import (
        critical_skills_dir, shipped_skills_dir,
        shipped_soul_md, shipped_workspace_bin,
    )
    # Each function returns a real Path. The actual content under it
    # arrives in later tasks; for now we only assert the resolution shape.
    assert isinstance(critical_skills_dir(), Path)
    assert isinstance(shipped_skills_dir(), Path)
    assert isinstance(shipped_soul_md(), Path)
    assert isinstance(shipped_workspace_bin(), Path)

    # Sanity check: the paths point inside the tend package.
    import tend
    pkg_root = Path(tend.__file__).resolve().parent
    assert pkg_root in critical_skills_dir().parents or pkg_root == critical_skills_dir().parent.parent


def test_critical_skills_dir_caches():
    """Repeated calls return the same Path (process-lifetime cache for the zipped-wheel case)."""
    from tend.paths import critical_skills_dir
    a = critical_skills_dir()
    b = critical_skills_dir()
    assert a == b
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_paths.py::test_wheel_side_paths_return_real_paths -v
```

Expected: `ImportError`.

- [ ] **Step 3: Create the `_defaults` package shell and add wheel-side functions**

Create `src/tend/_defaults/__init__.py` (empty, so `importlib.resources.files("tend._defaults")` works):

```python
"""Shipped defaults (persona, seed skills, helper scripts).

Read at runtime via importlib.resources; never imported as Python code.
"""
```

Append to `src/tend/paths.py`:

```python
from contextlib import ExitStack
from functools import lru_cache
from importlib.resources import as_file, files


_cleanup_stack = ExitStack()
import atexit
atexit.register(_cleanup_stack.close)


@lru_cache(maxsize=1)
def _materialize_defaults_root() -> Path:
    """Return the on-disk path of the shipped `_defaults` tree.

    For unzipped wheels (the pip/pipx/uv common case) this is a no-op —
    `files()` already points at a real directory. For zipped-wheel
    installs we materialize via `as_file()` into a tempdir cached for
    the process lifetime; `atexit` cleans it up.
    """
    traversable = files("tend._defaults")
    return Path(_cleanup_stack.enter_context(as_file(traversable)))


def critical_skills_dir() -> Path:
    return _materialize_defaults_root() / "critical-skills"


def shipped_skills_dir() -> Path:
    return _materialize_defaults_root() / "skills"


def shipped_soul_md() -> Path:
    return _materialize_defaults_root() / "soul.md"


def shipped_workspace_bin() -> Path:
    return _materialize_defaults_root() / "workspace" / "bin"
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_paths.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/paths.py src/tend/_defaults/__init__.py tests/test_paths.py
git commit -m "feat(paths): add wheel-side path functions via importlib.resources"
```

---

## Task 4: Move heartbeat skill from inline string to `_defaults/critical-skills/`

**Files:**
- Create: `src/tend/_defaults/critical-skills/heartbeat/SKILL.md`

- [ ] **Step 1: Create the heartbeat skill file**

Create `src/tend/_defaults/critical-skills/heartbeat/SKILL.md` with the body currently inlined at `main.py:56-74`:

```markdown
---
name: heartbeat
description: Periodic silent check-in. Review pending work and announce only when something genuinely needs the user's attention.
silent_default: true
---

# Heartbeat

You are running on the heartbeat tick. By default, exit silently.

Only announce if there is something the user genuinely wants to know
right now and would not have heard otherwise. Examples that justify an
announcement: a follow-up deadline arrived, a long-running task you
started earlier finished while the user was away.

If you have nothing worth surfacing, end your run with the literal
phrase "(nothing to surface)" as your last assistant message.
```

- [ ] **Step 2: Sanity-check the file**

```bash
ls -la src/tend/_defaults/critical-skills/heartbeat/SKILL.md
head -1 src/tend/_defaults/critical-skills/heartbeat/SKILL.md
```

Expected: file exists; first line is `---`.

- [ ] **Step 3: Commit**

```bash
git add src/tend/_defaults/critical-skills/heartbeat/SKILL.md
git commit -m "feat(defaults): move heartbeat skill into _defaults/critical-skills/

The inline HEARTBEAT_SKILL_BODY string in main.py will be removed in a
later task. This file is read at runtime via importlib.resources."
```

---

## Task 5: Move schedule-watcher into `_defaults/critical-skills/`

**Files:**
- Move: `skills/schedule-watcher/` → `src/tend/_defaults/critical-skills/schedule-watcher/`

- [ ] **Step 1: Move the directory with git mv**

```bash
mkdir -p src/tend/_defaults/critical-skills
git mv skills/schedule-watcher src/tend/_defaults/critical-skills/schedule-watcher
```

- [ ] **Step 2: Verify structure**

```bash
ls -la src/tend/_defaults/critical-skills/schedule-watcher/
```

Expected: includes `SKILL.md` and `bin/`.

- [ ] **Step 3: Commit**

```bash
git commit -m "feat(defaults): move schedule-watcher into _defaults/critical-skills/"
```

---

## Task 6: Move optional skills into `_defaults/skills/`

**Files:**
- Move: `skills/{briefing,lunch-prep,mail-triage,meeting-prep,post-deep-work,routine-setup,schedule-block}/` → `src/tend/_defaults/skills/`

- [ ] **Step 1: Move the seven optional skills**

```bash
mkdir -p src/tend/_defaults/skills
for skill in briefing lunch-prep mail-triage meeting-prep post-deep-work routine-setup schedule-block; do
  git mv "skills/$skill" "src/tend/_defaults/skills/$skill"
done
```

- [ ] **Step 2: Verify the move and that the source `skills/` directory is now empty**

```bash
ls src/tend/_defaults/skills/
ls skills/ 2>/dev/null || echo "skills/ already gone"
```

Expected: 7 directories listed under the new location; `skills/` empty or already removed.

- [ ] **Step 3: Remove the now-empty repo-root `skills/` directory**

```bash
rmdir skills 2>/dev/null || true
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat(defaults): move optional shipped skills into _defaults/skills/

Moves briefing, lunch-prep, mail-triage, meeting-prep, post-deep-work,
routine-setup, schedule-block. The REPO_SKILL_NAMES tuple in main.py
becomes obsolete and will be removed in a later task."
```

---

## Task 7: Move `workspace/bin/` and `soul.md` into `_defaults/`

**Files:**
- Move: `workspace/bin/` → `src/tend/_defaults/workspace/bin/`
- Move: `soul.md` → `src/tend/_defaults/soul.md`

- [ ] **Step 1: Move workspace/bin**

```bash
mkdir -p src/tend/_defaults/workspace
git mv workspace/bin src/tend/_defaults/workspace/bin
rmdir workspace 2>/dev/null || true
```

- [ ] **Step 2: Move soul.md**

```bash
git mv soul.md src/tend/_defaults/soul.md
```

- [ ] **Step 3: Verify**

```bash
ls src/tend/_defaults/workspace/bin/
head -3 src/tend/_defaults/soul.md
ls workspace 2>/dev/null || echo "workspace/ gone"
ls soul.md 2>/dev/null || echo "repo-root soul.md gone"
```

Expected: 5 helper scripts under bin; soul.md exists at new location; both repo-root sources gone.

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(defaults): move soul.md and workspace/bin into _defaults/"
```

---

## Task 8: Update `pyproject.toml` to ship `_defaults/`

**Files:**
- Modify: `pyproject.toml`
- Modify: `.gitignore`

- [ ] **Step 1: Add the package-data declaration**

Edit `pyproject.toml`. Find the `[tool.setuptools.packages.find]` block (currently around line 30-32):

```toml
[tool.setuptools.packages.find]
where = ["src"]
```

Add immediately below it:

```toml
[tool.setuptools.package-data]
"tend._defaults" = ["**/*"]
```

- [ ] **Step 2: Add `.tend-dev/` to `.gitignore`**

Append to `.gitignore`:

```
# Dev-mode TEND_HOME location (set with TEND_HOME=$PWD/.tend-dev)
.tend-dev/
```

- [ ] **Step 3: Verify the wheel builds and includes the defaults**

```bash
rm -rf dist/ build/
python -m build --wheel 2>&1 | tail -5
unzip -l dist/tend-*.whl | grep _defaults | head -20
```

Expected: wheel builds; output lists files under `tend/_defaults/critical-skills/`, `tend/_defaults/skills/`, `tend/_defaults/workspace/bin/`, and `tend/_defaults/soul.md`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml .gitignore
git commit -m "build: include src/tend/_defaults/ in the wheel via package-data"
```

---

## Task 9: Add `tests/test_packaging.py` to catch missing package-data regressions

**Files:**
- Create: `tests/test_packaging.py`

- [ ] **Step 1: Write the test**

Create `tests/test_packaging.py`:

```python
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
```

- [ ] **Step 2: Run the test**

```bash
pytest tests/test_packaging.py -v
```

Expected: 1 passed (may take ~10 seconds due to wheel build).

- [ ] **Step 3: Commit**

```bash
git add tests/test_packaging.py
git commit -m "test(packaging): assert built wheel contains every shipped default"
```

---

## Task 10: Add `paths.read_soul()` with three-tier fallback

**Files:**
- Modify: `src/tend/paths.py`
- Modify: `tests/test_paths.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_paths.py`:

```python
def test_read_soul_user_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    (tmp_path / "soul.md").write_text("MY CUSTOM PERSONA", encoding="utf-8")
    from tend.paths import read_soul
    assert read_soul() == "MY CUSTOM PERSONA"


def test_read_soul_shipped_fallback(monkeypatch, tmp_path):
    """No user soul.md → falls back to the shipped one."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import read_soul
    text = read_soul()
    # Shipped soul.md starts with "You are Tend, ..." per the moved file.
    assert "Tend" in text or "tend" in text


def test_read_soul_hardcoded_fallback(monkeypatch, tmp_path):
    """If both user and shipped are missing, return the hardcoded fallback."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend import paths
    from tend.paths import read_soul, DEFAULT_SOUL_FALLBACK
    # Force the shipped path to be a non-existent file.
    monkeypatch.setattr(paths, "shipped_soul_md", lambda: tmp_path / "does-not-exist.md")
    assert read_soul() == DEFAULT_SOUL_FALLBACK
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_paths.py::test_read_soul_user_wins -v
```

Expected: `ImportError: cannot import name 'read_soul'`.

- [ ] **Step 3: Add `read_soul` and `DEFAULT_SOUL_FALLBACK` to `paths.py`**

Append to `src/tend/paths.py`:

```python
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
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_paths.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/paths.py tests/test_paths.py
git commit -m "feat(paths): add read_soul() with user → shipped → fallback resolution"
```

---

## Task 11: Add `paths.read_version_marker()` and `write_version_marker()`

**Files:**
- Modify: `src/tend/paths.py`
- Modify: `tests/test_paths.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_paths.py`:

```python
def test_write_then_read_version_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import write_version_marker, read_version_marker
    write_version_marker("0.1.0")
    assert read_version_marker() == "0.1.0"


def test_read_version_marker_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import read_version_marker
    assert read_version_marker() is None


def test_write_version_marker_creates_parent(monkeypatch, tmp_path):
    """write_version_marker creates $TEND_HOME if needed."""
    target = tmp_path / "deep" / "tree"
    monkeypatch.setenv("TEND_HOME", str(target))
    from tend.paths import write_version_marker, version_marker_path
    write_version_marker("0.1.0")
    assert version_marker_path().exists()
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_paths.py::test_write_then_read_version_marker -v
```

Expected: `ImportError: cannot import name 'write_version_marker'`.

- [ ] **Step 3: Add the marker functions**

Append to `src/tend/paths.py`:

```python
def read_version_marker() -> str | None:
    """Return the workspace's `.tend-version` contents, or None if absent."""
    p = version_marker_path()
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()


def write_version_marker(version: str) -> None:
    """Write `.tend-version`, creating `$TEND_HOME` if missing."""
    p = version_marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(version, encoding="utf-8")
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_paths.py -v
```

Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/paths.py tests/test_paths.py
git commit -m "feat(paths): add read/write version marker helpers"
```

---

## Task 12: Add `paths.detect_workspace_state()` returning one of four enum values

**Files:**
- Modify: `src/tend/paths.py`
- Modify: `tests/test_paths.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_paths.py`:

```python
def test_workspace_state_missing(monkeypatch, tmp_path):
    target = tmp_path / "does-not-exist"
    monkeypatch.setenv("TEND_HOME", str(target))
    from tend.paths import detect_workspace_state, WorkspaceState
    assert detect_workspace_state() == WorkspaceState.MISSING


def test_workspace_state_unclaimed(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "briefing").mkdir()
    from tend.paths import detect_workspace_state, WorkspaceState
    assert detect_workspace_state() == WorkspaceState.UNCLAIMED


def test_workspace_state_unclaimed_when_empty(monkeypatch, tmp_path):
    """An empty $TEND_HOME directory is still UNCLAIMED (not MISSING)."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import detect_workspace_state, WorkspaceState
    assert detect_workspace_state() == WorkspaceState.UNCLAIMED


def test_workspace_state_initialized(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import detect_workspace_state, WorkspaceState, write_version_marker
    write_version_marker("0.1.0")
    assert detect_workspace_state() == WorkspaceState.INITIALIZED


def test_workspace_state_future(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import detect_workspace_state, WorkspaceState, write_version_marker
    write_version_marker("999.0.0")
    assert detect_workspace_state() == WorkspaceState.FUTURE_VERSION
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_paths.py::test_workspace_state_missing -v
```

Expected: `ImportError: cannot import name 'detect_workspace_state'`.

- [ ] **Step 3: Implement the detection**

Append to `src/tend/paths.py`:

```python
from enum import Enum

from packaging.version import InvalidVersion, Version

# Lazy import to avoid circular dependency; tend.__version__ becomes the source.
def _current_tend_version() -> Version:
    from tend import __version__
    return Version(__version__)


class WorkspaceState(Enum):
    MISSING = "missing"
    UNCLAIMED = "unclaimed"
    INITIALIZED = "initialized"
    FUTURE_VERSION = "future_version"


def detect_workspace_state() -> WorkspaceState:
    """Classify the current `$TEND_HOME` for boot-time gating."""
    home = tend_home()
    if not home.exists():
        return WorkspaceState.MISSING
    marker = read_version_marker()
    if marker is None:
        return WorkspaceState.UNCLAIMED
    try:
        marker_version = Version(marker)
    except InvalidVersion:
        # Garbage in the marker; treat as unclaimed so setup can fix it.
        return WorkspaceState.UNCLAIMED
    if marker_version > _current_tend_version():
        return WorkspaceState.FUTURE_VERSION
    return WorkspaceState.INITIALIZED
```

- [ ] **Step 4: Ensure `tend.__version__` exists**

Check `src/tend/__init__.py` — it should export `__version__`. If it doesn't, add:

```bash
grep '__version__' src/tend/__init__.py || echo '__version__ = "0.1.0"' >> src/tend/__init__.py
```

Note: the version string should match `pyproject.toml`'s `version = "0.1.0"`. If they're already aligned (current state), no change needed. If not, sync them.

- [ ] **Step 5: Add `packaging` to dependencies (or use stdlib)**

Check `pyproject.toml` dependencies. `packaging` is a transitive dep of `pip` and almost always present, but list it explicitly:

```toml
dependencies = [
    # ... existing ...
    "packaging>=23",
]
```

Alternatively, write a tiny version-parser to avoid the dep — three integers + optional pre-release. For now, prefer the explicit `packaging` dep; it's reliable and ubiquitous.

- [ ] **Step 6: Run tests**

```bash
pip install -e .
pytest tests/test_paths.py -v
```

Expected: 17 passed.

- [ ] **Step 7: Commit**

```bash
git add src/tend/paths.py src/tend/__init__.py pyproject.toml tests/test_paths.py
git commit -m "feat(paths): add WorkspaceState + detect_workspace_state()"
```

---

## Task 13: Add `skills.enumerate_all_skills()` with dual-root merge

**Files:**
- Modify: `src/tend/skills.py`
- Create: `tests/test_skills_enumeration.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_skills_enumeration.py`:

```python
"""Tests for skill enumeration across the critical (wheel) + user roots."""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_skill(root: Path, name: str, description: str = "test skill") -> None:
    """Helper: write a minimal SKILL.md at root/name/."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def user_home(monkeypatch, tmp_path):
    """Redirect $TEND_HOME to a tmp dir for the test."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    return tmp_path


def test_critical_only(user_home, monkeypatch, tmp_path):
    """Empty user dir → runtime catalog is just the critical skills."""
    fake_critical = tmp_path / "fake_critical"
    fake_critical.mkdir()
    _write_skill(fake_critical, "heartbeat")
    _write_skill(fake_critical, "schedule-watcher")

    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: fake_critical)

    from tend.skills import enumerate_all_skills
    result = enumerate_all_skills()
    assert sorted(s.name for s in result) == ["heartbeat", "schedule-watcher"]


def test_user_alongside_critical(user_home, monkeypatch, tmp_path):
    """Disjoint names → result is the union."""
    fake_critical = tmp_path / "fake_critical"
    fake_critical.mkdir()
    _write_skill(fake_critical, "heartbeat")
    _write_skill(user_home / "skills", "briefing")
    _write_skill(user_home / "skills", "meal-plan")

    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: fake_critical)

    from tend.skills import enumerate_all_skills
    result = enumerate_all_skills()
    assert sorted(s.name for s in result) == ["briefing", "heartbeat", "meal-plan"]


def test_user_shadows_critical(user_home, monkeypatch, tmp_path, caplog):
    """Same-name dir in both → user wins; warning logged."""
    fake_critical = tmp_path / "fake_critical"
    fake_critical.mkdir()
    _write_skill(fake_critical, "heartbeat", description="shipped heartbeat")
    _write_skill(user_home / "skills", "heartbeat", description="user override")

    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: fake_critical)

    from tend.skills import enumerate_all_skills
    result = enumerate_all_skills()
    assert len(result) == 1
    assert result[0].name == "heartbeat"
    assert result[0].description == "user override"
    # Warning was logged. loguru emits to caplog when configured; the
    # important behavior is that the function returns the user version.


def test_enumerate_all_skills_sorted(user_home, monkeypatch, tmp_path):
    fake_critical = tmp_path / "fake_critical"
    fake_critical.mkdir()
    _write_skill(fake_critical, "zebra")
    _write_skill(user_home / "skills", "alpha")

    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: fake_critical)

    from tend.skills import enumerate_all_skills
    result = enumerate_all_skills()
    assert [s.name for s in result] == ["alpha", "zebra"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_skills_enumeration.py::test_critical_only -v
```

Expected: `ImportError: cannot import name 'enumerate_all_skills'`.

- [ ] **Step 3: Add `enumerate_all_skills()` to `skills.py`**

Append to `src/tend/skills.py` (near the bottom, after the existing `enumerate_skills`):

```python
def enumerate_all_skills() -> list[SkillInfo]:
    """Runtime catalog: critical skills (wheel) ∪ user skills, user wins on name collision."""
    from tend import paths  # local import to avoid a circular import at module load

    critical = enumerate_skills(paths.critical_skills_dir())
    user = enumerate_skills(paths.user_skills_dir())

    by_name: dict[str, SkillInfo] = {s.name: s for s in critical}
    for s in user:
        if s.name in by_name:
            logger.warning(
                f"user skill {s.name!r} at {s.path} shadows critical skill "
                f"at {by_name[s.name].path}"
            )
        by_name[s.name] = s

    return sorted(by_name.values(), key=lambda s: s.name)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_skills_enumeration.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/skills.py tests/test_skills_enumeration.py
git commit -m "feat(skills): add enumerate_all_skills with dual-root merge"
```

---

## Task 14: Add `skills.find_event_subscribers_all()` for the merged catalog

**Files:**
- Modify: `src/tend/skills.py`
- Modify: `tests/test_skills_enumeration.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_skills_enumeration.py`:

```python
def test_find_event_subscribers_all(user_home, monkeypatch, tmp_path):
    fake_critical = tmp_path / "fake_critical"
    fake_critical.mkdir()
    # Critical skill subscribed to "tick"
    (fake_critical / "heartbeat").mkdir()
    (fake_critical / "heartbeat" / "SKILL.md").write_text(
        "---\nname: heartbeat\ndescription: heartbeat\nevents:\n  - tick\n---\n\n# heartbeat\n",
        encoding="utf-8",
    )
    # User skill subscribed to "lunch"
    (user_home / "skills" / "lunch-prep").mkdir(parents=True)
    (user_home / "skills" / "lunch-prep" / "SKILL.md").write_text(
        "---\nname: lunch-prep\ndescription: lunch\nevents:\n  - lunch\n---\n\n# lunch\n",
        encoding="utf-8",
    )

    from tend import paths
    monkeypatch.setattr(paths, "critical_skills_dir", lambda: fake_critical)

    from tend.skills import find_event_subscribers_all
    tick_subs = find_event_subscribers_all("tick")
    lunch_subs = find_event_subscribers_all("lunch")
    unknown_subs = find_event_subscribers_all("nope")
    assert [s.name for s in tick_subs] == ["heartbeat"]
    assert [s.name for s in lunch_subs] == ["lunch-prep"]
    assert unknown_subs == []
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_skills_enumeration.py::test_find_event_subscribers_all -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement**

Append to `src/tend/skills.py`:

```python
def find_event_subscribers_all(kind: str) -> list[SkillInfo]:
    """All skills in the merged runtime catalog whose ``events:`` list includes ``kind``."""
    return [s for s in enumerate_all_skills() if kind in s.events]
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_skills_enumeration.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/skills.py tests/test_skills_enumeration.py
git commit -m "feat(skills): add find_event_subscribers_all for the merged catalog"
```

---

## Task 15: Add `skills.enumerate_installable_skills()`

**Files:**
- Modify: `src/tend/skills.py`
- Modify: `tests/test_skills_enumeration.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_skills_enumeration.py`:

```python
def test_enumerate_installable_excludes_installed(user_home, monkeypatch, tmp_path):
    """Shipped optional catalog minus what the user already has."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing")
    _write_skill(fake_shipped, "mail-triage")
    _write_skill(fake_shipped, "weather")

    # User already has briefing.
    _write_skill(user_home / "skills", "briefing")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skills import enumerate_installable_skills
    result = enumerate_installable_skills()
    assert sorted(s.name for s in result) == ["mail-triage", "weather"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_skills_enumeration.py::test_enumerate_installable_excludes_installed -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement**

Append to `src/tend/skills.py`:

```python
def enumerate_installable_skills() -> list[SkillInfo]:
    """Shipped optional skills not yet present in the user's $TEND_HOME/skills/."""
    from tend import paths

    shipped = enumerate_skills(paths.shipped_skills_dir())
    user_names = {s.name for s in enumerate_skills(paths.user_skills_dir())}
    return [s for s in shipped if s.name not in user_names]
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_skills_enumeration.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/skills.py tests/test_skills_enumeration.py
git commit -m "feat(skills): add enumerate_installable_skills"
```

---

## Task 16: Switch `Hub` to use `enumerate_all_skills` and drop the `skills_root` arg

**Files:**
- Modify: `src/tend/audio/hub.py`
- Modify: `src/tend/main.py`
- Modify: `tests/audio/test_hub.py` (if it exists)

- [ ] **Step 1: Read current Hub signature**

```bash
grep -n 'def __init__\|skills_root' src/tend/audio/hub.py | head -10
```

Note the current signature: `skills_root` is a constructor kwarg. We remove it; `Hub` calls `enumerate_all_skills()` internally where needed.

- [ ] **Step 2: Update Hub**

Edit `src/tend/audio/hub.py`. Remove `skills_root` from the `__init__` signature. Any internal use of `self._skills_root` becomes a call to `enumerate_all_skills()` at the point of use. Concretely:

- Remove the `skills_root` parameter from `__init__`.
- Remove the `self._skills_root = skills_root` line.
- Find anywhere that uses `self._skills_root` (likely in a method that builds the available-skills XML for the brain). Replace `enumerate_skills(self._skills_root)` with `enumerate_all_skills()`.

Import at top of `hub.py`:

```python
from tend.skills import enumerate_all_skills, format_catalog_xml
```

- [ ] **Step 3: Update main.py wiring**

Edit `src/tend/main.py` around line 225. Currently:

```python
hub = Hub(
    "hub", bus=runner.bus, settings=settings,
    stt=stt, tts=tts, tts_sample_rate=tts_rate,
    brain=brain, announcer=announcer,
    skills_root=Path.home() / ".tend" / "skills",
)
```

Change to:

```python
hub = Hub(
    "hub", bus=runner.bus, settings=settings,
    stt=stt, tts=tts, tts_sample_rate=tts_rate,
    brain=brain, announcer=announcer,
)
```

- [ ] **Step 4: Update any test that passes `skills_root` to Hub**

```bash
grep -rn 'skills_root' tests/ src/tend/
```

For each test file that constructs `Hub(...)` with `skills_root=...`, remove that kwarg. If the test relied on a custom skills root, switch to `monkeypatch.setenv("TEND_HOME", str(tmp_path))` + populating `tmp_path / "skills/<name>"`.

- [ ] **Step 5: Run hub tests + full test suite**

```bash
pytest tests/audio/ -v
pytest -v
```

Expected: all passing (or pre-existing failures only).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(hub): use enumerate_all_skills; drop skills_root constructor arg"
```

---

## Task 17: Switch event dispatch + Scheduler to use `find_event_subscribers_all`

**Files:**
- Modify: `src/tend/dispatch.py`
- Modify: `src/tend/scheduler.py`
- Modify: `src/tend/main.py`
- Modify: `tests/test_dispatch.py`
- Modify: `tests/test_scheduler.py`

Two related changes in one task because they're coupled: `dispatch.py` is the shared event fan-out helper used by both the scheduler (cron event-mode jobs) and the webhook (`POST /event`). It currently takes a `skills_root: Path` and calls the single-root `find_event_subscribers(skills_root, kind)`. After migration it uses the merged catalog and drops the parameter, which in turn lets every caller drop the kwarg they pass.

- [ ] **Step 1: Update `dispatch.py`**

Open `src/tend/dispatch.py`. Drop the `skills_root` parameter from `dispatch_event()` (around line 23). Inside, replace `find_event_subscribers(skills_root, kind)` (around line 31) with `find_event_subscribers_all(kind)`:

```python
from tend.skills import find_event_subscribers_all
# ... drop the `from tend.skills import find_event_subscribers` import if it becomes unused
```

The new signature has no `skills_root` parameter at all. Callers stop passing it.

- [ ] **Step 2: Update Scheduler**

Edit `src/tend/scheduler.py`:

- Drop `skills_root` from `__init__` (line 44).
- Drop the `self._skills_root = skills_root` line (line 51).
- At line 241 (the call site that does `skills_root=self._skills_root`): drop that kwarg — `dispatch_event` no longer accepts it.

Import:

```python
from tend.skills import find_event_subscribers_all
```

(Only needed if the scheduler still calls `find_event_subscribers_all` directly anywhere — most uses route through `dispatch_event` after this task.)

- [ ] **Step 3: Update main.py wiring** around line 192-202. Currently:

```python
scheduler = Scheduler(
    "scheduler",
    bus=runner.bus,
    store=cron_store,
    dispatch=lambda target, payload: scheduler.request_task(
        target, payload=payload,
    ),
    skills_root=Path.home() / ".tend" / "skills",
    default_tz=settings.timezone or "UTC",
    missed_at_policy=settings.scheduler.missed_at_policy,
)
```

Drop the `skills_root` kwarg:

```python
scheduler = Scheduler(
    "scheduler",
    bus=runner.bus,
    store=cron_store,
    dispatch=lambda target, payload: scheduler.request_task(
        target, payload=payload,
    ),
    default_tz=settings.timezone or "UTC",
    missed_at_policy=settings.scheduler.missed_at_policy,
)
```

- [ ] **Step 4: Update tests**

```bash
grep -n 'skills_root' tests/test_scheduler.py tests/test_dispatch.py
```

For each test constructing `Scheduler(...)` with `skills_root=...` or calling `dispatch_event(..., skills_root=...)`, remove the kwarg. If the test needs to control which skills are visible, use `monkeypatch.setenv("TEND_HOME", str(tmp_path))` + populate the dir.

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_dispatch.py tests/test_scheduler.py -v
pytest -v
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(dispatch+scheduler): use find_event_subscribers_all; drop skills_root"
```

---

## Task 18: Switch `webhook.build_app` to drop the `skills_root` arg

**Files:**
- Modify: `src/tend/webhook.py`
- Modify: `src/tend/main.py`
- Modify: `tests/test_webhook.py`

- [ ] **Step 1: Read current signature**

```bash
grep -n 'def build_app\|skills_root' src/tend/webhook.py
```

- [ ] **Step 2: Update `build_app`**

In `src/tend/webhook.py`:

- Drop `skills_root` from `build_app`'s parameters (around line 27).
- Inside `build_app`, find the `dispatch_event(...)` call (around line 70-72): drop the `skills_root=skills_root` kwarg — that parameter was removed from `dispatch_event` in task 17.

No need to import `find_event_subscribers_all` directly — the webhook routes through `dispatch_event`.

- [ ] **Step 3: Update main.py wiring** around line 245-252:

```python
webhook_app = build_app(
    token=settings.tend_webhook_token or "",
    announcer=announcer,
    dispatch=lambda target, payload: scheduler.request_task(
        target, payload=payload,
    ),
)
```

(Remove the `skills_root=...` kwarg.)

- [ ] **Step 4: Update tests**

```bash
grep -n 'skills_root' tests/test_webhook.py
```

Remove `skills_root` from any `build_app(...)` call in tests; redirect via `monkeypatch.setenv("TEND_HOME", str(tmp_path))` where needed.

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_webhook.py -v
pytest -v
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(webhook): use find_event_subscribers_all; drop skills_root"
```

---

## Task 19: Drop `WorkerConfig.skills_dir`; `GeneralWorker` uses the merged catalog

**Files:**
- Modify: `src/tend/config.py`
- Modify: `src/tend/workers/general.py`
- Modify: `tests/workers/test_general.py`
- Modify: `tests/test_config_workers.py`

- [ ] **Step 1: Read current state**

```bash
grep -n 'skills_dir' src/tend/config.py src/tend/workers/general.py
```

- [ ] **Step 2: Remove `skills_dir` from `WorkerConfig`**

Edit `src/tend/config.py` lines around 34-39 (the `skills_dir` field with its long comment). Delete the field entirely. Also delete the `workspace_dir` field if its only purpose was to be overridable — but **keep** `workspace_dir`; it's used for the worker's CWD and is genuinely a per-worker concern, not a skills concern. Only `skills_dir` goes.

The block becomes:

```python
class WorkerConfig(BaseModel):
    """Per-worker overrides loaded from `[workers.<name>]` blocks in tend.toml."""

    model: str | None = None
    setting_sources: str = "user"
    allowed_tools: list[str] = []
    mcp_config_path: str | None = None
    # Persistent workspace where the worker builds and accumulates artifacts.
    # Resolved against `~` if it starts with `~`. None → $TEND_HOME/workspace/.
    workspace_dir: str | None = None
```

- [ ] **Step 3: Update `GeneralWorker`**

In `src/tend/workers/general.py`, find any reference to `config.skills_dir` or the `Path.home() / ".tend" / "skills"` fallback. Replace with `enumerate_all_skills()` directly:

```python
from tend.skills import enumerate_all_skills, format_catalog_xml
```

Where the worker was building a catalog from a configured skills dir, switch to:

```python
catalog = enumerate_all_skills()
catalog_xml = format_catalog_xml(catalog)
```

- [ ] **Step 4: Update tests**

```bash
grep -rn 'skills_dir' tests/
```

Remove `skills_dir=...` from any `WorkerConfig(...)` test construction. Tests that need to control the worker's view of skills use `monkeypatch.setenv("TEND_HOME", str(tmp_path))` + populate `tmp_path / "skills"`.

- [ ] **Step 5: Run tests**

```bash
pytest tests/workers/ tests/test_config_workers.py -v
pytest -v
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(workers): drop WorkerConfig.skills_dir; use merged catalog"
```

---

## Task 20: Drop `soul_path` from `Settings`; route through `paths.read_soul()`

**Files:**
- Modify: `src/tend/config.py`
- Modify: `src/tend/session.py`
- Modify: `src/tend/main.py`
- Modify: `tests/test_session.py`

- [ ] **Step 1: Remove `soul_path` field**

Edit `src/tend/config.py` line 116. Delete:

```python
soul_path: str = "soul.md"
```

- [ ] **Step 2: Update `SessionManager`**

Edit `src/tend/session.py`. Drop `soul_path` kwarg from `__init__`. Remove the `self._soul_path` attribute. Replace `_read_soul` with:

```python
def _read_soul(self) -> str:
    from tend import paths
    return paths.read_soul()
```

Delete the existing `DEFAULT_SOUL` module-level constant (it now lives in `paths.py` as `DEFAULT_SOUL_FALLBACK`).

- [ ] **Step 3: Update main.py wiring** around line 228-234:

```python
session_manager = SessionManager(
    brain=brain,
    hub=hub,
    reset_time=settings.daily_reset_time,
    timezone=settings.timezone,
)
```

(Remove the `soul_path=settings.soul_path` kwarg.)

- [ ] **Step 4: Update tests**

```bash
grep -n 'soul_path\|DEFAULT_SOUL' tests/test_session.py
```

For each test constructing `SessionManager(...)` with `soul_path=...`, remove the kwarg and set `TEND_HOME=tmp_path` then write `tmp_path / "soul.md"` with the desired persona. For any tests asserting on `DEFAULT_SOUL`, import `DEFAULT_SOUL_FALLBACK` from `tend.paths` instead.

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_session.py -v
pytest -v
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(session): drop soul_path; route through paths.read_soul()"
```

---

## Task 21: Wire `settings_customise_sources` to read `$TEND_HOME/{tend.toml,.env}`

**Files:**
- Modify: `src/tend/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write a failing test**

Append to `tests/test_config.py`:

```python
def test_settings_reads_user_toml(monkeypatch, tmp_path):
    """A non-default value in $TEND_HOME/tend.toml overrides the class default."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    (tmp_path / "tend.toml").write_text(
        'awake_timeout_s = 999\n',
        encoding="utf-8",
    )
    # Re-import so the new env var is picked up (the module-level
    # `settings = Settings()` was bound at first import).
    from tend.config import Settings
    s = Settings()
    assert s.awake_timeout_s == 999


def test_settings_reads_user_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=test-key\n", encoding="utf-8")
    from tend.config import Settings
    s = Settings()
    assert s.anthropic_api_key == "test-key"


def test_settings_no_user_files_uses_defaults(monkeypatch, tmp_path):
    """Absent $TEND_HOME/tend.toml + .env → class defaults apply."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.config import Settings
    s = Settings()
    assert s.whisper_model == "tiny.en"
    assert s.wake_threshold == 0.5
```

- [ ] **Step 2: Run to confirm test 1 fails** (currently `tend.toml` resolves from CWD)

```bash
pytest tests/test_config.py::test_settings_reads_user_toml -v
```

Expected: failure (either CWD-toml is picked up, or no override happens).

- [ ] **Step 3: Update `settings_customise_sources`**

Edit `src/tend/config.py`. Replace the `model_config` block and the `settings_customise_sources` method:

```python
class Settings(BaseSettings):
    """All tend settings. Field names are lowercase; env vars are TEND_<UPPERCASE>."""

    model_config = SettingsConfigDict(
        env_prefix="TEND_",
        extra="ignore",
        # env_file / toml_file resolved dynamically — see settings_customise_sources
    )

    # ... fields unchanged (minus soul_path, log_path, WorkerConfig.skills_dir already gone) ...

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        from pydantic_settings import DotEnvSettingsSource, TomlConfigSettingsSource
        from tend.paths import env_path, toml_path

        dotenv = DotEnvSettingsSource(settings_cls, env_file=str(env_path()))
        toml = TomlConfigSettingsSource(settings_cls, toml_file=str(toml_path()))
        return (init_settings, env_settings, dotenv, toml, file_secret_settings)
```

- [ ] **Step 4: Also drop `log_path` field** (now lives in `paths.log_path()`)

Delete line 119 in the original file:

```python
log_path: str = "/tmp/tend.log"
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_config.py -v
pytest -v
```

Expected: 3 new tests pass; existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(config): load env+toml from \$TEND_HOME via paths module

- Drop soul_path and log_path fields (now in paths.py)
- settings_customise_sources builds DotEnvSettingsSource + TomlConfigSettingsSource
  pointing at \$TEND_HOME/{.env,tend.toml}
- TomlConfigSettingsSource silently skips missing files"
```

---

## Task 22: Switch logging to `paths.log_path()` + `paths.fault_log_path()`

**Files:**
- Modify: `src/tend/main.py`

- [ ] **Step 1: Update `_setup_logging`** around lines 42-49:

```python
def _setup_logging() -> None:
    from tend import paths

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    log_target = paths.log_path()
    log_target.parent.mkdir(parents=True, exist_ok=True)
    logger.add(str(log_target), level="DEBUG", rotation="5 MB", retention=2)

    fault_target = paths.fault_log_path()
    fault_target.parent.mkdir(parents=True, exist_ok=True)
    fault_file = open(fault_target, "a", buffering=1)
    fault_file.write("\n--- tend start ---\n")
    faulthandler.enable(file=fault_file, all_threads=True)
```

And update `main()` (around line 271):

```python
def main() -> None:
    _setup_logging()
    from tend import paths
    logger.info(f"tend starting (log: {paths.log_path()})")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("tend stopped (KeyboardInterrupt)")
```

- [ ] **Step 2: Run smoke test**

```bash
TEND_HOME=/tmp/tend-smoke pytest tests/ -v
ls -la /tmp/tend-smoke/logs/ 2>/dev/null
```

Test suite passes (test code doesn't trigger `_setup_logging`); the logs/ dir is only created when main runs. The smoke test is a sanity check that we didn't break anything.

- [ ] **Step 3: Commit**

```bash
git add src/tend/main.py
git commit -m "refactor(logging): route log + faulthandler paths through paths.py"
```

---

## Task 23: Remove dead seeding code from `main.py`

**Files:**
- Modify: `src/tend/main.py`

- [ ] **Step 1: Delete the obsolete blocks**

Edit `src/tend/main.py`. Delete:

- Lines ~52-74: `HEARTBEAT_SKILL_BODY` constant + `_seed_heartbeat_skill()` function.
- Lines ~85-108: `_REPO_SKILLS_DIR`, `REPO_SKILL_NAMES`, `_seed_skill_from_repo` function.
- Lines ~111-132: `_REPO_WORKSPACE_BIN`, `_seed_workspace_bin` function.

Also delete the calls inside `_run()` (around line 239-242):

```python
# Seed heartbeat skill on first boot if missing.
_seed_heartbeat_skill()
for name in REPO_SKILL_NAMES:
    _seed_skill_from_repo(_REPO_SKILLS_DIR / name)
_seed_workspace_bin()
```

(Replaced by nothing — seeding moves to the `tend setup` wizard in sub-project #4. Critical skills live in the wheel and are enumerated directly.)

- [ ] **Step 2: Replace `Path.home() / ".tend"` callsites in `_run`**

Edit `src/tend/main.py`. Find the remaining hardcoded paths and replace with `paths` module:

```python
from tend import paths

# ...

store = SessionStore(root=paths.tend_home())
cron_store = CronStore(root=paths.tend_home())
```

(SessionStore and CronStore both take a `root` and add their own subpath. Their existing behavior of `root / "sessions"` and `root / "cron"` continues to work — they just now anchor at `$TEND_HOME` instead of `~/.tend`.)

- [ ] **Step 3: Run the test suite**

```bash
pytest -v
```

Expected: all passing. If any test references `_seed_*` or `_REPO_*` directly, remove those references.

- [ ] **Step 4: Commit**

```bash
git add src/tend/main.py
git commit -m "refactor(main): drop in-process seeding; rely on tend setup for workspace prep"
```

---

## Task 24: Boot-time workspace state check in `main()`

**Files:**
- Modify: `src/tend/main.py`
- Create: `tests/test_workspace_lifecycle.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_workspace_lifecycle.py`:

```python
"""Boot-state integration tests: tend's behavior when $TEND_HOME is missing/unclaimed/future."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def _run_tend(tend_home: Path, env=None) -> subprocess.CompletedProcess:
    """Run `python -m tend` with a controlled TEND_HOME; capture stdout/stderr/exit."""
    import os
    full_env = os.environ.copy()
    full_env["TEND_HOME"] = str(tend_home)
    # Strip API keys to ensure we don't accidentally connect.
    for k in ("ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        full_env.pop(k, None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "tend"],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=15,
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
    assert "tend" in output  # something about version mismatch
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
pytest tests/test_workspace_lifecycle.py -v
```

Expected: tests fail (currently tend boots regardless of state).

- [ ] **Step 3: Add state check in `main()`**

Edit `src/tend/main.py`. Update `main()`:

```python
def main() -> None:
    _setup_logging()
    from tend import paths
    from tend.paths import WorkspaceState, detect_workspace_state

    state = detect_workspace_state()
    if state == WorkspaceState.MISSING:
        print(
            f"No workspace at {paths.tend_home()}.\n"
            f"Run `tend setup` to initialize one.",
            file=sys.stderr,
        )
        sys.exit(1)
    if state == WorkspaceState.UNCLAIMED:
        print(
            f"Workspace at {paths.tend_home()} has files but isn't initialized.\n"
            f"Run `tend setup` to adopt it.",
            file=sys.stderr,
        )
        sys.exit(1)
    if state == WorkspaceState.FUTURE_VERSION:
        from tend.paths import read_version_marker
        print(
            f"Workspace at {paths.tend_home()} was written by tend "
            f"{read_version_marker()}.\nYou're running tend {__import__('tend').__version__}. "
            f"Upgrade tend or pick a different TEND_HOME.",
            file=sys.stderr,
        )
        sys.exit(1)

    logger.info(f"tend starting (log: {paths.log_path()})")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("tend stopped (KeyboardInterrupt)")
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_workspace_lifecycle.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/main.py tests/test_workspace_lifecycle.py
git commit -m "feat(main): refuse to boot without an initialized workspace

Adds boot-time WorkspaceState check; emits 'run tend setup' hint and
exits 1 for MISSING / UNCLAIMED, FUTURE_VERSION refuses with version
mismatch message. INITIALIZED proceeds normally."
```

---

## Task 25: Add a no-op `paths.migrate_workspace()` stub for future versions

**Files:**
- Modify: `src/tend/paths.py`
- Modify: `tests/test_paths.py`

- [ ] **Step 1: Write a failing test**

Append to `tests/test_paths.py`:

```python
def test_migrate_workspace_noop_for_same_version(monkeypatch, tmp_path):
    """Migration from current → current is a no-op (succeeds, no side effects)."""
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import migrate_workspace
    # Should not raise; no files should be created.
    migrate_workspace(from_version="0.1.0", to_version="0.1.0")
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_paths.py::test_migrate_workspace_noop_for_same_version -v
```

Expected: `ImportError`.

- [ ] **Step 3: Add the stub**

Append to `src/tend/paths.py`:

```python
def migrate_workspace(from_version: str, to_version: str) -> None:
    """Apply any required workspace-layout migrations between two tend versions.

    For v0.1 this is a no-op. When v0.2 ships a layout change, branch on
    the from_version here and apply the migration. Called from boot
    when `.tend-version < current`.
    """
    return
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_paths.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/tend/paths.py tests/test_paths.py
git commit -m "feat(paths): add migrate_workspace stub for future schema changes"
```

---

## Task 26: Implement the skill update flow primitives in `skill_update.py`

**Files:**
- Create: `src/tend/skill_update.py`
- Create: `tests/test_skill_update.py`

- [ ] **Step 1: Write failing tests for `compute_skill_updates`**

Create `tests/test_skill_update.py`:

```python
"""Tests for the skill-update flow primitives.

The Typer CLI wiring (`tend skill update`) lands in sub-project #4. This
module implements the underlying logic the wiring will call.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def user_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    return tmp_path


def _write_skill(root: Path, name: str, body: str = "minimal body") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {body}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_unchanged_skill_yields_no_update(user_home, monkeypatch, tmp_path):
    """Current user dir bytes equal shipped bytes → not in diff list."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "v1")
    # User has the exact same content (e.g., never edited after install).
    _write_skill(user_home / "skills", "briefing", "v1")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import compute_skill_updates
    updates, new = compute_skill_updates()
    assert updates == []
    assert new == []


def test_modified_skill_yields_update(user_home, monkeypatch, tmp_path):
    """User-edited or shipped-changed skill → appears in updates list."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "shipped-v2")
    _write_skill(user_home / "skills", "briefing", "user-edit")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import compute_skill_updates
    updates, new = compute_skill_updates()
    assert updates == ["briefing"]
    assert new == []


def test_new_shipped_skill_yields_new(user_home, monkeypatch, tmp_path):
    """Shipped name not in user dir → appears in 'new' list."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "weather", "v1")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import compute_skill_updates
    updates, new = compute_skill_updates()
    assert updates == []
    assert new == ["weather"]


def test_user_skill_not_in_shipped_untouched(user_home, monkeypatch, tmp_path):
    """A user-authored skill (e.g., meal-plan) not in shipped catalog → never in diff."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "v1")
    _write_skill(user_home / "skills", "briefing", "v1")
    _write_skill(user_home / "skills", "meal-plan", "user-authored")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import compute_skill_updates
    updates, new = compute_skill_updates()
    assert updates == []
    assert new == []
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_skill_update.py::test_unchanged_skill_yields_no_update -v
```

Expected: `ModuleNotFoundError: No module named 'tend.skill_update'`.

- [ ] **Step 3: Implement `compute_skill_updates`**

Create `src/tend/skill_update.py`:

```python
"""Skill update flow: diff user-installed skills against shipped, back up, overwrite.

Used by the `tend skill update` CLI command (Typer wiring lands in
sub-project #4 — this module implements the primitives that wiring will
call).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from loguru import logger


def _dir_checksum(d: Path) -> str:
    """SHA-256 of a directory's contents (file names + bytes), stable across runs."""
    h = hashlib.sha256()
    if not d.exists():
        return ""
    for f in sorted(d.rglob("*")):
        if not f.is_file():
            continue
        h.update(str(f.relative_to(d)).encode("utf-8"))
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def compute_skill_updates() -> Tuple[list[str], list[str]]:
    """Return (updates, new):

    - updates: shipped skill names where current user bytes differ from shipped bytes.
    - new: shipped skill names not in $TEND_HOME/skills/.

    User-authored skills (names not in shipped catalog) are ignored.
    """
    from tend import paths

    shipped_root = paths.shipped_skills_dir()
    user_root = paths.user_skills_dir()

    if not shipped_root.exists():
        return [], []

    updates: list[str] = []
    new: list[str] = []
    for shipped_dir in sorted(shipped_root.iterdir()):
        if not shipped_dir.is_dir():
            continue
        name = shipped_dir.name
        user_dir = user_root / name
        if not user_dir.exists():
            new.append(name)
            continue
        if _dir_checksum(shipped_dir) != _dir_checksum(user_dir):
            updates.append(name)
    return updates, new
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_skill_update.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/skill_update.py tests/test_skill_update.py
git commit -m "feat(skill_update): compute_skill_updates diff primitive"
```

---

## Task 27: Implement `apply_skill_updates` with rolling backup

**Files:**
- Modify: `src/tend/skill_update.py`
- Modify: `tests/test_skill_update.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_skill_update.py`:

```python
def test_apply_updates_backs_up_then_overwrites(user_home, monkeypatch, tmp_path):
    """apply_skill_updates: existing user dir → backup → overwritten with shipped bytes."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "shipped-v2")
    _write_skill(user_home / "skills", "briefing", "user-edit")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import apply_skill_updates
    apply_skill_updates(updates=["briefing"], new_to_install=[])

    # User dir now has the shipped bytes.
    user_skill_md = user_home / "skills" / "briefing" / "SKILL.md"
    assert "shipped-v2" in user_skill_md.read_text()

    # Backup directory contains the previous user version.
    backup_md = user_home / "skills-backup" / "briefing" / "SKILL.md"
    assert "user-edit" in backup_md.read_text()

    # Manifest written.
    manifest = user_home / "skills-backup" / ".restore-manifest.json"
    assert manifest.exists()
    data = json.loads(manifest.read_text())
    assert "briefing" in data["skills"]
    assert "backed_up_at" in data


def test_apply_updates_replaces_previous_backup(user_home, monkeypatch, tmp_path):
    """Second apply that backs up anything wipes the previous backup."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "shipped-v2")
    _write_skill(user_home / "skills", "briefing", "user-edit-1")
    _write_skill(user_home / "skills", "mail-triage", "edit-1")
    # Pre-existing stale backup from a "previous" update.
    (user_home / "skills-backup").mkdir()
    (user_home / "skills-backup" / "stale-thing").mkdir()
    (user_home / "skills-backup" / "stale-thing" / "old.txt").write_text("ancient")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import apply_skill_updates
    apply_skill_updates(updates=["briefing"], new_to_install=[])

    # New backup contains briefing.
    assert (user_home / "skills-backup" / "briefing" / "SKILL.md").exists()
    # Old stale backup is gone.
    assert not (user_home / "skills-backup" / "stale-thing").exists()


def test_apply_updates_installs_new(user_home, monkeypatch, tmp_path):
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "weather", "shipped-v1")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import apply_skill_updates
    apply_skill_updates(updates=[], new_to_install=["weather"])
    assert (user_home / "skills" / "weather" / "SKILL.md").exists()
    # No backup created — nothing was overwritten.
    assert not (user_home / "skills-backup").exists()


def test_apply_updates_empty_is_noop(user_home, monkeypatch, tmp_path):
    """apply with empty lists leaves the workspace untouched."""
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import apply_skill_updates
    apply_skill_updates(updates=[], new_to_install=[])
    assert not (user_home / "skills-backup").exists()
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_skill_update.py::test_apply_updates_backs_up_then_overwrites -v
```

Expected: `ImportError: cannot import name 'apply_skill_updates'`.

- [ ] **Step 3: Implement `apply_skill_updates` with atomic rolling backup**

Append to `src/tend/skill_update.py`:

```python
def apply_skill_updates(*, updates: list[str], new_to_install: list[str]) -> dict:
    """Back up each `update` from $TEND_HOME/skills/ atomically, overwrite with shipped.

    Then install each `new_to_install` from shipped → user dir (no backup needed).

    Returns a dict with summary counts for the caller to display.
    """
    from tend import paths

    shipped_root = paths.shipped_skills_dir()
    user_root = paths.user_skills_dir()
    backup_root = paths.skills_backup_root()

    if updates:
        # Wipe + atomically replace the backup dir.
        backup_tmp = backup_root.with_suffix(".tmp")
        if backup_tmp.exists():
            shutil.rmtree(backup_tmp)
        backup_tmp.mkdir(parents=True)

        for name in updates:
            user_skill_dir = user_root / name
            if user_skill_dir.exists():
                shutil.copytree(user_skill_dir, backup_tmp / name)

        manifest = {
            "tend_version": _current_tend_version_str(),
            "backed_up_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "skills": list(updates),
        }
        (backup_tmp / ".restore-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8",
        )

        if backup_root.exists():
            shutil.rmtree(backup_root)
        backup_tmp.rename(backup_root)

        # Overwrite each user dir with the shipped version.
        for name in updates:
            user_skill_dir = user_root / name
            if user_skill_dir.exists():
                shutil.rmtree(user_skill_dir)
            shutil.copytree(shipped_root / name, user_skill_dir)

    # Install new skills (no backup needed — nothing being overwritten).
    user_root.mkdir(parents=True, exist_ok=True)
    for name in new_to_install:
        shipped_dir = shipped_root / name
        if not shipped_dir.exists():
            logger.warning(f"asked to install {name!r} but it's not in the shipped catalog; skipping")
            continue
        shutil.copytree(shipped_dir, user_root / name)

    return {"updated": len(updates), "installed": len(new_to_install)}


def _current_tend_version_str() -> str:
    from tend import __version__
    return __version__
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_skill_update.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/skill_update.py tests/test_skill_update.py
git commit -m "feat(skill_update): apply_skill_updates with atomic rolling backup"
```

---

## Task 28: Add `paths.run_setup_seeding` for the wizard's copy-into-workspace step

**Files:**
- Modify: `src/tend/skill_update.py` (reuses the install machinery)
- Modify: `tests/test_skill_update.py`

The Typer wizard in sub-project #4 needs a primitive that copies a chosen list of optional skills (plus workspace/bin and optionally soul.md) into $TEND_HOME. The cleanest place is in `skill_update.py` since the install path is mostly shared with `apply_skill_updates`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_skill_update.py`:

```python
def test_install_optional_skills(user_home, monkeypatch, tmp_path):
    fake_shipped = tmp_path / "fake_shipped"
    fake_shipped.mkdir()
    _write_skill(fake_shipped, "briefing", "v1")
    _write_skill(fake_shipped, "mail-triage", "v1")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_skills_dir", lambda: fake_shipped)

    from tend.skill_update import install_optional_skills
    install_optional_skills(["briefing"])
    assert (user_home / "skills" / "briefing" / "SKILL.md").exists()
    assert not (user_home / "skills" / "mail-triage").exists()


def test_install_workspace_bin(user_home, monkeypatch, tmp_path):
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    (fake_bin / "tool.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (fake_bin / "tool.sh").chmod(0o755)

    from tend import paths
    monkeypatch.setattr(paths, "shipped_workspace_bin", lambda: fake_bin)

    from tend.skill_update import install_workspace_bin
    install_workspace_bin()
    target = user_home / "workspace" / "bin" / "tool.sh"
    assert target.exists()
    assert target.stat().st_mode & 0o111  # executable bit preserved


def test_install_soul(user_home, monkeypatch, tmp_path):
    fake_soul = tmp_path / "fake_soul.md"
    fake_soul.write_text("SHIPPED PERSONA", encoding="utf-8")

    from tend import paths
    monkeypatch.setattr(paths, "shipped_soul_md", lambda: fake_soul)

    from tend.skill_update import install_soul
    install_soul()
    assert (user_home / "soul.md").read_text() == "SHIPPED PERSONA"
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_skill_update.py::test_install_optional_skills -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement the three install helpers**

Append to `src/tend/skill_update.py`:

```python
def install_optional_skills(names: list[str]) -> None:
    """Copy the named optional skills from shipped → $TEND_HOME/skills/.

    Silently skips names not present in the shipped catalog (with a warning log).
    Existing user skills with the same name are NOT overwritten — use
    apply_skill_updates(updates=...) for that.
    """
    from tend import paths

    shipped_root = paths.shipped_skills_dir()
    user_root = paths.user_skills_dir()
    user_root.mkdir(parents=True, exist_ok=True)

    for name in names:
        shipped_dir = shipped_root / name
        target = user_root / name
        if not shipped_dir.exists():
            logger.warning(f"install requested for {name!r}, not in shipped catalog; skipping")
            continue
        if target.exists():
            logger.info(f"{name!r} already installed; skipping (use 'tend skill update' to refresh)")
            continue
        shutil.copytree(shipped_dir, target)


def install_workspace_bin() -> None:
    """Bulk-copy shipped workspace/bin/ scripts to $TEND_HOME/workspace/bin/.

    Per-file: never overwrite an existing script (user may have edited).
    """
    from tend import paths

    shipped = paths.shipped_workspace_bin()
    target = paths.workspace_bin_dir()
    target.mkdir(parents=True, exist_ok=True)
    if not shipped.exists():
        return
    for src in shipped.iterdir():
        if not src.is_file():
            continue
        dst = target / src.name
        if dst.exists():
            continue
        shutil.copy2(src, dst)
        dst.chmod(0o755)


def install_soul() -> None:
    """Copy shipped soul.md to $TEND_HOME/soul.md.

    Never overwrites an existing user soul.md.
    """
    from tend import paths

    target = paths.soul_path()
    if target.exists():
        return
    src = paths.shipped_soul_md()
    if not src.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_skill_update.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tend/skill_update.py tests/test_skill_update.py
git commit -m "feat(skill_update): add install_{optional_skills,workspace_bin,soul} helpers

These are the primitives the tend setup wizard in sub-project #4 will
call after asking the user which optional skills to install."
```

---

## Task 29: Full test suite green + update README's dev-run note

**Files:**
- Verify: all tests
- Modify: `README.md` (if necessary — the dev-run section)

- [ ] **Step 1: Run the full test suite**

```bash
pytest -v
```

Expected: all passing. Fix any straggler issues (likely tests that still reference removed `soul_path`, `log_path`, `skills_dir`, `skills_root` parameters or hardcoded `~/.tend/`).

- [ ] **Step 2: Check for any lingering references to old paths**

```bash
grep -rn 'Path.home() / ".tend"' src/ tests/ 2>/dev/null
grep -rn 'soul_path\|log_path\|REPO_SKILL\|HEARTBEAT_SKILL_BODY' src/ 2>/dev/null
```

Expected: no matches in `src/`. A few possible matches in tests are fine if they're testing the new functions; if they're stale, clean them up.

- [ ] **Step 3: Update README dev-run instructions**

Open `README.md`. Find the "Install" or "Running" section. The current README says `python -m tend` from the clone. Add a note:

```markdown
### Running from a clone (development)

Set `TEND_HOME` to a project-local workspace so you don't pollute your real one:

```bash
export TEND_HOME=$PWD/.tend-dev
tend setup    # populates .tend-dev/
tend
```
```

Don't rewrite the full README (that's sub-project #8); just add the dev-mode note.

- [ ] **Step 4: Run tests once more**

```bash
pytest -v
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: dev-run note for TEND_HOME workspace pattern"
```

---

## Task 30: Deploy on the Pi — one-time migration of the existing workspace

This task is run on the Pi as part of merging the workspace-migration branch. It's NOT a code change; it's a deployment step.

**Files:** none committed; shell commands on the Pi.

- [ ] **Step 1: Stop the existing tend service**

```bash
systemctl --user stop tend 2>/dev/null || true
# If tend is running from a tmux session or foreground shell, stop it manually.
```

- [ ] **Step 2: Move the existing workspace into the new location**

```bash
mv ~/.tend ~/.tend
```

- [ ] **Step 3: Remove the now-obsolete heartbeat and schedule-watcher user copies**

```bash
rm -rf ~/.tend/skills/heartbeat
rm -rf ~/.tend/skills/schedule-watcher
```

- [ ] **Step 4: Carry over the maintainer's `tend.toml` dev tweaks**

```bash
cd /home/pi/hasat  # or wherever the repo clone is
# The repo's tend.toml is being deleted in task 32 below;
# move the maintainer's local edits into the workspace first.
if [ -f tend.toml ]; then
  mv tend.toml ~/.tend/tend.toml
fi
```

- [ ] **Step 5: Reinstall the package** (so the new entry point and `_defaults/` are picked up)

```bash
cd /home/pi/hasat
pip install -e .   # if using a venv; else pipx install --force --editable .
```

- [ ] **Step 6: Run setup in adopt mode**

```bash
tend setup
```

(The wizard from sub-project #4 isn't built yet, so for now manually create the marker:)

```bash
echo "0.1.0" > ~/.tend/.tend-version
```

- [ ] **Step 7: Smoke test**

```bash
tend
```

Expected: tend boots normally; wake word + brain function as before. Stop with Ctrl-C.

- [ ] **Step 8: Restart the service** (if you use systemd)

```bash
systemctl --user start tend
journalctl --user -u tend -f
```

Listen for "tend starting" + normal pipeline construction.

- [ ] **Step 9: No commit — this is a deployment step only.**

---

## Task 31: Delete repo-root `tend.toml` if still present

**Files:**
- Delete: `tend.toml` (if it still exists at repo root)

- [ ] **Step 1: Check whether the file still exists**

```bash
ls tend.toml 2>/dev/null && echo "exists — need to delete" || echo "already gone"
```

If the maintainer moved it to `~/.tend/tend.toml` during task 30, this file may already be gone. If it's still here (e.g., a clean clone), delete it.

- [ ] **Step 2: Delete and commit**

```bash
git rm tend.toml 2>/dev/null || true
git diff --cached --name-only
```

If anything is staged:

```bash
git commit -m "chore: remove repo-root tend.toml (now lives in \$TEND_HOME)"
```

If nothing was staged (file was already removed in task 30 / never present in this branch), skip.

---

## Self-review

After completing every task:

- [ ] **Spec coverage** — for each section of `docs/superpowers/specs/2026-05-12-tend-workspace-migration-design.md`:
  - §1 (Path resolution): tasks 1-3, 11-12
  - §2 (Shipped defaults layout): tasks 4-8
  - §3 (First-launch + setup contract): tasks 12, 24, 25, 28
  - §4 (Runtime skill enumeration): tasks 13-19
  - §5 (Update flow): tasks 26-27
  - §6 (Configuration loading): tasks 20-22
  - §7 (Migration + testing + out-of-scope): tasks 29-31
- [ ] **No placeholders** — grep for "TODO", "TBD", "FIXME" in new code:

  ```bash
  grep -rn 'TODO\|TBD\|FIXME\|XXX' src/tend/paths.py src/tend/skill_update.py
  ```

  Expected: no matches.
- [ ] **Type consistency** — function names referenced in later tasks match earlier definitions:
  - `tend_home()` (task 1) used in tasks 2, 23
  - `enumerate_all_skills()` (task 13) used in tasks 16, 19
  - `find_event_subscribers_all()` (task 14) used in tasks 17, 18
  - `read_soul()` (task 10) used in task 20
  - `WorkspaceState` + `detect_workspace_state()` (task 12) used in task 24
  - `compute_skill_updates()` (task 26) used in test references; CLI uses come in sub-project #4
- [ ] **Final test run:** `pytest -v` — full suite green.

## Risks / open questions

None outstanding from the spec. If unexpected behavior surfaces during implementation, capture it as a follow-up task in the next sub-project's plan rather than amending this one mid-stream.

"""Skill update flow — diff user-installed skills against shipped, back up, overwrite.

Used by the `tend skill update` CLI command (Typer wiring lands in
sub-project #4 — this module implements the primitives that wiring will
call). Setup wizard primitives (install_optional_skills,
install_workspace_bin, install_soul) live here too, since they share
the same shutil-driven copy logic.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

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


def compute_skill_updates() -> tuple[list[str], list[str]]:
    """Diff installed user skills against the shipped catalog.

    Returns ``(updates, new)``:

    - ``updates``: shipped skill names whose user-dir bytes differ from
      shipped-dir bytes (covers both "user edited" and "shipped version
      changed" — the backup makes the distinction unnecessary).
    - ``new``: shipped skill names not present in ``$TEND_HOME/skills/``.

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


def apply_skill_updates(*, updates: list[str], new_to_install: list[str]) -> dict:
    """Back up each ``update``, then overwrite from shipped. Install each ``new_to_install``.

    Backup is a single rolling directory at ``$TEND_HOME/skills-backup/``.
    Each call that backs up anything wipes and atomically replaces the
    previous backup (write to ``.tmp/`` then rename). Calls with empty
    ``updates`` leave any existing backup untouched.

    Returns a summary dict with counts.
    """
    from tend import paths
    from tend import __version__

    shipped_root = paths.shipped_skills_dir()
    user_root = paths.user_skills_dir()
    backup_root = paths.skills_backup_root()

    if updates:
        # Stage the new backup into a sibling .tmp/, then atomically swap.
        backup_tmp = backup_root.with_suffix(".tmp")
        if backup_tmp.exists():
            shutil.rmtree(backup_tmp)
        backup_tmp.mkdir(parents=True)

        for name in updates:
            user_skill_dir = user_root / name
            if user_skill_dir.exists():
                shutil.copytree(user_skill_dir, backup_tmp / name)

        manifest = {
            "tend_version": __version__,
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
            logger.warning(
                f"install requested for {name!r}, not in shipped catalog; skipping"
            )
            continue
        shutil.copytree(shipped_dir, user_root / name)

    return {"updated": len(updates), "installed": len(new_to_install)}


def install_optional_skills(names: list[str]) -> None:
    """Copy the named optional skills from shipped → $TEND_HOME/skills/.

    Silently skips names not present in the shipped catalog (with a warning log).
    Existing user skills with the same name are NOT overwritten — use
    ``apply_skill_updates(updates=...)`` for that.
    """
    from tend import paths

    shipped_root = paths.shipped_skills_dir()
    user_root = paths.user_skills_dir()
    user_root.mkdir(parents=True, exist_ok=True)

    for name in names:
        shipped_dir = shipped_root / name
        target = user_root / name
        if not shipped_dir.exists():
            logger.warning(
                f"install requested for {name!r}, not in shipped catalog; skipping"
            )
            continue
        if target.exists():
            logger.info(
                f"{name!r} already installed; skipping "
                f"(use 'tend skill update' to refresh)"
            )
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

# SPDX-License-Identifier: MIT
"""On-disk persistence for worker sessions.

Layout under `root` (defaults to ~/.tend/):

    sessions.json                # index keyed by session_id → entry dict
    sessions/<session_id>.jsonl  # per-session claude stream-json transcript
    system-prompts/<id>.txt      # per-session system prompt file (passed to claude --append-system-prompt-file)

All index writes go through `_write_index`, which serialises to a temp file
in the same directory and renames into place — atomic on POSIX so a crashing
process never leaves a half-written index.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass
class SessionEntry:
    session_id: str
    worker: str
    request: str
    status: Literal["running", "done", "failed", "killed"]
    started_at: int
    last_interaction_at: int
    transcript_path: str
    task_id: str | None = None
    cwd: str | None = None
    ended_at: int | None = None
    spoken_summary: str | None = None
    model: str | None = None
    cost_usd: float | None = None
    error: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class SessionStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else Path.home() / ".tend"
        (self.root / "sessions").mkdir(parents=True, exist_ok=True)
        (self.root / "system-prompts").mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "sessions.json"
        if not self._index_path.exists():
            self._write_index({})

    # --- public API ---

    def start(
        self,
        *,
        session_id: str,
        worker: str,
        request: str,
        cwd: str | None,
        task_id: str | None = None,
    ) -> SessionEntry:
        now = _now_ms()
        entry = SessionEntry(
            session_id=session_id,
            worker=worker,
            request=request,
            status="running",
            started_at=now,
            last_interaction_at=now,
            transcript_path=str(self.transcript_path(session_id)),
            task_id=task_id,
            cwd=cwd,
        )
        self._update(lambda idx: idx.__setitem__(session_id, asdict(entry)))
        return entry

    def complete(
        self,
        session_id: str,
        *,
        status: Literal["done", "failed", "killed"],
        spoken_summary: str | None = None,
        usage: dict | None = None,
        error: str | None = None,
    ) -> SessionEntry:
        def patch(idx: dict) -> None:
            if session_id not in idx:
                raise KeyError(f"No session {session_id!r} in index")
            row = idx[session_id]
            now = _now_ms()
            row["status"] = status
            row["ended_at"] = now
            row["last_interaction_at"] = now
            if spoken_summary is not None:
                row["spoken_summary"] = spoken_summary
            if usage:
                row["cost_usd"] = usage.get("total_cost_usd")
            if error:
                row["error"] = error[:500]
        self._update(patch)
        return self._entry_from_dict(self._read_index()[session_id])

    def list_recent(self, *, limit: int = 10) -> list[SessionEntry]:
        idx = self._read_index()
        rows = sorted(
            (r for r in idx.values() if isinstance(r, dict)),
            key=lambda r: r.get("started_at", 0),
            reverse=True,
        )
        result: list[SessionEntry] = []
        for r in rows[:limit]:
            try:
                result.append(self._entry_from_dict(r))
            except (TypeError, KeyError):
                pass  # skip corrupt rows; do not crash the caller
        return result

    def get(self, session_id: str) -> SessionEntry | None:
        idx = self._read_index()
        row = idx.get(session_id)
        return self._entry_from_dict(row) if row else None

    def touch(self, session_id: str) -> SessionEntry:
        """Bump `last_interaction_at` and reset status to "running" for an
        existing session — used when resuming, so the entry's prior
        spoken_summary, cost_usd, ended_at, and error fields are preserved.

        Raises KeyError if the session is unknown.
        """
        def patch(idx: dict) -> None:
            if session_id not in idx:
                raise KeyError(f"No session {session_id!r} in index to resume")
            row = idx[session_id]
            row["status"] = "running"
            row["last_interaction_at"] = _now_ms()
            # Clear ended_at so it gets reset by the next complete() call.
            row["ended_at"] = None
        self._update(patch)
        return self._entry_from_dict(self._read_index()[session_id])

    def transcript_path(self, session_id: str) -> Path:
        return self.root / "sessions" / f"{session_id}.jsonl"

    def write_system_prompt(self, session_id: str, text: str) -> Path:
        p = self.root / "system-prompts" / f"{session_id}.txt"
        p.write_text(text)
        return p

    # --- internals ---

    def _update(self, mutator):
        idx = self._read_index()
        mutator(idx)
        self._write_index(idx)

    def _read_index(self) -> dict:
        try:
            text = self._index_path.read_text()
        except FileNotFoundError:
            return {}
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    def _write_index(self, idx: dict) -> None:
        # Atomic: temp file in same dir, fsync, then rename.
        fd, tmp_path = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(idx, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._index_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _entry_from_dict(row: dict) -> SessionEntry:
        # Tolerate unknown keys (forward compat) by stuffing them into extras.
        known = {f.name for f in SessionEntry.__dataclass_fields__.values()}
        extras = {k: v for k, v in row.items() if k not in known}
        kwargs = {k: v for k, v in row.items() if k in known and k != "extras"}
        kwargs.setdefault("extras", extras)
        return SessionEntry(**kwargs)


def _now_ms() -> int:
    return int(time.time() * 1000)

"""Shared event fan-out helper.

Used by both `webhook.py` (`POST /event`) and `scheduler.py` (event-mode
scheduled jobs) to find skills subscribed to an event kind and dispatch
each via the GeneralWorker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger

from tend.skills import find_event_subscribers


async def dispatch_event(
    *,
    kind: str,
    payload: dict,
    dispatch: Callable[[str, dict], Awaitable[None]],
    skills_root: Path,
) -> list[str]:
    """Find skills subscribed to `kind` and dispatch them via `dispatch`.

    Returns the list of skill names that were dispatched. Empty list is
    not an error — it means no skill subscribes to this kind.
    """
    try:
        matches = find_event_subscribers(skills_root, kind)
    except Exception:
        logger.exception("event dispatch failed listing subscribers")
        matches = []
    for skill in matches:
        await dispatch("general", {
            "request": f"Handle event {kind}",
            "skill": skill.name,
            "event": {"kind": kind, **payload},
        })
    return [s.name for s in matches]

"""SessionManager — owns the day-session lifecycle.

Loads soul.md at boot and at the configured wall-clock daily reset time.
Provides a manual reset_now() entry point that defers when Brain is active.
"""

from __future__ import annotations

import asyncio
import datetime
from zoneinfo import ZoneInfo

from loguru import logger

from tend import paths


class SessionManager:
    """Owns the day-session lifecycle: load soul.md, schedule daily reset, expose manual reset."""

    def __init__(
        self,
        *,
        brain,
        hub,
        reset_time: str = "04:00",
        timezone: str | None = None,
    ):
        # Brain owns the active flag and emits on_brain_deactivated; Hub owns
        # the LLMContext and the reset_session method. They're separate
        # concerns — keep both references explicit.
        self._brain = brain
        self._hub = hub
        self._reset_time = reset_time
        self._tz = ZoneInfo(timezone) if timezone else None
        self._pending_reset = False
        self._scheduled_task: asyncio.Task | None = None

    def _read_soul(self) -> str:
        return paths.read_soul()

    async def start(self) -> None:
        """Load soul.md and trigger an initial reset_session, then schedule the daily reset."""
        soul = self._read_soul()
        await self._hub.reset_session(soul)
        self._scheduled_task = asyncio.create_task(self._schedule_daily_reset())

    async def reset_now(self) -> None:
        """Immediate (or deferred) reset. If Brain is active, defer until next deactivation."""
        if self._brain.active:
            self._pending_reset = True
            logger.info("reset_now: Brain is active, deferring reset until next deactivation")
            return
        soul = self._read_soul()
        await self._hub.reset_session(soul)

    async def on_brain_deactivated(self) -> None:
        """Called by Hub when Brain transitions to inactive. Drains a pending reset, if any."""
        if not self._pending_reset:
            return
        self._pending_reset = False
        soul = self._read_soul()
        await self._hub.reset_session(soul)
        logger.info("deferred reset applied after brain deactivation")

    async def _schedule_daily_reset(self) -> None:
        """Sleep until the next configured reset_time, fire reset, repeat."""
        while True:
            now = datetime.datetime.now(tz=self._tz)
            hour, minute = (int(p) for p in self._reset_time.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            await self.reset_now()

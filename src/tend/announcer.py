# SPDX-License-Identifier: MIT
"""ProactiveAnnouncer — single entry point for proactive TTS announcements.

Used by Scheduler, webhook /say, and GeneralWorker. Enforces:
- Per-category cooldown (drops repeat announcements within the window).
- Active-Brain deferral (non-urgent calls queue while Brain.active=True).
- Urgency override (urgent calls bypass both).
- LLMContext logging (every published announcement also lands in Hub's
  context as a system message so Brain knows what was said).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat_subagents.bus import AgentBus
from pipecat_subagents.bus.messages import BusFrameMessage


@dataclass(frozen=True)
class _Pending:
    text: str
    source: str
    category: str


class ProactiveAnnouncer:
    def __init__(
        self,
        *,
        bus: AgentBus,
        is_brain_active: Callable[[], bool],
        default_cooldown_s: int,
        category_cooldowns: dict[str, int],
        agent_name: str = "announcer",
    ):
        self._bus = bus
        self._is_brain_active = is_brain_active
        self._default = float(default_cooldown_s)
        self._per_category = {k: float(v) for k, v in category_cooldowns.items()}
        self._last_fired: dict[str, float] = {}
        self._pending: deque[_Pending] = deque()
        self._name = agent_name

    def _cooldown_for(self, category: str) -> float:
        return self._per_category.get(category, self._default)

    def _on_cooldown(self, category: str) -> bool:
        last = self._last_fired.get(category)
        if last is None:
            return False
        return (time.monotonic() - last) < self._cooldown_for(category)

    async def announce(
        self, text: str, *, source: str, category: str, urgent: bool = False,
    ) -> bool:
        """Speak `text` (subject to cooldown / deferral). Returns True if
        delivered or queued; False if dropped on cooldown."""
        if not urgent and self._on_cooldown(category):
            logger.debug(
                f"announce dropped (cooldown {category}): {text!r}"
            )
            return False
        if not urgent and self._is_brain_active():
            self._pending.append(_Pending(text, source, category))
            logger.info(
                f"announce queued (brain active, {category}): {text!r}"
            )
            return True
        await self._publish(text, source, category)
        self._last_fired[category] = time.monotonic()
        return True

    async def drain_pending(self) -> None:
        """Called when Brain transitions to inactive. Publishes queued
        announcements that are no longer on cooldown; drops the rest."""
        while self._pending:
            p = self._pending.popleft()
            if self._on_cooldown(p.category):
                logger.debug(
                    f"drain dropped (cooldown {p.category}): {p.text!r}"
                )
                continue
            await self._publish(p.text, p.source, p.category)
            self._last_fired[p.category] = time.monotonic()

    async def _publish(self, text: str, source: str, category: str) -> None:
        await self._bus.publish(BusFrameMessage(
            source=self._name,
            frame=TTSSpeakFrame(text),
            direction=FrameDirection.DOWNSTREAM,
        ))
        await self._bus.publish(BusFrameMessage(
            source=self._name,
            frame=LLMMessagesAppendFrame(messages=[{
                "role": "system",
                "content": (
                    f"You announced (from {source}/{category}): {text!r}"
                ),
            }]),
            direction=FrameDirection.DOWNSTREAM,
        ))

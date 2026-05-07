"""ReminderWorker — the v1 stub worker.

Validates the persistent + autonomous-TTS + structured-context-update pattern
that real workers in later specs will follow:

1. Sleep for the requested duration (cancellable).
2. Publish a brief TTSSpeakFrame to the "voice" bridge for the audible announcement.
3. Send a structured task_update so the brain's LLMContext records the event.
4. Send a task_response ack.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat_subagents.agents import BaseAgent
from pipecat_subagents.agents.task_context import TaskStatus
from pipecat_subagents.agents.task_decorator import task
from pipecat_subagents.bus import AgentBus
from pipecat_subagents.bus.messages import BusFrameMessage


class ReminderWorker(BaseAgent):
    """Stub worker that validates the autonomous-completion pattern."""

    def __init__(self, name: str, *, bus: AgentBus):
        super().__init__(name, bus=bus)

    @task
    async def handle_reminder(self, message) -> None:
        seconds = float(message.payload["seconds"])
        what = str(message.payload["what"])
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            logger.info(f"reminder cancelled: {what!r}")
            raise
        except Exception as e:
            await self._announce_error(message.task_id, what, e)
            return
        try:
            await self._announce_completion(message.task_id, what)
        except Exception as e:
            await self._announce_error(message.task_id, what, e)

    async def _announce_completion(self, task_id: str, what: str) -> None:
        spoken = f"Reminder: {what}"
        await self.bus.publish(BusFrameMessage(
            source=self.name,
            frame=TTSSpeakFrame(spoken),
            direction=FrameDirection.DOWNSTREAM,
        ))
        await self.send_task_update(task_id, {
            "kind": "announcement",
            "spoken": spoken,
            "context": {"what": what, "completed_at": time.time()},
        })
        await self.send_task_response(task_id, {"delivered": True})

    async def _announce_error(self, task_id: str, what: str, exc: Exception) -> None:
        logger.exception(f"reminder failed for {what!r}")
        spoken = "Sorry, I couldn't complete the reminder."
        await self.bus.publish(BusFrameMessage(
            source=self.name,
            frame=TTSSpeakFrame(spoken),
            direction=FrameDirection.DOWNSTREAM,
        ))
        await self.send_task_update(task_id, {
            "kind": "error",
            "spoken": spoken,
            "context": {"what": what, "error": str(exc)},
        })
        await self.send_task_response(
            task_id, {"error": str(exc)}, status=TaskStatus.ERROR
        )

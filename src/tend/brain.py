"""Brain — the conversational LLM agent.

Thin LLMAgent: owns the LLM service and its tools, but not the conversation
context. The context lives in Hub (the transport-owning parent), so frames
flow through Hub's user aggregator → bridge → Brain's LLM → bridge → Hub's
TTS → assistant aggregator. This is the canonical pipecat-subagents pattern.

Workers report autonomous announcements via `on_task_update` (a bus-message
hook that fires regardless of `Brain.active`); Brain forwards the announcement
to Hub's context as an `LLMMessagesAppendFrame` so the next conversation turn
includes it.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat_subagents.agents import LLMAgent, tool
from pipecat_subagents.bus import AgentBus
from pipecat_subagents.bus.messages import BusFrameMessage


class Brain(LLMAgent):
    """Conversational brain. LLM + tools + worker awareness."""

    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        llm_service: LLMService | None,
        session_manager=None,
    ):
        super().__init__(name, bus=bus, bridged=())
        self._llm_service = llm_service
        self._session_manager = session_manager

    def attach_session_manager(self, session_manager) -> None:
        self._session_manager = session_manager

    def build_llm(self) -> LLMService:
        if self._llm_service is None:
            raise RuntimeError(
                "Brain has no LLMService. The main entry point should ensure one is provided "
                "(or the brain should not be added to the runner if Anthropic preflight failed)."
            )
        return self._llm_service

    async def on_deactivated(self) -> None:
        await super().on_deactivated()
        if self._session_manager:
            await self._session_manager.on_brain_deactivated()

    async def on_task_update(self, message) -> None:
        await super().on_task_update(message)
        update = getattr(message, "update", {}) or {}
        kind = update.get("kind")
        if kind == "announcement":
            spoken = update.get("spoken", "")
            ctx = update.get("context", {}) or {}
            ctx_block = "\n".join(f"  {k}: {v}" for k, v in ctx.items())
            await self._append_to_context(
                f"You announced to the user while you were deactivated: {spoken!r}\n"
                f"Full task details (for follow-up questions):\n{ctx_block}"
            )
        elif kind == "error":
            spoken = update.get("spoken", "")
            ctx = update.get("context", {}) or {}
            ctx_block = "\n".join(f"  {k}: {v}" for k, v in ctx.items())
            await self._append_to_context(
                f"A worker failed; you announced to the user: {spoken!r}\n"
                f"You could not complete the task. Failure details:\n{ctx_block}"
            )

    async def _append_to_context(self, content: str) -> None:
        """Add a system message to Hub's context via the bus.

        We publish an LLMMessagesAppendFrame to the bus; Hub's bridge forwards
        it past the LLM, and Hub's assistant aggregator captures it into the
        shared LLMContext. Works whether Brain is active or not.
        """
        await self.bus.publish(BusFrameMessage(
            source=self.name,
            frame=LLMMessagesAppendFrame(
                messages=[{"role": "system", "content": content}],
            ),
            direction=FrameDirection.DOWNSTREAM,
        ))

    async def _ensure_reminder_worker(self) -> None:
        from tend.workers.reminder import ReminderWorker
        for child in getattr(self, "_children", []) or []:
            if getattr(child, "name", None) == "reminder":
                return
        try:
            await self.add_agent(ReminderWorker("reminder", bus=self.bus))
        except Exception as e:
            logger.debug(f"reminder worker may already exist: {e!r}")

    @tool
    async def remind_in(self, params: FunctionCallParams, seconds: int, what: str):
        """Ask the assistant to remind you about something after a delay.

        Args:
            seconds (int): How long to wait (in seconds) before the reminder fires.
            what (str): The thing to remind about. Plain text, will be spoken aloud.
        """
        await self._ensure_reminder_worker()
        await self.request_task("reminder", payload={"seconds": int(seconds), "what": what})
        return f"Got it. I'll remind you in {int(seconds)} seconds."

    @tool
    async def start_fresh(self, params: FunctionCallParams):
        """Reset the conversation. Use only when the user explicitly asks to start over.

        After this, your conversation context is cleared and reloaded from soul.md.
        """
        if self._session_manager:
            asyncio.create_task(self._session_manager.reset_now())
        return "Starting fresh."

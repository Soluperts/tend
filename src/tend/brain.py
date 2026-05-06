"""Brain — the conversational LLM agent.

Wraps an Anthropic LLMService (or a fallback when Anthropic is unavailable)
inside an LLMAgent. Persists LLMContext across wake/sleep within a
day-session; `reset_session` swaps the context for a fresh one.

Workers report autonomous announcements via `on_task_update` (a bus-message
hook that fires regardless of `Brain.active`), keeping the brain aware of
what was said while it was deactivated.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat_subagents.agents import LLMAgent, tool
from pipecat_subagents.bus import AgentBus

VOICE_RULES = (
    "Replies are spoken aloud. Keep them brief — usually one short sentence. "
    "No markdown, no lists, no code blocks. Plain conversational prose only. "
    "If the user does not appear to be addressing you, stay silent."
)


def _build_system_prompt(soul_text: str) -> dict:
    content = soul_text.strip() + "\n\n" + VOICE_RULES
    return {"role": "system", "content": content}


class Brain(LLMAgent):
    """Conversational brain. LLMAgent with day-session reset and worker awareness."""

    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        llm_service: LLMService | None,
        session_manager=None,
    ):
        super().__init__(name, bus=bus, bridged=("voice",))
        self._llm_service = llm_service
        self._session_manager = session_manager  # set later via attach_session_manager
        self._context = LLMContext()

    @property
    def context(self) -> LLMContext:
        """The LLMContext for this brain's conversation."""
        return self._context

    def attach_session_manager(self, session_manager) -> None:
        self._session_manager = session_manager

    def build_llm(self) -> LLMService:
        if self._llm_service is None:
            raise RuntimeError(
                "Brain has no LLMService. The main entry point should ensure one is provided "
                "(or the brain should not be added to the runner if Anthropic preflight failed)."
            )
        return self._llm_service

    async def reset_session(self, soul_text: str) -> None:
        """Replace the LLMContext's messages with a fresh system prompt only."""
        self._context.set_messages([_build_system_prompt(soul_text)])
        logger.info("brain: session reset (LLMContext flushed, soul reloaded)")

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
            self._context.add_message({
                "role": "system",
                "content": (
                    f"You announced to the user while you were deactivated: {spoken!r}\n"
                    f"Full task details (for follow-up questions):\n{ctx_block}"
                ),
            })
        elif kind == "error":
            spoken = update.get("spoken", "")
            ctx = update.get("context", {}) or {}
            ctx_block = "\n".join(f"  {k}: {v}" for k, v in ctx.items())
            self._context.add_message({
                "role": "system",
                "content": (
                    f"A worker failed; you announced to the user: {spoken!r}\n"
                    f"You could not complete the task. Failure details:\n{ctx_block}"
                ),
            })
        # other kinds: ignored

    async def _ensure_reminder_worker(self) -> None:
        from tend.workers.reminder import ReminderWorker
        # Idempotent — guard against double-add.
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

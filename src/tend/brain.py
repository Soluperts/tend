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

from tend.sessions import SessionStore


class Brain(LLMAgent):
    """Conversational brain. LLM + tools + worker awareness."""

    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        llm_service: LLMService | None,
        session_manager=None,
        store: SessionStore | None = None,
    ):
        super().__init__(name, bus=bus, bridged=())
        self._llm_service = llm_service
        self._session_manager = session_manager
        self._store = store

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

    async def _ensure_coding_worker(self) -> None:
        from tend.config import WorkerConfig, settings
        from tend.workers.coding import CodingWorker

        for child in getattr(self, "_children", []) or []:
            if getattr(child, "name", None) == "coding":
                return
        cfg = settings.workers.get("general") or WorkerConfig()
        if self._store is None:
            logger.warning("Brain has no SessionStore; coding worker cannot be added.")
            return
        try:
            await self.add_agent(
                CodingWorker("coding", bus=self.bus, store=self._store, config=cfg),
            )
        except Exception as e:
            logger.debug(f"coding worker may already exist: {e!r}")

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

    @tool
    async def code_in(self, params: FunctionCallParams, request: str):
        """Dispatch a build/coding task to the deskclaw coding worker.

        Use this when you need a small tool, script, parser, or glue
        component built so you can complete another workflow. Code is
        written into the deskclaw workspace, not into arbitrary user repos.

        Args:
            request (str): What you want built, in plain English.
        """
        if self._store is None:
            # Without a store, _ensure_coding_worker no-ops and request_task
            # would fire at a worker that doesn't exist. Fail loud and clear.
            return "Session store unavailable — cannot dispatch coding tasks."
        await self._ensure_coding_worker()
        await self.request_task("coding", payload={"request": request})
        return f"Got it. I'll work on '{request[:80]}' and let you know when it's ready."

    @tool
    async def list_recent_jobs(self, params: FunctionCallParams, limit: int = 5):
        """List recent background jobs and their status.

        Args:
            limit (int): How many recent jobs to return (default 5).
        """
        if self._store is None:
            return "Session store unavailable."
        rows = self._store.list_recent(limit=limit)
        if not rows:
            return "No jobs yet."
        lines = []
        for r in rows:
            lines.append(
                f"[{r.status}] {r.worker} {r.session_id[:8]}: {r.request[:80]}"
                + (f" — {r.spoken_summary}" if r.spoken_summary else "")
            )
        return "\n".join(lines)

    @tool
    async def session_status(self, params: FunctionCallParams, session_id: str):
        """Look up the status of a specific background job by session id.

        Args:
            session_id (str): The session id (full or unique prefix).
        """
        if self._store is None:
            return "Session store unavailable."
        rows = self._store.list_recent(limit=1000)
        match = next((r for r in rows if r.session_id.startswith(session_id)), None)
        if not match:
            return f"No session matching '{session_id}'."
        parts = [f"[{match.status}] {match.worker}: {match.request}"]
        if match.spoken_summary:
            parts.append(f"summary: {match.spoken_summary}")
        if match.error:
            parts.append(f"error: {match.error}")
        return " | ".join(parts)

    @tool
    async def continue_session(
        self, params: FunctionCallParams, session_id: str, follow_up: str,
    ):
        """Resume a previous background job with a follow-up instruction.

        Args:
            session_id (str): The session id (full or unique prefix) to resume.
            follow_up (str): What to do next, in plain English.
        """
        if self._store is None:
            return "Session store unavailable."
        rows = self._store.list_recent(limit=1000)
        match = next((r for r in rows if r.session_id.startswith(session_id)), None)
        if not match:
            return f"Couldn't find session '{session_id}'."
        # v1: only the coding worker can be resumed. When more workers learn
        # to resume, replace this with a registry lookup keyed on match.worker.
        if match.worker != "coding":
            return (
                f"Session {match.session_id[:8]} belongs to worker "
                f"'{match.worker}', which doesn't support resuming yet."
            )
        await self._ensure_coding_worker()
        await self.request_task(
            match.worker,
            payload={
                "request": follow_up,
                "resume_session_id": match.session_id,
            },
        )
        return f"Got it. I'll follow up on session {match.session_id[:8]}."

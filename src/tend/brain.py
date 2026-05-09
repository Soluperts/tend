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
        scheduler=None,
    ):
        super().__init__(name, bus=bus, bridged=())
        self._llm_service = llm_service
        self._session_manager = session_manager
        self._store = store
        self._scheduler = scheduler

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

    async def _ensure_general_worker(self) -> None:
        from tend.config import WorkerConfig, settings
        from tend.workers.general import GeneralWorker

        for child in getattr(self, "_children", []) or []:
            if getattr(child, "name", None) == "general":
                return
        cfg = settings.workers.get("general") or WorkerConfig()
        if self._store is None:
            logger.warning("Brain has no SessionStore; general worker cannot be added.")
            return
        try:
            await self.add_agent(
                GeneralWorker("general", bus=self.bus, store=self._store, config=cfg),
            )
        except Exception as e:
            logger.debug(f"general worker may already exist: {e!r}")

    @tool
    async def remind_in(self, params: FunctionCallParams, seconds: int, what: str):
        """Ask the assistant to remind you about something after a delay.

        Args:
            seconds (int): How long to wait (in seconds) before the reminder fires.
            what (str): The thing to remind about. Plain text, will be spoken aloud.
        """
        if self._scheduler is None:
            await params.result_callback("Scheduler not configured.")
            return
        self._scheduler.add_job(
            when=f"in {int(seconds)}s",
            request=what,
            name=f"reminder-{int(__import__('time').time())}",
            source="voice",
        )
        await params.result_callback(
            f"Got it. I'll remind you in {int(seconds)} seconds."
        )

    @tool
    async def start_fresh(self, params: FunctionCallParams):
        """Reset the conversation. Use only when the user explicitly asks to start over.

        After this, your conversation context is cleared and reloaded from soul.md.
        """
        if self._session_manager:
            asyncio.create_task(self._session_manager.reset_now())
        return "Starting fresh."

    @tool
    async def do_task(self, params: FunctionCallParams, request: str):
        """Hand a task off to the deskclaw worker. Use for anything that needs
        work beyond conversation: meal plans, fitness check-ins, calendar work,
        building tools, running scripts.

        Args:
            request (str): What you want done, in plain English.
        """
        if self._store is None:
            await params.result_callback(
                "Session store unavailable — cannot dispatch tasks."
            )
            return
        await self._ensure_general_worker()
        await self.request_task("general", payload={"request": request})
        await params.result_callback(
            f"Got it. Working on '{request[:80]}'..."
        )

    @tool
    async def list_skills(self, params: FunctionCallParams):
        """List the workflows tend currently knows how to do."""
        import os as _os
        from pathlib import Path as _Path

        from tend.skills import enumerate_skills

        root = _Path(_os.environ.get("TEND_SKILLS_ROOT")
                     or _Path.home() / ".tend" / "skills")
        skills = enumerate_skills(root)
        if not skills:
            await params.result_callback(
                "I haven't built any workflows yet."
            )
            return
        lines = [f"{s.name} — {s.description}" for s in skills]
        await params.result_callback("\n".join(lines))

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
        # v1: only the general worker can be resumed. When more workers learn
        # to resume, replace this with a registry lookup keyed on match.worker.
        if match.worker != "general":
            return (
                f"Session {match.session_id[:8]} belongs to worker "
                f"'{match.worker}', which doesn't support resuming yet."
            )
        await self._ensure_general_worker()
        await self.request_task(
            match.worker,
            payload={
                "request": follow_up,
                "resume_session_id": match.session_id,
            },
        )
        return f"Got it. I'll follow up on session {match.session_id[:8]}."

    @tool
    async def schedule(
        self,
        params: FunctionCallParams,
        when: str,
        request: str,
        name: str | None = None,
    ):
        """Schedule a recurring or one-shot job. The worker fires when the
        schedule matches and announces the result.

        Args:
            when: cron expression like '0 12 * * *', a relative offset like
                  'in 3 hours', an ISO timestamp, or 'every 30m'.
            request: what should happen, in plain English.
            name: optional human label for cancel/list later.
        """
        if self._scheduler is None:
            await params.result_callback("Scheduler not configured.")
            return
        from tend.cron_time import InvalidWhen
        try:
            job = self._scheduler.add_job(
                when=when, request=request,
                name=name or f"job-{int(__import__('time').time())}",
                source="voice",
            )
        except InvalidWhen as e:
            await params.result_callback(
                f"I couldn't parse that schedule: {e}. Try '0 12 * * *' "
                f"for cron, 'in 30 minutes' for one-shot, or 'every 30m'."
            )
            return
        await params.result_callback(
            f"Scheduled '{job.name}'."
        )

    @tool
    async def list_schedules(self, params: FunctionCallParams):
        """List currently active schedules and what they'll do."""
        if self._scheduler is None:
            await params.result_callback("Scheduler not configured.")
            return
        jobs = self._scheduler.list_jobs()
        if not jobs:
            await params.result_callback("You have no active schedules.")
            return
        lines = []
        for j in jobs:
            kind = j.kind
            sched = j.schedule
            lines.append(f"{j.name}: {kind} {sched} ({j.source})")
        await params.result_callback("\n".join(lines))

    @tool
    async def cancel_schedule(
        self, params: FunctionCallParams, name_or_id: str,
    ):
        """Cancel a scheduled job by name or id prefix."""
        if self._scheduler is None:
            await params.result_callback("Scheduler not configured.")
            return
        ok = self._scheduler.cancel_job(name_or_id)
        if ok:
            await params.result_callback(f"Cancelled '{name_or_id}'.")
        else:
            await params.result_callback(
                f"Couldn't find a schedule matching '{name_or_id}'."
            )

    @tool
    async def enable_skill_triggers(
        self, params: FunctionCallParams, skill: str,
    ):
        """Activate the schedule triggers declared in a skill's frontmatter.

        Idempotent: re-running replaces any previously copied triggers for
        this skill.
        """
        if self._scheduler is None:
            await params.result_callback("Scheduler not configured.")
            return
        import os as _os
        from pathlib import Path as _Path

        from tend.skills import enumerate_skills

        root = _Path(
            _os.environ.get("TEND_SKILLS_ROOT")
            or _Path.home() / ".tend" / "skills"
        )
        matching = [s for s in enumerate_skills(root) if s.name == skill]
        if not matching:
            await params.result_callback(f"No skill named '{skill}'.")
            return
        info = matching[0]
        if not info.triggers:
            await params.result_callback(
                f"'{skill}' has no triggers declared in its frontmatter."
            )
            return
        # Wipe any prior copies of this source first.
        source = f"skill:{skill}"
        for old in self._scheduler.find_by_source(source):
            self._scheduler.cancel_job(old.id)
        added = 0
        for t in info.triggers:
            when = t.get("cron") or t.get("every") or t.get("at")
            if not when:
                continue
            request = t.get("request") or f"Run {skill}."
            self._scheduler.add_job(
                when=when, request=request,
                name=f"{skill}-{added+1}",
                source=source,
                tz=t.get("tz"),
                payload_extras={"skill": skill},
            )
            added += 1
        await params.result_callback(
            f"Enabled {added} trigger(s) for '{skill}'."
        )

    @tool
    async def disable_skill_triggers(
        self, params: FunctionCallParams, skill: str,
    ):
        """Remove all schedule triggers previously enabled from a skill."""
        if self._scheduler is None:
            await params.result_callback("Scheduler not configured.")
            return
        source = f"skill:{skill}"
        rows = self._scheduler.find_by_source(source)
        for r in rows:
            self._scheduler.cancel_job(r.id)
        await params.result_callback(
            f"Disabled {len(rows)} trigger(s) for '{skill}'."
        )

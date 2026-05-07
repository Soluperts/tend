"""CodingWorker — runs `claude` in a per-job git worktree to do coding tasks.

Brain dispatches via `request_task('coding', payload={'repo': ..., 'request': ...})`.
For follow-ups, payload may also include `resume_session_id`, in which case
the worker resumes the existing claude session and reuses its worktree.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat_subagents.agents.task_context import TaskStatus
from pipecat_subagents.agents.task_decorator import task
from pipecat_subagents.bus import AgentBus
from pipecat_subagents.bus.messages import BusFrameMessage

from tend.config import WorkerConfig
from tend.sessions import SessionStore
from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec


def default_worktree_factory(repo: Path, session_id: str) -> Path:
    """Create `~/.tend/worktrees/<session_id>/` as a git worktree off `repo`."""
    target = Path.home() / ".tend" / "worktrees" / session_id
    target.parent.mkdir(parents=True, exist_ok=True)
    branch = f"tend/coding/{session_id[:12]}"
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(target), "HEAD"],
        cwd=str(repo), check=True,
    )
    return target


class CodingWorker(ClaudeCliWorker):
    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        store: SessionStore,
        config: WorkerConfig,
        subprocess_factory=None,
        worktree_factory=default_worktree_factory,
    ):
        super().__init__(
            name, bus=bus, store=store, subprocess_factory=subprocess_factory,
        )
        self._config = config
        self._worktree_factory = worktree_factory

    @task
    async def code_in(self, message) -> None:
        request = str(message.payload["request"])
        resume_id = message.payload.get("resume_session_id")

        # Note: `except Exception` deliberately does NOT catch
        # `asyncio.CancelledError` (a BaseException). Cancellation should
        # propagate up through `run_claude`'s finally block so the session
        # is marked "killed" and the subprocess is reaped.

        # Phase 1: do the work. Failures here trigger _announce_error.
        try:
            if resume_id:
                # Resuming: claude already knows the worktree from the prior turn.
                spec = ClaudeRunSpec(
                    prompt=request,
                    resume_session_id=resume_id,
                    allowed_tools=self._config.allowed_tools,
                    setting_sources=self._config.setting_sources,
                    model=self._config.model,
                )
            else:
                repo = Path(message.payload["repo"]).expanduser()
                session_id = uuid.uuid4().hex
                # Worktree creation can take 50–200 ms on a Pi 5 (longer on
                # cold cache). Hand it to the executor so the asyncio loop
                # keeps running for audio / VAD / STT.
                worktree = await asyncio.to_thread(
                    self._worktree_factory, repo, session_id,
                )
                spec = ClaudeRunSpec(
                    prompt=request,
                    session_id=session_id,
                    allowed_tools=self._config.allowed_tools,
                    setting_sources=self._config.setting_sources,
                    model=self._config.model,
                    cwd=worktree,
                )
            entry = await self.run_claude(spec)
        except Exception as e:
            logger.exception("CodingWorker failed")
            await self._announce_error(message.task_id, request, e)
            return

        # Phase 2: announce. A failure here must not also call _announce_error
        # with the original exception — the work succeeded, the announce just
        # didn't land. Fall through to a separate error-announce attempt.
        try:
            await self._announce(message.task_id, entry)
        except Exception as e:
            logger.exception("CodingWorker _announce failed after successful run")
            await self._announce_error(message.task_id, request, e)

    async def _announce(self, task_id, entry):
        spoken = entry.spoken_summary or "Coding task complete."
        await self.bus.publish(BusFrameMessage(
            source=self.name,
            frame=TTSSpeakFrame(spoken),
            direction=FrameDirection.DOWNSTREAM,
        ))
        await self.send_task_update(task_id, {
            "kind": "announcement",
            "spoken": spoken,
            "context": {
                "session_id": entry.session_id,
                "worktree": entry.cwd,
                "request": entry.request,
            },
        })
        await self.send_task_response(task_id, {"delivered": True})

    async def _announce_error(self, task_id, request, exc):
        spoken = "Sorry, the coding task didn't complete."
        await self.bus.publish(BusFrameMessage(
            source=self.name,
            frame=TTSSpeakFrame(spoken),
            direction=FrameDirection.DOWNSTREAM,
        ))
        await self.send_task_update(task_id, {
            "kind": "error",
            "spoken": spoken,
            "context": {"request": request, "error": str(exc)},
        })
        await self.send_task_response(
            task_id, {"error": str(exc)}, status=TaskStatus.ERROR
        )

"""CodingWorker — runs `claude` inside the persistent deskclaw workspace.

DeskClaw's main job isn't coding — it's meal plans, fitness, routines,
reminders, dashboards. The coding worker is the escape hatch: when the
assistant decides it needs to *build* something to enable a workflow
(a small script, a parser, a glue tool), it dispatches a coding task here.

All coding tasks share a single persistent workspace directory (default
`~/.tend/workspace/`). There is no per-job isolation — DeskClaw accumulates
everything it builds in that one place. Resumes reuse the same cwd; claude
itself remembers per-session state via its `--resume` flag.

Brain dispatches via `request_task('coding', payload={'request': ...})`.
For follow-ups, payload also includes `resume_session_id`.
"""

from __future__ import annotations

import asyncio
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


DEFAULT_WORKSPACE = Path.home() / ".tend" / "workspace"


def _resolve_workspace(config: WorkerConfig) -> Path:
    raw = config.workspace_dir
    if not raw:
        return DEFAULT_WORKSPACE
    return Path(raw).expanduser()


class CodingWorker(ClaudeCliWorker):
    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        store: SessionStore,
        config: WorkerConfig,
        subprocess_factory=None,
        workspace_dir: Path | None = None,
    ):
        super().__init__(
            name, bus=bus, store=store, subprocess_factory=subprocess_factory,
        )
        self._config = config
        self._workspace_dir = workspace_dir or _resolve_workspace(config)

    def _ensure_workspace(self) -> Path:
        """Make sure the workspace dir exists. Idempotent."""
        self._workspace_dir.mkdir(parents=True, exist_ok=True)
        return self._workspace_dir

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
            workspace = await asyncio.to_thread(self._ensure_workspace)
            spec = ClaudeRunSpec(
                prompt=request,
                resume_session_id=resume_id,
                allowed_tools=self._config.allowed_tools,
                setting_sources=self._config.setting_sources,
                model=self._config.model,
                cwd=workspace,
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
                "workspace": entry.cwd,
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

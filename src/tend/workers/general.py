# SPDX-License-Identifier: MIT
"""GeneralWorker — runs `claude` inside the persistent deskclaw workspace.

The worker is general-purpose: capability comes from markdown skills under
`~/.tend/skills/<name>/SKILL.md`. At spawn time the worker enumerates the
catalog and injects a compact `<available-skills>` XML block into claude's
system prompt; claude reads full SKILL.md bodies on demand. When no skill
matches an incoming request, claude may author a new SKILL.md plus any
scripts under `~/.tend/workspace/bin/`; a regex safety scanner runs after
claude exits and quarantines anything with critical findings.

All tasks share a single persistent workspace directory (default
`~/.tend/workspace/`). There is no per-job isolation. Resumes reuse the
same cwd; claude itself remembers per-session state via its `--resume` flag.

Brain dispatches via `request_task('general', payload={'request': ...})`.
For follow-ups, payload also includes `resume_session_id`.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat_subagents.agents.task_context import TaskStatus
from pipecat_subagents.agents.task_decorator import task
from pipecat_subagents.bus import AgentBus
from pipecat_subagents.bus.messages import BusFrameMessage

from tend import paths
from tend.config import WorkerConfig
from tend.sessions import SessionStore
from tend.skills import (
    enumerate_all_skills,
    format_catalog_xml,
    quarantine_skill,
    scan_text,
)
from tend.workers.claude_cli import ClaudeCliWorker, ClaudeRunSpec


SILENT_MARKER = "(nothing to surface)"


def _resolve_workspace(config: WorkerConfig) -> Path:
    raw = config.workspace_dir
    if not raw:
        return paths.tend_home() / "workspace"
    return Path(raw).expanduser()


_GENERAL_PREAMBLE = """\
You are tend's general-purpose worker. The user is a desk worker who talks
to tend through a smart speaker; you are their hands. You run inside a
persistent workspace at ~/.tend/workspace/ where everything you build
accumulates.

Two paths for any incoming request:

1. SKILL MATCH. If <available-skills> below contains a skill matching the
   request, Read its SKILL.md and follow the procedure exactly. Skills
   typically tell you which scripts in ~/.tend/workspace/bin/ to invoke.

2. NO SKILL MATCH. If the request is a *recurring workflow* (named inputs,
   plausibly repeatable), build a skill for it before executing:
   a. Author any scripts you need under ~/.tend/workspace/bin/.
   b. Author ~/.tend/skills/<name>/SKILL.md describing the workflow.
   c. Run `tend scan-skill <name>` via Bash. Exit 0 = run it. Exit 1 = warn,
      review the warnings then run only if they're acceptable. Exit 2 = move
      the skill to ~/.tend/skills-quarantined/<name>/ and announce a graceful
      fallback to the user instead of running it.
   d. If the scan was clean, execute the skill end-to-end.

   If the request is a *one-shot* (chitchat, "what's 17x19", "tell me a
   joke"), just answer inline; do not author a skill.

If the dispatch payload sets silent_default=true, you are running on the
heartbeat tick. Default to NOT speaking: only produce a non-empty spoken
summary if there is something the user genuinely needs to hear right
now. If nothing is worth surfacing, end your run with the literal phrase
"(nothing to surface)" as your last assistant message.

If the dispatch payload includes an `event` block, the user did not ask
for this directly — a trigger fired (vision daemon, calendar, etc.). Be
brief; the user did not invite a long answer.

Skill naming: hyphen-case lowercase, descriptive (`meal-plan`, not `mp`).
Same-name conflicts: prefer appending or replacing a section over silent
overwrite.

Scripts must be self-contained and idempotent where reasonable. Never write
secrets or tokens into scripts; read from environment variables.

When you finish, your last assistant message becomes the spoken summary.
Keep it short — one or two sentences for TTS. Save the long-form artifact
to ~/.tend/workspace/plans/, ~/.tend/workspace/data/, or wherever the skill
directs.
"""


class GeneralWorker(ClaudeCliWorker):
    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        store: SessionStore,
        config: WorkerConfig,
        subprocess_factory=None,
        workspace_dir: Path | None = None,
        announcer=None,
    ):
        super().__init__(
            name, bus=bus, store=store, subprocess_factory=subprocess_factory,
        )
        self._config = config
        self._workspace_dir = workspace_dir or _resolve_workspace(config)
        self._announcer = announcer

    def _ensure_workspace(self) -> Path:
        """Make sure the workspace dir exists. Idempotent."""
        self._workspace_dir.mkdir(parents=True, exist_ok=True)
        return self._workspace_dir

    @task
    async def do_task(self, message) -> None:
        request = str(message.payload["request"])
        resume_id = message.payload.get("resume_session_id")
        silent_default = bool(message.payload.get("silent_default", False))
        pinned_skill = message.payload.get("skill")
        event = message.payload.get("event")

        # Note: `except Exception` deliberately does NOT catch
        # `asyncio.CancelledError` (a BaseException). Cancellation should
        # propagate up through `run_claude`'s finally block so the session
        # is marked "killed" and the subprocess is reaped.

        # Phase 1: do the work. Failures here trigger _announce_error.
        # Grab task_start slightly in the past so filesystem mtime granularity
        # (sub-second floor on most fs, plus the gap between time.time() and
        # the actual write) doesn't cause us to miss freshly-written files.
        task_start = time.time() - 1.0
        user_skills_dir = paths.user_skills_dir()
        try:
            workspace = await asyncio.to_thread(self._ensure_workspace)
            skills = await asyncio.to_thread(enumerate_all_skills)
            system_prompt = _GENERAL_PREAMBLE
            catalog = format_catalog_xml(skills)
            if catalog:
                system_prompt = system_prompt + "\n" + catalog
            # Inject context blocks for the worker preamble to read.
            if pinned_skill:
                system_prompt += (
                    f"\n\n<pinned-skill>{pinned_skill}</pinned-skill>"
                )
            if event:
                system_prompt += (
                    f"\n\n<event>{event}</event>"
                )
            if silent_default:
                system_prompt += "\n\n<silent_default>true</silent_default>"
            spec = ClaudeRunSpec(
                prompt=request,
                system_prompt=system_prompt,
                resume_session_id=resume_id,
                allowed_tools=self._config.allowed_tools,
                permission_mode=self._config.permission_mode,
                setting_sources=self._config.setting_sources,
                model=self._config.model,
                cwd=workspace,
            )
            entry = await self.run_claude(spec)
        except Exception as e:
            logger.exception("GeneralWorker failed")
            await self._announce_error(message.task_id, request, e)
            return

        # Phase 2: post-hoc safety scan of any SKILL.md files claude may have
        # authored during the run. Critical findings → auto-quarantine. A
        # failure during the scan must not invalidate the run itself.
        try:
            created, quarantined = await asyncio.to_thread(
                self._post_run_scan, user_skills_dir, task_start,
            )
        except Exception:
            logger.exception("post-hoc scan failed; treating run as clean")
            created, quarantined = [], []

        # Phase 3: announce. A failure here must not also call _announce_error
        # with the original exception — the work succeeded, the announce just
        # didn't land. Fall through to a separate error-announce attempt.
        try:
            await self._announce(
                message.task_id, entry,
                created=created, quarantined=quarantined,
                silent_default=silent_default,
                category=pinned_skill or "general",
            )
        except Exception as e:
            logger.exception("GeneralWorker _announce failed after successful run")
            await self._announce_error(message.task_id, request, e)

    def _post_run_scan(
        self, skills_dir: Path, since_ts: float,
    ) -> tuple[list[str], list[str]]:
        """Scan SKILL.md files in <skills_dir> with mtime >= since_ts.

        Returns (created, quarantined): names of skills that landed cleanly
        versus those moved to <skills_dir>/../skills-quarantined/. Files that
        existed before the task are ignored.
        """
        created: list[str] = []
        quarantined: list[str] = []
        if not skills_dir.exists():
            return created, quarantined
        quarantine_root = skills_dir.parent / "skills-quarantined"
        # Sort for stable iteration order in tests + announcements.
        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            if skill_md.stat().st_mtime < since_ts:
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            report = scan_text(text)
            if report.is_critical:
                quarantine_skill(
                    name=child.name,
                    skills_root=skills_dir,
                    quarantine_root=quarantine_root,
                    findings=report.findings,
                )
                quarantined.append(child.name)
            else:
                created.append(child.name)
        return created, quarantined

    async def _announce(
        self, task_id, entry, *,
        created=None, quarantined=None,
        silent_default: bool = False,
        category: str = "general",
    ):
        spoken = (entry.spoken_summary or "").strip()
        is_silent = silent_default and (
            spoken == "" or spoken.lower() == SILENT_MARKER.lower()
        )
        if not is_silent:
            text = spoken or "Task complete."
            if self._announcer is not None:
                await self._announcer.announce(
                    text=text,
                    source=f"worker:{self.name}",
                    category=category,
                    urgent=False,
                )
            else:
                # Back-compat for the period where main.py hasn't wired the
                # announcer yet — fall through to publishing TTS directly.
                await self.bus.publish(BusFrameMessage(
                    source=self.name,
                    frame=TTSSpeakFrame(text),
                    direction=FrameDirection.DOWNSTREAM,
                ))
        await self.send_task_update(task_id, {
            "kind": "announcement",
            "spoken": "" if is_silent else (spoken or "Task complete."),
            "context": {
                "session_id": entry.session_id,
                "workspace": entry.cwd,
                "request": entry.request,
                "skills_created": list(created or []),
                "skills_quarantined": list(quarantined or []),
                "silent": is_silent,
            },
        })
        await self.send_task_response(task_id, {"delivered": not is_silent})

    async def _announce_error(self, task_id, request, exc):
        spoken = "Sorry, the task didn't complete."
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

"""Tend entry point.

Boot order:
1. Configure logging (loguru + faulthandler).
2. Load Settings from class defaults / tend.toml / .env / env vars.
3. Run cloud preflights; build STT, TTS, Brain LLM (with local fallbacks).
4. Construct AgentRunner, ProactiveAnnouncer, Scheduler, Brain, SessionManager,
   Hub, WebhookServer.
5. Eagerly register GeneralWorker as a Brain child.
6. SessionManager.start() loads soul.md and triggers the first reset_session.
7. Hub and Scheduler are added as top-level agents. Hub adds Brain as a child
   on_ready. Scheduler starts its loop on_ready.
8. Seed heartbeat skill + heartbeat job (idempotent on subsequent boots).
9. runner.run() — listens forever. WebhookServer starts before the loop and
   stops in a finally block.
"""

from __future__ import annotations

import asyncio
import faulthandler
import sys
from pathlib import Path

from loguru import logger
from pipecat_subagents.runner import AgentRunner

from tend.announcer import ProactiveAnnouncer
from tend.audio.hub import Hub
from tend.brain import Brain
from tend.config import Settings, WorkerConfig, settings
from tend.cron_store import CronStore
from tend.preflight import claude_cli_preflight
from tend.scheduler import Scheduler
from tend.services import _make_brain_llm, _make_stt, _make_tts
from tend.session import SessionManager
from tend.sessions import SessionStore
from tend.webhook import WebhookServer, build_app
from tend.workers.general import GeneralWorker


def _setup_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(settings.log_path, level="DEBUG", rotation="5 MB", retention=2)
    fault_path = settings.log_path.replace(".log", ".faults.log")
    fault_file = open(fault_path, "a", buffering=1)
    fault_file.write("\n--- tend start ---\n")
    faulthandler.enable(file=fault_file, all_threads=True)


# ---------------------------------------------------------------------------
# Heartbeat skill seeding
# ---------------------------------------------------------------------------

HEARTBEAT_SKILL_BODY = """\
---
name: heartbeat
description: Periodic silent check-in. Review pending work and announce only when something genuinely needs the user's attention.
silent_default: true
---

# Heartbeat

You are running on the heartbeat tick. By default, exit silently.

Only announce if there is something the user genuinely wants to know
right now and would not have heard otherwise. Examples that justify an
announcement: a follow-up deadline arrived, a long-running task you
started earlier finished while the user was away.

If you have nothing worth surfacing, end your run with the literal
phrase "(nothing to surface)" as your last assistant message.
"""


def _seed_heartbeat_skill() -> None:
    target = Path.home() / ".tend" / "skills" / "heartbeat" / "SKILL.md"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(HEARTBEAT_SKILL_BODY, encoding="utf-8")


def _seed_heartbeat_job(scheduler: Scheduler, every: str) -> None:
    if (every or "").lower() == "off":
        # Remove existing heartbeat job if disabled.
        for job in scheduler.find_by_source("system:heartbeat"):
            scheduler.cancel_job(job.id)
        return
    if scheduler.find_by_source("system:heartbeat"):
        return
    scheduler.add_job(
        when=f"every {every}",
        request="Heartbeat tick — review and surface anything worth saying.",
        name="heartbeat",
        source="system:heartbeat",
        payload_extras={"skill": "heartbeat", "silent_default": True},
    )


# ---------------------------------------------------------------------------
# Main async entry
# ---------------------------------------------------------------------------

async def _run() -> None:
    runner = AgentRunner()

    claude_cli_preflight()  # logs warning on failure; non-fatal
    store = SessionStore(root=Path.home() / ".tend")
    cron_store = CronStore(root=Path.home() / ".tend")

    llm_service = _make_brain_llm(settings)
    if llm_service is None:
        logger.error(
            "Brain LLM unavailable. Tend boots but the brain will not respond. "
            "Set ANTHROPIC_API_KEY or fix the preflight."
        )
        # We still construct Brain so the rest of the stack is exercisable;
        # the build_llm path will raise if accessed. Acceptable degraded mode for v1.

    # Build the announcer. It needs to query Brain.active, but Brain doesn't
    # exist yet. We use a mutable holder so the lambda closes over the holder
    # and looks up the brain reference after construction.
    announcer_holder: dict = {}

    def _is_brain_active() -> bool:
        b = announcer_holder.get("brain")
        return bool(b and getattr(b, "active", False))

    announcer = ProactiveAnnouncer(
        bus=runner.bus,
        is_brain_active=_is_brain_active,
        default_cooldown_s=settings.announcer.default_cooldown_s,
        category_cooldowns=dict(settings.announcer.category),
    )

    # Scheduler needs a dispatch callable. We use a lambda that closes over
    # the scheduler variable. Python lambdas are late-bound, so by the time
    # dispatch is ever called (from _fire_job, which runs in the async loop
    # that starts via on_ready), scheduler is already assigned.
    scheduler = Scheduler(
        "scheduler",
        bus=runner.bus,
        store=cron_store,
        dispatch=lambda target, payload: scheduler.request_task(
            target, payload=payload,
        ),
        default_tz=settings.timezone or "UTC",
        missed_at_policy=settings.scheduler.missed_at_policy,
    )

    brain = Brain(
        "brain", bus=runner.bus, llm_service=llm_service, store=store,
        scheduler=scheduler,
    )
    announcer_holder["brain"] = brain

    stt = _make_stt(settings)
    tts, tts_rate = _make_tts(settings)

    hub = Hub(
        "hub", bus=runner.bus, settings=settings,
        stt=stt, tts=tts, tts_sample_rate=tts_rate,
        brain=brain, announcer=announcer,
    )

    session_manager = SessionManager(
        brain=brain,
        hub=hub,
        soul_path=settings.soul_path,
        reset_time=settings.daily_reset_time,
        timezone=settings.timezone,
    )
    brain.attach_session_manager(session_manager)
    await session_manager.start()

    # Eagerly register the GeneralWorker so scheduler/webhook fires can
    # land even before the user has spoken (Brain adds workers as children
    # on demand, but we pre-register to avoid a race on first heartbeat).
    cfg = settings.workers.get("general") or WorkerConfig()
    general = GeneralWorker(
        "general", bus=runner.bus, store=store, config=cfg,
        announcer=announcer,
    )
    await brain.add_agent(general)

    # Seed heartbeat skill on first boot if missing.
    _seed_heartbeat_skill()

    # Webhook server — start before runner.run() so it is ready immediately.
    webhook_app = build_app(
        token=settings.tend_webhook_token or "",
        announcer=announcer,
        dispatch=lambda target, payload: scheduler.request_task(
            target, payload=payload,
        ),
        skills_root=Path.home() / ".tend" / "skills",
    )
    webhook_server = WebhookServer(
        host=settings.webhook.host,
        port=settings.webhook.port,
        app=webhook_app,
    )
    await webhook_server.start()
    try:
        await runner.add_agent(hub)
        await runner.add_agent(scheduler)
        # Seed heartbeat job AFTER scheduler is registered (add_job sets
        # _wakeup; the loop only starts on on_ready after add_agent).
        _seed_heartbeat_job(scheduler, settings.scheduler.heartbeat_every)
        await scheduler.fire_missed_now()
        await runner.run()
    finally:
        await webhook_server.stop()


def main() -> None:
    _setup_logging()
    logger.info(f"tend starting (log: {settings.log_path})")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("tend stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()

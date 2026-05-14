"""Tend entry point.

Boot order:
1. Configure logging (loguru + faulthandler).
2. Load Settings from class defaults / $TEND_HOME/tend.toml / $TEND_HOME/.env / env vars.
3. Check the workspace state via paths.detect_workspace_state(); bail with
   a setup hint if missing / unclaimed / from a future version.
4. Run cloud preflights; build STT, TTS, Brain LLM (with local fallbacks).
5. Construct AgentRunner, ProactiveAnnouncer, Scheduler, GeneralWorker, Brain
   (with general_worker=general), SessionManager, Hub, WebhookServer.
6. SessionManager.start() loads soul.md and triggers the first reset_session.
7. Hub and Scheduler are added as top-level agents. Hub adds Brain as a child
   on_ready. Brain.on_ready registers GeneralWorker on the (now-live) bus.
   Scheduler.on_ready fires missed at-jobs then starts the loop.
8. Seed the heartbeat job (idempotent on subsequent boots).
9. runner.run() — listens forever. WebhookServer starts before the loop and
   stops in a finally block.
"""

from __future__ import annotations

import asyncio
import faulthandler
import sys

from loguru import logger
from pipecat_subagents.runner import AgentRunner

from tend import paths
from tend.announcer import ProactiveAnnouncer
from tend.audio.hub import Hub
from tend.brain import Brain
from tend.config import WorkerConfig, settings
from tend.cron_store import CronStore
from tend.preflight import claude_cli_preflight
from tend.scheduler import Scheduler
from tend.services import _make_brain_llm, _make_stt, _make_tts
from tend.session import SessionManager
from tend.sessions import SessionStore
from tend.webhook import WebhookServer, build_app
from tend.workers.general import GeneralWorker


def _check_macos_minimum() -> None:
    """Exit cleanly if running on macOS older than 14 (Sonoma).

    Several runtime deps (onnxruntime arm64 wheels, AVSpeechSynthesizer
    streaming API) require macOS 14+. Catching this at startup gives a
    clear error instead of a downstream import or attribute error.
    """
    import platform

    if sys.platform != "darwin":
        return
    ver = platform.mac_ver()[0]
    if not ver:
        return
    try:
        major = int(ver.split(".")[0])
    except (ValueError, IndexError):
        return
    if major < 14:
        print(
            f"tend requires macOS 14 (Sonoma) or later; detected {ver}",
            file=sys.stderr,
        )
        sys.exit(1)


def _setup_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    log_target = paths.log_path()
    log_target.parent.mkdir(parents=True, exist_ok=True)
    logger.add(str(log_target), level="DEBUG", rotation="5 MB", retention=2)

    fault_target = paths.fault_log_path()
    fault_target.parent.mkdir(parents=True, exist_ok=True)
    fault_file = open(fault_target, "a", buffering=1)
    fault_file.write("\n--- tend start ---\n")
    faulthandler.enable(file=fault_file, all_threads=True)


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
    store = SessionStore(root=paths.tend_home())
    cron_store = CronStore(root=paths.tend_home())

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

    # Eagerly construct GeneralWorker so scheduler/webhook can dispatch
    # before the user has spoken; Brain.on_ready will register it on the bus.
    cfg = settings.workers.get("general") or WorkerConfig()
    general = GeneralWorker(
        "general", bus=runner.bus, store=store, config=cfg,
        announcer=announcer,
    )

    brain = Brain(
        "brain", bus=runner.bus, llm_service=llm_service, store=store,
        scheduler=scheduler, general_worker=general,
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
        reset_time=settings.daily_reset_time,
        timezone=settings.timezone,
    )
    brain.attach_session_manager(session_manager)
    await session_manager.start()

    # Webhook server — start before runner.run() so it is ready immediately.
    webhook_app = build_app(
        token=settings.tend_webhook_token or "",
        announcer=announcer,
        dispatch=lambda target, payload: scheduler.request_task(
            target, payload=payload,
        ),
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
        # Now that the scheduler is registered, seed the heartbeat job.
        _seed_heartbeat_job(scheduler, settings.scheduler.heartbeat_every)
        await runner.run()
    finally:
        await webhook_server.stop()


def main() -> None:
    _check_macos_minimum()
    _setup_logging()

    state = paths.detect_workspace_state()
    if state == paths.WorkspaceState.MISSING:
        print(
            f"No workspace at {paths.tend_home()}.\n"
            f"Run `tend setup` to initialize one.",
            file=sys.stderr,
        )
        sys.exit(1)
    if state == paths.WorkspaceState.UNCLAIMED:
        print(
            f"Workspace at {paths.tend_home()} has files but isn't initialized.\n"
            f"Run `tend setup` to adopt it.",
            file=sys.stderr,
        )
        sys.exit(1)
    if state == paths.WorkspaceState.FUTURE_VERSION:
        from tend import __version__
        print(
            f"Workspace at {paths.tend_home()} was written by tend "
            f"{paths.read_version_marker()}.\n"
            f"You're running tend {__version__}. Upgrade tend or pick a different "
            f"TEND_HOME.",
            file=sys.stderr,
        )
        sys.exit(1)

    logger.info(f"tend starting (log: {paths.log_path()})")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("tend stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()

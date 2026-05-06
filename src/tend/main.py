"""Tend entry point.

Boot order:
1. Configure logging (loguru + faulthandler).
2. Load Settings from class defaults / tend.toml / .env / env vars.
3. Run cloud preflights; build STT, TTS, Brain LLM (with local fallbacks).
4. Construct AgentRunner, Brain, SessionManager, Hub.
5. SessionManager.start() loads soul.md and triggers the first reset_session.
6. Hub is added as the only top-level agent; it adds Brain as a child on_ready.
7. runner.run() — listens forever.
"""

from __future__ import annotations

import asyncio
import faulthandler
import sys

from loguru import logger
from pipecat_subagents.runner import AgentRunner

from tend.audio.hub import Hub
from tend.brain import Brain
from tend.config import settings
from tend.services import _make_brain_llm, _make_stt, _make_tts
from tend.session import SessionManager


def _setup_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(settings.log_path, level="DEBUG", rotation="5 MB", retention=2)
    fault_path = settings.log_path.replace(".log", ".faults.log")
    fault_file = open(fault_path, "a", buffering=1)
    fault_file.write("\n--- tend start ---\n")
    faulthandler.enable(file=fault_file, all_threads=True)


async def _run() -> None:
    runner = AgentRunner()

    llm_service = _make_brain_llm(settings)
    if llm_service is None:
        logger.error(
            "Brain LLM unavailable. Tend boots but the brain will not respond. "
            "Set ANTHROPIC_API_KEY or fix the preflight."
        )
        # We still construct Brain so the rest of the stack is exercisable;
        # the build_llm path will raise if accessed. Acceptable degraded mode for v1.

    brain = Brain("brain", bus=runner.bus, llm_service=llm_service)

    session_manager = SessionManager(
        brain=brain,
        soul_path=settings.soul_path,
        reset_time=settings.daily_reset_time,
        timezone=settings.timezone,
    )
    brain.attach_session_manager(session_manager)
    await session_manager.start()

    stt = _make_stt(settings)
    tts, tts_rate = _make_tts(settings)

    hub = Hub(
        "hub", bus=runner.bus, settings=settings,
        stt=stt, tts=tts, tts_sample_rate=tts_rate,
        brain=brain,
    )
    await runner.add_agent(hub)
    await runner.run()


def main() -> None:
    _setup_logging()
    logger.info(f"tend starting (log: {settings.log_path})")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("tend stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()

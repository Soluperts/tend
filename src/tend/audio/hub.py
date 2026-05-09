"""Hub — the always-running audio agent.

Owns the local audio transport, gates, STT/TTS, the LLMContext, and the
LLMContextAggregatorPair that wraps Brain via the bus. Pipeline order matches
the canonical pipecat-subagents pattern: user aggregator BEFORE the bridge,
assistant aggregator AFTER transport.output. That way:

  STT → user_agg (consumes TranscriptionFrame, emits LLMContextFrame)
       → bridge → bus → Brain.LLM → bus → bridge → TTS → output
       → assistant_agg (captures the LLM's TextFrames into context)

Brain itself is a thin LLMAgent — it doesn't own the context, it just runs
the LLM on whatever LLMContextFrame the bridge delivers.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat_subagents.agents import BaseAgent
from pipecat_subagents.bus import AgentBus, BusBridgeProcessor

from tend.audio.channels import StereoToMonoLeft
from tend.audio.gates import OpenWakeWordGate, SleepPhraseGate
from tend.audio.logging import InputLatencyLogger, OutputLatencyLogger
from tend.config import Settings

VOICE_RULES = (
    "Replies are spoken aloud. Keep them brief — usually one short sentence. "
    "No markdown, no lists, no code blocks. Plain conversational prose only. "
    "If the user does not appear to be addressing you, stay silent. "
    "Exception: when the user asks you to introduce yourself or explain what "
    "you are or what you can do (especially 'to the group'), reply with three "
    "or four sentences in plain prose, covering both the scheduling/inbox "
    "side and the posture/hydration/work-life-balance side. Still no lists "
    "or markdown. "
    "When the user requests something matching one of the available-skills "
    "below, call the do_task tool with the user's verbatim request and give "
    "a one-sentence acknowledgement. For questions you can't answer from "
    "your own knowledge (current news, weather, web lookups, anything time-"
    "sensitive), also call do_task — the worker has internet access and will "
    "announce the result."
)


def _skills_catalog_xml(skills_root: Path | None) -> str:
    """Compact <available-skills> XML block built from installed SKILL.md
    files. Empty string if no skills_root or no skills installed."""
    if skills_root is None:
        return ""
    from tend.skills import enumerate_skills
    skills = enumerate_skills(skills_root)
    if not skills:
        return ""
    parts = ["<available-skills>"]
    for s in skills:
        name = _xml_escape(s.name)
        desc = _xml_escape(s.description.strip())
        parts.append(f'  <skill name="{name}">{desc}</skill>')
    parts.append("</available-skills>")
    return "\n".join(parts)


def _build_system_prompt(soul_text: str, skills_xml: str = "") -> dict:
    sections = [soul_text.strip(), VOICE_RULES]
    if skills_xml:
        sections.append(skills_xml)
    content = "\n\n".join(sections)
    return {"role": "system", "content": content}


class Hub(BaseAgent):
    """Audio + context owner. Wraps Brain (the LLM child) via the bus bridge."""

    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        settings: Settings,
        stt: STTService,
        tts: TTSService,
        tts_sample_rate: int,
        brain: BaseAgent,
        skills_root: Path | None = None,
        announcer=None,
    ):
        super().__init__(name, bus=bus)
        self._settings = settings
        self._stt = stt
        self._tts = tts
        self._tts_sample_rate = tts_sample_rate
        self._brain = brain
        self._skills_root = skills_root
        self._announcer = announcer
        self._context = LLMContext()

    @property
    def context(self) -> LLMContext:
        return self._context

    async def on_brain_deactivated(self) -> None:
        """Called when Brain transitions from active to inactive.

        Flushes any queued announcements from the ProactiveAnnouncer so that
        pending notifications are delivered as soon as Brain goes to sleep.
        Safe to call without an announcer (no-op).
        """
        if self._announcer is not None:
            await self._announcer.drain_pending()

    async def reset_session(self, soul_text: str) -> None:
        """Replace the LLMContext's messages with a fresh system prompt only."""
        skills_xml = _skills_catalog_xml(self._skills_root)
        self._context.set_messages(
            [_build_system_prompt(soul_text, skills_xml)],
        )

    async def build_pipeline(self) -> Pipeline:
        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=self._settings.sample_rate,
                audio_in_channels=2,  # XVF3800: L=AEC-processed, R=echo ref
                audio_out_sample_rate=self._tts_sample_rate,
            )
        )

        aggregators = LLMContextAggregatorPair(self._context)

        # exclude_frames keeps Hub-originated TTSSpeakFrames (e.g. the wake-word
        # ack "Yes?") in Hub's pipeline so they reach TTS instead of being
        # broadcast to Brain.
        bridge = BusBridgeProcessor(
            bus=self.bus,
            agent_name=self.name,
            exclude_frames=(TTSSpeakFrame,),
            name=f"{self.name}::voice-bridge",
        )

        return Pipeline([
            transport.input(),
            StereoToMonoLeft(),
            VADProcessor(vad_analyzer=SileroVADAnalyzer()),
            OpenWakeWordGate(
                model_name=self._settings.openwakeword_model,
                threshold=self._settings.wake_threshold,
                hub=self,
                brain=self._brain,
            ),
            self._stt,
            InputLatencyLogger(),
            SleepPhraseGate(
                sleep_phrase=self._settings.sleep_phrase,
                fuzz_ratio=self._settings.sleep_fuzz_ratio,
                timeout_s=self._settings.awake_timeout_s,
                hub=self,
                brain=self._brain,
            ),
            aggregators.user(),
            bridge,
            self._tts,
            OutputLatencyLogger(),
            transport.output(),
            aggregators.assistant(),
        ])

    async def on_ready(self) -> None:
        await super().on_ready()
        await self.add_agent(self._brain)

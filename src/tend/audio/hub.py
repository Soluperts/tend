"""Hub — the always-running audio agent.

Owns the local audio transport, gates, STT/TTS services, and the BusBridge that
routes voice frames to/from Brain. Brain is added as a child of Hub at runtime.
"""

from __future__ import annotations

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat_subagents.agents import BaseAgent
from pipecat_subagents.bus import AgentBus, BusBridgeProcessor

from tend.audio.gates import OpenWakeWordGate, SleepPhraseGate
from tend.audio.logging import InputLatencyLogger, OutputLatencyLogger
from tend.config import Settings


class Hub(BaseAgent):
    """The audio hub. Owns mic+speakers, gates, STT, TTS, and the voice bridge."""

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
    ):
        super().__init__(name, bus=bus)
        self._settings = settings
        self._stt = stt
        self._tts = tts
        self._tts_sample_rate = tts_sample_rate
        self._brain = brain

    async def build_pipeline(self) -> Pipeline:
        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=self._settings.sample_rate,
                audio_out_sample_rate=self._tts_sample_rate,
            )
        )

        # No bridge= name: the framework's _BusEdgeProcessor doesn't tag
        # outgoing frames with a bridge, so a named filter would drop Brain's
        # responses. exclude_frames keeps Hub-originated TTSSpeakFrames (e.g.
        # the wake-word ack "Yes?") in Hub's pipeline so they reach TTS instead
        # of being broadcast to Brain.
        bridge = BusBridgeProcessor(
            bus=self.bus,
            agent_name=self.name,
            exclude_frames=(TTSSpeakFrame,),
            name=f"{self.name}::voice-bridge",
        )

        return Pipeline([
            transport.input(),
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
            bridge,
            self._tts,
            OutputLatencyLogger(),
            transport.output(),
        ])

    async def on_ready(self) -> None:
        """Add Brain as a child once Hub's pipeline is up."""
        await super().on_ready()
        await self.add_agent(self._brain)

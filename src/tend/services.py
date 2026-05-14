"""Cloud-preferred / local-fallback service factories.

Each factory runs an HTTP preflight against the cloud provider and returns
the cloud service on success or the local equivalent on failure. The decision
is made once, at boot.
"""

from __future__ import annotations

import httpx
from loguru import logger
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

from tend.config import Settings


def _avfoundation_importable() -> bool:
    """Return True if the AVFoundation PyObjC bridge is available."""
    try:
        import AVFoundation  # noqa: F401
        return True
    except ImportError:
        return False


# STT ---------------------------------------------------------------------

def _check_deepgram(api_key: str) -> str | None:
    try:
        r = httpx.get(
            "https://api.deepgram.com/v1/projects",
            headers={"Authorization": f"Token {api_key}"},
            timeout=5,
        )
    except Exception as e:
        return f"network error: {e!r}"
    if r.status_code == 401:
        return f"auth failed (401): {r.text[:300]}"
    if r.status_code != 200:
        return f"HTTP {r.status_code}: {r.text[:300]}"
    n = len(r.json().get("projects", []))
    logger.info(f"Deepgram OK ({n} project(s) accessible)")
    return None


def _make_stt(settings: Settings) -> STTService:
    if settings.deepgram_api_key:
        err = _check_deepgram(settings.deepgram_api_key)
        if err:
            logger.warning(f"Deepgram preflight failed: {err}; falling back to local Whisper")
        else:
            from pipecat.services.deepgram.stt import DeepgramSTTService
            return DeepgramSTTService(
                api_key=settings.deepgram_api_key,
                settings=DeepgramSTTService.Settings(
                    model=settings.deepgram_model,
                    interim_results=True,
                    endpointing=300,
                    utterance_end_ms=1000,
                    smart_format=True,
                ),
            )
    from pipecat.services.whisper.stt import WhisperSTTService
    return WhisperSTTService(
        settings=WhisperSTTService.Settings(model=settings.whisper_model),
        device="cpu",
        compute_type="int8",
    )


# TTS ---------------------------------------------------------------------

def _check_elevenlabs(api_key: str) -> str | None:
    try:
        r = httpx.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": api_key},
            timeout=5,
        )
    except Exception as e:
        return f"network error: {e!r}"
    if r.status_code == 401:
        return f"auth failed (401): {r.text[:300]}"
    if r.status_code != 200:
        return f"HTTP {r.status_code}: {r.text[:300]}"
    body = r.json()
    used = body.get("character_count", 0)
    limit = body.get("character_limit", 0)
    remaining = max(0, limit - used) if limit else 0
    logger.info(f"ElevenLabs OK (credits {used}/{limit}, remaining {remaining})")
    if remaining < 200:
        return f"quota nearly exhausted ({remaining} credits left)"
    return None


def _make_tts(settings: Settings) -> tuple[TTSService, int]:
    """Resolve [tts] provider to a concrete pipecat TTSService instance.

    Output rate is pinned to settings.sample_rate so the local audio transport
    can play it without resampling (USB mic-arrays like the reSpeaker are
    typically rate-locked to 16 kHz). ElevenLabs serves PCM at the requested
    rate; Piper resamples internally from the voice's native rate.

    Returns: (service, sample_rate).
    """
    import os
    import sys

    rate = settings.sample_rate
    provider = (settings.tts_provider or "auto").lower()

    # Whether auto-resolution already validated the provider (skip preflight).
    _auto_resolved = False

    if provider == "auto":
        if sys.platform == "darwin":
            if _avfoundation_importable():
                provider = "avspeech"
            elif os.environ.get("ELEVENLABS_API_KEY") or settings.elevenlabs_api_key:
                # AVFoundation unavailable but a key is present — use ElevenLabs
                # without preflight (no Piper fallback on macOS; trust the key).
                provider = "elevenlabs"
                _auto_resolved = True
            else:
                raise RuntimeError(
                    "no working TTS provider on macOS: AVFoundation is not "
                    "importable and ELEVENLABS_API_KEY is not set. Install "
                    "pyobjc-framework-AVFoundation or set ELEVENLABS_API_KEY."
                )
        else:
            # Linux: prefer ElevenLabs key if present, else Piper.
            provider = "elevenlabs" if settings.elevenlabs_api_key else "piper"

    if provider == "avspeech":
        if sys.platform != "darwin":
            raise RuntimeError(
                "tts_provider='avspeech' is macOS-only (uses AVSpeechSynthesizer)."
            )
        svc = AVSpeechSynthesizerTTSService(
            voice_identifier=settings.avspeech_voice,
            sample_rate=rate,
        )
        return svc, rate

    if provider == "piper":
        if sys.platform == "darwin":
            raise RuntimeError(
                "tts_provider='piper' requires a Linux Piper HTTP server. "
                "On macOS use tts_provider='avspeech' or 'elevenlabs'."
            )
        import aiohttp
        from pipecat.services.piper.tts import PiperTTSService
        session = aiohttp.ClientSession()
        return (
            PiperTTSService(
                base_url=settings.piper_voice,  # piper_voice holds the HTTP server URL on Linux
                aiohttp_session=session,
                sample_rate=rate,
            ),
            rate,
        )

    if provider == "elevenlabs":
        api_key = settings.elevenlabs_api_key or os.environ.get("ELEVENLABS_API_KEY")
        if api_key:
            # Skip preflight when auto-resolved on macOS (no Piper fallback available).
            if not _auto_resolved:
                err = _check_elevenlabs(api_key)
                if err:
                    logger.warning(
                        f"ElevenLabs preflight failed: {err}; falling back to local Piper"
                    )
                    api_key = None  # signal fallback
            if api_key:
                from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
                params_kwargs: dict = {}
                if settings.elevenlabs_speed is not None:
                    params_kwargs["speed"] = settings.elevenlabs_speed
                return (
                    ElevenLabsTTSService(
                        api_key=api_key,
                        voice_id=settings.elevenlabs_voice_id,
                        model=settings.elevenlabs_model,
                        sample_rate=rate,
                        params=ElevenLabsTTSService.InputParams(**params_kwargs),
                    ),
                    rate,
                )
        # No key or preflight failed — fall back to Piper on Linux.
        if sys.platform == "darwin":
            raise RuntimeError(
                "no working TTS provider on macOS: ELEVENLABS_API_KEY is not set "
                "or preflight failed and Piper is Linux-only. "
                "Install pyobjc-framework-AVFoundation or set ELEVENLABS_API_KEY."
            )
        import aiohttp
        from pipecat.services.piper.tts import PiperTTSService
        session = aiohttp.ClientSession()
        return (
            PiperTTSService(
                base_url=settings.piper_voice,
                aiohttp_session=session,
                sample_rate=rate,
            ),
            rate,
        )

    raise ValueError(f"Unknown tts_provider: {provider!r}")


# Brain LLM ---------------------------------------------------------------

def _check_anthropic(api_key: str, model: str) -> str | None:
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}],
            },
            timeout=10,
        )
    except Exception as e:
        return f"network error: {e!r}"
    if r.status_code == 200:
        logger.info(f"Anthropic OK (model {model} responsive)")
        return None
    if r.status_code == 401:
        return f"auth failed (401): {r.text[:300]}"
    body = r.text[:400]
    if "credit balance" in body.lower():
        return f"credit balance too low: {body}"
    return f"HTTP {r.status_code}: {body}"


class AVSpeechSynthesizerTTSService(TTSService):
    """Local TTS on macOS via Apple's AVSpeechSynthesizer.

    Uses `write(_:toBufferCallback:)` (macOS 13+) to receive
    AVAudioPCMBuffer chunks and emits them as `TTSAudioRawFrame`s at
    the pipecat-requested sample rate. Voice is selected via
    `AVSpeechSynthesisVoice(identifier:)`; empty identifier means the
    system default voice.

    Siri-quality voices are not exposed by this API. Premium/Enhanced
    voices installed via VoiceOver Utility (or Read & Speak on older
    macOS) are reachable.
    """

    def __init__(
        self,
        *,
        voice_identifier: str = "",
        sample_rate: int = 16000,
        **kwargs,
    ):
        # Pipecat 1.1's TTSService requires every TTSSettings field to be
        # initialized (None for unsupported fields). AVSpeechSynthesizer has
        # no "model" concept (the voice IS the model) and we don't expose a
        # separate language setting — language is part of the voice id.
        from pipecat.services.settings import TTSSettings

        super().__init__(
            sample_rate=sample_rate,
            settings=TTSSettings(
                model=None,
                voice=voice_identifier or None,
                language=None,
            ),
            **kwargs,
        )
        self._voice_identifier = voice_identifier
        self._sample_rate = sample_rate

    def can_generate_metrics(self) -> bool:
        return True

    async def _synthesize_to_pcm(self, text: str):
        """Synthesize via `speakUtterance_`, route audio through the system speaker.

        For v1, we use AVSpeechSynthesizer's direct-to-speaker path
        (`speakUtterance_`) rather than its buffer-callback path
        (`writeUtterance_toBufferCallback_`). The buffer-callback API is
        async on a private dispatch queue that needs the main NSRunLoop
        pumped to fire — incompatible with an asyncio event loop, so
        the callback never runs and the pipeline hangs.

        We work around this by running the synthesizer inside a worker
        thread that pumps its own runloop. The audio plays directly
        through the system speaker; we yield zero bytes to the
        pipecat pipeline so its TTSStarted/TTSStopped framing fires
        correctly around the (zero-frame) audio block. The downside:
        `OutputAudioCapture` sees no PCM, so AEC has no reference
        signal. That's an accepted v1 limitation — Task 15 (WebRTC
        AEC3) is gated and can revisit the PCM extraction story when
        AEC matters.
        """
        import asyncio
        import threading

        import objc
        import AVFoundation
        from Foundation import NSDate, NSObject, NSRunLoop

        loop = asyncio.get_running_loop()
        done_event = asyncio.Event()

        class _SpeechDelegate(NSObject):
            def init(self):
                self = objc.super(_SpeechDelegate, self).init()
                if self is None:
                    return None
                return self

            def speechSynthesizer_didFinishSpeechUtterance_(self, _synth, _utt):
                loop.call_soon_threadsafe(done_event.set)

            def speechSynthesizer_didCancelSpeechUtterance_(self, _synth, _utt):
                loop.call_soon_threadsafe(done_event.set)

        def _worker():
            try:
                synth = AVFoundation.AVSpeechSynthesizer.new()
                delegate = _SpeechDelegate.alloc().init()
                synth.setDelegate_(delegate)

                utt = AVFoundation.AVSpeechUtterance.speechUtteranceWithString_(text)
                if self._voice_identifier:
                    voice = AVFoundation.AVSpeechSynthesisVoice.voiceWithIdentifier_(
                        self._voice_identifier,
                    )
                    if voice is not None:
                        utt.setVoice_(voice)

                synth.speakUtterance_(utt)

                # Pump this thread's runloop until the delegate fires. 60 s cap
                # protects against a stuck synth (worst case: the user just
                # never hears the rest of one utterance).
                rl = NSRunLoop.currentRunLoop()
                elapsed = 0.0
                while not done_event.is_set() and elapsed < 60.0:
                    rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
                    elapsed += 0.1
            except Exception as e:
                logger.exception(f"[avspeech] worker error: {e!r}")
                loop.call_soon_threadsafe(done_event.set)

        logger.debug(
            f"[avspeech] speakUtterance for {len(text)}-char text "
            f"with voice={self._voice_identifier or '(default)'}"
        )
        t = threading.Thread(target=_worker, daemon=True, name="avspeech-worker")
        t.start()

        await done_event.wait()
        logger.debug("[avspeech] utterance complete")
        # We yielded no actual PCM — the system speaker played it.
        # Return without yielding (this is still a valid empty generator).
        return
        yield  # noqa: unreachable — keeps this function a generator

    async def run_tts(self, text: str, context_id: str):
        """Pipecat 1.1 entry point.

        Yields the start/audio/stop frame sequence. The framework's
        `_stream_audio_frames_from_iterator` handles framing and
        resampling — we just produce raw int16 mono PCM bytes at
        `self._sample_rate` and hand them over.
        """
        from pipecat.frames.frames import ErrorFrame

        try:
            await self.start_tts_usage_metrics(text)

            async def _pcm_iter():
                async for pcm in self._synthesize_to_pcm(text):
                    yield pcm

            async for frame in self._stream_audio_frames_from_iterator(
                _pcm_iter(),
                in_sample_rate=self._sample_rate,
                context_id=context_id,
            ):
                await self.stop_ttfb_metrics()
                yield frame
        except Exception as e:
            logger.error(f"{self} exception: {e}")
            yield ErrorFrame(error=f"AVSpeechSynthesizer error: {e}")
        finally:
            await self.stop_ttfb_metrics()


def _ptr_to_bytes(ptr, n: int) -> bytes:
    """Read `n` bytes from a PyObjC C-pointer."""
    import ctypes
    return ctypes.string_at(int(ptr), n)


def _make_brain_llm(settings: Settings):
    """Return an `AnthropicLLMService` if the preflight passes, else None.

    Returning None signals to the caller that the brain should run in degraded
    mode (echo) — see Brain.build_llm for that path.
    """
    if not settings.anthropic_api_key:
        return None
    err = _check_anthropic(settings.anthropic_api_key, settings.llm_model)
    if err:
        logger.warning(f"Anthropic preflight failed: {err}; brain will run in degraded mode")
        return None
    from pipecat.services.anthropic.llm import AnthropicLLMService
    return AnthropicLLMService(
        api_key=settings.anthropic_api_key,
        settings=AnthropicLLMService.Settings(
            model=settings.llm_model,
            enable_prompt_caching=True,
        ),
    )

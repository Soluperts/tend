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
        """Async generator: yields raw int16 mono PCM bytes at self._sample_rate.

        On macOS, drives AVSpeechSynthesizer via PyObjC. Resampling to
        self._sample_rate is done from the buffer's native format.

        IMPORTANT (implementer note): the PCM extraction below from
        AVAudioPCMBuffer.floatChannelData() via PyObjC is the fiddliest
        bit. The structure varies slightly across PyObjC versions. If
        the buffer pointer doesn't support indexing as written, consult
        `python -c "import AVFoundation; help(AVFoundation.AVAudioPCMBuffer)"`
        for the canonical access path on this PyObjC version. The unit
        test mocks this method out, so the test passes regardless of
        native plumbing — but a manual smoke test on a real Mac is
        required to confirm the implementation works in practice.
        """
        import AVFoundation
        import asyncio
        import array
        import audioop

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        synth = AVFoundation.AVSpeechSynthesizer.new()
        utt = AVFoundation.AVSpeechUtterance.speechUtteranceWithString_(text)
        if self._voice_identifier:
            voice = AVFoundation.AVSpeechSynthesisVoice.voiceWithIdentifier_(
                self._voice_identifier,
            )
            if voice is not None:
                utt.setVoice_(voice)

        call_count = {"n": 0, "bytes": 0}

        def callback(buffer):
            call_count["n"] += 1
            try:
                if buffer is None:
                    logger.debug(f"[avspeech] callback fired with buffer=None (call #{call_count['n']}); sending sentinel")
                    loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)
                    return

                fmt = buffer.format()
                channels = fmt.channelCount()
                native_sr = int(fmt.sampleRate())
                n_frames = buffer.frameLength()

                logger.debug(
                    f"[avspeech] callback #{call_count['n']}: "
                    f"buffer={type(buffer).__name__} channels={channels} "
                    f"native_sr={native_sr} n_frames={n_frames}"
                )

                if n_frames == 0:
                    logger.debug(f"[avspeech] zero-length buffer treated as EOF; sending sentinel")
                    loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)
                    return

                # Try int16ChannelData first (less common but cheaper).
                i16 = buffer.int16ChannelData()
                if i16 is not None:
                    logger.debug(f"[avspeech] using int16ChannelData path; i16[0] type={type(i16[0]).__name__}")
                    i16_bytes = _ptr_to_bytes(i16[0], n_frames * 2)
                else:
                    # Fall back to floatChannelData → convert.
                    fc = buffer.floatChannelData()
                    logger.debug(f"[avspeech] using floatChannelData path; fc type={type(fc).__name__}")
                    if fc is None:
                        logger.warning("[avspeech] floatChannelData() returned None; yielding empty")
                        loop.call_soon_threadsafe(queue.put_nowait, b"")
                        return
                    floats = array.array("f")
                    raw = _ptr_to_bytes(fc[0], n_frames * 4)
                    logger.debug(f"[avspeech] raw float bytes len={len(raw)}; first 16: {raw[:16].hex()}")
                    floats.frombytes(raw)
                    i16_bytes = array.array(
                        "h",
                        [max(-32768, min(32767, int(x * 32767))) for x in floats],
                    ).tobytes()

                # Resample if needed
                if native_sr != self._sample_rate:
                    logger.debug(f"[avspeech] resampling {native_sr} -> {self._sample_rate}")
                    i16_bytes, _ = audioop.ratecv(
                        i16_bytes, 2, channels, native_sr, self._sample_rate, None,
                    )

                call_count["bytes"] += len(i16_bytes)
                logger.debug(
                    f"[avspeech] emitting {len(i16_bytes)} int16 bytes "
                    f"(running total {call_count['bytes']}); "
                    f"first 16 bytes: {i16_bytes[:16].hex()}"
                )
                loop.call_soon_threadsafe(queue.put_nowait, i16_bytes)
            except Exception as e:
                logger.exception(f"[avspeech] callback error: {e!r}")
                loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)

        logger.debug(f"[avspeech] starting writeUtterance for {len(text)}-char text with voice={self._voice_identifier or '(default)'}")
        synth.writeUtterance_toBufferCallback_(utt, callback)

        chunks_yielded = 0
        while True:
            chunk = await queue.get()
            if chunk is SENTINEL:
                break
            if chunk:
                chunks_yielded += 1
                yield chunk

        logger.info(
            f"[avspeech] run complete: {call_count['n']} callbacks, "
            f"{call_count['bytes']} total bytes, {chunks_yielded} chunks yielded"
        )

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

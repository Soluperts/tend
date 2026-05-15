# SPDX-License-Identifier: MIT
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

        # push_start_frame / push_stop_frames let the base TTSService open an
        # audio context, emit TTSStartedFrame, and close it with TTSStoppedFrame
        # around each synthesis. Without these the audio frames yielded from
        # _stream_audio_frames_from_iterator have no context registered with
        # transport.output, so they're effectively dropped — synthesis "works"
        # but the speaker hears nothing. Piper does the same.
        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
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

        Drives AVSpeechSynthesizer.writeUtterance_toBufferCallback_ on the
        main thread. Two facts about that API on current macOS:

          1. It must run on the main thread — verified empirically;
             called from a worker thread (even with NSRunLoop pumped
             there) produces 0 callbacks.
          2. The callback fires only when the main NSRunLoop is being
             pumped. asyncio holds the main thread but doesn't pump
             NSRunLoop natively.

        So we ensure a background asyncio task is pumping the main
        NSRunLoop in 5 ms slices (see `_ensure_runloop_pumper`). That
        task is started lazily on first synthesis and lives for the
        rest of the asyncio loop's lifetime.

        PCM extraction details:
          - The buffer's floatChannelData() returns a tuple of
            `objc.varlist` objects, one per channel. `fc[0][:n]`
            returns a tuple of float32 values.
          - int16ChannelData() returns None for AVSpeechSynthesizer
            output; only the float path is available.
          - Native sample rate is typically 22050 Hz; we resample to
            self._sample_rate via audioop.ratecv.
        """
        import asyncio
        import audioop

        import AVFoundation
        import numpy as np

        loop = asyncio.get_running_loop()
        _ensure_runloop_pumper(loop)

        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()
        call_count = {"n": 0, "bytes": 0}
        # Resampler state must persist across callbacks for ratecv continuity.
        resampler_state: list = [None]

        def callback(buffer):
            call_count["n"] += 1
            try:
                if buffer is None:
                    loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)
                    return

                n_frames = buffer.frameLength()
                if n_frames == 0:
                    loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)
                    return

                fmt = buffer.format()
                channels = fmt.channelCount()
                native_sr = int(fmt.sampleRate())

                fc = buffer.floatChannelData()
                if fc is None:
                    logger.warning("[avspeech] floatChannelData() returned None; skipping buffer")
                    return

                # fc is a tuple of objc.varlist (one per channel). Read channel 0
                # as a Python tuple of floats, then vectorize the int16 conversion.
                # For mono this is the only channel; for multi-channel we drop
                # extras (AVSpeechSynthesizer is always mono in practice anyway).
                floats = np.asarray(fc[0][:n_frames], dtype=np.float32)
                i16 = np.clip(floats * 32767.0, -32768, 32767).astype(np.int16)
                pcm = i16.tobytes()

                if native_sr != self._sample_rate:
                    pcm, resampler_state[0] = audioop.ratecv(
                        pcm, 2, channels, native_sr, self._sample_rate,
                        resampler_state[0],
                    )

                call_count["bytes"] += len(pcm)
                loop.call_soon_threadsafe(queue.put_nowait, pcm)
            except Exception as e:
                logger.exception(f"[avspeech] callback error: {e!r}")
                loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)

        synth = AVFoundation.AVSpeechSynthesizer.new()
        utt = AVFoundation.AVSpeechUtterance.speechUtteranceWithString_(text)
        if self._voice_identifier:
            voice = AVFoundation.AVSpeechSynthesisVoice.voiceWithIdentifier_(
                self._voice_identifier,
            )
            if voice is not None:
                utt.setVoice_(voice)

        logger.debug(
            f"[avspeech] starting writeUtterance for {len(text)}-char text "
            f"with voice={self._voice_identifier or '(default)'}"
        )
        synth.writeUtterance_toBufferCallback_(utt, callback)

        chunks_yielded = 0
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[avspeech] synthesis timed out after 30s "
                    f"({call_count['n']} callbacks, {call_count['bytes']} bytes)"
                )
                break
            if chunk is SENTINEL:
                break
            if chunk:
                chunks_yielded += 1
                yield chunk

        logger.info(
            f"[avspeech] run complete: {call_count['n']} callbacks, "
            f"{call_count['bytes']} bytes, {chunks_yielded} chunks yielded"
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


# --- main-thread NSRunLoop pumping for AVSpeechSynthesizer -----------------

_runloop_pumper_task = None


def _ensure_runloop_pumper(loop) -> None:
    """Start a background task on `loop` that pumps the main NSRunLoop.

    AVSpeechSynthesizer dispatches its buffer callbacks via GCD to the main
    NSRunLoop. asyncio holds the main thread but doesn't pump NSRunLoop
    natively, so without this task `writeUtterance_toBufferCallback_`
    queues callbacks forever. Verified empirically (~200 callbacks fire in
    ~0.4 s for a short utterance with this pumper running; 0 callbacks
    without it).

    Idempotent — only one pumper runs per process.
    """
    import asyncio
    from Foundation import NSDate, NSRunLoop

    global _runloop_pumper_task
    if _runloop_pumper_task is not None and not _runloop_pumper_task.done():
        return

    nsloop = NSRunLoop.currentRunLoop()

    async def _pump() -> None:
        while True:
            # 5 ms NSRunLoop slice, then yield back to asyncio.
            nsloop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.005))
            await asyncio.sleep(0)

    _runloop_pumper_task = loop.create_task(
        _pump(), name="cocoa-main-runloop-pumper",
    )
    logger.info("[avspeech] started main-NSRunLoop pumper task")


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

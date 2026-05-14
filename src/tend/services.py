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

        We use the `say(1)` CLI rather than `AVSpeechSynthesizer.writeUtterance_toBufferCallback_`
        because the latter is broken on macOS: per Apple Developer Forums
        thread 122690 (and confirmed empirically — 0 callbacks in 30 s), the
        callback never fires on macOS regardless of threading or NSRunLoop
        pumping. `say` shells out to the same speech engine in a separate
        process, with a `--data-format` flag that produces real PCM.

        Flow:
          1. Write WAV-headered PCM to a temp file via `say -o tmpfile.wav
             --file-format=WAVE --data-format=LEI16@<rate>`.
          2. Read the file bytes and yield them as a single chunk; pipecat's
             `_stream_audio_frames_from_iterator(..., strip_wav_header=True)`
             parses the header and emits framed audio downstream.

        This preserves the AEC reference-signal path (Task 4's
        OutputAudioCapture sees the bytes via the same pipeline).
        """
        import asyncio
        import os
        import shlex
        import tempfile

        # `say -v` takes the short voice name (e.g. "Matilda"), not the long
        # identifier ("com.apple.voice.premium.en-AU.Matilda"). The short
        # name is the last dot-separated component.
        voice_short = (
            self._voice_identifier.rsplit(".", 1)[-1]
            if self._voice_identifier else ""
        )

        # NamedTemporaryFile with delete=False so we control unlink in finally.
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        wav_path = tmp.name

        args = [
            "say",
            "-o", wav_path,
            "--file-format=WAVE",
            f"--data-format=LEI16@{self._sample_rate}",
        ]
        if voice_short:
            args.extend(["-v", voice_short])
        args.append(text)

        logger.debug(f"[avspeech] running: {shlex.join(args)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    f"[avspeech] say exited {proc.returncode}: "
                    f"{stderr.decode(errors='replace')!r}"
                )
                return

            size = os.path.getsize(wav_path)
            if size < 44:
                logger.warning(f"[avspeech] say wrote suspiciously small file ({size} bytes)")
                return

            with open(wav_path, "rb") as f:
                # Yield the whole WAV (header + data). Pipecat's
                # _stream_audio_frames_from_iterator(strip_wav_header=True)
                # strips the header on first chunk and emits audio frames.
                yield f.read()

            logger.info(f"[avspeech] say complete for {len(text)}-char text ({size} bytes)")
        except FileNotFoundError:
            logger.error("[avspeech] `say` binary not found on PATH")
        except Exception as e:
            logger.exception(f"[avspeech] say invocation failed: {e!r}")
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

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
                strip_wav_header=True,
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

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
    """Return (service, output_sample_rate).

    Output rate is pinned to settings.sample_rate so the local audio transport
    can play it without resampling (USB mic-arrays like the reSpeaker are
    typically rate-locked to 16 kHz). ElevenLabs serves PCM at the requested
    rate; Piper resamples internally from the voice's native rate.
    """
    rate = settings.sample_rate
    if settings.elevenlabs_api_key:
        err = _check_elevenlabs(settings.elevenlabs_api_key)
        if err:
            logger.warning(f"ElevenLabs preflight failed: {err}; falling back to local Piper")
        else:
            from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
            return (
                ElevenLabsTTSService(
                    api_key=settings.elevenlabs_api_key,
                    sample_rate=rate,
                    settings=ElevenLabsTTSService.Settings(
                        voice=settings.elevenlabs_voice_id,
                        model=settings.elevenlabs_model,
                    ),
                ),
                rate,
            )
    from pipecat.services.piper.tts import PiperTTSService
    return (
        PiperTTSService(
            settings=PiperTTSService.Settings(voice=settings.piper_voice),
            sample_rate=rate,
        ),
        rate,
    )


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

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
            settings_kwargs: dict = {
                "voice": settings.elevenlabs_voice_id,
                "model": settings.elevenlabs_model,
            }
            if settings.elevenlabs_speed is not None:
                settings_kwargs["speed"] = settings.elevenlabs_speed
            return (
                ElevenLabsTTSService(
                    api_key=settings.elevenlabs_api_key,
                    sample_rate=rate,
                    settings=ElevenLabsTTSService.Settings(**settings_kwargs),
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
        super().__init__(sample_rate=sample_rate, **kwargs)
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

        def callback(buffer):
            if buffer is None:
                loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)
                return

            fmt = buffer.format()
            channels = fmt.channelCount()
            native_sr = int(fmt.sampleRate())
            n_frames = buffer.frameLength()

            # Try int16ChannelData first (less common but cheaper).
            i16 = buffer.int16ChannelData()
            if i16 is not None:
                i16_bytes = _ptr_to_bytes(i16[0], n_frames * 2)
            else:
                # Fall back to floatChannelData → convert.
                fc = buffer.floatChannelData()
                if fc is None:
                    loop.call_soon_threadsafe(queue.put_nowait, b"")
                    return
                floats = array.array("f")
                floats.frombytes(_ptr_to_bytes(fc[0], n_frames * 4))
                i16_bytes = array.array(
                    "h",
                    [max(-32768, min(32767, int(x * 32767))) for x in floats],
                ).tobytes()

            # Resample if needed
            if native_sr != self._sample_rate:
                i16_bytes, _ = audioop.ratecv(
                    i16_bytes, 2, channels, native_sr, self._sample_rate, None,
                )
            loop.call_soon_threadsafe(queue.put_nowait, i16_bytes)

        synth.writeUtterance_toBufferCallback_(utt, callback)

        while True:
            chunk = await queue.get()
            if chunk is SENTINEL:
                break
            if chunk:
                yield chunk

    async def run_tts(self, text: str):
        from pipecat.frames.frames import (
            TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame,
        )

        yield TTSStartedFrame()
        async for pcm in self._synthesize_to_pcm(text):
            yield TTSAudioRawFrame(
                audio=pcm, sample_rate=self._sample_rate, num_channels=1,
            )
        yield TTSStoppedFrame()


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

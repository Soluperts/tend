"""AVAudioEngine-based transport for macOS, with VoiceProcessingIO enabled.

Mirrors pipecat's LocalAudioTransport shape so it's pasteable upstream as
pipecat.transports.local.av_audio when we file the PR.

Why this exists: built-in MacBook mic+speaker generate echo that the speex
software AEC (~12-26 dB) doesn't suppress below Deepgram's transcription
threshold. AVAudioEngine + setVoiceProcessingEnabled gives OS-grade AEC +
NS + AGC — same path FaceTime uses — which drops the residual below
Deepgram's threshold and stops the bot from transcribing its own voice
back into the LLM context.
"""

from __future__ import annotations

from pipecat.transports.base_transport import TransportParams


class AVAudioTransportParams(TransportParams):
    """Parameters for AVAudioTransport.

    No extra fields for v1. Reserved for input_device_uid / output_device_uid
    additions later — AVAudioEngine uses CoreAudio device UIDs (strings) not
    PyAudio indices, so adding them would be a separate decision.
    """

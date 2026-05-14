"""AVAudioEngine-based transport for macOS, with VoiceProcessingIO enabled.

Why this exists: built-in MacBook mic+speaker generate echo that the speex
software AEC (~12-26 dB) doesn't suppress below Deepgram's transcription
threshold. AVAudioEngine + setVoiceProcessingEnabled gives OS-grade AEC +
NS + AGC — same path FaceTime uses — which drops the residual below
Deepgram's threshold and stops the bot from transcribing its own voice
back into the LLM context.

AVAudioEngine uses CoreAudio device UIDs (strings), not PyAudio indices,
so device-selection fields would require a different shape than
LocalAudioTransportParams — that's why no device fields are added in v1.
"""

from __future__ import annotations

from pipecat.transports.base_transport import TransportParams


class AVAudioTransportParams(TransportParams):
    """Parameters for AVAudioTransport."""

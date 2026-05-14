"""LocalAudioTransport variants that tap output PCM into the AEC reference buffer.

Why this exists: pipecat's `LocalAudioOutputTransport` is opened in PortAudio
blocking-write mode (no `stream_callback`). That means `stream.write(bytes)`
blocks until PortAudio's internal buffer has space — naturally throttling
the write rate to playback-rate.

If we tap inside `write_audio_frame` (just before the blocking write), the
data we tap is one PortAudio buffer (~30-50 ms) ahead of what will actually
hit the speaker. Speex AEC's adaptive delay estimation handles that easily.

Compare to a FrameProcessor sitting in the pipeline before `transport.output()`:
TTS synthesis produces a whole utterance in ~0.4 s while playback takes ~6 s,
so the synthesis-rate tap puts 6 s of "future" audio in the ring buffer in
0.4 s. AEC reads the most recent reference bytes for the current mic chunk
and gets audio that won't play for several seconds — no correlation with
the echo, AEC subtracts nothing.

This module replaces `OutputAudioCapture` for the AEC reference path. The
FrameProcessor variant is kept around for cases that don't care about
timing (e.g., debug logging of TTS audio).
"""

from __future__ import annotations

import pyaudio  # noqa: F401 — required by pipecat's transport
from pipecat.frames.frames import OutputAudioRawFrame
from pipecat.transports.local.audio import (
    LocalAudioOutputTransport,
    LocalAudioTransport,
    LocalAudioTransportParams,
)

from tend.audio.aec import ReferenceBuffer


class _RefTappedOutputTransport(LocalAudioOutputTransport):
    """LocalAudioOutputTransport that writes each frame to an AEC reference buffer.

    The tap runs synchronously before the parent's blocking PortAudio write.
    This places the reference signal ~one-PortAudio-buffer ahead of speaker
    output, which is within speex AEC's tolerance for adaptive delay.
    """

    def __init__(self, py_audio, params, *, reference: ReferenceBuffer):
        super().__init__(py_audio, params)
        self._aec_reference = reference

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        # Write reference first, then let the parent block on the realtime
        # PortAudio write. Order matters: we want the reference visible to
        # the AEC filter slightly before the audio reaches the speaker, not
        # after.
        self._aec_reference.write(frame.audio)
        return await super().write_audio_frame(frame)


class RefTappedLocalAudioTransport(LocalAudioTransport):
    """LocalAudioTransport whose output transport feeds an AEC reference buffer.

    Use when AEC is enabled. For the AEC-off case, prefer plain
    `LocalAudioTransport` — there's no consumer for the reference.
    """

    def __init__(self, params: LocalAudioTransportParams, *, reference: ReferenceBuffer):
        super().__init__(params)
        self._aec_reference = reference

    def output(self):
        if not self._output:
            self._output = _RefTappedOutputTransport(
                self._pyaudio, self._params, reference=self._aec_reference,
            )
        return self._output

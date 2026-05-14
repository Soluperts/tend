"""OutputAudioCapture — tap speaker frames into the AEC ReferenceBuffer.

Sits in the Hub pipeline just before `transport.output()` so it sees
every TTS chunk on its way to the speaker. Pure pass-through; the
captured bytes are read back by the AEC filter sitting on the input
side of the same pipeline.
"""

from __future__ import annotations

from pipecat.frames.frames import Frame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from tend.audio.aec import ReferenceBuffer


class OutputAudioCapture(FrameProcessor):
    def __init__(self, reference: ReferenceBuffer, **kwargs):
        super().__init__(**kwargs)
        self._reference = reference

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            self._reference.write(frame.audio)
        await self.push_frame(frame, direction)

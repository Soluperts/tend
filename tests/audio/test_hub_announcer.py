"""Hub drains the announcer when Brain transitions inactive."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tend.audio.hub import Hub
from tend.config import Settings


async def test_on_brain_deactivated_drains_announcer():
    bus = MagicMock()
    bus.publish = AsyncMock()
    announcer = MagicMock()
    announcer.drain_pending = AsyncMock()
    settings = Settings()
    stt = MagicMock()
    tts = MagicMock()
    brain = MagicMock()
    hub = Hub(
        "hub", bus=bus, settings=settings,
        stt=stt, tts=tts, tts_sample_rate=16000,
        brain=brain, announcer=announcer,
    )
    await hub.on_brain_deactivated()
    announcer.drain_pending.assert_awaited_once()


async def test_on_brain_deactivated_no_announcer_safe():
    bus = MagicMock()
    settings = Settings()
    hub = Hub(
        "hub", bus=bus, settings=settings,
        stt=MagicMock(), tts=MagicMock(), tts_sample_rate=16000,
        brain=MagicMock(),
    )
    # Should not raise even without announcer.
    await hub.on_brain_deactivated()

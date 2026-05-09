"""Hub injects available-skills XML into Brain's system prompt."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tend.audio.hub import Hub
from tend.config import Settings


def _seed_skill(root: Path, name: str, description: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"
    )


def _make_hub(skills_root: Path | None) -> Hub:
    bus = MagicMock()
    bus.publish = AsyncMock()
    return Hub(
        "hub",
        bus=bus,
        settings=Settings(),
        stt=MagicMock(),
        tts=MagicMock(),
        tts_sample_rate=16000,
        brain=MagicMock(),
        skills_root=skills_root,
    )


async def test_reset_session_includes_available_skills_xml(tmp_path):
    _seed_skill(tmp_path, "briefing", "Morning briefing")
    _seed_skill(tmp_path, "lunch-prep", "Suggest lunch")

    hub = _make_hub(tmp_path)
    await hub.reset_session("You are tend.")

    messages = hub.context.get_messages()
    assert len(messages) == 1
    content = messages[0]["content"]
    assert "<available-skills>" in content
    assert '<skill name="briefing">Morning briefing</skill>' in content
    assert '<skill name="lunch-prep">Suggest lunch</skill>' in content
    assert "</available-skills>" in content


async def test_reset_session_omits_block_when_no_skills(tmp_path):
    hub = _make_hub(tmp_path)  # tmp_path empty
    await hub.reset_session("You are tend.")
    content = hub.context.get_messages()[0]["content"]
    assert "<available-skills>" not in content


async def test_reset_session_omits_block_when_skills_root_none():
    hub = _make_hub(skills_root=None)
    await hub.reset_session("You are tend.")
    content = hub.context.get_messages()[0]["content"]
    assert "<available-skills>" not in content


async def test_reset_session_includes_voice_rules_dispatch_guidance(tmp_path):
    _seed_skill(tmp_path, "briefing", "x")
    hub = _make_hub(tmp_path)
    await hub.reset_session("soul")
    content = hub.context.get_messages()[0]["content"]
    assert "do_task" in content
    assert "internet" in content.lower()

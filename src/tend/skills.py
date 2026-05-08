"""Skills layer for tend's general worker.

Loads procedure markdown from ~/.tend/skills/<name>/SKILL.md, hands the
worker a compact catalog at spawn time, and gates new on-disk files
(SKILL.md bodies and ~/.tend/workspace/bin/ scripts) through a regex
safety scanner before they get executed.

See docs/superpowers/specs/2026-05-07-deskclaw-skills-and-general-worker-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from loguru import logger


class SkillFrontmatterError(ValueError):
    """The SKILL.md frontmatter block is missing or malformed."""


@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str


_FENCE = "---"


def parse_frontmatter(text: str) -> SkillFrontmatter:
    """Parse the YAML-ish frontmatter at the start of a SKILL.md.

    v1 only supports two single-line keys: name, description. Both are
    required. Values may be optionally wrapped in single or double quotes.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise SkillFrontmatterError("no frontmatter fence at start of file")
    fields: dict[str, str] = {}
    closed = False
    bad_line: str | None = None
    for raw in lines[1:]:
        if raw.strip() == _FENCE:
            closed = True
            break
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            # Defer raising — if the block is also unterminated, that's the
            # more useful error to surface.
            if bad_line is None:
                bad_line = raw
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        fields[key] = value
    if not closed:
        raise SkillFrontmatterError("unterminated frontmatter (no closing ---)")
    if bad_line is not None:
        raise SkillFrontmatterError(f"unparseable frontmatter line: {bad_line!r}")
    if not fields.get("name"):
        raise SkillFrontmatterError("frontmatter missing required key: name")
    if not fields.get("description"):
        raise SkillFrontmatterError("frontmatter missing required key: description")
    return SkillFrontmatter(name=fields["name"], description=fields["description"])


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path


def enumerate_skills(root: Path) -> list[SkillInfo]:
    """Return all valid skills under <root>, sorted alphabetically by name.

    Quietly skips directories without a SKILL.md and SKILL.md files whose
    frontmatter doesn't parse — a malformed skill should not break the
    catalog for the rest.
    """
    if not root.exists():
        return []
    out: list[SkillInfo] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text()
            fm = parse_frontmatter(text)
        except (OSError, SkillFrontmatterError) as e:
            logger.warning(f"skipping malformed skill at {skill_md}: {e}")
            continue
        out.append(SkillInfo(
            name=fm.name,
            description=fm.description,
            path=skill_md.resolve(),
        ))
    out.sort(key=lambda s: s.name)
    return out


def format_catalog_xml(skills: list[SkillInfo]) -> str:
    """Render the <available-skills> XML block for the worker prompt.

    Returns "" for an empty list so the caller can omit the section
    entirely without a special case.
    """
    if not skills:
        return ""
    lines = ["<available-skills>"]
    for s in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{_xml_escape(s.name, {'\"': '&quot;'})}</name>")
        lines.append(
            f"    <description>{_xml_escape(s.description, {'\"': '&quot;'})}</description>"
        )
        lines.append(
            f"    <path>{_xml_escape(str(s.path), {'\"': '&quot;'})}</path>"
        )
        lines.append("  </skill>")
    lines.append("</available-skills>")
    return "\n".join(lines)

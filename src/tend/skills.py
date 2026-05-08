"""Skills layer for tend's general worker.

Loads procedure markdown from ~/.tend/skills/<name>/SKILL.md, hands the
worker a compact catalog at spawn time, and gates new on-disk files
(SKILL.md bodies and ~/.tend/workspace/bin/ scripts) through a regex
safety scanner before they get executed.

See docs/superpowers/specs/2026-05-07-deskclaw-skills-and-general-worker-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    if "name" not in fields:
        raise SkillFrontmatterError("frontmatter missing required key: name")
    if "description" not in fields:
        raise SkillFrontmatterError("frontmatter missing required key: description")
    return SkillFrontmatter(name=fields["name"], description=fields["description"])

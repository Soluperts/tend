"""Skills layer for tend's general worker.

Loads procedure markdown from ~/.tend/skills/<name>/SKILL.md, hands the
worker a compact catalog at spawn time, and gates new on-disk files
(SKILL.md bodies and ~/.tend/workspace/bin/ scripts) through a regex
safety scanner before they get executed.

See docs/superpowers/specs/2026-05-07-deskclaw-skills-and-general-worker-design.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape as _xml_escape

from loguru import logger


class SkillFrontmatterError(ValueError):
    """The SKILL.md frontmatter block is missing or malformed."""


@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str


_FENCE = "---"


_QUOT_MAP = {'"': "&quot;"}


def _esc(s: str) -> str:
    """XML-escape including double quotes."""
    return _xml_escape(s, _QUOT_MAP)


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
    entirely without a special case. The output has no leading or
    trailing newline — the caller controls separators when concatenating
    with other prompt text.
    """
    if not skills:
        return ""
    lines = ["<available-skills>"]
    for s in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{_esc(s.name)}</name>")
        lines.append(f"    <description>{_esc(s.description)}</description>")
        lines.append(f"    <path>{_esc(str(s.path))}</path>")
        lines.append("  </skill>")
    lines.append("</available-skills>")
    return "\n".join(lines)


Severity = Literal["critical", "warn"]


@dataclass(frozen=True)
class ScanFinding:
    rule: str
    severity: Severity
    line: int  # 1-based
    snippet: str


@dataclass(frozen=True)
class ScanReport:
    findings: tuple[ScanFinding, ...]

    @property
    def is_critical(self) -> bool:
        return any(f.severity == "critical" for f in self.findings)

    @property
    def is_clean(self) -> bool:
        return len(self.findings) == 0


# Each rule: (rule_id, severity, compiled regex). Patterns are intentionally
# narrow — false positives go to quarantine, which is friction the user
# can't bypass without inspection, so we err toward precision over recall.
_RULES: list[tuple[str, Severity, re.Pattern[str]]] = [
    (
        "prompt-injection-ignore-instructions",
        "critical",
        re.compile(
            r"\bignore\b[^.\n]{0,40}\b(prior|previous|above|earlier|system)\s+instructions?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt-injection-system",
        "critical",
        re.compile(
            r"\b(disregard|override)\b[^.\n]{0,40}\b(system\s+prompt|developer\s+message|hidden\s+instructions?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt-injection-tool",
        "critical",
        re.compile(
            r"\b(bypass|skip)\s+(?:tool\s+)?(approval|permission|consent)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "shell-pipe-to-shell",
        "critical",
        re.compile(r"\b(curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(sh|bash|zsh)\b", re.IGNORECASE),
    ),
    (
        "secret-exfiltration",
        "critical",
        re.compile(
            r"\b(curl|wget|nc)\b[^\n]*\$\{?(ANTHROPIC_API_KEY|DEEPGRAM_API_KEY|ELEVENLABS_API_KEY|OPENAI_API_KEY|AWS_SECRET_ACCESS_KEY|GITHUB_TOKEN)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "destructive-delete",
        "warn",
        re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?[a-zA-Z]*\s+(/|~|\$HOME)", re.IGNORECASE),
    ),
    (
        "unsafe-permissions",
        "warn",
        re.compile(r"\bchmod\s+0?777\b", re.IGNORECASE),
    ),
]


def scan_text(text: str) -> ScanReport:
    """Run the v1 safety rules over a blob of text. No I/O."""
    findings: list[ScanFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule_id, severity, pattern in _RULES:
            m = pattern.search(line)
            if m:
                findings.append(ScanFinding(
                    rule=rule_id, severity=severity, line=lineno,
                    snippet=line.strip()[:200],
                ))
    return ScanReport(findings=tuple(findings))

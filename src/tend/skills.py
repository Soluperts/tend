"""Skills layer for tend's general worker.

Loads procedure markdown from ~/.tend/skills/<name>/SKILL.md, hands the
worker a compact catalog at spawn time, and gates new on-disk files
(SKILL.md bodies and ~/.tend/workspace/bin/ scripts) through a regex
safety scanner before they get executed.

See docs/superpowers/specs/2026-05-07-deskclaw-skills-and-general-worker-design.md
and docs/superpowers/specs/2026-05-08-tend-proactive-triggers-design.md.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
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
    triggers: tuple[dict, ...] = ()
    events: tuple[str, ...] = ()
    silent_default: bool = False


_FENCE = "---"


_QUOT_MAP = {'"': "&quot;"}


def _esc(s: str) -> str:
    """XML-escape including double quotes."""
    return _xml_escape(s, _QUOT_MAP)


def _strip_quotes(s: str) -> str:
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1]
    return s


def _parse_yaml_frontmatter_block(text: str) -> tuple[dict, str | None]:
    """Minimal yaml-ish parser for our frontmatter dialect.

    Supports:
      key: value          (scalar)
      key:                (block — list of scalars or dicts)
        - scalar
        - nested_key: nested_value
          nested_key2: nested_value2

    Quotes are stripped on scalars.  Returns ``(fields, bad_line)`` where
    ``bad_line`` is the first unparseable top-level line encountered (or
    ``None`` if everything parsed cleanly).  The caller decides whether to
    raise on a bad line — deferring until after the fence-closure check
    allows the more useful "unterminated" error to win.

    Raises ``SkillFrontmatterError`` for structural problems (no opening
    fence, no closing fence).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise SkillFrontmatterError("no frontmatter fence at start of file")

    closed = False
    body_lines: list[str] = []
    for raw in lines[1:]:
        if raw.strip() == _FENCE:
            closed = True
            break
        body_lines.append(raw)

    if not closed:
        raise SkillFrontmatterError("unterminated frontmatter (no closing ---)")

    out: dict = {}
    bad_line: str | None = None
    i = 0
    while i < len(body_lines):
        line = body_lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if line.startswith(" ") or line.startswith("\t"):
            # stray indented line at top level — skip
            i += 1
            continue
        if ":" not in stripped:
            # Unparseable top-level line — defer to let caller prioritise
            # "unterminated" over "unparseable".
            if bad_line is None:
                bad_line = line
            i += 1
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if value:
            # Scalar value on the same line
            out[key] = _strip_quotes(value)
            i += 1
            continue
        # No inline value — consume an indented block (list of scalars or dicts)
        items: list = []
        i += 1
        cur_map: dict | None = None
        while i < len(body_lines):
            l2 = body_lines[i]
            if not l2.strip():
                i += 1
                continue
            if not (l2.startswith("  ") or l2.startswith("\t")):
                # Back at top level — stop consuming the block
                break
            stripped2 = l2.strip()
            if stripped2.startswith("- "):
                # New list item
                if cur_map is not None:
                    items.append(cur_map)
                first_kv = stripped2[2:].strip()
                if ":" in first_kv:
                    k, _, v = first_kv.partition(":")
                    cur_map = {k.strip(): _strip_quotes(v.strip())}
                else:
                    items.append(_strip_quotes(first_kv))
                    cur_map = None
                i += 1
                continue
            # Continuation key-value inside the current dict item
            if cur_map is not None and ":" in stripped2:
                k, _, v = stripped2.partition(":")
                cur_map[k.strip()] = _strip_quotes(v.strip())
                i += 1
                continue
            i += 1  # skip unrecognised indented lines
        if cur_map is not None:
            items.append(cur_map)
        out[key] = items

    return out, bad_line


def parse_frontmatter(text: str) -> SkillFrontmatter:
    """Parse the YAML-ish frontmatter at the start of a SKILL.md."""
    fields, bad_line = _parse_yaml_frontmatter_block(text)

    if bad_line is not None:
        raise SkillFrontmatterError(f"unparseable frontmatter line: {bad_line!r}")

    name = fields.get("name", "")
    description = fields.get("description", "")

    # An empty block-parse produces [] for name/description — treat as missing.
    if not name:
        raise SkillFrontmatterError("frontmatter missing required key: name")
    if not description:
        raise SkillFrontmatterError("frontmatter missing required key: description")

    triggers_raw = fields.get("triggers") or []
    triggers: list[dict] = []
    if isinstance(triggers_raw, list):
        for item in triggers_raw:
            if isinstance(item, dict):
                triggers.append(item)
            # non-dict entries (e.g. bare scalars) are silently skipped

    events_raw = fields.get("events") or []
    events: list[str] = []
    if isinstance(events_raw, list):
        for item in events_raw:
            if isinstance(item, str):
                events.append(item)

    silent = str(fields.get("silent_default", "")).strip().lower() == "true"

    return SkillFrontmatter(
        name=str(name),
        description=str(description),
        triggers=tuple(triggers),
        events=tuple(events),
        silent_default=silent,
    )


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path
    triggers: tuple[dict, ...] = ()
    events: tuple[str, ...] = ()
    silent_default: bool = False


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
        if child.name.startswith((".", "_")):
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
            triggers=fm.triggers,
            events=fm.events,
            silent_default=fm.silent_default,
        ))
    out.sort(key=lambda s: s.name)
    return out


def find_event_subscribers(root: Path, kind: str) -> list[SkillInfo]:
    """Return all skills under <root> whose ``events:`` list includes ``kind``."""
    return [s for s in enumerate_skills(root) if kind in s.events]


def enumerate_all_skills() -> list[SkillInfo]:
    """Runtime catalog: critical skills (wheel) ∪ user skills, user wins on collision."""
    from tend import paths

    critical = enumerate_skills(paths.critical_skills_dir())
    user = enumerate_skills(paths.user_skills_dir())

    by_name: dict[str, SkillInfo] = {s.name: s for s in critical}
    for s in user:
        if s.name in by_name:
            logger.warning(
                f"user skill {s.name!r} at {s.path} shadows critical skill "
                f"at {by_name[s.name].path}"
            )
        by_name[s.name] = s

    return sorted(by_name.values(), key=lambda s: s.name)


def find_event_subscribers_all(kind: str) -> list[SkillInfo]:
    """All skills in the merged runtime catalog whose ``events:`` list includes ``kind``."""
    return [s for s in enumerate_all_skills() if kind in s.events]


def enumerate_installable_skills() -> list[SkillInfo]:
    """Shipped optional skills not yet present in the user's $TEND_HOME/skills/."""
    from tend import paths

    shipped = enumerate_skills(paths.shipped_skills_dir())
    user_names = {s.name for s in enumerate_skills(paths.user_skills_dir())}
    return [s for s in shipped if s.name not in user_names]


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


def atomic_write_text(path: Path, content: str) -> None:
    """Write content to path atomically. Parent dirs are created if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def quarantine_skill(
    *,
    name: str,
    skills_root: Path,
    quarantine_root: Path,
    findings: Iterable[ScanFinding],
) -> Path:
    """Move skills_root/<name>/ to quarantine_root/<name>/ + write findings sidecar.

    Returns the final destination path. If quarantine_root/<name>/ already
    exists, appends .1, .2, ... until a free name is found.
    """
    src = skills_root / name
    quarantine_root.mkdir(parents=True, exist_ok=True)
    dest = quarantine_root / name
    suffix = 1
    while dest.exists():
        dest = quarantine_root / f"{name}.{suffix}"
        suffix += 1
    shutil.move(str(src), str(dest))
    findings_data = [
        {"rule": f.rule, "severity": f.severity, "line": f.line, "snippet": f.snippet}
        for f in findings
    ]
    (dest / "_findings.json").write_text(json.dumps(findings_data, indent=2), encoding="utf-8")
    return dest

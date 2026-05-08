import textwrap
from pathlib import Path

import pytest

from tend.skills import (
    enumerate_skills,
    format_catalog_xml,
    parse_frontmatter,
    SkillFrontmatter,
    SkillFrontmatterError,
    SkillInfo,
)


def test_parse_frontmatter_minimal():
    text = textwrap.dedent("""\
        ---
        name: meal-plan
        description: Generate a 3-day meal plan from current fridge contents.
        ---

        # Meal Plan

        Body text follows.
    """)
    fm = parse_frontmatter(text)
    assert fm.name == "meal-plan"
    assert fm.description == "Generate a 3-day meal plan from current fridge contents."


def test_parse_frontmatter_strips_quotes():
    # Some authors will quote values; accept both.
    text = "---\nname: \"x\"\ndescription: 'y'\n---\nbody"
    fm = parse_frontmatter(text)
    assert fm.name == "x"
    assert fm.description == "y"


def test_parse_frontmatter_missing_block():
    with pytest.raises(SkillFrontmatterError, match="no frontmatter"):
        parse_frontmatter("# Just a heading\n")


def test_parse_frontmatter_missing_name():
    text = "---\ndescription: a thing\n---\nbody"
    with pytest.raises(SkillFrontmatterError, match="name"):
        parse_frontmatter(text)


def test_parse_frontmatter_missing_description():
    text = "---\nname: x\n---\nbody"
    with pytest.raises(SkillFrontmatterError, match="description"):
        parse_frontmatter(text)


def test_parse_frontmatter_empty_value_treated_as_missing():
    text = "---\nname:\ndescription: y\n---\nbody"
    with pytest.raises(SkillFrontmatterError, match="name"):
        parse_frontmatter(text)


def test_parse_frontmatter_unterminated():
    with pytest.raises(SkillFrontmatterError, match="unterminated"):
        parse_frontmatter("---\nname: x\ndescription: y\nno closing fence\n")


def _write_skill(root, name, description, body="body\n"):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
    )
    return skill_dir / "SKILL.md"


def test_enumerate_skills_empty(tmp_path):
    assert enumerate_skills(tmp_path) == []


def test_enumerate_skills_returns_sorted_by_name(tmp_path):
    _write_skill(tmp_path, "zeta", "z desc")
    _write_skill(tmp_path, "alpha", "a desc")
    _write_skill(tmp_path, "mu", "m desc")
    skills = enumerate_skills(tmp_path)
    assert [s.name for s in skills] == ["alpha", "mu", "zeta"]
    assert [s.description for s in skills] == ["a desc", "m desc", "z desc"]
    for s in skills:
        assert s.path.is_absolute()
        assert s.path.name == "SKILL.md"


def test_enumerate_skills_skips_dirs_without_skill_md(tmp_path):
    (tmp_path / "no_skill_here").mkdir()
    _write_skill(tmp_path, "real", "ok")
    assert [s.name for s in enumerate_skills(tmp_path)] == ["real"]


def test_enumerate_skills_skips_malformed(tmp_path, caplog):
    _write_skill(tmp_path, "good", "ok")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("not valid frontmatter at all\n")
    skills = enumerate_skills(tmp_path)
    assert [s.name for s in skills] == ["good"]


def test_enumerate_skills_missing_root(tmp_path):
    assert enumerate_skills(tmp_path / "nope") == []


def test_format_catalog_xml_empty():
    assert format_catalog_xml([]) == ""


def test_format_catalog_xml_renders_skills(tmp_path):
    skills = [
        SkillInfo(name="alpha", description="A desc", path=Path("/x/alpha/SKILL.md")),
        SkillInfo(name="beta",  description="B desc", path=Path("/x/beta/SKILL.md")),
    ]
    out = format_catalog_xml(skills)
    assert "<available-skills>" in out
    assert "</available-skills>" in out
    assert "<name>alpha</name>" in out
    assert "<description>A desc</description>" in out
    assert "<path>/x/alpha/SKILL.md</path>" in out
    assert "<name>beta</name>" in out


def test_format_catalog_xml_escapes_special_chars():
    skills = [SkillInfo(
        name="x", description="A & B <c> \"d\"", path=Path("/p/SKILL.md"),
    )]
    out = format_catalog_xml(skills)
    assert "A &amp; B &lt;c&gt; &quot;d&quot;" in out
    assert "<description>A & B" not in out  # raw ampersand must not appear

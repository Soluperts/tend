import textwrap
import pytest

from tend.skills import parse_frontmatter, SkillFrontmatter, SkillFrontmatterError


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

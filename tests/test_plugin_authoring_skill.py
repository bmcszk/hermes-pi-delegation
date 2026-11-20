"""The authoring skill ships as a loadable skill: frontmatter contract and size."""

import os

import pytest

SKILL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "skills", "plugin-authoring", "SKILL.md"
)


def _frontmatter(text):
    assert text.startswith("---\n"), text[:40]
    end = text.index("\n---\n", 4)
    return text[4:end].splitlines(), text[end + 5 :]


def _field(front, key):
    """Frontmatter values, including the ones nested under `metadata:`."""
    for line in front:
        if line.strip().startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    raise AssertionError(f"missing frontmatter field: {key}")


@pytest.fixture
def skill_text():
    with open(SKILL_PATH) as f:
        return f.read()


def test_plugin_authoring_skill_frontmatter_is_loadable(skill_text):
    # given
    front, _ = _frontmatter(skill_text)

    # then
    assert _field(front, "name") == "plugin-authoring"
    description = _field(front, "description")
    assert len(description) <= 60, description
    assert description.endswith("."), description
    assert _field(front, "github-path") == "skills/plugin-authoring"
    assert _field(front, "github-repo").endswith("hermes-pi-delegation")


def test_plugin_authoring_skill_body_stays_within_line_budget(skill_text):
    # given
    _, body = _frontmatter(skill_text)

    # then a skill is read whole: keep it under the budget
    assert len(body.splitlines()) < 150, len(body.splitlines())

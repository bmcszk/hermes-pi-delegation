"""Plugin-suite fixtures: one world per test, no machine-HOME reads."""

import pytest

import __init__ as p


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    """Every test builds its own world: no test may read the machine's real HOME."""
    agent_dir = tmp_path / "agent-worker"
    agent_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(p, "WORKER_AGENT_DIR", str(agent_dir))
    monkeypatch.setattr(p, "REGISTRY_PATH", str(tmp_path / "pi-agents.jsonl"))
    return tmp_path


@pytest.fixture
def tmp_skill(tmp_path):
    """A fake skill dir the test owns: no dependency on the machine's real skill roots."""
    skill_dir = tmp_path / "skills" / "fake-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: fake-skill\n---\ntest\n")
    return skill_dir

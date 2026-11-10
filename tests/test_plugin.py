import os
import shlex

from helpers import (
    CODING_RULES,
    CONTRACT_MD,
    GO_DEV,
    TMP_CWD,
    _parse,
)

import __init__ as p
from __init__ import WORKER_TYPES, pi_launch


def test_pi_launch_standard_coder():
    """Test standard mode coder launch command generation."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [CODING_RULES, GO_DEV],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epic-1n0a",
                "mode": "standard",
            }
        )
    )
    assert "error" not in result, result
    assert result["session_id"] == "epic-1n0a-coder"
    assert result["worker_type"] == "coder"
    assert result["mode"] == "standard"
    cmd = result["command"]
    parts = shlex.split(cmd)
    # the caller-supplied model alias lands 1:1 on the worker argv
    assert parts[parts.index("--model") + 1] == "anything"
    assert "--session-id" in parts and "epic-1n0a-coder" in parts
    assert "--skill" in parts and CODING_RULES in parts
    assert GO_DEV in parts
    assert "--codemode" in parts
    assert "--ns" not in parts


def test_pi_launch_emits_worker_agent_dir():
    """Every standard launch points pi at the worker agent dir (no
    pi-subagents, no-delegation AGENTS.md) when it exists on disk."""
    assert os.path.isdir(p.WORKER_AGENT_DIR), "worker agent dir missing: " + p.WORKER_AGENT_DIR
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [CODING_RULES],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epic-1n0a",
                "mode": "standard",
            }
        )
    )
    assert "error" not in result, result
    parts = shlex.split(result["command"])
    i = parts.index("--agent-dir")
    assert parts[i + 1] == p.WORKER_AGENT_DIR


def test_pi_launch_warns_when_worker_agent_dir_missing(monkeypatch, tmp_path):
    """No worker agent dir on disk -> launch still works but carries a warning."""
    monkeypatch.setattr(p, "WORKER_AGENT_DIR", str(tmp_path / "nope"))
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [CODING_RULES],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epic-1n0a",
                "mode": "standard",
            }
        )
    )
    assert "error" not in result, result
    assert any("pi-subagents" in w or "AGENTS.md" in w for w in result.get("warnings", []))
    assert "--agent-dir" not in shlex.split(result["command"])


def test_pi_launch_rejects_legacy_mode():
    """Legacy mode is gone: it ran workers with -ns (skills disabled)."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [CODING_RULES],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epic-1n0a",
                "mode": "legacy",
            }
        )
    )
    assert "error" in result
    assert "skills" in result["error"]


def test_pi_launch_reasoner():
    """Reasoner: read-only --tools, no --codemode."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "reasoner",
                "model": "anything",
                "skills": [CODING_RULES],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epic-uc44",
                "mode": "standard",
            }
        )
    )
    assert "error" not in result, result
    assert result["session_id"] == "epic-uc44-reasoner"
    cmd = result["command"]
    assert "--model anything" in cmd
    assert "--tools read,grep,find,ls" in cmd
    assert "--codemode" not in cmd


def test_worker_with_edit_tools_launch():
    """Brainer: read+write+edit tools (docs and task files), no shell, no --codemode."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "brainer",
                "model": "anything",
                "skills": [CODING_RULES],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epic-uc44",
                "mode": "standard",
            }
        )
    )
    assert "error" not in result, result
    assert result["session_id"] == "epic-uc44-brainer"
    cmd = result["command"]
    assert "--tools read,grep,find,ls,write,edit" in cmd
    assert "--model anything" in cmd
    assert "--codemode" not in cmd


def _write_skill(base, sub, name):
    """Create a fake skill dir with SKILL.md under base/sub/name."""
    d = os.path.join(str(base), sub, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(f"---\nname: {name}\n---\ntest\n")
    return d


def _launch_with_skill(skill, cwd):
    """Parse a standard coder launch for one skills entry."""
    return _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [skill],
                "contract_path": CONTRACT_MD,
                "cwd": cwd,
                "epic_id": "eS",
                "mode": "standard",
            }
        )
    )


def test_pi_launch_resolves_bare_name_project_root(tmp_path, monkeypatch):
    # given the skill exists only under a project .agents/skills root
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    _write_skill(proj, ".agents/skills", "probe-widget-skill")
    monkeypatch.setenv("HOME", str(tmp_path))
    # when launching with the bare name
    result = _launch_with_skill("probe-widget-skill", str(proj))
    # then the command carries the resolved project path and no discovery warning
    assert "error" not in result, result
    parts = shlex.split(result["command"])
    assert os.path.join(str(proj), ".agents/skills", "probe-widget-skill") in parts
    assert result.get("warnings", []) == []


def test_pi_launch_resolves_bare_name_project_ancestor(tmp_path, monkeypatch):
    # given the skill lives in a .pi/skills root ABOVE the project cwd
    _write_skill(tmp_path, ".pi/skills", "probe-piskill-skill")
    sub = tmp_path / "proj" / "deep"
    sub.mkdir(parents=True)
    (sub / "AGENTS.md").write_text("test repo marker\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    # when launching from below that root
    result = _launch_with_skill("probe-piskill-skill", str(sub))
    # then the ancestor-root path is resolved
    assert "error" not in result, result
    assert os.path.join(str(tmp_path), ".pi/skills", "probe-piskill-skill") in shlex.split(
        result["command"]
    )


def test_pi_launch_resolves_bare_name_global_agents(tmp_path, monkeypatch):
    # given the skill exists only in the global agents root
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    _write_skill(tmp_path, ".agents/skills", "probe-agents-skill")
    monkeypatch.setenv("HOME", str(tmp_path))
    # when launching with the bare name
    result = _launch_with_skill("probe-agents-skill", str(proj))
    # then the global path is resolved
    assert "error" not in result, result
    parts = shlex.split(result["command"])
    assert os.path.join(str(tmp_path), ".agents/skills", "probe-agents-skill") in parts


def test_pi_launch_resolves_bare_name_hermes_root_level(tmp_path, monkeypatch):
    # given the skill sits directly at the hermes skills root, no category dir
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    _write_skill(tmp_path, ".hermes/skills", "probe-root-skill")
    monkeypatch.setenv("HOME", str(tmp_path))
    # when launching with the bare name
    result = _launch_with_skill("probe-root-skill", str(proj))
    # then the hermes root-level path is resolved AND flagged as outside pi discovery
    assert "error" not in result, result
    parts = shlex.split(result["command"])
    assert os.path.join(str(tmp_path), ".hermes/skills/probe-root-skill") in parts
    assert any("outside pi skill discovery" in w for w in result.get("warnings", []))


def test_pi_launch_resolves_bare_name_hermes_local(tmp_path, monkeypatch):
    # given the skill exists only under a hermes skills category root
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    _write_skill(tmp_path, ".hermes/skills/dev", "probe-hermes-skill")
    monkeypatch.setenv("HOME", str(tmp_path))
    # when launching with the bare name
    result = _launch_with_skill("probe-hermes-skill", str(proj))
    # then the hermes-local path is resolved AND flagged as outside pi discovery
    assert "error" not in result, result
    parts = shlex.split(result["command"])
    assert os.path.join(str(tmp_path), ".hermes/skills/dev/probe-hermes-skill") in parts
    assert any("outside pi skill discovery" in w for w in result.get("warnings", []))


def test_pi_launch_bare_name_prefers_project_over_global(tmp_path, monkeypatch):
    # given the same probe name exists in the project root and the global agents root
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    _write_skill(proj, ".agents/skills", "probe-dup-skill")
    _write_skill(tmp_path, ".agents/skills", "probe-dup-skill")
    monkeypatch.setenv("HOME", str(tmp_path))
    # when launching
    result = _launch_with_skill("probe-dup-skill", str(proj))
    # then the project copy wins
    assert "error" not in result, result
    parts = shlex.split(result["command"])
    assert os.path.join(str(proj), ".agents/skills", "probe-dup-skill") in parts


def test_pi_launch_bare_name_prefers_global_agents_over_hermes(tmp_path, monkeypatch):
    # given the same probe name exists in the global agents root and hermes-local
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    _write_skill(tmp_path, ".agents/skills", "probe-dup-skill2")
    _write_skill(tmp_path, ".hermes/skills/dev", "probe-dup-skill2")
    monkeypatch.setenv("HOME", str(tmp_path))
    # when launching
    result = _launch_with_skill("probe-dup-skill2", str(proj))
    # then the global agents copy wins
    assert "error" not in result, result
    parts = shlex.split(result["command"])
    assert os.path.join(str(tmp_path), ".agents/skills", "probe-dup-skill2") in parts


def test_pi_launch_rejects_unknown_bare_name(tmp_path, monkeypatch):
    # given no skill root contains the name
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    # when launching with a bare name that exists nowhere
    result = _launch_with_skill("probe-not-anywhere-xyz", str(proj))
    # then the launch is refused with the same error shape as unknown paths
    assert "preflight" in result.get("error", ""), result
    assert "not resolved: probe-not-anywhere-xyz" in result["error"]


def test_pi_launch_compliance_block_uses_resolved_paths(tmp_path, monkeypatch):
    # given a bare-name skill resolved from the global agents root
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    _write_skill(tmp_path, ".agents/skills", "probe-agents-skill")
    monkeypatch.setenv("HOME", str(tmp_path))
    # when launching
    result = _launch_with_skill("probe-agents-skill", str(proj))
    # then the compliance block names the RESOLVED path the worker must read
    assert "error" not in result, result
    assert (
        os.path.join(str(tmp_path), ".agents/skills", "probe-agents-skill")
        in result["system_prompt"]
    )


def test_pi_launch_emits_plugin_fresh_when_module_matches_disk():
    # when the served module matches disk (normal import state)
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [GO_DEV],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "eF1",
                "mode": "standard",
            }
        )
    )
    # then the client is told staleness need not be recomputed
    assert "error" not in result, result
    assert "--plugin-fresh" in shlex.split(result["command"])


def test_pi_launch_drops_plugin_fresh_when_module_is_stale(monkeypatch):
    # given a served module whose import hash no longer matches disk; the impl
    # is called directly to bypass _call_impl's fresh-from-disk reload
    monkeypatch.setattr(p, "_LOADED_SHA", "0" * 64)
    # when launching
    result = _parse(
        p._pi_launch_impl(
            worker_type="coder",
            skills=[GO_DEV],
            contract_path=CONTRACT_MD,
            cwd=TMP_CWD,
            model="anything",
            epic_id="eF2",
            mode="standard",
        )
    )
    # then no freshness flag is emitted and the client keeps its heuristic
    assert "error" not in result, result
    assert "--plugin-fresh" not in shlex.split(result["command"])


def test_generated_command_single_line_and_parseable():
    """All worker types: single line, no backslash escapes,
    balanced quotes, shlex-parsable, contains --session-id and --prompt."""
    with open(os.path.join(TMP_CWD, "audit.md"), "w") as f:
        f.write("anchor\n")
    for wt in WORKER_TYPES:
        result = _parse(
            pi_launch(
                {
                    "worker_type": wt,
                    "model": "anything",
                    "skills": [CODING_RULES],
                    "contract_path": CONTRACT_MD,
                    "cwd": TMP_CWD,
                    "epic_id": "epicX",
                    "mode": "standard",
                    **({"require_edit": "audit.md"} if wt == "auditor" else {}),
                }
            )
        )
        assert "error" not in result, (wt, result)
        cmd = result["command"]
        assert "\n" not in cmd, wt
        assert not cmd.endswith("\\\\"), wt
        assert "\\\\" not in cmd, wt
        parts = shlex.split(cmd)
        # shlex.split proving shell-parseable; substitute $(cat ...) manually
        parts = [CONTRACT_MD if v.startswith("$(cat") else v for v in parts]
        assert "--session-id" in parts, wt
        assert "--prompt" in parts, wt
        # the emitted line starts with the preflight + budget notes, then the client
        assert parts[0] == "echo", wt
        assert parts[1].endswith(
            "preflight ok: contract, require_edit anchors, skills, budgets, cwd"
        ), wt
        assert parts[2] == "&&", wt
        assert parts[3] == "echo", wt
        assert parts[4].startswith("budget: per-call <="), wt
        assert parts[5] == "&&", wt
        assert parts[6] == "python3", wt


def test_prompt_substitution_not_quoted_away():
    """The --prompt value must keep $(cat ...) in double quotes so the shell
    performs command substitution."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epicQ",
                "mode": "standard",
            }
        )
    )
    assert "error" not in result, result
    cmd = result["command"]
    assert "'$(cat" not in cmd, cmd  # no single-quoted substitution
    assert '"$(cat' in cmd, cmd  # double-quoted substitution


def test_no_tool_dsl_prefix():
    """Emitted command must be plain shell, no Hermes terminal tool-DSL prefix."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epicDsl",
                "mode": "standard",
            }
        )
    )
    assert not result["command"].startswith("terminal"), result


def test_unknown_worker_type():
    result = _parse(
        pi_launch(
            {
                "worker_type": "nope",
                "model": "anything",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "e",
            }
        )
    )
    assert "error" in result


def test_missing_epic_and_slug():
    # when neither epic_id nor task_slug is given
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
            }
        )
    )

    # then the launch still succeeds with a cwd-derived session id
    assert "error" not in result, result
    assert result["session_id"] == f"task-{os.path.basename(os.path.normpath(TMP_CWD))}-coder"


def test_task_slug_session():
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "task_slug": "lint-fix",
            }
        )
    )
    assert result["session_id"] == "task-lint-fix-coder"


def test_zero_skill_dispatch_no_compliance_block():
    """No skills means no system_prompt key and no --append-system-prompt flag."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epicZero",
                "mode": "standard",
            }
        )
    )
    assert "error" not in result, result
    assert "system_prompt" not in result
    assert "--append-system-prompt" not in shlex.split(result["command"])


def test_skill_dispatch_injects_compliance_block():
    """With skills, pi_launch returns system_prompt and the command carries
    --append-system-prompt with the compliance text."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [CODING_RULES, GO_DEV],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epicSkills",
                "mode": "standard",
            }
        )
    )
    assert "error" not in result, result
    sp = result["system_prompt"]
    assert "SKILL COMPLIANCE" in sp
    assert "coding-rules" in sp and "go-dev" in sp
    parts = shlex.split(result["command"])
    assert "--append-system-prompt" in parts
    idx = parts.index("--append-system-prompt")
    assert parts[idx + 1] == sp

"""pi_launch command generation: worker toolsets, skill resolution, compliance
block, prompt substitution, and the plugin-fresh flag."""

import logging
import os
import shlex

import pytest
from helpers import CODING_RULES, CONTRACT_MD, GO_DEV, TMP_CWD, _parse

import __init__ as p
from __init__ import WORKER_TYPES, pi_launch


def _launch(**over):
    payload = {
        "worker_type": "coder",
        "model": "anything",
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
        "epic_id": "epicPlugin",
        "mode": "standard",
        "auto": False,
        **over,
    }
    return _parse(pi_launch(payload))


def test_pi_launch_standard_coder(monkeypatch):
    result = _launch(skills=[CODING_RULES, GO_DEV])
    assert "error" not in result, result
    assert result["session_id"] == "epicPlugin-coder"
    assert result["worker_type"] == "coder"
    # the echo answer names its own mode: diagnostic printout, nothing spawned
    assert result["mode"] == "echo-only"
    parts = shlex.split(result["command"])
    # the caller-supplied model alias lands 1:1 on the worker argv
    assert parts[parts.index("--model") + 1] == "anything"
    assert "--session-id" in parts and "epicPlugin-coder" in parts
    assert "--skill" in parts and CODING_RULES in parts and GO_DEV in parts
    assert "--codemode" in parts
    assert "--ns" not in parts
    # legacy mode is gone: it ran workers with -ns (skills disabled)
    legacy = _launch(mode="legacy", skills=[CODING_RULES])
    assert "error" in legacy and "skills" in legacy["error"], legacy

    # the worker agent dir lands on the argv when it exists
    assert os.path.isdir(p.WORKER_AGENT_DIR), "worker agent dir missing: " + p.WORKER_AGENT_DIR
    result = _launch(skills=[CODING_RULES])
    parts = shlex.split(result["command"])
    assert parts[parts.index("--agent-dir") + 1] == p.WORKER_AGENT_DIR

    # and the launch warns (dropping the flag) when it does not
    monkeypatch.setattr(p, "WORKER_AGENT_DIR", "/nonexistent/agent-worker")
    result = _launch(skills=[CODING_RULES])
    assert "error" not in result, result
    assert any("pi-subagents" in w or "AGENTS.md" in w for w in result.get("warnings", []))
    assert "--agent-dir" not in shlex.split(result["command"])


@pytest.mark.parametrize(
    ("worker_type", "tools_flag"),
    [
        pytest.param("reasoner", "--tools read,grep,find,ls", id="reasoner-readonly"),
        pytest.param("brainer", "--tools read,grep,find,ls,write,edit", id="brainer-read-write"),
    ],
)
def test_worker_toolsets(worker_type, tools_flag):
    result = _launch(worker_type=worker_type, epic_id="epicTools")
    assert "error" not in result, result
    assert result["session_id"] == f"epicTools-{worker_type}"
    assert tools_flag in result["command"], result["command"]
    assert "--codemode" not in result["command"], result["command"]


def _write_skill(base, sub, name):
    d = os.path.join(str(base), sub, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(f"---\nname: {name}\n---\ntest\n")
    return d


def _launch_with_skill(skill, cwd):
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
                "auto": False,
            }
        )
    )


@pytest.mark.parametrize(
    ("skill_sub", "expect_outside_pi_discovery"),
    [
        pytest.param(".agents/skills", False, id="project-agents-root"),
        pytest.param(".pi/skills", False, id="project-ancestor-pi-root"),
        pytest.param(".hermes/skills", True, id="hermes-root-level"),
        pytest.param(".hermes/skills/dev", True, id="hermes-local-category"),
        pytest.param("__missing__", False, id="unknown-name-refused"),
    ],
)
def test_pi_launch_resolves_bare_names_by_root(
    tmp_path, monkeypatch, skill_sub, expect_outside_pi_discovery
):
    name = "probe-skill"
    if skill_sub != "__missing__":
        _write_skill(tmp_path, skill_sub, name)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    result = _launch_with_skill(name, str(proj))
    if skill_sub == "__missing__":
        # no root contains the name: refused with the same error shape as bad paths
        assert "preflight" in result.get("error", ""), result
        assert "not resolved: probe-skill" in result["error"]
        return
    assert "error" not in result, result
    assert os.path.join(str(tmp_path), skill_sub, name) in shlex.split(result["command"])
    warnings = [w for w in result.get("warnings", []) if "outside pi skill discovery" in w]
    assert bool(warnings) is expect_outside_pi_discovery, result.get("warnings")


def test_pi_launch_bare_name_resolution_order(tmp_path, monkeypatch):
    """Duplicate names resolve project > global agents > hermes-local."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    monkeypatch.setenv("HOME", str(tmp_path))

    _write_skill(proj, ".agents/skills", "probe-dup-skill")
    _write_skill(tmp_path, ".agents/skills", "probe-dup-skill")
    result = _launch_with_skill("probe-dup-skill", str(proj))
    assert "error" not in result, result
    assert os.path.join(str(proj), ".agents/skills", "probe-dup-skill") in shlex.split(
        result["command"]
    )

    _write_skill(tmp_path, ".agents/skills", "probe-dup-skill2")
    _write_skill(tmp_path, ".hermes/skills/dev", "probe-dup-skill2")
    result = _launch_with_skill("probe-dup-skill2", str(proj))
    assert "error" not in result, result
    assert os.path.join(str(tmp_path), ".agents/skills", "probe-dup-skill2") in shlex.split(
        result["command"]
    )


def test_pi_launch_compliance_block_uses_resolved_paths(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    _write_skill(tmp_path, ".agents/skills", "probe-agents-skill")
    monkeypatch.setenv("HOME", str(tmp_path))
    result = _launch_with_skill("probe-agents-skill", str(proj))
    assert "error" not in result, result
    sp = result["system_prompt"]
    assert "SKILL COMPLIANCE" in sp
    assert os.path.join(str(tmp_path), ".agents/skills", "probe-agents-skill") in sp
    parts = shlex.split(result["command"])
    assert (
        "--append-system-prompt" in parts and parts[parts.index("--append-system-prompt") + 1] == sp
    )

    # no skills: no compliance block, no system prompt flag
    result = _launch()
    assert "error" not in result, result
    assert "system_prompt" not in result
    assert "--append-system-prompt" not in shlex.split(result["command"])


def test_generated_command_single_line_and_parseable():
    """All worker types: single line, shlex-parsable, preflight + budget notes
    then the client, --session-id and --prompt present."""
    with open(os.path.join(TMP_CWD, "audit.md"), "w") as f:
        f.write("anchor\n")
    for wt in WORKER_TYPES:
        result = _launch(
            worker_type=wt,
            skills=[CODING_RULES],
            epic_id="epicX",
            **({"require_edit": os.path.join(TMP_CWD, "audit.md")} if wt == "auditor" else {}),
        )
        assert "error" not in result, (wt, result)
        cmd = result["command"]
        assert "\n" not in cmd and "\\\\" not in cmd, wt
        parts = shlex.split(cmd)
        # shlex.split proves shell-parseable; substitute $(cat ...) manually
        parts = [CONTRACT_MD if v.startswith("$(cat") else v for v in parts]
        assert "--session-id" in parts and "--prompt" in parts, wt
        assert parts[parts.index("--cwd") + 1] == TMP_CWD, wt
        assert parts[0] == "echo" and parts[2] == "&&", wt
        assert parts[1].endswith(
            "preflight ok: contract, anchors, skills, budgets, cwd, model, naming"
        ), wt
        assert parts[3] == "echo" and parts[5] == "&&", wt
        assert parts[4].startswith("budget: per-call <="), wt
        assert parts[6] == "python3", wt
        # the prompt stays substitutable and the line is plain shell
        assert "'$(cat" not in cmd, cmd
        assert '"$(cat' in cmd, cmd
        assert not cmd.startswith("terminal"), wt


def test_pi_launch_plugin_fresh_tracks_module_staleness(monkeypatch):
    """A served module matching disk certifies freshness for the client; a
    stale one drops the flag so the client keeps its own heuristic."""
    result = _launch(skills=[GO_DEV])
    assert "--plugin-fresh" in shlex.split(result["command"]), result

    # the impl is called directly to bypass _call_impl's fresh-from-disk reload
    monkeypatch.setattr(p, "_LOADED_SHA", "0" * 64)
    result = _parse(
        p._pi_launch_impl(
            worker_type="coder",
            skills=[GO_DEV],
            contract_path=CONTRACT_MD,
            cwd=TMP_CWD,
            model="anything",
            epic_id="eF2",
            mode="standard",
            auto=False,
        )
    )
    assert "error" not in result, result
    assert "--plugin-fresh" not in shlex.split(result["command"])


def test_register_isolates_tool_failures(monkeypatch, caplog):
    """A launch-side schema failure must not take the control plane down."""
    # given
    registered = {}

    class RecordingCtx:
        def register_hook(self, *a, **k):
            pass

        def register_tool(self, name, toolset, schema, handler):
            registered[name] = schema

    def boom():
        raise RuntimeError("schema build broke")

    monkeypatch.setattr(p, "_launch_properties", boom)
    _seed_fake_display(monkeypatch)

    # when
    with caplog.at_level(logging.WARNING):
        p.register(RecordingCtx())

    # then
    assert "pi_manage" in registered
    assert "pi_launch" not in registered
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "pi_launch" in warnings[0], warnings


def _seed_fake_display(monkeypatch):
    """Install a stand-in agent.display so register() patches a throwaway module
    instead of requiring, and mutating, the host's real one."""
    import sys
    import types

    display_mod = types.ModuleType("agent.display")
    display_mod._PREVIEW_BUILDERS = {}
    display_mod._PRIMARY_ARGS = {}
    agent_mod = types.ModuleType("agent")
    agent_mod.display = display_mod
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.display", display_mod)
    return display_mod


def _registered_preview_builder(monkeypatch):
    """Return the pi_launch builder the way agent.display would look it up."""

    class Ctx:
        def register_hook(self, *a, **k):
            pass

        def register_tool(self, *a, **k):
            pass

    display_mod = _seed_fake_display(monkeypatch)
    p.register(Ctx())
    assert "pi_launch" in display_mod._PREVIEW_BUILDERS

    return display_mod._PREVIEW_BUILDERS["pi_launch"]


@pytest.mark.parametrize(
    ("over", "expected_tail"),
    [
        pytest.param(
            {"worker_type": "coder"},
            "hard9-bubble · coder · codemode",
            id="codemode-type",
        ),
        pytest.param(
            {"worker_type": "coder", "model": "fast"},
            "hard9-bubble · coder · fast · codemode",
            id="model-set",
        ),
        pytest.param(
            {"worker_type": "brainer"},
            "hard9-bubble · brainer",
            id="no-codemode",
        ),
        pytest.param(
            {"worker_type": "brainer", "mode": "echo-only"},
            "hard9-bubble · brainer · echo-only",
            id="echo-only",
        ),
        pytest.param(
            {"worker_type": "mystery", "model": "fast"},
            "hard9-bubble · mystery · fast",
            id="unknown-worker-type-falls-back",
        ),
    ],
)
def test_launch_preview_labels_the_dispatch(monkeypatch, over, expected_tail):
    # given
    args = {"task_slug": "hard9-bubble", "mode": "standard", **over}

    # when
    label = _registered_preview_builder(monkeypatch)(args, 200)

    # then
    assert label == expected_tail


def test_launch_preview_caps_the_slug(monkeypatch):
    # given
    args = {"task_slug": "x" * 50, "worker_type": "coder"}

    # when
    label = _registered_preview_builder(monkeypatch)(args, 200)

    # then
    assert label == "x" * 32 + " · coder · codemode"


def test_display_registration_degrades_without_display_module(monkeypatch, caplog):
    # given
    registered = {}

    class RecordingCtx:
        def register_hook(self, *a, **k):
            pass

        def register_tool(self, name, toolset, schema, handler):
            registered[name] = schema

    def import_boom():
        raise ImportError("no agent.display here")

    monkeypatch.setattr(p, "_import_agent_display", import_boom)

    # when
    with caplog.at_level(logging.WARNING):
        p.register(RecordingCtx())

    # then
    assert sorted(registered) == ["pi_launch", "pi_manage"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("display preview registration skipped" in w for w in warnings)

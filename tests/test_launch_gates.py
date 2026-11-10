import json
import os
import shlex
import shutil
import tempfile
import types

from helpers import (
    CODING_RULES,
    CONTRACT_MD,
    TMP_CWD,
    _parse,
)

import __init__ as p
from __init__ import WORKER_TYPES, pi_launch


def test_missing_cwd_errors():
    """cwd is required: omitting it returns an error dict mentioning cwd."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "epic_id": "e",
            }
        )
    )
    assert "error" in result, result
    assert "cwd" in result["error"]


def test_nonexistent_cwd_errors():
    """cwd pointing to a nonexistent path returns an error dict."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "cwd": "/nonexistent/pi-delegation-cwd-test",
                "epic_id": "e",
            }
        )
    )
    assert "error" in result, result
    assert "cwd" in result["error"]


def test_cwd_flag_in_generated_command_all_worker_types():
    with open(os.path.join(TMP_CWD, "audit.md"), "w") as f:
        f.write("anchor\n")
    """Generated command contains --cwd <path> for all 4 worker types."""
    for wt in WORKER_TYPES:
        result = _parse(
            pi_launch(
                {
                    "worker_type": wt,
                    "model": "anything",
                    "skills": [CODING_RULES],
                    "contract_path": CONTRACT_MD,
                    "cwd": TMP_CWD,
                    "epic_id": "epicCwd",
                    **({"require_edit": "audit.md"} if wt == "auditor" else {}),
                }
            )
        )
        assert "error" not in result, (wt, result)
        parts = shlex.split(result["command"])
        idx = parts.index("--cwd")
        assert parts[idx + 1] == TMP_CWD, (wt, parts)


def test_launch_rejects_missing_contract():
    """contract_path that does not exist -> error dict."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [],
                "contract_path": "/nonexistent.md",
                "cwd": TMP_CWD,
                "epic_id": "epic-fix9",
                "mode": "standard",
            }
        )
    )
    assert "error" in result, result
    assert "not found or unreadable" in result["error"], result


def test_launch_rejects_missing_skill():
    """Skill path that does not exist -> error dict."""
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": ["/nonexistent/skill"],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epic-fix9",
                "mode": "standard",
            }
        )
    )
    assert "error" in result, result
    assert "skills - not resolved: /nonexistent/skill" in result["error"], result


def test_launch_warns_hermes_local_skill(tmp_skill):
    """Existing skill outside .agents/.pi discovery -> warnings, command still built."""
    skill = str(tmp_skill)
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [skill],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epic-fix9",
                "mode": "standard",
            }
        )
    )
    assert "error" not in result, result
    assert result.get("warnings"), result
    assert "outside pi skill discovery" in result["warnings"][0], result
    parts = shlex.split(result["command"])
    assert "--skill" in parts and skill in parts, parts


def test_launch_no_warning_for_agents_skill():
    """Skill under <cwd>/.agents/skills -> no warnings."""
    proj = tempfile.mkdtemp(prefix="pi-delegation-test-proj-")
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("test repo marker\n")
    skill_dir = os.path.join(proj, ".agents", "skills", "foo")
    os.makedirs(skill_dir)
    with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
        f.write("x\n")
    skill = skill_dir
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "model": "anything",
                "skills": [skill],
                "contract_path": CONTRACT_MD,
                "cwd": proj,
                "epic_id": "epic-fix9",
                "mode": "standard",
            }
        )
    )
    assert "error" not in result, result
    assert "warnings" not in result, result


def test_launch_schema_matches_impl_signature():
    # given
    import inspect as _inspect

    # when
    impl = {
        n
        for n, param in _inspect.signature(p._pi_launch_impl).parameters.items()
        if param.kind is not _inspect.Parameter.VAR_KEYWORD
    }

    # then
    assert set(p.LAUNCH_PARAMS) == impl


def _contract_file():
    """Temp file standing in for a contract path."""
    handle, path = tempfile.mkstemp(suffix=".md")
    os.close(handle)
    return path


def test_launch_marks_cwd_required():
    # when
    required = p._launch_required()

    # then only cwd and the contract path are mandatory
    assert required == ["contract_path", "cwd"]


def test_launch_marks_model_required():
    # given a payload that omits the model alias entirely
    payload = {
        "worker_type": "coder",
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
        "epic_id": "eNoModel",
    }

    # when
    required = p._launch_required()
    result = _parse(pi_launch(payload))

    # then model is optional: the command omits --model so pi uses its defaultModel
    assert "model" not in required
    assert "error" not in result, result
    assert "--model" not in shlex.split(result["command"])


def test_launch_omits_model_flag_when_model_is_none():
    # given an explicit model=None
    payload = {
        "worker_type": "coder",
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
        "epic_id": "eNoneModel",
        "model": None,
    }

    # when
    result = _parse(pi_launch(payload))

    # then no --model flag reaches the client
    assert "error" not in result, result
    assert "--model" not in shlex.split(result["command"])


def test_launch_explicit_model_still_wins():
    # given an explicit alias
    payload = {
        "worker_type": "coder",
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
        "epic_id": "eModel",
        "model": "explicit-m",
    }

    # when
    parts = shlex.split(_parse(pi_launch(payload))["command"])

    # then the alias lands on the worker argv
    assert parts[parts.index("--model") + 1] == "explicit-m"


def test_launch_worker_type_defaults_to_default():
    # given a payload without worker_type
    payload = {
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
        "epic_id": "eDef",
    }

    # when
    result = _parse(pi_launch(payload))
    parts = shlex.split(result["command"])

    # then 'default' is a policy-free passthrough and names the session
    assert "error" not in result, result
    assert result["worker_type"] == "default"
    assert result["session_id"] == "eDef-default"
    assert "--tools" not in parts
    assert "--codemode" not in parts
    assert "--thinking" not in parts


def test_launch_unknown_worker_type_still_errors():
    # given a non-default unknown name
    payload = {
        "worker_type": "nope",
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
        "epic_id": "eNope",
    }

    # when
    result = _parse(pi_launch(payload))

    # then the passthrough default did not soften the unknown-name error
    assert "error" in result, result
    assert "nope" in result["error"]


def test_launch_derives_session_id_from_cwd_when_epic_and_slug_missing():
    # given a payload with neither epic_id nor task_slug
    payload = {
        "worker_type": "coder",
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
    }

    # when
    result = _parse(pi_launch(payload))

    # then the session id is derived from the project directory basename
    assert "error" not in result, result
    expected = f"task-{os.path.basename(os.path.normpath(TMP_CWD))}-coder"
    assert result["session_id"] == expected
    assert expected in shlex.split(result["command"])


def test_launch_explicit_session_ids_still_win():
    # when epic_id and task_slug are both present
    both = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "epic_id": "epicWin",
                "task_slug": "slugWin",
            }
        )
    )
    # when only task_slug is present
    slug_only = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "cwd": TMP_CWD,
                "task_slug": "slugWin",
            }
        )
    )

    # then the explicit ids still name the session
    assert both["session_id"] == "epicWin-coder"
    assert slug_only["session_id"] == "task-slugWin-coder"


def test_launch_schema_documents_model_param():
    # when
    schema = p.LAUNCH_PARAMS["model"]

    # then
    assert schema["type"] == "string"
    assert "alias" in schema["description"]


def test_worker_types_carry_no_model_key():
    # when/then: the plugin holds worker policy only, never a model name
    for name, cfg in WORKER_TYPES.items():
        assert "model" not in cfg, name


def test_launch_timeout_override():
    # given
    _proj = tempfile.mkdtemp(prefix="pi-delegation-pf-")
    with open(os.path.join(_proj, "AGENTS.md"), "w") as f:
        f.write("test repo marker\n")
    base = dict(
        worker_type="coder",
        skills=[],
        contract_path=_contract_file(),
        cwd=_proj,
        model="anything",
        epic_id="test-epic",
    )

    # when
    ok = p._pi_launch_impl(**base, timeout_s=123)
    bad = p._pi_launch_impl(**base, timeout_s=10)

    # then
    assert "--timeout 123" in ok["command"]
    assert "error" in bad


def test_launch_rejects_gates_without_require_edit():
    # given
    _proj = tempfile.mkdtemp(prefix="pi-delegation-pf-")
    with open(os.path.join(_proj, "AGENTS.md"), "w") as f:
        f.write("test repo marker\n")
    base = dict(
        worker_type="coder",
        skills=[],
        contract_path=_contract_file(),
        cwd=_proj,
        model="anything",
        epic_id="test-epic",
    )

    # when
    result = p._pi_launch_impl(**base, min_tool_calls=5)

    # then
    assert "error" in result and "require_edit" in result["error"]


def test_launch_emits_default_gates_with_require_edit():
    # given
    _proj = tempfile.mkdtemp(prefix="pi-delegation-pf-")
    with open(os.path.join(_proj, "AGENTS.md"), "w") as f:
        f.write("test repo marker\n")
    with open(os.path.join(_proj, "a.go"), "w") as f:
        f.write("package main\n")
    base = dict(
        worker_type="coder",
        skills=[],
        contract_path=_contract_file(),
        cwd=_proj,
        model="anything",
        epic_id="test-epic",
    )

    # when
    result = p._pi_launch_impl(**base, require_edit="a.go")
    # then
    assert "--min-tool-calls 3" in result["command"]
    assert "--max-nudges 4" in result["command"]


def _registered_manage_actions():
    """Actions advertised by the registered pi_manage schema."""
    tools = {}

    class Ctx:
        def register_hook(self, *a, **k):
            pass

        def register_tool(self, name, toolset, schema, handler):
            tools[name] = schema

    p.register(Ctx())
    return tools["pi_manage"]["parameters"]["properties"]["action"]["enum"]


def test_manage_doctor_reports_current_file():
    # when
    report = p._pi_manage_impl(action="doctor")

    # then
    assert report["stale"] is False
    assert report["schema_missing"] == []
    assert report["client_exists"] is True
    assert report["registered_props"] == report["impl_params"]


def test_launch_flags_stale_plugin_when_reload_fails(monkeypatch):
    # given the loaded copy is old AND the on-disk code cannot be reloaded
    monkeypatch.setattr(p, "_LOADED_SHA", "0" * 64)

    def boom(name):
        raise ImportError("broken file")

    monkeypatch.setattr(p, "_fresh_impl", boom)

    # when
    result = json.loads(
        p.pi_launch(
            dict(
                worker_type="coder",
                model="anything",
                skills=[],
                contract_path=_contract_file(),
                cwd=os.getcwd(),
                task_slug="stale-probe",
            )
        )
    )

    # then the caller is warned that the command came from a stale module
    assert result["plugin"]["stale"] is True
    assert "warning" in result


def test_launch_hot_reload_reports_current(monkeypatch):
    # given the loaded copy is old but the on-disk code reloads fine
    monkeypatch.setattr(p, "_LOADED_SHA", "0" * 64)
    fresh = types.SimpleNamespace(_pi_launch_impl=lambda **kw: {"command": "fresh"})
    monkeypatch.setattr(p, "_fresh_impl", lambda name: getattr(fresh, name))

    # when
    result = json.loads(
        p.pi_launch(
            dict(
                worker_type="coder",
                model="anything",
                skills=[],
                contract_path=_contract_file(),
                cwd=os.getcwd(),
                task_slug="stale-probe",
            )
        )
    )

    # then the command came from disk, so no warning is due
    assert result["command"] == "fresh"
    assert result["plugin"]["hot_reloaded"] is True
    assert "warning" not in result


def test_hot_reload_clears_stale_warning(monkeypatch):
    # given the gateway holds an old copy but the on-disk code reloads fine
    monkeypatch.setattr(p, "_LOADED_SHA", "0" * 64)
    fresh = types.SimpleNamespace(
        _pi_manage_impl=lambda **kw: {"stale": False, "ok": True},
    )
    monkeypatch.setattr(p, "_fresh_impl", lambda name: getattr(fresh, name))

    # when
    result = json.loads(p.pi_manage(dict(action="doctor")))

    # then the reloaded code is current, so no stale warning is emitted
    assert result["ok"] is True
    assert result["plugin"]["stale"] is False
    assert result["plugin"]["hot_reloaded"] is True
    assert "warning" not in result


def test_failed_hot_reload_keeps_stale_warning(monkeypatch):
    # given the on-disk code cannot be imported
    monkeypatch.setattr(p, "_LOADED_SHA", "0" * 64)

    def boom(name):
        raise ImportError("broken file")

    monkeypatch.setattr(p, "_fresh_impl", boom)

    # when the local impl still answers
    result = json.loads(p.pi_manage(dict(action="doctor")))

    # then the caller is told the answer came from a stale module
    assert result["plugin"]["stale"] is True
    assert "broken file" in result["plugin"]["detail"]
    assert "warning" in result


def test_manage_logs_missing_session():
    # when
    result = p._pi_manage_impl(action="logs", session_id="definitely-not-a-real-session-id")

    # then
    assert "error" in result


def test_manage_schema_advertises_doctor_and_logs():
    # when
    registered = _registered_manage_actions()

    # then
    assert {"doctor", "logs"} <= set(registered)


def _preflight_cwd(tmp_path, marker="AGENTS.md"):
    """A launch cwd with one repo marker (beans/agents), a contract, an anchor."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    (proj / marker).write_text("marker\n")
    contract = proj / "contract.md"
    contract.write_text("worker contract\n")
    anchor = proj / "lane-log.md"
    return proj, str(contract), str(anchor)


def test_preflight_missing_require_edit_anchor_errors(tmp_path):
    proj, contract, anchor = _preflight_cwd(tmp_path)
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [],
                "contract_path": contract,
                "cwd": str(proj),
                "epic_id": "epicPre",
                "require_edit": [anchor],
            }
        )
    )
    assert "error" in result, result
    assert "preflight" in result["error"], result["error"]
    assert anchor in result["error"], result["error"]


def test_preflight_create_anchor_makes_file_with_header(tmp_path):
    proj, contract, anchor = _preflight_cwd(tmp_path)
    assert not os.path.exists(anchor)
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [],
                "contract_path": contract,
                "cwd": str(proj),
                "epic_id": "epicAnchor",
                "require_edit": [anchor],
                "create_anchor": True,
            }
        )
    )
    assert "error" not in result, result
    assert os.path.exists(anchor)
    with open(anchor) as f:
        first = f.readline().strip()
    assert first == "## lane-log lane log (append-only)", first


def test_preflight_accepts_relative_anchor_against_cwd(tmp_path):
    proj, contract, _ = _preflight_cwd(tmp_path)
    (proj / "rel.md").write_text("target\n")
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [],
                "contract_path": contract,
                "cwd": str(proj),
                "epic_id": "epicRel",
                "require_edit": ["rel.md"],
            }
        )
    )
    assert "error" not in result, result


def test_preflight_missing_skill_path_errors(tmp_path):
    proj, contract, _ = _preflight_cwd(tmp_path)
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [str(proj / "missing-skill")],
                "contract_path": contract,
                "cwd": str(proj),
                "epic_id": "epicSkillPf",
            }
        )
    )
    assert "error" in result, result
    assert "preflight" in result["error"], result["error"]


def test_preflight_skill_root_without_skill_md_errors(tmp_path):
    proj, contract, _ = _preflight_cwd(tmp_path)
    bare = proj / "bare-skill"
    bare.mkdir()
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [str(bare)],
                "contract_path": contract,
                "cwd": str(proj),
                "epic_id": "epicSkillMd",
            }
        )
    )
    assert "error" in result, result
    assert "SKILL.md" in result["error"], result["error"]


def test_preflight_wall_timeout_below_idle_budget_errors(tmp_path):
    proj, contract, _ = _preflight_cwd(tmp_path)
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [],
                "contract_path": contract,
                "cwd": str(proj),
                "epic_id": "epicWall",
                "timeout_s": 600,
                "wall_timeout_s": 300,
            }
        )
    )
    assert "error" in result, result
    assert "preflight" in result["error"], result["error"]


def test_preflight_min_tool_calls_needs_positive_or_no_nudges(tmp_path):
    proj, contract, _ = _preflight_cwd(tmp_path)
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [],
                "contract_path": contract,
                "cwd": str(proj),
                "epic_id": "epicGate",
                "require_edit": [str(proj / "anchored.md")],
                "min_tool_calls": 0,
                "max_nudges": 2,
            }
        )
    )
    assert "error" in result, result
    assert "preflight" in result["error"], result["error"]


def test_preflight_min_max_without_require_edit_errors(tmp_path):
    proj, contract, _ = _preflight_cwd(tmp_path)
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [],
                "contract_path": contract,
                "cwd": str(proj),
                "epic_id": "epicAim",
                "min_tool_calls": 3,
            }
        )
    )
    assert "error" in result, result
    assert "preflight" in result["error"], result["error"]


def test_preflight_cwd_without_repo_markers_errors(tmp_path):
    proj, contract, _ = _preflight_cwd(tmp_path)
    (proj / "AGENTS.md").unlink()
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [],
                "contract_path": contract,
                "cwd": str(proj),
                "epic_id": "epicMark",
            }
        )
    )
    assert "error" in result, result
    assert "preflight" in result["error"], result["error"]
    assert "AGENTS.md" in result["error"], result["error"]


def test_preflight_happy_path_emits_ok_line(tmp_path):
    proj, contract, anchor = _preflight_cwd(tmp_path)
    (proj / "a.md").write_text("x")
    result = _parse(
        pi_launch(
            {
                "worker_type": "coder",
                "skills": [],
                "contract_path": contract,
                "cwd": str(proj),
                "epic_id": "epicHappy",
                "require_edit": [str(proj / "a.md")],
            }
        )
    )
    assert "error" not in result, result
    assert (
        "preflight ok: contract, require_edit anchors, skills, budgets, cwd" in result["command"]
    ), result["command"]


def test_doctor_schema_missing_empty_with_create_anchor():
    report = p._pi_manage_impl(action="doctor")
    assert report["schema_missing"] == []
    assert "create_anchor" in report["registered_props"], report["registered_props"]
    assert "auto" in report["registered_props"], report["registered_props"]


def _budget_launch(**over):
    """One marked-cwd launch with explicit budgets, returning the parsed result."""
    proj = tempfile.mkdtemp(prefix="pi-delegation-budget-")
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("test repo marker\n")
    with open(os.path.join(proj, "a.go"), "w") as f:
        f.write("package main\n")
    base = dict(
        worker_type="coder",
        skills=[],
        contract_path=_contract_file(),
        cwd=proj,
        model="anything",
        epic_id="epicBudget",
        require_edit=[os.path.join(proj, "a.go")],
    )
    result = p._pi_launch_impl(**{**base, **over})
    cleanup = proj
    return result, cleanup


def test_budget_line_carries_resolved_numbers():
    result, proj = _budget_launch(
        timeout_s=600, wall_timeout_s=2400, min_tool_calls=3, max_nudges=2
    )
    assert "error" not in result, result
    cmd = result["command"]
    assert "budget: per-call <= 600s" in cmd, cmd
    assert "wall <= 2400s" in cmd, cmd
    assert "nudge: >=3 tool calls, <=2 nudges then exit 3" in cmd, cmd
    shutil.rmtree(proj, ignore_errors=True)


def test_budget_line_zero_nudge_form():
    result, proj = _budget_launch(max_nudges=0, min_tool_calls=1)
    assert "error" not in result, result
    cmd = result["command"]
    assert "nudge: 0 - no correction loop" in cmd, cmd
    assert "wall <=" in cmd and "per-call <=" in cmd, cmd
    shutil.rmtree(proj, ignore_errors=True)


def test_budget_line_defaults_when_unspecified():
    result, proj = _budget_launch()
    assert "error" not in result, result
    cmd = result["command"]
    # resolved wall defaults to max(3600, timeout+900), idle default 900
    assert "budget: per-call <= 900s" in cmd, cmd
    assert "wall <= 3600s" in cmd, cmd
    assert "nudge: >=3 tool calls, <=4 nudges then exit 3" in cmd, cmd
    shutil.rmtree(proj, ignore_errors=True)


def test_budget_line_before_client_invocation_single_line():
    result, proj = _budget_launch(timeout_s=330, wall_timeout_s=3300)
    assert "error" not in result, result
    cmd = result["command"]
    assert "\n" not in cmd, cmd
    budget_pos = cmd.index("budget: per-call")
    client_pos = cmd.index("python3", budget_pos)
    assert budget_pos < client_pos
    shutil.rmtree(proj, ignore_errors=True)


def test_readme_exit_code_table_covers_3_and_143():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "README.md")) as f:
        readme = f.read()
    assert "| 3 | incomplete: correction budget exhausted (nudged out) |" in readme
    assert "| 143 | SIGTERM (external kill; kill action stamps 143 itself) |" in readme
    assert "| 0 | done: dispatch satisfied |" in readme
    assert "| 130 | KeyboardInterrupt (Ctrl-C) |" in readme

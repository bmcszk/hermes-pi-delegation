"""Launch gates: schema shape, cwd/contract/skill/gate preflight, budget line,
hot reload, and the README-documented exit-code table."""

import json
import os
import shlex
import shutil
import tempfile
import types

import pytest
from helpers import CONTRACT_MD, TMP_CWD, _parse

import __init__ as p
from __init__ import WORKER_TYPES, pi_launch


def _contract_file():
    handle, path = tempfile.mkstemp(suffix=".md")
    os.close(handle)
    return path


def _marked_project(dir_path=None, contract=True, anchor="a.go"):
    proj = dir_path or tempfile.mkdtemp(prefix="pi-delegation-gates-")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("test repo marker\n")
    if contract:
        with open(os.path.join(proj, "contract.md"), "w") as f:
            f.write("worker contract\n")
    if anchor:
        with open(os.path.join(proj, anchor), "w") as f:
            f.write("package main\n")
    return proj


def _launch(payload):
    return _parse(pi_launch(payload))


def test_launch_schema_matches_impl_and_marks_required():
    import inspect

    impl = {
        n
        for n, param in inspect.signature(p._pi_launch_impl).parameters.items()
        if param.kind is not inspect.Parameter.VAR_KEYWORD
    }
    assert set(p.LAUNCH_PARAMS) == impl
    # only cwd and the contract path are mandatory
    assert p._launch_required() == ["contract_path", "cwd"]
    # cwd is required and must exist; a missing or nonexistent cwd is a named error
    assert (
        "cwd"
        in _launch(
            {"worker_type": "coder", "model": "m", "skills": [], "contract_path": CONTRACT_MD}
        )["error"]
    )
    assert (
        "cwd"
        in _launch(
            {
                "worker_type": "coder",
                "model": "m",
                "skills": [],
                "contract_path": CONTRACT_MD,
                "cwd": "/nonexistent/pi-delegation-cwd",
            }
        )["error"]
    )
    # the plugin holds worker policy only, never a model name
    for name, cfg in WORKER_TYPES.items():
        assert "model" not in cfg, name


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            {"worker_type": "coder", "cwd": TMP_CWD, "contract_path": "/nonexistent.md"},
            id="missing-contract",
        ),
        pytest.param(
            {
                "worker_type": "coder",
                "cwd": TMP_CWD,
                "contract_path": CONTRACT_MD,
                "skills": ["/nonexistent/skill"],
            },
            id="missing-skill",
        ),
        pytest.param(None, id="missing-anchor"),
        pytest.param(None, id="markerless-cwd"),
    ],
)
def test_launch_preflight_rejects_missing_inputs(case, tmp_path, request):
    if request.node.callspec.id.endswith("markerless-cwd"):
        proj = _marked_project(str(tmp_path))
        os.unlink(os.path.join(proj, "AGENTS.md"))
        case = {
            "worker_type": "coder",
            "model": "m",
            "skills": [],
            "contract_path": os.path.join(proj, "contract.md"),
            "cwd": proj,
            "epic_id": "epicMark",
        }
        result = _launch(case)
        assert "AGENTS.md" in result["error"], result
        return
    if case is None:
        proj = _marked_project(str(tmp_path), anchor=None)
        case = {
            "worker_type": "coder",
            "model": "m",
            "skills": [],
            "contract_path": os.path.join(proj, "contract.md"),
            "cwd": proj,
            "epic_id": "epicPre",
            "require_edit": [os.path.join(proj, "lane-log.md")],
        }
    result = _launch(case)
    assert "error" in result, result


def test_preflight_anchor_path_handling(tmp_path):
    """create_anchor writes the append-only header; a relative anchor resolves
    against the launch cwd."""
    # given a project whose anchor does not exist yet
    proj = _marked_project(str(tmp_path / "a"), anchor=None)
    anchor = os.path.join(proj, "lane-log.md")
    result = _launch(
        {
            "worker_type": "coder",
            "model": "m",
            "skills": [],
            "contract_path": os.path.join(proj, "contract.md"),
            "cwd": proj,
            "epic_id": "epicAnchor",
            "require_edit": [anchor],
            "create_anchor": True,
        }
    )
    assert "error" not in result, result
    with open(anchor) as f:
        assert f.readline().strip() == "## lane-log lane log (append-only)"

    # when a relative anchor path is passed with the file present
    with open(os.path.join(proj, "rel.md"), "w") as f:
        f.write("target\n")
    result = _launch(
        {
            "worker_type": "coder",
            "model": "m",
            "skills": [],
            "contract_path": os.path.join(proj, "contract.md"),
            "cwd": proj,
            "epic_id": "epicRel",
            "require_edit": ["rel.md"],
        }
    )
    assert "error" not in result, result

    # and the happy path carries the ok line naming every preflight check
    proj2 = _marked_project(str(tmp_path / "b"), anchor="a.md")
    result = _launch(
        {
            "worker_type": "coder",
            "model": "m",
            "skills": [],
            "contract_path": os.path.join(proj2, "contract.md"),
            "cwd": proj2,
            "epic_id": "epicHappy",
            "require_edit": [os.path.join(proj2, "a.md")],
        }
    )
    assert (
        "preflight ok: contract, anchors, skills, budgets, cwd, model, naming" in result["command"]
    ), result


@pytest.mark.parametrize(
    ("over", "expect"),
    [
        pytest.param(
            {"wall_timeout_s": 300, "timeout_s": 600},
            "preflight",
            id="wall-below-idle",
        ),
        pytest.param(
            {"require_edit": ["anchored.md"], "min_tool_calls": 0, "max_nudges": 2},
            "preflight",
            id="min0-with-nudges",
        ),
        pytest.param(
            {"min_tool_calls": 3},
            "preflight",
            id="min-without-require-edit",
        ),
    ],
)
def test_preflight_budget_contradictions(over, expect, tmp_path):
    proj = _marked_project(str(tmp_path))
    result = _launch(
        {
            "worker_type": "coder",
            "model": "m",
            "skills": [],
            "contract_path": os.path.join(proj, "contract.md"),
            "cwd": proj,
            "epic_id": "epicGate",
            **over,
        }
    )
    assert "error" in result, result
    assert expect in result["error"], result["error"]


@pytest.mark.parametrize(
    ("payload_over", "expect"),
    [
        pytest.param({}, "worker-type-defaults"),
        pytest.param({"worker_type": "nope"}, "unknown-worker-type"),
        pytest.param(
            {"worker_type": "coder", "task_slug": "slugWin"},
            "task-slug-session",
        ),
        pytest.param(
            {"worker_type": "coder", "epic_id": "epicWin", "task_slug": "slugWin"},
            "double-naming",
        ),
        pytest.param(
            {"worker_type": "coder", "_no_naming": True}, "cwd-derived", id="no-naming-cwd-derived"
        ),
    ],
)
def test_launch_naming_and_worker_type_contract(payload_over, expect):
    no_naming = payload_over.pop("_no_naming", False)
    payload = {
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
        **payload_over,
    }
    # single naming only: epic_id must stay off the task_slug-only case
    if not no_naming and "task_slug" not in payload_over:
        payload.setdefault("epic_id", "epicName")
    result = _launch(payload)
    if expect == "worker-type-defaults":
        assert "error" not in result, result
        assert result["worker_type"] == "default"
        assert result["session_id"] == "epicName-default"
        parts = shlex.split(result["command"])
        assert "--tools" not in parts and "--codemode" not in parts and "--thinking" not in parts
    elif expect == "unknown-worker-type":
        assert "nope" in result.get("error", ""), result
    elif expect == "task-slug-session":
        assert result["session_id"] == "task-slugWin-coder", result
    elif expect == "cwd-derived":
        expected = f"task-{os.path.basename(os.path.normpath(TMP_CWD))}-coder"
        assert result["session_id"] == expected, result
        assert expected in shlex.split(result["command"])
    else:
        assert "mutually exclusive" in result.get("error", ""), result
        assert "epic_id wins the session id" in result["error"], result


def test_launch_model_flag_contract():
    # an explicit alias lands on the worker argv
    result = _launch(
        {
            "worker_type": "coder",
            "skills": [],
            "contract_path": CONTRACT_MD,
            "cwd": TMP_CWD,
            "epic_id": "eModel",
            "model": "explicit-m",
        }
    )
    assert "error" not in result, result
    parts = shlex.split(result["command"])
    assert parts[parts.index("--model") + 1] == "explicit-m"
    # model=None omits the flag so pi uses its defaultModel
    result = _launch(
        {
            "worker_type": "coder",
            "skills": [],
            "contract_path": CONTRACT_MD,
            "cwd": TMP_CWD,
            "epic_id": "eNone",
            "model": None,
        }
    )
    assert "error" not in result, result
    assert "--model" not in shlex.split(result["command"])
    # the schema documents the param
    schema = p.LAUNCH_PARAMS["model"]
    assert schema["type"] == "string" and "alias" in schema["description"]


def test_launch_timeout_and_gate_flags(tmp_path):
    proj = _marked_project(str(tmp_path))
    base = dict(
        worker_type="coder",
        skills=[],
        contract_path=os.path.join(proj, "contract.md"),
        cwd=proj,
        model="m",
        epic_id="epicTimeout",
    )
    ok = p._pi_launch_impl(**base, timeout_s=123, require_edit=os.path.join(proj, "a.go"))
    assert "--timeout 123" in ok["command"], ok
    assert "--min-tool-calls 3" in ok["command"] and "--max-nudges 4" in ok["command"], ok
    # gates without a deliverable are refused
    bad = p._pi_launch_impl(**base, min_tool_calls=5)
    assert "error" in bad and "require_edit" in bad["error"], bad
    # a timeout too small for preflight is refused
    tiny = p._pi_launch_impl(**base, timeout_s=10)
    assert "error" in tiny, tiny


@pytest.mark.parametrize(
    ("cmd_over",),
    [
        pytest.param(
            {"timeout_s": 600, "wall_timeout_s": 2400, "min_tool_calls": 3, "max_nudges": 2},
            id="resolved",
        ),
        pytest.param({"max_nudges": 0, "min_tool_calls": 1}, id="zero-nudge"),
        pytest.param({}, id="defaults"),
        pytest.param({"timeout_s": 330, "wall_timeout_s": 3300}, id="single-line"),
    ],
)
def test_budget_line_carries_resolved_numbers(cmd_over, tmp_path):
    proj = _marked_project(str(tmp_path))
    result = p._pi_launch_impl(
        worker_type="coder",
        skills=[],
        contract_path=os.path.join(proj, "contract.md"),
        cwd=proj,
        model="m",
        epic_id="epicBudget",
        require_edit=[os.path.join(proj, "a.go")],
        **cmd_over,
    )
    assert "error" not in result, result
    cmd = result["command"]
    if cmd_over.get("max_nudges") == 0:
        assert "nudge: 0 - no correction loop" in cmd, cmd
        assert "wall <=" in cmd and "per-call <=" in cmd, cmd
    elif not cmd_over:
        assert "budget: per-call <= 900s" in cmd, cmd
        assert "wall <= 3600s" in cmd, cmd
        assert "nudge: >=3 tool calls, <=4 nudges then exit 3" in cmd, cmd
    elif "min_tool_calls" in cmd_over:
        assert "budget: per-call <= 600s" in cmd, cmd
        assert "wall <= 2400s" in cmd, cmd
        assert "nudge: >=3 tool calls, <=2 nudges then exit 3" in cmd, cmd
    else:
        assert "\n" not in cmd, cmd
        assert cmd.index("budget: per-call") < cmd.index("python3", cmd.index("budget:"))
    shutil.rmtree(proj, ignore_errors=True)


def _registered_manage_actions():
    tools = {}

    class Ctx:
        def register_hook(self, *a, **k):
            pass

        def register_tool(self, name, toolset, schema, handler):
            tools[name] = schema

    p.register(Ctx())
    return tools["pi_manage"]["parameters"]["properties"]["action"]["enum"]


def _stale_launch(monkeypatch, fresh_impl):
    monkeypatch.setattr(p, "_LOADED_SHA", "0" * 64)
    monkeypatch.setattr(p, "_fresh_impl", fresh_impl)
    return json.loads(
        p.pi_launch(
            dict(
                worker_type="coder",
                model="m",
                skills=[],
                contract_path=_contract_file(),
                cwd=os.getcwd(),
                task_slug="stale-probe",
            )
        )
    )


def test_manage_doctor_and_launch_staleness(monkeypatch):
    # doctor reports the current schema and both manage actions
    report = p._pi_manage_impl(action="doctor")
    assert report["stale"] is False
    assert report["schema_missing"] == []
    assert report["client_exists"] is True
    assert report["registered_props"] == report["impl_params"]
    assert {"create_anchor", "auto"} <= set(report["registered_props"])
    assert {"doctor", "logs"} <= set(_registered_manage_actions())
    # logs on an absent session is a named error
    assert "error" in p._pi_manage_impl(action="logs", session_id="definitely-not-real")

    # reload fails: the caller is warned the command came from a stale module
    def boom(name):
        raise ImportError("broken file")

    result = _stale_launch(monkeypatch, boom)
    assert result["plugin"]["stale"] is True
    assert "warning" in result and "broken file" in result["plugin"]["detail"], result

    # the on-disk code reloads fine: fresh command, no warning
    fresh = types.SimpleNamespace(_pi_launch_impl=lambda **kw: {"command": "fresh"})
    result = _stale_launch(monkeypatch, lambda name: getattr(fresh, name))
    assert result["command"] == "fresh"
    assert result["plugin"]["hot_reloaded"] is True
    assert "warning" not in result, result

    # the reloaded manage impl answers current too: no stale warning there
    fresh_manage = types.SimpleNamespace(_pi_manage_impl=lambda **kw: {"stale": False, "ok": True})
    monkeypatch.setattr(p, "_fresh_impl", lambda name: getattr(fresh_manage, name))
    result = json.loads(p.pi_manage(dict(action="doctor")))
    assert result["ok"] is True
    assert result["plugin"]["stale"] is False
    assert result["plugin"]["hot_reloaded"] is True
    assert "warning" not in result, result


def test_readme_exit_code_table_covers_the_codes():
    readme_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "README.md")
    with open(readme_path) as f:
        readme = f.read()
    assert "| 3 | incomplete: correction budget exhausted (nudged out) |" in readme
    assert "| 143 | SIGTERM (external kill; kill action stamps 143 itself) |" in readme
    assert "| 0 | done: dispatch satisfied |" in readme
    assert "| 130 | KeyboardInterrupt (Ctrl-C) |" in readme


@pytest.mark.parametrize(
    ("contract_text", "expect_warn"),
    [
        pytest.param("do the work\nwrite DONE-MARK at the end\n", False, id="sentinel-present"),
        pytest.param("do the work, nothing else\n", True, id="sentinel-absent"),
    ],
)
def test_preflight_warns_sentinel_missing_from_contract(contract_text, expect_warn, tmp_path):
    """require_sentinel absent from the contract text draws a warning (the
    worker was never told to write it); present, or no sentinel, stays silent."""
    proj = _marked_project(str(tmp_path))
    contract = os.path.join(proj, "contract.md")
    with open(contract, "w") as f:
        f.write(contract_text)
    payload = {
        "worker_type": "coder",
        "model": "m",
        "skills": [],
        "contract_path": contract,
        "cwd": proj,
        "epic_id": "epicSentinel",
        "require_edit": os.path.join(proj, "a.go"),
    }
    result = _launch({**payload, "require_sentinel": "DONE-MARK"})
    assert "error" not in result, result
    warnings = json.dumps(result.get("warnings", []))
    assert ("never appears in the contract" in warnings) is expect_warn, result.get("warnings")

    # no require_sentinel: the check is skipped entirely
    result = _launch(payload)
    assert "error" not in result and "never appears in the contract" not in json.dumps(
        result.get("warnings", [])
    ), result


def _seeded_registry(tmp_path, *rows):
    """Point the plugin at a fake registry holding rows; restore at teardown."""
    backup = p.REGISTRY_PATH
    p.REGISTRY_PATH = str(tmp_path / "pi-agents.jsonl")
    with open(p.REGISTRY_PATH, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return backup


def test_preflight_warns_on_session_reuse_and_same_cwd_concurrency(tmp_path):
    """Launch over a finished session id warns that pi resumes its context;
    a live sibling on the same cwd warns one-worker-per-project; a fresh id
    on a quiet cwd warns neither."""
    proj = _marked_project(str(tmp_path))
    contract = os.path.join(proj, "contract.md")
    payload = {
        "worker_type": "coder",
        "model": "m",
        "skills": [],
        "contract_path": contract,
        "cwd": proj,
    }
    backup = _seeded_registry(
        tmp_path,
        {
            "session": "task-proj-coder",
            "pid": 999999,
            "status": "done",
            "cwd": proj,
            "started": "2024-01-01T00:00:00",
        },
    )
    try:
        result = _launch({**payload, "task_slug": "proj"})
        assert "error" not in result, result
        warnings = json.dumps(result.get("warnings", []))
        assert "resume" in warnings and "task-proj-coder" in warnings, result.get("warnings")

        # a live sibling row on the same cwd draws the concurrency warning
        _write_registry_line(
            {
                "session": "other-epic-coder",
                "pid": os.getpid(),
                "status": "running",
                "cwd": proj,
                "started": "2024-01-02T00:00:00",
            }
        )
        result = _launch({**payload, "epic_id": "fresh-id"})
        assert "error" not in result, result
        assert "one worker per project directory" in json.dumps(
            result.get("warnings", [])
        ), result.get("warnings")

        # fresh id on a quiet cwd: neither warning
        p.REGISTRY_PATH = str(tmp_path / "empty.jsonl")
        with open(p.REGISTRY_PATH, "w") as f:
            f.write("")
        result = _launch({**payload, "epic_id": "fresh-id"})
        assert "error" not in result, result
        warnings = json.dumps(result.get("warnings", []))
        assert "resume" not in warnings and "one worker per project" not in warnings, warnings
    finally:
        p.REGISTRY_PATH = backup


def test_preflight_warns_on_stale_lane_locks(tmp_path):
    """Launch into a crash-leftover cwd warns per stale lock (path, owning
    session, exact rm command); a live lock never draws the stale message and
    the launcher never removes anything."""
    proj = _marked_project(str(tmp_path))
    lock_dir = os.path.join(proj, ".hermes", "lane", "LOCK")
    os.makedirs(lock_dir)
    stale_path = os.path.join(lock_dir, "crashed-epic-coder.lock")
    with open(stale_path, "w") as f:
        json.dump({"session_id": "crashed-epic-coder", "pid": 999999}, f)
    live_lock = os.path.join(lock_dir, "live-epic-coder.lock")
    with open(live_lock, "w") as f:
        json.dump({"session_id": "live-epic-coder", "pid": os.getpid()}, f)

    result = _launch(
        {
            "worker_type": "coder",
            "model": "m",
            "skills": [],
            "contract_path": os.path.join(proj, "contract.md"),
            "cwd": proj,
            "epic_id": "lockCheck",
        }
    )
    assert "error" not in result, result
    warnings = json.dumps(result.get("warnings", []))
    assert "crashed-epic-coder.lock" in warnings, result.get("warnings")
    assert f"rm {stale_path}" in warnings, result.get("warnings")
    assert "live-epic-coder" not in warnings, result.get("warnings")
    assert os.path.exists(stale_path) and os.path.exists(live_lock), "launcher removed a lock"

    # no lock files: no warning, no error
    os.remove(stale_path)
    os.remove(live_lock)
    result = _launch(
        {
            "worker_type": "coder",
            "model": "m",
            "skills": [],
            "contract_path": os.path.join(proj, "contract.md"),
            "cwd": proj,
            "epic_id": "lockCheck2",
        }
    )
    assert "error" not in result and "stale" not in json.dumps(result.get("warnings", [])), result


def _write_registry_line(row):
    import __init__ as pmod

    with open(pmod.REGISTRY_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")


@pytest.mark.parametrize(
    ("worker_type", "with_require_edit", "expect_warn"),
    [
        pytest.param("coder", False, True, id="coder-ungated-warns"),
        pytest.param("coder", True, False, id="coder-gated-no-warning"),
        pytest.param("reasoner", False, False, id="reasoner-no-warning"),
        pytest.param("brainer", False, False, id="brainer-no-warning"),
        pytest.param("default", False, False, id="default-no-warning"),
    ],
)
def test_preflight_warns_coder_without_require_edit(worker_type, with_require_edit, expect_warn):
    """An ungated coder dispatch stays legal but draws the exit-0-no-edits
    warning; every other worker type stays silent."""
    proj = _marked_project()
    payload = {
        "worker_type": worker_type,
        "model": "m",
        "skills": [],
        "contract_path": os.path.join(proj, "contract.md"),
        "cwd": proj,
        "epic_id": f"epicWarn-{worker_type}",
    }
    if with_require_edit:
        payload["require_edit"] = os.path.join(proj, "a.go")
    result = _launch(payload)
    assert "error" not in result, result
    warnings = json.dumps(result.get("warnings", []))
    assert ("exit 0" in warnings and "require_edit" in warnings) is expect_warn, result.get(
        "warnings"
    )

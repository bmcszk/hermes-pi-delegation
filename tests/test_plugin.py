import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types

import pytest

# The repo root is the plugin root: conftest.py makes it importable, and this
# insert keeps the plugin importable when a test file is run on its own.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import __init__ as p
from __init__ import (
    WORKER_TYPES,
    pi_launch,
    pi_manage,
)

TMP_CWD = tempfile.mkdtemp(prefix="pi-delegation-test-cwd-")
with open(os.path.join(TMP_CWD, "AGENTS.md"), "w") as _f:
    _f.write("test repo marker\n")
CONTRACT_MD = os.path.join(TMP_CWD, "contract.md")
with open(CONTRACT_MD, "w") as _f:
    _f.write("test contract\n")

_registry_backup = p.REGISTRY_PATH

SKILLS = os.path.join(TMP_CWD, "skills")
CODING_RULES = os.path.join(SKILLS, "coding-rules")
GO_DEV = os.path.join(SKILLS, "go-dev")
for _name in (CODING_RULES, GO_DEV):
    os.makedirs(_name, exist_ok=True)
    with open(os.path.join(_name, "SKILL.md"), "w") as _f:
        _f.write(f"---\nname: {os.path.basename(_name)}\n---\ntest\n")


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


def _parse(result):
    """Helper: parse JSON string from handler returns."""
    return json.loads(result) if isinstance(result, str) else result


def _setup_registry(tmpdir):
    global _registry_backup
    _registry_backup = p.REGISTRY_PATH
    p.REGISTRY_PATH = os.path.join(tmpdir, "pi-agents.jsonl")


def _teardown_registry():
    p.REGISTRY_PATH = _registry_backup


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


def test_pi_manage_list_empty(tmpdir=None):
    """list on an empty (temp) registry returns []. Never touches the live registry."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        result = _parse(pi_manage({"action": "list"}))
        assert result["agents"] == []  # handlers now also report "plugin" health
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_list_sorted_desc():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(json.dumps({"session": "a", "pid": 1, "started": "2024-01-01T00:00:00"}) + "\n")
            f.write(json.dumps({"session": "b", "pid": 2, "started": "2024-01-02T00:00:00"}) + "\n")
        agents = _parse(pi_manage({"action": "list"}))["agents"]
        assert [a["session"] for a in agents] == ["b", "a"]
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_concurrent_guard_blocks_running_session():
    """pi_launch errors when the session has a live registry entry."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epicZ-coder",
                        "agent": "epicZ-coder",
                        "pid": os.getpid(),  # this process is alive
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        result = _parse(
            pi_launch(
                {
                    "worker_type": "coder",
                    "model": "anything",
                    "skills": [],
                    "contract_path": CONTRACT_MD,
                    "cwd": TMP_CWD,
                    "epic_id": "epicZ",
                    "mode": "standard",
                }
            )
        )
        assert "error" in result, result
        assert "already running" in result["error"]
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_concurrent_guard_ignores_dead_pid():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epicY-coder",
                        "agent": "epicY-coder",
                        "pid": 999999,
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        result = _parse(
            pi_launch(
                {
                    "worker_type": "coder",
                    "model": "anything",
                    "skills": [],
                    "contract_path": CONTRACT_MD,
                    "cwd": TMP_CWD,
                    "epic_id": "epicY",
                    "mode": "standard",
                }
            )
        )
        assert "error" not in result, result
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_kill_refuses_non_client_pid():
    """kill refuses a pid whose cmdline is not pi_rpc_client."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    # live decoy process whose cmdline lacks "pi_rpc_client"
    decoy = subprocess.Popen(["sleep", "30"])
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epicK-coder",
                        "pid": decoy.pid,
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        result = _parse(p._pi_manage_impl("kill", session_id="epicK-coder"))
        assert "error" in result, result
        assert "pi_rpc_client" in result["error"]
        assert decoy.poll() is None  # decoy was NOT signalled
    finally:
        decoy.terminate()
        decoy.wait(timeout=5)
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_kill_dead_pid_reports_dead():
    """kill on a session whose pid is gone reports dead and marks the entry dead."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epicM-coder",
                        "pid": 999999,
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        result = _parse(pi_manage({"action": "kill", "session_id": "epicM-coder"}))
        assert "status" in result, result
        assert "dead" in result["status"], result
        with open(p.REGISTRY_PATH) as f:
            entry = json.loads(f.readline())
        assert entry["status"] == "dead", entry
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_kill_stamps_exit_code_143():
    """kill SIGTERMs a real own-group client decoy and stamps exit_code 143.

    The client never gets to write its own terminal line, so the kill action
    must carry the code the client cannot report.
    """
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    decoy = subprocess.Popen(
        [sys.executable, "-c", "import time  # pi_rpc_client\nimport time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epic143-coder",
                        "pid": decoy.pid,
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        result = _parse(pi_manage({"action": "kill", "session_id": "epic143-coder"}))
        assert "SIGTERM sent" in result["status"], result
        assert decoy.wait(timeout=10) != 0
        with open(p.REGISTRY_PATH) as f:
            entry = json.loads(f.readline())
        assert entry["status"] == "killed", entry
        assert entry["exit_code"] == 143, entry
    finally:
        if decoy.poll() is None:
            decoy.terminate()
            decoy.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_list_only_running():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "live-one",
                        "pid": os.getpid(),
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "session": "dead-one",
                        "pid": 999999,
                        "status": "running",
                        "started": "2024-01-02T00:00:00",
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "session": "killed-one",
                        "pid": os.getpid(),
                        "status": "killed",
                        "started": "2024-01-03T00:00:00",
                    }
                )
                + "\n"
            )
        only = _parse(pi_manage({"action": "list", "only_running": True}))["agents"]
        assert [a["session"] for a in only] == ["live-one"], only
        everything = _parse(pi_manage({"action": "list"}))["agents"]
        assert len(everything) == 3, everything
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


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


def test_handlers_match_framework_contract():
    """Framework calls handler(args: dict) with ONE positional dict.
    Both wrappers return JSON strings (registry contract); parse via
    json.loads after calling the dict-form wrappers."""
    result = pi_manage({"action": "list"})
    assert isinstance(result, str), result
    parsed = json.loads(result)
    assert "agents" in parsed, parsed
    launch = pi_launch(
        {
            "worker_type": "coder",
            "model": "anything",
            "skills": [CODING_RULES],
            "contract_path": CONTRACT_MD,
            "cwd": TMP_CWD,
            "epic_id": "epicContract",
        }
    )
    assert isinstance(launch, str), launch
    launch_parsed = json.loads(launch)
    assert "error" not in launch_parsed, launch_parsed
    assert launch_parsed["session_id"] == "epicContract-coder"
    assert "python3" in launch_parsed["command"]


def test_handlers_return_json_strings():
    """Wrappers return str and json.loads succeeds on the result."""
    mgr = pi_manage({"action": "list"})
    assert type(mgr) is str, mgr
    assert isinstance(json.loads(mgr), dict), mgr
    launch = pi_launch(
        {
            "worker_type": "coder",
            "model": "anything",
            "skills": [CODING_RULES],
            "contract_path": CONTRACT_MD,
            "cwd": TMP_CWD,
            "epic_id": "epicStr",
        }
    )
    assert type(launch) is str, launch
    assert json.loads(launch)["session_id"] == "epicStr-coder"


def test_handlers_tolerate_unknown_keys():
    """Unknown keys in args must not crash the wrappers; results are JSON."""
    r1 = pi_manage({"action": "list", "bogus": 1})
    assert "agents" in json.loads(r1), r1
    r2 = pi_launch(
        {
            "worker_type": "coder",
            "model": "anything",
            "skills": [],
            "contract_path": CONTRACT_MD,
            "cwd": TMP_CWD,
            "epic_id": "e",
            "wat": True,
        }
    )
    assert "error" not in json.loads(r2), r2


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


def _write_registry(*entries):
    with open(p.REGISTRY_PATH, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def test_pi_manage_wait_unknown_session():
    """wait on a session with no registry entry reports unfinished and unknown."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        _write_registry(
            {
                "session": "epicOther-coder",
                "pid": 999999,
                "status": "done",
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(pi_manage({"action": "wait", "session_id": "epicW-coder"}))
        assert result["finished"] is False, result
        assert result["status"] == "unknown", result
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_wait_terminal_pid_gone():
    """A done entry whose pid is gone is finished, and wait writes nothing."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    original_update = p._registry_update
    writes = []
    p._registry_update = lambda *a, **kw: writes.append((a, kw))
    try:
        _write_registry(
            {
                "session": "epicDone-coder",
                "pid": 999999,
                "status": "done",
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(pi_manage({"action": "wait", "session_id": "epicDone-coder"}))
        assert result["finished"] is True, result
        assert result["status"] == "done", result
        assert writes == [], writes
    finally:
        p._registry_update = original_update
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_wait_surfaces_exit_code():
    """wait reports the client's true exit code when the terminal line carries one."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        _write_registry(
            {
                "session": "epicX143-coder",
                "pid": 999999,
                "status": "killed",
                "exit_code": 143,
                "started": "2024-01-01T00:00:00",
            },
            {
                "session": "epicX0-coder",
                "pid": 999999,
                "status": "done",
                "exit_code": 0,
                "started": "2024-01-02T00:00:00",
            },
            {
                "session": "epicXnone-coder",
                "pid": 999999,
                "status": "timeout",
                "started": "2024-01-03T00:00:00",
            },
        )
        killed = _parse(pi_manage({"action": "wait", "session_id": "epicX143-coder"}))
        assert killed["finished"] is True and killed["status"] == "killed", killed
        assert killed["exit_code"] == 143, killed
        done = _parse(pi_manage({"action": "wait", "session_id": "epicX0-coder"}))
        assert done["finished"] is True and done["status"] == "done", done
        assert done["exit_code"] == 0, done
        without = _parse(pi_manage({"action": "wait", "session_id": "epicXnone-coder"}))
        assert without["finished"] is True and without["status"] == "timeout", without
        assert "exit_code" not in without, without
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_list_surfaces_exit_code():
    """list passes terminal exit_code entries through to the agents payload."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        _write_registry(
            {
                "session": "epicL143-coder",
                "pid": 999999,
                "status": "killed",
                "exit_code": 143,
                "started": "2024-01-01T00:00:00",
            }
        )
        agents = _parse(pi_manage({"action": "list"}))["agents"]
        assert len(agents) == 1, agents
        assert agents[0]["exit_code"] == 143, agents
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_wait_running_times_out():
    """A running entry with a live pid never finishes on a single probe."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_registry(
            {
                "session": "epicRun-coder",
                "pid": child.pid,
                "status": "running",
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(pi_manage({"action": "wait", "session_id": "epicRun-coder"}))
        assert result["finished"] is False, result
        assert result["status"] == "running", result
        assert result["pid"] == child.pid, result
    finally:
        child.terminate()
        child.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_wait_polls_until_dead():
    """A vanished pid settles a run even when status stayed running.

    finished=True means the completion state is settled in the registry, even if
    the exit write never landed: PID GONE settles it.
    """
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
    try:
        _write_registry(
            {
                "session": "epicPoll-coder",
                "pid": child.pid,
                "status": "running",
                "started": "2024-01-01T00:00:00",
            }
        )
        reaper = threading.Thread(target=child.wait, daemon=True)
        reaper.start()
        result = _parse(
            pi_manage(
                {
                    "action": "wait",
                    "session_id": "epicPoll-coder",
                    "timeout_s": 5,
                    "poll_s": 1,
                }
            )
        )
        assert result["finished"] is True, result
        assert result["status"] == "running", result
        assert p._pid_alive(child.pid) is False, result
    finally:
        reaper.join(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_wait_unknown_only_when_session_absent():
    """unknown is reserved for a session with no registry rows at all."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        _write_registry(
            {
                "session": "otherSession-coder",
                "pid": os.getpid(),
                "status": "running",
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(pi_manage({"action": "wait", "session_id": "ghostSession-coder"}))
        assert result["finished"] is False and result["status"] == "unknown", result
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_wait_running_entry_reports_running_not_unknown():
    """A registered live worker answers running with pid and heartbeat age."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_registry(
            {
                "session": "epicLive-coder",
                "pid": child.pid,
                "status": "running",
                "started": "2024-01-01T00:00:00",
            }
        )
        heartbeat = os.path.join(
            os.path.dirname(p.REGISTRY_PATH), "pi-heartbeat-epicLive-coder.json"
        )
        with open(heartbeat, "w") as f:
            json.dump({"ts": time.time(), "frames": 3, "tools": 1}, f)
        result = _parse(pi_manage({"action": "wait", "session_id": "epicLive-coder"}))
        assert result["finished"] is False, result
        assert result["status"] == "running", result
        assert result["pid"] == child.pid, result
        assert isinstance(result.get("last_activity_s"), int), result
        assert "unknown" not in json.dumps(result), result
    finally:
        child.terminate()
        child.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir)


FAKE_PI_SH = """#!/bin/sh
echo '{"type":"ready"}'
exec sleep 5
"""


def _spawn_real_client(tmpdir, session_id, cwd=None):
    """Spawn the real scripts/pi_rpc_client.py against a fake sleeping pi."""
    bin_dir = os.path.join(tmpdir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    fake_pi = os.path.join(bin_dir, "pi")
    with open(fake_pi, "w") as f:
        f.write(FAKE_PI_SH)
    os.chmod(fake_pi, 0o755)
    env = {
        **os.environ,
        "HOME": tmpdir,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [
            os.path.dirname(os.path.abspath(__file__)),
            os.path.dirname(p.CLIENT_PATH),
            env.get("PYTHONPATH", ""),
        ]
    )
    return subprocess.Popen(
        [
            sys.executable,
            os.path.join(os.path.dirname(p.CLIENT_PATH), "pi_rpc_client.py"),
            "--session-id",
            session_id,
            "--model",
            "m",
            "--prompt",
            "p",
            "--timeout",
            "60",
            *(["--cwd", str(cwd)] if cwd else []),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )


def test_pi_manage_wait_live_client_running_then_sigterm_143():
    """Real client subprocess: wait says running while its pid lives; after
    SIGTERM it finishes killed with exit_code 143. Never unknown. The fake pi
    emits one frame then sleeps, so last_activity_s is not guaranteed here
    (a running worker's first heartbeat tick can land before its logs dir
    exists) — the age field itself is covered by
    test_pi_manage_wait_running_entry_reports_running_not_unknown.
    """
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    p.REGISTRY_PATH = os.path.join(tmpdir, ".hermes", "logs", "pi-agents.jsonl")
    child = _spawn_real_client(tmpdir, "epicReal-coder")
    try:
        deadline = time.time() + 10
        saw_running_row = False
        while time.time() < deadline:
            if os.path.exists(p.REGISTRY_PATH):
                with open(p.REGISTRY_PATH) as f:
                    if "running" in f.read():
                        saw_running_row = True
                        break
            time.sleep(0.05)
        assert saw_running_row, "client never wrote its running row"

        running = _parse(pi_manage({"action": "wait", "session_id": "epicReal-coder"}))
        assert running["finished"] is False, running
        assert running["status"] == "running", running
        assert running["pid"] == child.pid, running

        child.terminate()
        assert child.wait(timeout=10) == 143
        finished = _parse(pi_manage({"action": "wait", "session_id": "epicReal-coder"}))
        assert finished["finished"] is True, finished
        assert finished["status"] == "killed", finished
        assert finished["exit_code"] == 143, finished
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir)


class FakeClientProc:
    """Alive decoy whose /proc cmdline names pi_rpc_client, like a real worker."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "import time  # pi_rpc_client\nimport time; time.sleep(30)"]
        )

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


def test_pi_manage_steer_writes_control_file(tmpdir):
    """steer targets a verified live pi_rpc_client process and writes the file."""
    _setup_registry(tmpdir)
    decoy = FakeClientProc()
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epicS-coder",
                        "pid": decoy.proc.pid,
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        result = _parse(
            p._pi_manage_impl(action="steer", session_id="epicS-coder", message="commit now")
        )
        steer_file = os.path.join(os.path.dirname(p.REGISTRY_PATH), "pi-steer", "epicS-coder.json")
        with open(steer_file) as f:
            payload = json.load(f)
        assert payload["message"] == "commit now"
        assert payload["queue"] is False
        assert "steer queued" in result["status"], result
    finally:
        decoy.stop()
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_steer_queue_true(tmpdir):
    _setup_registry(tmpdir)
    decoy = FakeClientProc()
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epicQ-coder",
                        "pid": decoy.proc.pid,
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        result = _parse(
            p._pi_manage_impl(
                action="steer", session_id="epicQ-coder", message="skip that", queue=True
            )
        )
        steer_file = os.path.join(os.path.dirname(p.REGISTRY_PATH), "pi-steer", "epicQ-coder.json")
        with open(steer_file) as f:
            payload = json.load(f)
        assert payload["queue"] is True
        assert "follow_up queued" in result["status"], result
    finally:
        decoy.stop()
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_steer_refuses_missing_session(tmpdir):
    _setup_registry(tmpdir)
    try:
        result = _parse(p._pi_manage_impl(action="steer", session_id="ghost-coder", message="hi"))
        assert "error" in result, result
        assert "not found in registry" in result["error"]
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_steer_refuses_dead_pid(tmpdir):
    _setup_registry(tmpdir)
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epicD-coder",
                        "pid": 999999,
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        result = _parse(p._pi_manage_impl(action="steer", session_id="epicD-coder", message="hi"))
        assert "error" in result, result
        assert "not running" in result["error"]
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_steer_refuses_non_client_pid(tmpdir):
    """The decoy is alive but not a pi_rpc_client: steer refuses."""
    _setup_registry(tmpdir)
    decoy = subprocess.Popen(["sleep", "30"])
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epicN-coder",
                        "pid": decoy.pid,
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        result = _parse(p._pi_manage_impl(action="steer", session_id="epicN-coder", message="hi"))
        assert "error" in result, result
        assert "pi_rpc_client" in result["error"]
    finally:
        decoy.terminate()
        decoy.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_steer_requires_message(tmpdir):
    _setup_registry(tmpdir)
    try:
        result = _parse(p._pi_manage_impl(action="steer", session_id="epicX-coder"))
        assert "error" in result, result
        assert "message required" in result["error"]
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_list_reports_heartbeat_age(tmpdir):
    """Running agents get last_activity_s from the client's heartbeat stamp."""
    import time as time_mod

    _setup_registry(tmpdir)
    decoy = subprocess.Popen(["sleep", "30"])
    try:
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epicH-coder",
                        "pid": decoy.pid,  # list only checks liveness
                        "status": "running",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        heartbeat = os.path.join(os.path.dirname(p.REGISTRY_PATH), "pi-heartbeat-epicH-coder.json")
        with open(heartbeat, "w") as f:
            json.dump({"ts": time_mod.time() - 120, "frames": 5, "tools": 2}, f)

        agents = _parse(p._pi_manage_impl(action="list"))["agents"]
        entry = next(a for a in agents if a["session"] == "epicH-coder")
        assert entry["last_activity_s"] >= 119, entry

        # finished agents never carry the stamp
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(
                json.dumps(
                    {
                        "session": "epicH2-coder",
                        "pid": decoy.pid,
                        "status": "done",
                        "started": "2024-01-01T00:00:00",
                    }
                )
                + "\n"
            )
        agents = _parse(p._pi_manage_impl(action="list"))["agents"]
        done_entry = next(a for a in agents if a["session"] == "epicH2-coder")
        assert "last_activity_s" not in done_entry, done_entry
    finally:
        decoy.terminate()
        decoy.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir)


REAL_PI_SESSIONS = """#!/bin/sh
mkdir -p "$PI_FAKE_SESSIONS/probe-dir"
printf '%s\n' '{"role":"assistant","content":"swept 40 tiles"}' \
  > "$PI_FAKE_SESSIONS/probe-dir/2026-01-01T00-00-00-000Z_epicReal2-coder.jsonl"
echo '{"type":"ready"}'
exec sleep 30
"""


def _spawn_real_client_transcript(tmpdir, session_id="epicReal2-coder"):
    """Real client subprocess whose fake pi leaves a transcript behind."""
    sessions_dir = os.path.join(tmpdir, ".pi", "agent", "sessions")
    bin_dir = os.path.join(tmpdir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    fake_pi = os.path.join(bin_dir, "pi")
    with open(fake_pi, "w") as f:
        f.write(REAL_PI_SESSIONS)
    os.chmod(fake_pi, 0o755)
    env = {
        **os.environ,
        "HOME": tmpdir,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "PI_FAKE_SESSIONS": sessions_dir,
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [
            os.path.dirname(os.path.abspath(__file__)),
            os.path.dirname(p.CLIENT_PATH),
            env.get("PYTHONPATH", ""),
        ]
    )
    child = subprocess.Popen(
        [
            sys.executable,
            os.path.join(os.path.dirname(p.CLIENT_PATH), "pi_rpc_client.py"),
            "--session-id",
            session_id,
            "--model",
            "m",
            "--prompt",
            "p",
            "--timeout",
            "30",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    return child, sessions_dir


def _wait_for_registry(fn, deadline=10):
    end = time.time() + deadline
    while time.time() < end:
        value = fn()
        if value is not None:
            return value
        time.sleep(0.05)
    return None


def test_pi_manage_logs_resolves_through_registry_log_path():
    """logs prefers the registry row's log_path over the slug glob: a worker
    with a differently-slugged transcript still resolves."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    transcript = os.path.join(tmpdir, "pi", "sessions", "other-slug", "2026-01-01T_a.jsonl")
    os.makedirs(os.path.dirname(transcript))
    with open(transcript, "w") as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                }
            )
            + "\n"
        )
    try:
        _write_registry(
            {
                "session": "epicLogs-coder",
                "pid": 999999,
                "status": "killed",
                "log_path": transcript,
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("logs", session_id="epicLogs-coder", lines=10))
        assert result.get("path") == transcript, result
        assert result.get("last_text") == "hi", result
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_logs_falls_back_to_slug_glob_without_log_path():
    """A pre-fix row without log_path still resolves through the old glob."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    real_home = os.environ.get("HOME")
    os.environ["HOME"] = tmpdir
    try:
        transcript_dir = os.path.join(tmpdir, ".pi", "agent", "sessions", "slugged")
        os.makedirs(transcript_dir)
        transcript = os.path.join(transcript_dir, "2026-01-01T_epicOld-coder.jsonl")
        with open(transcript, "w") as f:
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "old"}],
                        },
                    }
                )
                + "\n"
            )
        _write_registry(
            {
                "session": "epicOld-coder",
                "pid": 999999,
                "status": "done",
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("logs", session_id="epicOld-coder", lines=10))
        assert result.get("path") == transcript, result
    finally:
        if real_home is not None:
            os.environ["HOME"] = real_home
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_logs_unknown_when_no_path_and_no_glob_hit():
    """Neither log_path nor a glob hit: the no-transcript error stays."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    real_home = os.environ.get("HOME")
    os.environ["HOME"] = tmpdir
    try:
        _write_registry(
            {
                "session": "epicNone-coder",
                "pid": 999999,
                "status": "done",
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("logs", session_id="epicNone-coder", lines=10))
        assert "error" in result, result
        assert "no session log" in result["error"], result
    finally:
        if real_home is not None:
            os.environ["HOME"] = real_home
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_pi_manage_logs_killed_worker_resolves_via_log_path():
    """SIGTERM'd worker: logs still reads the transcript the row names."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    transcript = os.path.join(tmpdir, "pi", "sessions", "elsewhere", "2026-01-01T_b.jsonl")
    os.makedirs(os.path.dirname(transcript))
    with open(transcript, "w") as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "toolCall", "name": "bash"}],
                    },
                }
            )
            + "\n"
        )
    try:
        _write_registry(
            {
                "session": "epicKilled-coder",
                "pid": 999999,
                "status": "killed",
                "exit_code": 143,
                "log_path": transcript,
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("logs", session_id="epicKilled-coder", lines=10))
        assert result.get("path") == transcript, result
        assert result.get("tool_calls") == 1, result
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir)


def test_client_records_transcript_path_in_registry():
    """After the first frames, the client's registry row carries log_path
    pointing at the REAL transcript file the (fake) pi wrote."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    p.REGISTRY_PATH = os.path.join(tmpdir, ".hermes", "logs", "pi-agents.jsonl")
    child, sessions_dir = _spawn_real_client_transcript(tmpdir)
    transcript = os.path.join(
        sessions_dir, "probe-dir", "2026-01-01T00-00-00-000Z_epicReal2-coder.jsonl"
    )
    try:
        # given the running row lands
        deadline = time.time() + 10
        saw_running_row = False
        while time.time() < deadline:
            if os.path.exists(p.REGISTRY_PATH):
                with open(p.REGISTRY_PATH) as f:
                    if "running" in f.read():
                        saw_running_row = True
                        break
            time.sleep(0.05)
        assert saw_running_row, "client never wrote its running row"

        # when the transcript exists and the client processes its frame
        assert os.path.exists(transcript), "fake pi never wrote the transcript"

        def _row_log_path():
            if not os.path.exists(p.REGISTRY_PATH):
                return None
            with open(p.REGISTRY_PATH) as f:
                return json.loads(f.read()).get("log_path")

        log_path = _wait_for_registry(_row_log_path)

        # then the row names the real transcript
        assert log_path == transcript, log_path
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir)


GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "salvage",
    "GIT_AUTHOR_EMAIL": "salvage@test",
    "GIT_COMMITTER_NAME": "salvage",
    "GIT_COMMITTER_EMAIL": "salvage@test",
}


def _make_repo(pwd):
    """Init a tmp git repo: one base commit, then one later 'work' commit."""
    env = {**os.environ, **GIT_IDENTITY_ENV}
    subprocess.run(["git", "init", "-q", "-b", "main", pwd], check=True, env=env)
    subprocess.run(["git", "config", "user.email", "s@t"], cwd=pwd, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "s"], cwd=pwd, check=True, env=env)
    with open(os.path.join(pwd, "a.txt"), "w") as f:
        f.write("base\n")
    subprocess.run(["git", "add", "a.txt"], cwd=pwd, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "base commit"], cwd=pwd, check=True, env=env)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=pwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()
    with open(os.path.join(pwd, "a.txt"), "w") as f:
        f.write("changed\n")
    subprocess.run(["git", "add", "a.txt"], cwd=pwd, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "work commit"], cwd=pwd, check=True, env=env)
    with open(os.path.join(pwd, "a.txt"), "w") as f:
        f.write("uncommitted\n")
    return base


def test_salvage_report_collects_one_call(tmp_path):
    """One kill-shaped row (cwd/base/log_path stamped): the whole salvage
    order in a single answer, shaped for a continuation contract."""
    repo = str(tmp_path / "repo")
    base = _make_repo(repo)
    transcript = tmp_path / "pi" / "sessions" / "anywhere" / "t_epicSal-coder.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash"}]},
            }
        )
        + "\n"
    )
    _setup_registry(str(tmp_path))
    try:
        _write_registry(
            {
                "session": "epicSal-coder",
                "pid": 999999,
                "status": "killed",
                "exit_code": 143,
                "cwd": repo,
                "base": base,
                "log_path": str(transcript),
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicSal-coder", lines=5))
        assert "error" not in result, result
        assert result["session"] == "epicSal-coder"
        assert result["cwd"] == repo
        assert result["base"] == base
        assert "collected_at" in result
        assert result["git"]["status"].strip(), result["git"]
        assert result["git"]["commits_since_base"][0].endswith("work commit"), result["git"]
        assert any("a.txt" in line for line in result["git"]["diff_stat"]), result["git"]
        assert result["git"]["branch_head"], result["git"]
        assert result["frames"]["path"] == str(transcript), result
        assert result["frames"]["tool_calls"] == 1, result["frames"]
        gates = {row["name"]: row for row in result["gates"]}
        assert set(gates) == {"uvx-ruff-check", "compileall-plugin"}, gates
        for row in result["gates"]:
            assert row["ok"] in (True, False), row
            assert row["command"], row
    finally:
        _teardown_registry()


def test_salvage_report_git_facts_from_recorded_cwd(tmp_path):
    """The git facts describe the row's recorded cwd, not the process cwd."""
    repo = str(tmp_path / "repo")
    base = _make_repo(repo)
    other = str(tmp_path / "elsewhere-empty")
    os.makedirs(other)
    _setup_registry(str(tmp_path))
    old_cwd = os.getcwd()
    os.chdir(other)
    try:
        _write_registry(
            {
                "session": "epicCwd-coder",
                "pid": 999999,
                "status": "killed",
                "cwd": repo,
                "base": base,
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicCwd-coder"))
        assert "error" not in result, result
        # an unrelated empty cwd would make "git log" fail; the recorded cwd works
        assert result["git"]["commits_since_base"][0].endswith("work commit"), result["git"]
        assert result["cwd"] == repo
    finally:
        os.chdir(old_cwd)
        _teardown_registry()


def test_salvage_report_missing_base_reports_named_skips(tmp_path):
    """No base row field: commits_since_base/diff_stat carry skip reasons,
    the rest of the report still lands."""
    repo = str(tmp_path / "repo")
    _make_repo(repo)
    _setup_registry(str(tmp_path))
    try:
        _write_registry(
            {
                "session": "epicNoBase-coder",
                "pid": 999999,
                "status": "killed",
                "cwd": repo,
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicNoBase-coder"))
        assert "error" not in result, result
        assert result["base"] is None, result["base"]
        assert "skipped" in result["git"]["commits_since_base"], result["git"]
        assert "no base recorded" in result["git"]["commits_since_base"]["skipped"].lower(), result[
            "git"
        ]
        assert "skipped" in result["git"]["diff_stat"], result["git"]
        assert result["git"]["status"], result["git"]
    finally:
        _teardown_registry()


def test_salvage_report_invalid_base_reports_named_skips(tmp_path):
    """A base the repo does not know: named skip, not an exception."""
    repo = str(tmp_path / "repo")
    _make_repo(repo)
    _setup_registry(str(tmp_path))
    try:
        _write_registry(
            {
                "session": "epicBadBase-coder",
                "pid": 999999,
                "status": "killed",
                "cwd": repo,
                "base": "0" * 40,
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicBadBase-coder"))
        assert "error" not in result, result
        assert "skipped" in result["git"]["commits_since_base"], result["git"]
        assert "unknown revision" in result["git"]["commits_since_base"]["skipped"].lower(), result[
            "git"
        ]
    finally:
        _teardown_registry()


def test_salvage_report_unknown_session_error_unchanged(tmp_path):
    """No registry rows for the session: the existing unknown shape."""
    _setup_registry(str(tmp_path))
    try:
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicGhost-coder"))
        assert result["finished"] is False and result["status"] == "unknown", result
    finally:
        _teardown_registry()


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


def _lane_lock_path(cwd, session_id):
    return os.path.join(cwd, ".hermes", "lane", "LOCK", f"{session_id}.lock")


def test_lane_lock_lifecycle_live_and_exit(tmp_path):
    """Real client subprocess: lock present while running (list surfaces
    present:true stale:false), lock gone when the client exits."""
    proj = str(tmp_path / "proj")
    os.makedirs(proj)
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("marker\n")
    session = "task-lockfix-default"
    # ONE world: the child's HOME carries the registry (like prod), list reads
    # that p.REGISTRY_PATH, and the LOCK lands per-cwd under proj/.hermes/lane.
    child_home = str(tmp_path / "home")
    _setup_registry(os.path.join(child_home, ".hermes", "logs"))
    p.REGISTRY_PATH = os.path.join(child_home, ".hermes", "logs", "pi-agents.jsonl")
    child = _spawn_real_client(child_home, session, cwd=proj)
    lock_path = _lane_lock_path(proj, session)
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(lock_path):
            time.sleep(0.05)
        assert os.path.exists(lock_path), "client never wrote its lane lock"
        with open(lock_path) as f:
            payload = json.load(f)
        assert payload["session_id"] == session
        assert payload["pid"] == child.pid

        agents = _parse(p._pi_manage_impl("list"))["agents"]
        mine = next(a for a in agents if a["session"] == session)
        assert mine["lane_lock"] == {"present": True, "stale": False}, mine

        child.terminate()
        assert child.wait(timeout=10) == 143
        assert not os.path.exists(lock_path), "lock file survived the client exit"
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        _teardown_registry()


def test_lane_lock_stale_after_crash_surfaced_and_reported(tmp_path):
    """kill -9 shaped crash: lock persists; list marks stale:true;
    salvage_report names the file + the rm command and deletes nothing."""
    proj = str(tmp_path / "proj")
    os.makedirs(proj)
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("marker\n")
    session = "task-lockstale-default"
    _setup_registry(str(tmp_path))
    lock_dir = os.path.join(proj, ".hermes", "lane", "LOCK")
    os.makedirs(lock_dir)
    lock_path = os.path.join(lock_dir, f"{session}.lock")
    dead_pid = 999999
    with open(lock_path, "w") as f:
        json.dump({"session_id": session, "pid": dead_pid, "model": "m", "started_at": "now"}, f)
    _write_registry(
        {
            "session": session,
            "pid": dead_pid,
            "status": "running",
            "cwd": proj,
            "started": "2024-01-01T00:00:00",
        }
    )

    agents = _parse(p._pi_manage_impl("list"))["agents"]
    mine = next(a for a in agents if a["session"] == session)
    assert mine["lane_lock"] == {"present": True, "stale": True}, mine

    result = _parse(p._pi_manage_impl("salvage_report", session_id=session))
    lock_row = result.get("lane_lock")
    assert lock_row, result
    assert lock_row["path"] == lock_path
    assert lock_row["stale"] is True
    assert f"rm {lock_path}" in lock_row["removal"], lock_row
    # the action itself deletes nothing
    assert os.path.exists(lock_path)
    _teardown_registry()


def _auditor_launch(**over):
    """One marked-cwd auditor launch skeleton with an existing anchor."""
    proj = tempfile.mkdtemp(prefix="pi-delegation-auditor-")
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("test repo marker\n")
    with open(os.path.join(proj, "review-report.md"), "w") as f:
        f.write("empty\n")
    base = dict(
        worker_type="auditor",
        skills=[],
        contract_path=_contract_file(),
        cwd=proj,
        model="anything",
        epic_id="epicAuditor",
        require_edit=[os.path.join(proj, "review-report.md")],
    )
    result = p._pi_launch_impl(**{**base, **over})
    return result, proj


def test_auditor_argv_carries_deliverable_toolset():
    result, proj = _auditor_launch(min_tool_calls=1, max_nudges=0)
    assert "error" not in result, result
    assert "--tools read,grep,find,ls,write,edit" in result["command"]
    assert "--codemode" not in result["command"], result["command"]
    assert "--require-edit" in result["command"] and "review-report.md" in result["command"], result
    shutil.rmtree(proj, ignore_errors=True)


def test_auditor_without_require_edit_errors_at_preflight():
    cmd = str(tempfile.mkdtemp())
    with open(os.path.join(cmd, "AGENTS.md"), "w") as f:
        f.write("m\n")
    result = _parse(
        p._pi_launch_impl(
            worker_type="auditor",
            skills=[],
            contract_path=_contract_file(),
            cwd=cmd,
            model="m2",
            epic_id="epicAuditorNoAim",
        )
    )
    assert "error" in result, result
    assert "auditor" in result["error"].lower(), result["error"]
    assert "deliverable" in result["error"].lower(), result["error"]
    shutil.rmtree(cmd, ignore_errors=True)


def test_auditor_final_text_written_to_require_edit_at_done(tmp_path):
    """Real subprocess client (auditor-style run): capture-into-deliverable on
    done keeps integrity: status done + exit_code 0 + the text file filled."""
    import pi_rpc_client as pmod

    ack_frame = (
        '{"type":"response","command":"prompt","success":true,"data":{"disposition":"started"}}'
    )
    end_frame = (
        '{"type":"agent_end","messages":[{"role":"assistant","content":'
        '[{"type":"text","text":"AUDIT FINAL REPORT: OK"}]}]}'
    )
    fake_pi_sh = (
        "#!/bin/sh\nsleep 0.2\n"
        "printf '%s\\n' '{\"type\":\"ready\"}'\n"
        f"printf '%s\\n' '{ack_frame}'\n"
        f"printf '%s\\n' '{end_frame}'\n"
        "exec sleep 30\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_pi = bin_dir / "pi"
    fake_pi.write_text(fake_pi_sh)
    fake_pi.chmod(0o755)
    deliverable = tmp_path / "review-report.md"
    deliverable.write_text("empty\n")
    (tmp_path / ".hermes" / "logs").mkdir(parents=True)
    pmod.REGISTRY_PATH = str(tmp_path / ".hermes" / "logs" / "pi-agents.jsonl")
    child = subprocess.Popen(
        [
            sys.executable,
            p.CLIENT_PATH,
            "--session-id",
            "task-audit-default",
            "--model",
            "m2",
            "--prompt",
            "review things",
            "--timeout",
            "60",
            "--require-edit",
            str(deliverable),
            "--min-tool-calls",
            "0",
            "--max-nudges",
            "0",
            "--auditor-deliverable",
            "--agent-dir",
            str(tmp_path / "agent-worker"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "PYTHONPATH": os.pathsep.join(
                [
                    os.path.dirname(os.path.abspath(__file__)),
                    os.path.dirname(p.CLIENT_PATH),
                    os.environ.get("PYTHONPATH", ""),
                ]
            ),
        },
    )
    try:
        rc = child.wait(timeout=20)
        assert rc == 0, rc
        assert (
            deliverable.read_text() == "AUDIT FINAL REPORT: OK\n"
            or deliverable.read_text().startswith("AUDIT FINAL REPORT")
        ), deliverable.read_text()
        with open(pmod.REGISTRY_PATH) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        terminal = rows[-1]
        assert terminal["status"] == "done", rows
        assert terminal["exit_code"] == 0, terminal
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


def test_auditor_wait_reports_done_exit_code(tmp_path):
    """pi_manage wait on the auditor row: finished true, done, exit_code 0."""
    proj = tempfile.mkdtemp(prefix="pi-delegation-auditor-wait-")
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("m\n")
    with open(os.path.join(proj, "review.md"), "w") as f:
        f.write("x\n")
    _setup_registry(proj)
    try:
        _write_registry(
            {
                "session": "epicAuditorWait-coder",
                "pid": 999999,
                "status": "done",
                "exit_code": 0,
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(pi_manage({"action": "wait", "session_id": "epicAuditorWait-coder"}))
        assert result["finished"] is True and result["status"] == "done", result
        assert result["exit_code"] == 0, result
    finally:
        _teardown_registry()
        shutil.rmtree(proj, ignore_errors=True)


def _auto_launch_env(tmp_path):
    """A fake pi + fake HOME: launcher-spawned clients frame against it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ack = '{"type":"response","command":"prompt","success":true,"data":{"disposition":"started"}}'
    end = (
        '{"type":"agent_end","messages":[{"role":"assistant","content":'
        '[{"type":"text","text":"AUTO DISPATCH OK"}]}]}'
    )
    fake_pi = bin_dir / "pi"
    fake_pi.write_text(
        "#!/bin/sh\nsleep 0.2\n"
        f"printf '%s\\n' '{ack}'\n"
        "printf '%s\\n' '{\"type\":\"ready\"}'\n"
        f"printf '%s\\n' '{end}'\n"
        "exec sleep 30\n"
    )
    fake_pi.chmod(0o755)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("marker\n")
    return str(bin_dir), str(proj)


def _auto_launch_kwargs(bin_dir, proj):
    contract = os.path.join(proj, "contract.md")
    with open(contract, "w") as f:
        f.write("worker contract\n")
    with open(os.path.join(proj, "a.go"), "w") as f:
        f.write("package main\n")
    return {
        "worker_type": "coder",
        "skills": [],
        "contract_path": contract,
        "cwd": proj,
        "epic_id": "epicAuto",
    }


def test_launch_auto_true_dispatches_and_registers(tmp_path, monkeypatch):
    bin_dir, proj = _auto_launch_env(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path))
    batter = _auto_launch_kwargs(bin_dir, proj)
    result = _parse(pi_launch({**batter, "auto": True}))
    assert "error" not in result, result
    assert result.get("dispatched") is True, result
    assert isinstance(result.get("pid"), int), result
    assert result["session_id"] == "epicAuto-coder"
    log_path = result.get("log")
    assert log_path and os.path.exists(log_path), result

    p.REGISTRY_PATH = os.path.join(str(tmp_path), ".hermes", "logs", "pi-agents.jsonl")

    def _row():
        if not os.path.exists(p.REGISTRY_PATH):
            return None
        with open(p.REGISTRY_PATH) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return next((r for r in rows if r.get("session") == "epicAuto-coder"), None)

    row = _wait_for_registry(_row)
    assert row and row["status"] == "running", row
    os.kill(result["pid"], signal.SIGTERM)


def test_launch_auto_false_prints_command_only(tmp_path, monkeypatch):
    import shlex as _shlex

    bin_dir, proj = _auto_launch_env(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    result = _parse(pi_launch(_auto_launch_kwargs(bin_dir, proj)))
    assert "error" not in result, result
    assert "dispatched" not in result and "pid" not in result, result
    cmd = result["command"]
    assert "\n" not in cmd and cmd.startswith("echo 'preflight"), result
    assert _shlex.split(cmd)


def test_launch_auto_true_with_preflight_failure_spawns_nothing(tmp_path, monkeypatch):
    bin_dir, proj = _auto_launch_env(tmp_path)
    kwargs = _auto_launch_kwargs(bin_dir, proj)
    kwargs["require_edit"] = os.path.join(proj, "anchor-does-not-exist.md")
    result = _parse(pi_launch({**kwargs, "auto": True}))
    assert result.get("error", "").startswith("preflight"), result
    assert "dispatched" not in result and "pid" not in result, result
    # no process landed
    objs = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    assert "pi_rpc_client.py" not in objs or "epicAuto" not in objs


def test_launch_auto_no_contract_spawns_nothing(tmp_path, monkeypatch):
    bin_dir, proj = _auto_launch_env(tmp_path)
    kwargs = _auto_launch_kwargs(bin_dir, proj)
    kwargs["contract_path"] = os.path.join(proj, "missing-contract.md")
    result = _parse(pi_launch({**kwargs, "auto": True}))
    assert result.get("error", "").startswith("preflight"), result
    assert "dispatched" not in result, result

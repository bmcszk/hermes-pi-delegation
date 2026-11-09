import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import types

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
CONTRACT_MD = os.path.join(TMP_CWD, "contract.md")
with open(CONTRACT_MD, "w") as _f:
    _f.write("test contract\n")

_registry_backup = p.REGISTRY_PATH


def _parse(result):
    """Helper: parse JSON string from handler returns."""
    return json.loads(result) if isinstance(result, str) else result


def _setup_registry(tmpdir):
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
                "skills": ["~/.agents/skills/coding-rules", "~/.agents/skills/go-dev"],
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
    assert "--skill" in parts and os.path.expanduser("~/.agents/skills/coding-rules") in parts
    assert os.path.expanduser("~/.agents/skills/go-dev") in parts
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
                "skills": ["~/.agents/skills/coding-rules"],
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
                "skills": ["~/.agents/skills/coding-rules"],
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
                "skills": ["~/.agents/skills/coding-rules"],
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
                "skills": ["~/.agents/skills/coding-rules"],
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
                "skills": ["~/.agents/skills/coding-rules"],
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
    # given the skill exists only under ~/.hermes/skills/<category>/
    proj = tmp_path / "proj"
    proj.mkdir()
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
    monkeypatch.setenv("HOME", str(tmp_path))
    # when launching with a bare name that exists nowhere
    result = _launch_with_skill("probe-not-anywhere-xyz", str(proj))
    # then the launch is refused with the same error shape as unknown paths
    assert result.get("error") == "skill not found: probe-not-anywhere-xyz"


def test_pi_launch_compliance_block_uses_resolved_paths(tmp_path, monkeypatch):
    # given a bare-name skill resolved from the global agents root
    proj = tmp_path / "proj"
    proj.mkdir()
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
                "skills": ["~/.agents/skills/go-dev"],
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
            "coder",
            ["~/.agents/skills/go-dev"],
            CONTRACT_MD,
            TMP_CWD,
            "anything",
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
    for wt in WORKER_TYPES:
        result = _parse(
            pi_launch(
                {
                    "worker_type": wt,
                    "model": "anything",
                    "skills": ["~/.agents/skills/coding-rules"],
                    "contract_path": CONTRACT_MD,
                    "cwd": TMP_CWD,
                    "epic_id": "epicX",
                    "mode": "standard",
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
        assert parts[0] == "python3", wt


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
    assert "error" in result


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
                "skills": ["~/.agents/skills/coding-rules", "~/.agents/skills/go-dev"],
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
                    "skills": ["~/.agents/skills/coding-rules"],
                    "contract_path": CONTRACT_MD,
                    "cwd": TMP_CWD,
                    "epic_id": "epicCwd",
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
            "skills": ["~/.agents/skills/coding-rules"],
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
            "skills": ["~/.agents/skills/coding-rules"],
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
    assert "contract_path not found" in result["error"], result


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
    assert "skill not found: /nonexistent/skill" in result["error"], result


def test_launch_warns_hermes_local_skill():
    """Existing skill outside .agents/.pi discovery -> warnings, command still built."""
    skill = os.path.join(TMP_CWD, "outside-skill.md")
    with open(skill, "w") as f:
        f.write("x\n")
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
    skill_dir = os.path.join(proj, ".agents", "skills", "foo")
    os.makedirs(skill_dir)
    skill = os.path.join(skill_dir, "SKILL.md")
    with open(skill, "w") as f:
        f.write("x\n")
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

    # then
    assert "cwd" in required


def test_launch_marks_model_required():
    # given a payload carrying every other required field, but no model alias
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

    # then the schema marks model required and a real launch without it errors
    assert "model" in required
    assert "error" in result, result
    assert "model" in result["error"]

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
    base = dict(
        worker_type="coder",
        skills=[],
        contract_path=_contract_file(),
        cwd=p.os.getcwd(),
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
    base = dict(
        worker_type="coder",
        skills=[],
        contract_path=_contract_file(),
        cwd=p.os.getcwd(),
        model="anything",
        epic_id="test-epic",
    )

    # when
    result = p._pi_launch_impl(**base, min_tool_calls=5)

    # then
    assert "error" in result and "require_edit" in result["error"]


def test_launch_emits_default_gates_with_require_edit():
    # given
    base = dict(
        worker_type="coder",
        skills=[],
        contract_path=_contract_file(),
        cwd=p.os.getcwd(),
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

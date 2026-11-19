"""Launch defaults: the bare launch dispatches, echo mode is diagnostic only.

A launch without `auto` must spawn the worker and answer dispatched/pid/
session_id/log with no `command` field, so no caller can read a printout as a
live dispatch. `auto=false` is the explicit diagnostic echo: it carries the
command and names itself echo-only, and it never carries the wait hint.
"""

import contextlib
import json
import os
import signal
from datetime import datetime, timedelta

from helpers import CONTRACT_MD, TMP_CWD, _parse

import __init__ as p
from __init__ import pi_launch, pi_manage


def _dispatch_env(tmp_path):
    """A fake pi + project: a dispatched client frames against the fake."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ack = '{"type":"response","command":"prompt","success":true,"data":{"disposition":"started"}}'
    end = (
        '{"type":"agent_end","messages":[{"role":"assistant","content":'
        '[{"type":"text","text":"DEFAULT DISPATCH OK"}]}]}'
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
    proj.mkdir(exist_ok=True)
    (proj / "AGENTS.md").write_text("marker\n")
    (proj / "contract.md").write_text("worker contract\n")
    return bin_dir, proj


def _dispatch_kwargs(proj, epic_id="epicDefault"):
    return {
        "worker_type": "coder",
        "skills": [],
        "contract_path": str(proj / "contract.md"),
        "cwd": str(proj),
        "epic_id": epic_id,
    }


def _registry_rows():
    with open(p.REGISTRY_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def test_launch_without_auto_spawns_and_answers_no_command(tmp_path, monkeypatch):
    # given a launchable project and a fake pi on PATH
    bin_dir, proj = _dispatch_env(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path))
    # when the launch passes no auto argument at all
    result = _parse(pi_launch(_dispatch_kwargs(proj)))
    try:
        # then the worker is dispatched and no printout is offered
        assert "error" not in result, result
        assert result.get("dispatched") is True, result
        assert isinstance(result.get("pid"), int), result
        assert result["session_id"] == "epicDefault-coder", result
        assert result.get("log") and os.path.exists(result["log"]), result
        assert "command" not in result, result
        assert "wait" in result.get("next", ""), result
    finally:
        if result.get("pid"):
            with contextlib.suppress(OSError):
                os.kill(result["pid"], signal.SIGTERM)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(result["pid"], 0)
        with contextlib.suppress(Exception):
            pi_manage({"action": "kill", "session_id": "epicDefault-coder"})


def test_echo_mode_names_itself_and_carries_no_wait_hint():
    # given the explicit diagnostic mode
    payload = {
        "worker_type": "coder",
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
        "epic_id": "epicEcho",
        "auto": False,
    }
    # when the launch runs
    result = _parse(pi_launch(payload))
    # then it is a printout named as such, with nothing to wait on
    assert "error" not in result, result
    assert result["mode"] == "echo-only", result
    assert "next" not in result, result
    assert "dispatched" not in result and "pid" not in result, result
    assert result["command"].startswith("echo 'preflight"), result
    assert result["session_id"] == "epicEcho-coder", result


def test_echo_mode_leaves_intent_row_so_wait_classifies_immediately():
    # given an echo-only launch
    payload = {
        "worker_type": "coder",
        "skills": [],
        "contract_path": CONTRACT_MD,
        "cwd": TMP_CWD,
        "epic_id": "epicEchoRow",
        "auto": False,
    }
    assert "error" not in _parse(pi_launch(payload)), "launch refused"
    # when the intent row ages past the grace window
    _age_pending_row("epicEchoRow-coder", 60)
    original = p._client_pids
    p._client_pids = lambda session_id: []
    try:
        result = _parse(
            pi_manage({"action": "wait", "session_id": "epicEchoRow-coder", "timeout_s": 3})
        )
    finally:
        p._client_pids = original
    # then wait answers never-dispatched at once instead of burning the budget
    assert result["finished"] is False, result
    assert result["status"] == "never-dispatched", result
    assert result["command_age_s"] >= 25, result


def test_auto_schema_documents_dispatch_default_and_echo_mode():
    description = p.LAUNCH_PARAMS["auto"]["description"].lower()
    assert "default true" in description, description
    assert "diagnostic" in description, description
    assert "spawns nothing" in description, description


def _age_pending_row(session_id, age_s):
    """Rewrite one pending row's started stamp, as if the command were old."""
    started = (datetime.now() - timedelta(seconds=age_s)).isoformat()
    rows = _registry_rows()
    for row in rows:
        if row.get("session") == session_id and row.get("status") == "pending-dispatch":
            row["started"] = started
    with open(p.REGISTRY_PATH, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

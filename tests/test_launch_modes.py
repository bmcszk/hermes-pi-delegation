import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

from helpers import (
    _parse,
    _setup_registry,
    _teardown_registry,
    _wait_for_registry,
    _write_registry,
)

import __init__ as p
from __init__ import pi_launch, pi_manage


def _auditor_launch(**over):
    """One marked-cwd auditor launch skeleton with an existing anchor."""
    proj = tempfile.mkdtemp(prefix="pi-delegation-auditor-")
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("test repo marker\n")
    with open(os.path.join(proj, "contract.md"), "w") as f:
        f.write("worker contract\n")
    with open(os.path.join(proj, "review-report.md"), "w") as f:
        f.write("empty\n")
    base = dict(
        worker_type="auditor",
        skills=[],
        contract_path=os.path.join(proj, "contract.md"),
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
    with open(os.path.join(cmd, "contract.md"), "w") as f:
        f.write("c\n")
    result = _parse(
        p._pi_launch_impl(
            worker_type="auditor",
            skills=[],
            contract_path=os.path.join(cmd, "contract.md"),
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

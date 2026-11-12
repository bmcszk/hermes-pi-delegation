"""Launch modes: the auditor deliverable toolset and the auto-dispatch gate
(preflight failure spawns nothing)."""

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

import pytest
from helpers import _parse, _wait_for_registry

import __init__ as p
from __init__ import pi_launch


def _auditor_project(dir_path=None):
    proj = dir_path or tempfile.mkdtemp(prefix="pi-delegation-auditor-")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("test repo marker\n")
    with open(os.path.join(proj, "contract.md"), "w") as f:
        f.write("worker contract\n")
    with open(os.path.join(proj, "review-report.md"), "w") as f:
        f.write("empty\n")
    return proj


@pytest.mark.parametrize(
    ("require_edit", "expect"),
    [
        pytest.param(
            {"require_edit": ["review-report.md"], "min_tool_calls": 1, "max_nudges": 0},
            "ok",
            id="argv-carries-deliverable-toolset",
        ),
        pytest.param({}, "error", id="without-require-edit-refused"),
    ],
)
def test_auditor_launch_contract(require_edit, expect):
    proj = _auditor_project()
    base = dict(
        worker_type="auditor",
        skills=[],
        contract_path=os.path.join(proj, "contract.md"),
        cwd=proj,
        model="anything",
        epic_id="epicAuditor",
    )
    result = _parse(p._pi_launch_impl(**{**base, **require_edit}))
    if expect == "ok":
        assert "error" not in result, result
        assert "--tools read,grep,find,ls,write,edit" in result["command"], result
        assert "--codemode" not in result["command"], result["command"]
        assert "--require-edit" in result["command"] and "review-report.md" in result["command"]
    else:
        assert "error" in result, result
        assert "auditor" in result["error"].lower() and "deliverable" in result["error"].lower()
    shutil.rmtree(proj, ignore_errors=True)


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
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_pi = bin_dir / "pi"
    fake_pi.write_text(
        "#!/bin/sh\nsleep 0.2\n"
        "printf '%s\\n' '{\"type\":\"ready\"}'\n"
        f"printf '%s\\n' '{ack_frame}'\n"
        f"printf '%s\\n' '{end_frame}'\n"
        "exec sleep 30\n"
    )
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
        assert deliverable.read_text().startswith("AUDIT FINAL REPORT"), deliverable.read_text()
        with open(pmod.REGISTRY_PATH) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        terminal = rows[-1]
        assert terminal["status"] == "done" and terminal["exit_code"] == 0, rows
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


def _auto_launch_env(tmp_path):
    """A fake pi + fake HOME: launcher-spawned clients frame against it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
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
    proj.mkdir(exist_ok=True)
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
    result = _parse(pi_launch({**_auto_launch_kwargs(bin_dir, proj), "auto": True}))
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
    with contextlib.suppress(ChildProcessError):
        # the launcher's child may already be reaped; SIGTERM still landed
        os.waitpid(result["pid"], 0)

    # auto=false prints the command only: no dispatch, no pid
    result = _parse(pi_launch(_auto_launch_kwargs(bin_dir, proj)))
    assert "error" not in result, result
    assert "dispatched" not in result and "pid" not in result, result
    cmd = result["command"]
    assert "\n" not in cmd and cmd.startswith("echo 'preflight"), result
    shlex.split(cmd)


def test_launch_auto_with_preflight_failure_spawns_nothing(tmp_path, monkeypatch):
    for broken_param in ("require_edit", "contract_path"):
        bin_dir, proj = _auto_launch_env(tmp_path)
        kwargs = _auto_launch_kwargs(bin_dir, proj)
        # the launcher's own copy of the contract exists; point at a missing one
        kwargs[broken_param] = os.path.join(proj, "does-not-exist.md")
        result = _parse(pi_launch({**kwargs, "auto": True}))
        assert result.get("error", "").startswith("preflight"), (broken_param, result)
        assert "dispatched" not in result and "pid" not in result, result
    # no process for OUR session landed (unrelated workers may run on this machine)
    objs = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    ours = [line for line in objs.splitlines() if "pi_rpc_client.py" in line]
    assert all("epicAuto" not in line for line in ours), ours

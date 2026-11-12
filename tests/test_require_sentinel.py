"""require_sentinel: the deliverable gate that call counts cannot fake.

A coder dispatch with min_tool_calls=12 exited 0 / satisfied after 35 tool calls
that were all upstream research: the require_edit file still held only its
header. `satisfied` counts calls, so the parent had to read the anchor file to
learn the sentinel never landed. require_sentinel greps that file at exit and
records deliverable: met|unmet|unchecked on the registry row, which wait and
list pass straight through.
"""

import json
import os
import subprocess
import sys

import pytest
from helpers import _parse, _setup_registry, _teardown_registry, _write_registry

import __init__ as p
from __init__ import pi_launch, pi_manage

CLIENT = p.CLIENT_PATH


def _registry_rows(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _deliverable_probe(tmp_path, anchor_text):
    """The client module plus an anchor file holding anchor_text."""
    sys.path.insert(0, os.path.dirname(CLIENT))
    import pi_rpc_client as mod

    anchor = tmp_path / "lane-log.md"
    anchor.write_text(anchor_text)
    return mod, str(anchor)


# -- launch side ---------------------------------------------------------------


def _marker_cwd(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "AGENTS.md").write_text("test repo marker\n")
    (proj / "contract.md").write_text("worker contract\n")
    (proj / "a.go").write_text("package main\n")
    return proj


def _sentinel_launch(tmp_path, require_edit=True, **over):
    proj = _marker_cwd(tmp_path)
    _setup_registry(str(tmp_path))
    payload = {
        "worker_type": "coder",
        "skills": [],
        "contract_path": str(proj / "contract.md"),
        "cwd": str(proj),
        "epic_id": "epicSent",
        **over,
    }
    if require_edit:
        payload["require_edit"] = [str(proj / "a.go")]
    try:
        return _parse(pi_launch(payload))
    finally:
        _teardown_registry()


def test_launch_require_sentinel_flag(tmp_path):
    # the registered schema and the impl agree on the new param
    assert "require_sentinel" in p.LAUNCH_PARAMS
    assert p._doctor()["schema_missing"] == []

    result = _sentinel_launch(tmp_path, require_sentinel="TEXTS1-DONE")
    assert "error" not in result, result
    assert "--require-sentinel" in result["command"], result["command"]
    assert "TEXTS1-DONE" in result["command"]
    assert result["require_sentinel"] == "TEXTS1-DONE", result

    # the flag is omitted when not requested
    result = _sentinel_launch(tmp_path)
    assert "error" not in result, result
    assert "--require-sentinel" not in result["command"], result["command"]
    assert "require_sentinel" not in result, result

    # the gate has no file to grep without require_edit: refused at preflight
    result = _sentinel_launch(
        tmp_path, require_edit=False, epic_id="epicSentNoAim", require_sentinel="DONE"
    )
    assert "error" in result, result
    assert "require_sentinel" in result["error"], result["error"]
    assert "require_edit" in result["error"], result["error"]

    # the pattern is documented with a real example
    readme_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"
    )
    with open(readme_path) as f:
        readme = f.read()
    assert "require_sentinel" in readme and "deliverable" in readme and "unmet" in readme


# -- exit-time check -----------------------------------------------------------


@pytest.mark.parametrize(
    ("sentinel", "anchor_text", "verdict"),
    [
        pytest.param("TEXTS1-DONE", "## log\nwork\nTEXTS1-DONE\n", "met", id="present-met"),
        pytest.param("TEXTS1-DONE", "## log\n(header only)\n", "unmet", id="absent-unmet"),
        pytest.param(None, "## log\nanything\n", "unchecked", id="not-requested-unchecked"),
        pytest.param("A.B", "A B\nAxB\n", "unmet", id="literal-not-regex"),
    ],
)
def test_deliverable_verdict(sentinel, anchor_text, verdict, tmp_path):
    mod, anchor = _deliverable_probe(tmp_path, anchor_text)
    if sentinel is None:
        assert mod._deliverable_verdict(anchor, None) == verdict
    else:
        assert mod._deliverable_verdict(anchor, sentinel) == verdict


# -- registry column -----------------------------------------------------------


def _run_sentinel_client(tmp_path, session, anchor_text, fake_pi_body):
    """Run the real client against a fake pi: (rc, terminal registry row)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ack = '{"type":"response","command":"prompt","success":true,"data":{"disposition":"started"}}'
    tool = (
        '{"type":"message","message":{"role":"assistant","content":'
        '[{"type":"toolCall","name":"bash","id":"t1"}]}}'
    )
    end = (
        '{"type":"agent_end","messages":[{"role":"assistant",'
        '"content":[{"type":"text","text":"d"}]}]}'
    )
    fake_pi = bin_dir / "pi"
    # ready FIRST (the client waits for it before sending the prompt), then the
    # body's `read` unblocks on the prompt frame, so any file write lands after
    # the client snapshotted its digests
    fake_pi.write_text(
        "#!/bin/sh\nsleep 0.1\n"
        "printf '%s\\n' '{\"type\":\"ready\"}'\n"
        f"{fake_pi_body}\n"
        f"printf '%s\\n' '{ack}'\n"
        f"printf '%s\\n' '{tool}'\n"
        f"printf '%s\\n' '{end}'\n"
        "exec sleep 20\n"
    )
    fake_pi.chmod(0o755)
    anchor = tmp_path / "lane-log.md"
    anchor.write_text(anchor_text)
    registry = tmp_path / ".hermes" / "logs" / "pi-agents.jsonl"
    (tmp_path / ".hermes" / "logs").mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        [
            sys.executable,
            CLIENT,
            "--session-id",
            session,
            "--model",
            "m",
            "--prompt",
            "p",
            "--cwd",
            str(tmp_path),
            "--timeout",
            "30",
            "--min-tool-calls",
            "1",
            "--max-nudges",
            "0",
            "--require-edit",
            str(anchor),
            "--require-sentinel",
            "TEXTS1-DONE",
            "--agent-dir",
            str(tmp_path / "agent-worker"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "PYTHONPATH": os.path.dirname(CLIENT),
        },
    )
    try:
        rc = child.wait(timeout=30)
        rows = [r for r in _registry_rows(registry) if r["session"] == session]
        return rc, rows[-1]
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


@pytest.mark.parametrize(
    ("appended", "expect_deliverable"),
    [
        pytest.param("work done", "unmet", id="worked-but-no-sentinel"),
        pytest.param("TEXTS1-DONE", "met", id="sentinel-landed"),
    ],
)
def test_client_records_deliverable_on_the_terminal_row(appended, expect_deliverable, tmp_path):
    session = f"task-sent-{expect_deliverable}-default"
    # the run touches the anchor after the client's before-snapshot (`read`
    # waits for the prompt frame) and optionally appends the sentinel line
    body = f'read __prompt; printf "%s\\\\n" "{appended}" >> {tmp_path / "lane-log.md"}'

    rc, terminal = _run_sentinel_client(tmp_path, session, "## lane log (append-only)\n", body)

    # then
    assert rc == 0, rc
    assert terminal["deliverable"] == expect_deliverable, terminal


# -- wait / list pass-through --------------------------------------------------


def test_deliverable_verdict_edge_cases(tmp_path):
    # a vanished deliverable file cannot count as met; regex metacharacters in
    # the sentinel are matched literally
    mod, _ = _deliverable_probe(tmp_path, "x\n")
    assert mod._deliverable_verdict(str(tmp_path / "gone.md"), "DONE") == "unmet"
    mod2, anchor2 = _deliverable_probe(tmp_path, "A B\nAxB\n")
    assert mod2._deliverable_verdict(anchor2, "A.B") == "unmet"


def test_deliverable_passes_through_to_wait_and_list(tmp_path):
    _setup_registry(str(tmp_path))
    try:
        _write_registry(
            {
                "session": "epicPass-coder",
                "pid": 999999,
                "status": "incomplete",
                "exit_code": 3,
                "deliverable": "unmet",
                "started": "2024-01-01T00:00:00",
            },
            {
                "session": "epicOld-coder",
                "pid": 999999,
                "status": "done",
                "exit_code": 0,
                "started": "2024-01-01T00:00:01",
            },
        )
        unmet = _parse(pi_manage({"action": "wait", "session_id": "epicPass-coder"}))
        assert unmet["deliverable"] == "unmet", unmet
        # a pre-existing row keeps its old shape
        old = _parse(pi_manage({"action": "wait", "session_id": "epicOld-coder"}))
        assert "deliverable" not in old, old
        agents = _parse(pi_manage({"action": "list"}))["agents"]
        mine = next(a for a in agents if a["session"] == "epicPass-coder")
        assert mine["deliverable"] == "unmet", agents
    finally:
        _teardown_registry()

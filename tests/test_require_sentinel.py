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

from helpers import _parse, _setup_registry, _teardown_registry, _write_registry

import __init__ as p
from __init__ import pi_launch, pi_manage

CLIENT = p.CLIENT_PATH


def _registry_rows(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _deliverable_probe(tmp_path, sentinel, anchor_text):
    """The client module plus an anchor file holding anchor_text."""
    import sys

    sys.path.insert(0, os.path.dirname(CLIENT))
    import pi_rpc_client as mod

    anchor = tmp_path / "lane-log.md"
    anchor.write_text(anchor_text)
    return mod, anchor


# -- launch side ---------------------------------------------------------------


def _marker_cwd(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("test repo marker\n")
    (proj / "contract.md").write_text("worker contract\n")
    (proj / "a.go").write_text("package main\n")
    return proj


def test_launch_carries_require_sentinel_to_the_client(tmp_path):
    proj = _marker_cwd(tmp_path)
    _setup_registry(str(tmp_path))
    try:
        result = _parse(
            pi_launch(
                {
                    "worker_type": "coder",
                    "skills": [],
                    "contract_path": str(proj / "contract.md"),
                    "cwd": str(proj),
                    "epic_id": "epicSent",
                    "require_edit": [str(proj / "a.go")],
                    "require_sentinel": "TEXTS1-DONE",
                }
            )
        )
        # then
        assert "error" not in result, result
        assert "--require-sentinel" in result["command"], result["command"]
        assert "TEXTS1-DONE" in result["command"], result["command"]
        assert result["require_sentinel"] == "TEXTS1-DONE", result
    finally:
        _teardown_registry()


def test_launch_rejects_require_sentinel_without_require_edit(tmp_path):
    proj = _marker_cwd(tmp_path)
    _setup_registry(str(tmp_path))
    try:
        result = _parse(
            pi_launch(
                {
                    "worker_type": "coder",
                    "skills": [],
                    "contract_path": str(proj / "contract.md"),
                    "cwd": str(proj),
                    "epic_id": "epicSentNoAim",
                    "require_sentinel": "DONE",
                }
            )
        )
        # then the gate has no file to grep: refused at preflight
        assert "error" in result, result
        assert "require_sentinel" in result["error"], result["error"]
        assert "require_edit" in result["error"], result["error"]
    finally:
        _teardown_registry()


def test_launch_omits_sentinel_flag_when_not_requested(tmp_path):
    proj = _marker_cwd(tmp_path)
    _setup_registry(str(tmp_path))
    try:
        result = _parse(
            pi_launch(
                {
                    "worker_type": "coder",
                    "skills": [],
                    "contract_path": str(proj / "contract.md"),
                    "cwd": str(proj),
                    "epic_id": "epicNoSent",
                    "require_edit": [str(proj / "a.go")],
                }
            )
        )
        # then
        assert "error" not in result, result
        assert "--require-sentinel" not in result["command"], result["command"]
        assert "require_sentinel" not in result, result
    finally:
        _teardown_registry()


def test_launch_schema_advertises_require_sentinel():
    # when / then the registered schema and the impl agree on the new param
    assert "require_sentinel" in p.LAUNCH_PARAMS
    assert p._doctor()["schema_missing"] == []


# -- exit-time check -----------------------------------------------------------


def test_sentinel_present_is_met(tmp_path):
    mod, anchor = _deliverable_probe(tmp_path, "TEXTS1-DONE", "## log\nwork\nTEXTS1-DONE\n")
    # when / then
    verdict = mod._deliverable_verdict(str(anchor), "TEXTS1-DONE")
    assert verdict == "met", verdict


def test_sentinel_absent_is_unmet(tmp_path):
    mod, anchor = _deliverable_probe(tmp_path, "TEXTS1-DONE", "## log\n(header only)\n")
    # when / then the research-only session's exact shape
    verdict = mod._deliverable_verdict(str(anchor), "TEXTS1-DONE")
    assert verdict == "unmet", verdict


def test_no_sentinel_requested_is_unchecked(tmp_path):
    mod, anchor = _deliverable_probe(tmp_path, None, "## log\nanything\n")
    # when / then
    verdict = mod._deliverable_verdict(str(anchor), None)
    assert verdict == "unchecked", verdict


def test_missing_anchor_is_unmet_not_a_crash(tmp_path):
    mod = _deliverable_probe(tmp_path, "DONE", "x\n")[0]
    # when / then a vanished deliverable file cannot count as met
    verdict = mod._deliverable_verdict(str(tmp_path / "gone.md"), "DONE")
    assert verdict == "unmet", verdict


def test_sentinel_is_matched_literally(tmp_path):
    mod, anchor = _deliverable_probe(tmp_path, "A.B", "A B\nAxB\n")
    # when / then a sentinel with regex metacharacters is not a pattern
    assert mod._deliverable_verdict(str(anchor), "A.B") == "unmet"


# -- registry column -----------------------------------------------------------


def _run_sentinel_client(tmp_path, session, anchor_text, fake_pi_body):
    """Run the real client against a fake pi: (rc, terminal registry row)."""
    import subprocess
    import sys

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


def test_client_records_unmet_on_the_terminal_row(tmp_path):
    """A dispatch that worked (one tool call, anchor touched) but never wrote the
    sentinel: deliverable unmet, and the exit code stays 0."""
    session = "task-sent-default"
    # the run works (touches the anchor) but never appends the sentinel line;
    # `read` waits for the prompt frame, so the edit lands AFTER the client's
    # before-snapshot and the run still counts as satisfied
    body = f'read __prompt; printf "%s\\\\n" "work done" >> {tmp_path / "lane-log.md"}'

    rc, terminal = _run_sentinel_client(tmp_path, session, "## lane log (append-only)\n", body)

    # then
    assert rc == 0, rc
    assert terminal["deliverable"] == "unmet", terminal


def test_client_records_met_when_the_sentinel_landed(tmp_path):
    session = "task-sent-ok-default"
    # the run appends the sentinel line to the deliverable file, after the
    # client's before-snapshot (see the unmet case)
    body = f'read __prompt; printf "%s\\\\n" "TEXTS1-DONE" >> {tmp_path / "lane-log.md"}'

    rc, terminal = _run_sentinel_client(tmp_path, session, "## lane log (append-only)\n", body)

    # then
    assert rc == 0, rc
    assert terminal["deliverable"] == "met", terminal


# -- wait / list pass-through --------------------------------------------------


def test_wait_passes_deliverable_through(tmp_path):
    _setup_registry(str(tmp_path))
    try:
        _write_registry(
            {
                "session": "epicUnmet-coder",
                "pid": 999999,
                "status": "incomplete",
                "exit_code": 3,
                "deliverable": "unmet",
                "started": "2024-01-01T00:00:00",
            },
            {
                "session": "epicChecked-coder",
                "pid": 999999,
                "status": "done",
                "exit_code": 0,
                "deliverable": "unchecked",
                "started": "2024-01-01T00:00:01",
            },
        )
        # when
        unmet = _parse(pi_manage({"action": "wait", "session_id": "epicUnmet-coder"}))
        unchecked = _parse(pi_manage({"action": "wait", "session_id": "epicChecked-coder"}))

        # then the parent's first check sees the deliverable verdict
        assert unmet["deliverable"] == "unmet", unmet
        assert unchecked["deliverable"] == "unchecked", unchecked
    finally:
        _teardown_registry()


def test_wait_omits_deliverable_for_a_row_that_never_recorded_one(tmp_path):
    _setup_registry(str(tmp_path))
    try:
        _write_registry(
            {
                "session": "epicOld-coder",
                "pid": 999999,
                "status": "done",
                "exit_code": 0,
                "started": "2024-01-01T00:00:00",
            }
        )
        # when
        result = _parse(pi_manage({"action": "wait", "session_id": "epicOld-coder"}))

        # then a pre-existing row keeps its old shape
        assert "deliverable" not in result, result
    finally:
        _teardown_registry()


def test_list_passes_deliverable_through(tmp_path):
    _setup_registry(str(tmp_path))
    try:
        _write_registry(
            {
                "session": "epicListUnmet-coder",
                "pid": 999999,
                "status": "incomplete",
                "exit_code": 3,
                "deliverable": "unmet",
                "started": "2024-01-01T00:00:00",
            }
        )
        # when
        agents = _parse(pi_manage({"action": "list"}))["agents"]

        # then
        assert len(agents) == 1, agents
        assert agents[0]["deliverable"] == "unmet", agents
    finally:
        _teardown_registry()


def test_readme_documents_the_sentinel_pattern():
    readme_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"
    )
    with open(readme_path) as f:
        readme = f.read()

    # then the pattern is documented with a real example
    assert "require_sentinel" in readme
    assert "deliverable" in readme
    assert "unmet" in readme

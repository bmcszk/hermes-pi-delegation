"""Fail-fast detection of never-dispatched / unregistered workers.

A manual launch writes a pending-dispatch intent row; wait classifies an
aged pending row with pgrep instead of burning its whole clamp on a
command that was never run.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta

import pytest
from helpers import _parse, _setup_registry, _teardown_registry, _write_registry

import __init__ as p
from __init__ import pi_manage

SESSION = "epicPending-coder"


class VirtualClock:
    """Sleep advances monotonic time; slept counts every slept second."""

    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += float(seconds)
        self.slept += float(seconds)


def _wait_with_clock(payload, clock):
    original = p.time
    p.time = clock
    try:
        return _parse(pi_manage(payload))
    finally:
        p.time = original


def _aged_pending_row(age_s=60, session=SESSION):
    return {
        "session": session,
        "status": "pending-dispatch",
        "pid": None,
        "mode": "manual",
        "cwd": tempfile.mkdtemp(prefix="pd-cwd-"),
        "started": (datetime.now() - timedelta(seconds=age_s)).isoformat(),
    }


def _client_cmdline(session_id):
    return f"python3 /x/pi_rpc_client.py --session-id {session_id} --cwd /y"


# --- pgrep pattern: boundary-anchored, session-id escaped -------------------


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        pytest.param(_client_cmdline("task-foo-coder"), True, id="exact-match"),
        pytest.param(
            _client_cmdline("task-foo-coder") + " --timeout 900", True, id="with-suffix-args"
        ),
        pytest.param(
            _client_cmdline("task-foo-coder2"), False, id="longer-id-no-match"
        ),
        pytest.param(
            "python3 /x/pi_rpc_client.py --session-id task-foo-coder-extra --cwd /y",
            False,
            id="dashed-suffix-no-match",
        ),
        pytest.param("sleep 60", False, id="unrelated-process"),
    ],
)
def test_client_pid_pattern_is_boundary_anchored(cmdline, expected):
    pattern = p._client_pid_pattern("task-foo-coder")
    assert bool(re_search(pattern, cmdline)) is expected


def re_search(pattern, text):
    import re

    return re.search(pattern, text)


# --- intent rows at launch --------------------------------------------------


def test_launch_manual_writes_pending_row():
    result = _parse(
        p._pi_launch_impl(
            worker_type="coder",
            skills=[],
            contract_path=helpers_contract(),
            cwd=helpers_cwd(),
            epic_id="epicPending",
        )
    )
    assert "error" not in result, result
    rows = _registry_rows()
    pending = [r for r in rows if r.get("session") == "epicPending-coder"]
    assert len(pending) == 1, rows
    row = pending[0]
    assert row["status"] == "pending-dispatch", row
    assert row["pid"] is None, row
    assert row["mode"] == "manual", row
    assert row["cwd"] == helpers_cwd(), row
    assert row["started"], row


def test_launch_relaunch_replaces_pending_row():
    for _ in range(2):
        result = _parse(
            p._pi_launch_impl(
                worker_type="coder",
                skills=[],
                contract_path=helpers_contract(),
                cwd=helpers_cwd(),
                epic_id="epicPending",
            )
        )
        assert "error" not in result, result
        time.sleep(0.01)
    rows = _registry_rows()
    pending = [r for r in rows if r.get("session") == "epicPending-coder"]
    assert len(pending) == 1, rows
    assert pending[0]["status"] == "pending-dispatch", rows


def test_launch_pending_write_failure_degrades_to_warning():
    original = p._record_pending_dispatch
    p._record_pending_dispatch = lambda *a, **kw: False
    try:
        result = _parse(
            p._pi_launch_impl(
                worker_type="coder",
                skills=[],
                contract_path=helpers_contract(),
                cwd=helpers_cwd(),
                epic_id="epicPending",
            )
        )
    finally:
        p._record_pending_dispatch = original
    assert "error" not in result, result
    assert any("pending" in w for w in result.get("warnings", [])), result


def test_launch_auto_updates_pending_row_with_pid(tmp_path, monkeypatch):

    # reuse the fake-pi harness pieces from test_launch_modes via local setup
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
    contract = proj / "contract.md"
    contract.write_text("worker contract\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path))
    p.REGISTRY_PATH = str(tmp_path / "registry.jsonl")
    result = _parse(
        p._pi_launch_impl(
            worker_type="coder",
            skills=[],
            contract_path=str(contract),
            cwd=str(proj),
            epic_id="epicAutoPending",
            auto=True,
        )
    )
    try:
        assert result.get("dispatched") is True, result
        row = _wait_for_pending_update(result["session_id"])
        assert row is not None, result
        assert row["pid"] == result["pid"], (row, result)
        assert row["log_path"] == result["log"], (row, result)
    finally:
        if result.get("pid"):
            subprocess.run(["kill", str(result["pid"])], capture_output=True)


def _wait_for_pending_update(session_id, deadline=10):
    end = time.time() + deadline
    while time.time() < end:
        rows = _registry_rows()
        for row in rows:
            if row.get("session") == session_id and row.get("pid"):
                return row
        time.sleep(0.05)
    return None


# --- wait state machine -----------------------------------------------------


def test_wait_pending_within_grace_keeps_polling():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    clock = VirtualClock()
    try:
        _write_registry(_aged_pending_row(age_s=1))
        result = _wait_with_clock(
            {"action": "wait", "session_id": SESSION, "timeout_s": 3, "poll_s": 1}, clock
        )
        assert result["finished"] is False, result
        assert result["status"] == "pending-dispatch", result
        assert clock.slept > 0, clock.slept
    finally:
        _teardown_registry()


def test_wait_pending_past_grace_no_process_never_dispatched():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    clock = VirtualClock()
    original = p._client_pids
    p._client_pids = lambda session_id: []
    try:
        _write_registry(_aged_pending_row(age_s=60))
        result = _wait_with_clock(
            {"action": "wait", "session_id": SESSION, "timeout_s": 300, "poll_s": 1}, clock
        )
        assert result["finished"] is False, result
        assert result["status"] == "never-dispatched", result
        assert result["command_age_s"] >= 25, result
        assert "no process found" in result["hint"], result
        assert "run the returned command in background" in result["hint"], result
        assert "resumed_by" not in result, result
        assert clock.slept == 0, clock.slept
    finally:
        p._client_pids = original
        _teardown_registry()


def test_wait_pending_past_grace_pgrep_hit_unregistered_client():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    clock = VirtualClock()
    original = p._client_pids
    p._client_pids = lambda session_id: [4242]
    try:
        _write_registry(_aged_pending_row(age_s=60))
        result = _wait_with_clock(
            {"action": "wait", "session_id": SESSION, "timeout_s": 300, "poll_s": 1}, clock
        )
        assert result["finished"] is False, result
        assert result["status"] == "unregistered-client", result
        assert result["pid"] == 4242, result
        assert "never registered" in result["hint"], result
        assert clock.slept == 0, clock.slept
    finally:
        p._client_pids = original
        _teardown_registry()


def test_wait_no_rows_pgrep_hit_unregistered_client():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    clock = VirtualClock()
    original = p._client_pids
    p._client_pids = lambda session_id: [4242]
    try:
        result = _wait_with_clock(
            {"action": "wait", "session_id": SESSION, "timeout_s": 300, "poll_s": 1}, clock
        )
        assert result["status"] == "unregistered-client", result
        assert result["pid"] == 4242, result
        assert clock.slept == 0, clock.slept
    finally:
        p._client_pids = original
        _teardown_registry()


def test_wait_no_rows_no_process_unknown_immediately():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    clock = VirtualClock()
    original = p._client_pids
    p._client_pids = lambda session_id: []
    try:
        result = _wait_with_clock(
            {"action": "wait", "session_id": "epicGhost-coder", "timeout_s": 300, "poll_s": 1},
            clock,
        )
        assert result["finished"] is False, result
        assert result["status"] == "unknown", result
        assert "no launch record and no process" in result["hint"], result
        assert clock.slept == 0, clock.slept
    finally:
        p._client_pids = original
        _teardown_registry()


def test_newest_entry_prefers_pid_bearing_row_over_pending():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        client_row = {
            "session": SESSION,
            "pid": 999999,
            "status": "running",
            "started": "2024-01-01T00:00:00",
        }
        pending_row = _aged_pending_row(age_s=1)
        _write_registry(client_row, pending_row)
        entry = p._newest_registry_entry(SESSION)
        assert entry is client_row or entry.get("pid") == 999999, entry
    finally:
        _teardown_registry()


# --- pending never derives finished -----------------------------------------


def test_verify_and_salvage_treat_pending_as_unfinished():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        _write_registry(_aged_pending_row(age_s=1))
        verdict = _parse(pi_manage({"action": "verify", "session_id": SESSION}))
        assert verdict["finished"] is False, verdict
        assert verdict["status"] == "pending-dispatch", verdict
        report = _parse(pi_manage({"action": "salvage_report", "session_id": SESSION}))
        assert report["finished"] is False, report
        assert report["status"] == "pending-dispatch", report
    finally:
        _teardown_registry()


def test_list_shows_pending_age_and_no_tree():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    repo = tempfile.mkdtemp(prefix="pd-repo-")
    try:
        row = _aged_pending_row(age_s=7)
        row["cwd"] = repo
        _write_registry(row)
        result = _parse(pi_manage({"action": "list"}))
        entry = next(a for a in result["agents"] if a.get("session") == SESSION)
        assert entry["pending_age_s"] >= 5, entry
        assert "tree" not in entry, entry
    finally:
        _teardown_registry()


def test_launch_concurrency_warnings_ignore_pending_rows():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        _write_registry(_aged_pending_row(age_s=1))
        warnings = p._launch_concurrency_warnings(SESSION, helpers_cwd(), "default")
        assert warnings == [], warnings
    finally:
        _teardown_registry()


# --- kill semantics ---------------------------------------------------------


def test_kill_pending_no_process_aborts_row():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    original = p._client_pids
    p._client_pids = lambda session_id: []
    try:
        _write_registry(_aged_pending_row(age_s=60))
        result = _parse(pi_manage({"action": "kill", "session_id": SESSION}))
        assert "cancelled" in result.get("status", ""), result
        rows = _registry_rows()
        assert rows[0]["status"] == "aborted", rows
        assert rows[0].get("finished"), rows
    finally:
        p._client_pids = original
        _teardown_registry()


def test_kill_pending_with_live_client_kills_and_marks_killed():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    original = p._client_pids
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "pi_rpc_client.py",
            "--session-id",
            SESSION,
        ],
        start_new_session=True,
    )
    p._client_pids = lambda session_id: [child.pid]
    try:
        _write_registry(_aged_pending_row(age_s=60))
        result = _parse(pi_manage({"action": "kill", "session_id": SESSION}))
        assert "SIGTERM" in result.get("status", "") or "killed" in json.dumps(result), result
        child.wait(timeout=10)
        rows = _registry_rows()
        assert rows[0]["status"] == "killed", rows
    finally:
        p._client_pids = original
        _teardown_registry()


def test_kill_no_row_no_process_names_run_the_command():
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    original = p._client_pids
    p._client_pids = lambda session_id: []
    try:
        result = _parse(pi_manage({"action": "kill", "session_id": "epicGhost-coder"}))
        assert "error" in result, result
        assert "command" in result["error"], result
    finally:
        p._client_pids = original
        _teardown_registry()


# --- helpers ----------------------------------------------------------------


def helpers_contract():
    from helpers import CONTRACT_MD

    return CONTRACT_MD


def helpers_cwd():
    import helpers

    return helpers.TMP_CWD


def _registry_rows():
    with open(p.REGISTRY_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]

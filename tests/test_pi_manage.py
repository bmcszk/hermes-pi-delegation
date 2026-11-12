"""pi_manage behaviors: list, wait, kill, and the framework handler contract."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest
from helpers import (
    CODING_RULES,
    CONTRACT_MD,
    TMP_CWD,
    _parse,
    _setup_registry,
    _spawn_real_client,
    _teardown_registry,
    _write_registry,
)

import __init__ as p
from __init__ import pi_launch, pi_manage


def _registry(tmpdir, *entries):
    tmpdir = str(tmpdir)
    _setup_registry(tmpdir)
    if entries:
        _write_registry(*entries)
    try:
        return p.REGISTRY_PATH
    finally:
        pass


def _cleanup_registry(tmpdir):
    _teardown_registry()
    shutil.rmtree(str(tmpdir), ignore_errors=True)


def _write_raw(tmpdir, *entries):
    with open(p.REGISTRY_PATH, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def test_pi_manage_list_empty_and_sorted():
    tmpdir = tempfile.mkdtemp()
    _registry(tmpdir)
    try:
        assert _parse(pi_manage({"action": "list"}))["agents"] == []
        _write_raw(
            tmpdir,
            {"session": "a", "pid": 1, "started": "2024-01-01T00:00:00"},
            {"session": "b", "pid": 2, "started": "2024-01-02T00:00:00"},
        )
        agents = _parse(pi_manage({"action": "list"}))["agents"]
        assert [a["session"] for a in agents] == ["b", "a"]

        # only_running filters to live pids with a running status
        _write_raw(
            tmpdir,
            {
                "session": "live-one",
                "pid": os.getpid(),
                "status": "running",
                "started": "2024-01-01T00:00:00",
            },
            {
                "session": "dead-one",
                "pid": 999999,
                "status": "running",
                "started": "2024-01-02T00:00:00",
            },
            {
                "session": "killed-one",
                "pid": os.getpid(),
                "status": "killed",
                "started": "2024-01-03T00:00:00",
            },
        )
        only = _parse(pi_manage({"action": "list", "only_running": True}))["agents"]
        assert [a["session"] for a in only] == ["live-one"], only
        everything = _parse(pi_manage({"action": "list"}))["agents"]
        assert len(everything) == 3, everything
    finally:
        _cleanup_registry(tmpdir)


def test_concurrent_guard_blocks_running_session_and_ignores_dead_pid():
    tmpdir = tempfile.mkdtemp()
    _registry(tmpdir)
    try:
        # a live pid with status running blocks the relaunch
        _write_raw(
            tmpdir,
            {
                "session": "epicZ-coder",
                "pid": os.getpid(),
                "status": "running",
                "started": "2024-01-01T00:00:00",
            },
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
                }
            )
        )
        assert "error" in result and "already running" in result["error"], result

        # a dead pid does not
        _write_raw(
            tmpdir,
            {
                "session": "epicY-coder",
                "pid": 999999,
                "status": "running",
                "started": "2024-01-01T00:00:00",
            },
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
                }
            )
        )
        assert "error" not in result, result
    finally:
        _cleanup_registry(tmpdir)


@pytest.mark.parametrize(
    ("decoy_cmd", "expect_error"),
    [
        pytest.param(["sleep", "30"], "pi_rpc_client", id="non-client-pid-refused"),
        pytest.param(
            [sys.executable, "-c", "import time  # pi_rpc_client\nimport time; time.sleep(30)"],
            None,
            id="client-shaped-pid-signalled",
        ),
    ],
)
def test_pi_manage_kill_verifies_the_pid_before_signalling(decoy_cmd, expect_error):
    """kill refuses a pid whose cmdline is not pi_rpc_client; a real client-shaped
    decoy gets SIGTERM and a registry row stamped killed/143 (the client cannot
    write its own terminal line). A dead pid reports dead and marks the entry."""
    tmpdir = tempfile.mkdtemp()
    _registry(tmpdir)
    decoy = subprocess.Popen(decoy_cmd, start_new_session=True)
    try:
        _write_raw(
            tmpdir,
            {
                "session": "epicKill-coder",
                "pid": decoy.pid,
                "status": "running",
                "started": "2024-01-01T00:00:00",
            },
        )
        result = _parse(pi_manage({"action": "kill", "session_id": "epicKill-coder"}))
        if expect_error:
            assert "error" in result and expect_error in result["error"], result
            assert decoy.poll() is None, "refused pid was signalled anyway"
        else:
            assert "SIGTERM sent" in result["status"], result
            assert decoy.wait(timeout=10) != 0
            with open(p.REGISTRY_PATH) as f:
                entry = json.loads(f.readline())
            assert entry["status"] == "killed" and entry["exit_code"] == 143, entry

        # a dead pid reports dead and marks the entry
        _write_raw(
            tmpdir,
            {
                "session": "epicM-coder",
                "pid": 999999,
                "status": "running",
                "started": "2024-01-01T00:00:00",
            },
        )
        result = _parse(pi_manage({"action": "kill", "session_id": "epicM-coder"}))
        assert "dead" in result["status"], result
        with open(p.REGISTRY_PATH) as f:
            assert json.loads(f.readline())["status"] == "dead"
    finally:
        if decoy.poll() is None:
            decoy.terminate()
            decoy.wait(timeout=10)
        _cleanup_registry(tmpdir)


def test_handlers_match_framework_contract():
    """Framework calls handler(args: dict) with ONE positional dict; both
    wrappers return JSON strings."""
    result = pi_manage({"action": "list"})
    assert isinstance(result, str), result
    assert "agents" in json.loads(result), result
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
    # unknown keys must not crash the wrappers
    assert "agents" in json.loads(pi_manage({"action": "list", "bogus": 1}))
    tolerant = pi_launch(
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
    assert "error" not in json.loads(tolerant), tolerant


@pytest.mark.parametrize(
    ("row", "expect"),
    [
        pytest.param(
            {
                "session": "registered-other-coder",
                "pid": 999999,
                "status": "done",
                "started": "2024-01-01T00:00:00",
            },
            {"finished": False, "status": "unknown"},
            id="unknown-when-session-absent",
        ),
        pytest.param(
            {
                "session": "epicDone-coder",
                "pid": 999999,
                "status": "done",
                "started": "2024-01-01T00:00:00",
            },
            {"finished": True, "status": "done"},
            id="terminal-row-is-finished",
        ),
    ],
)
def test_pi_manage_wait_shapes(row, expect):
    tmpdir = tempfile.mkdtemp()
    _registry(tmpdir)
    original_update = p._registry_update
    writes = []
    p._registry_update = lambda *a, **kw: writes.append((a, kw))
    try:
        _write_registry(row)
        # unknown is reserved for a session with no registry rows at all
        ghost = _parse(pi_manage({"action": "wait", "session_id": "epicGhost-coder"}))
        if expect["status"] == "unknown":
            assert ghost["finished"] is False and ghost["status"] == "unknown", ghost
            return
        result = _parse(pi_manage({"action": "wait", "session_id": row["session"]}))
        assert result["finished"] is expect["finished"], result
        assert result["status"] == expect["status"], result
        if expect["status"] == "done":
            # wait writes nothing for a settled terminal row
            assert writes == [], writes
    finally:
        p._registry_update = original_update
        _cleanup_registry(tmpdir)


def test_pi_manage_wait_surfaces_exit_codes():
    """wait reports the client's true exit code; absent code stays absent."""
    tmpdir = tempfile.mkdtemp()
    _registry(tmpdir)
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
        assert killed["finished"] is True and killed["exit_code"] == 143, killed
        done = _parse(pi_manage({"action": "wait", "session_id": "epicX0-coder"}))
        assert done["finished"] is True and done["exit_code"] == 0, done
        without = _parse(pi_manage({"action": "wait", "session_id": "epicXnone-coder"}))
        assert without["finished"] is True and "exit_code" not in without, without
        # list passes terminal exit_code through too
        agents = _parse(pi_manage({"action": "list"}))["agents"]
        mine = next(a for a in agents if a["session"] == "epicX143-coder")
        assert mine["exit_code"] == 143, agents
    finally:
        _cleanup_registry(tmpdir)


def test_pi_manage_wait_running_times_out():
    """A running entry with a live pid never finishes on a single probe."""
    tmpdir = tempfile.mkdtemp()
    _registry(tmpdir)
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
        assert result["finished"] is False and result["status"] == "running", result
        assert result["pid"] == child.pid, result
        # a registered live worker reports its heartbeat age, never unknown
        heartbeat = os.path.join(
            os.path.dirname(p.REGISTRY_PATH), "pi-heartbeat-epicRun-coder.json"
        )
        with open(heartbeat, "w") as f:
            json.dump({"ts": time.time(), "frames": 3, "tools": 1}, f)
        result = _parse(pi_manage({"action": "wait", "session_id": "epicRun-coder"}))
        assert isinstance(result.get("last_activity_s"), int), result
        assert "unknown" not in json.dumps(result), result
    finally:
        child.terminate()
        child.wait(timeout=10)
        _cleanup_registry(tmpdir)


def test_pi_manage_wait_polls_until_dead():
    """A vanished pid settles a run even when status stayed running."""
    tmpdir = tempfile.mkdtemp()
    _registry(tmpdir)
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
                {"action": "wait", "session_id": "epicPoll-coder", "timeout_s": 5, "poll_s": 1}
            )
        )
        assert result["finished"] is True and result["status"] == "running", result
        assert p._pid_alive(child.pid) is False, result
    finally:
        reaper.join(timeout=10)
        _cleanup_registry(tmpdir)


def test_pi_manage_wait_live_client_running_then_sigterm_143():
    """Real client subprocess: wait says running while its pid lives; after
    SIGTERM it finishes killed with exit_code 143. Never unknown. The fake pi
    emits one frame then sleeps, so last_activity_s is not guaranteed here."""
    tmpdir = tempfile.mkdtemp()
    _registry(tmpdir)
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
        assert running["finished"] is False and running["status"] == "running", running
        assert running["pid"] == child.pid, running

        child.terminate()
        assert child.wait(timeout=10) == 143
        finished = _parse(pi_manage({"action": "wait", "session_id": "epicReal-coder"}))
        assert finished["finished"] is True, finished
        assert finished["status"] == "killed" and finished["exit_code"] == 143, finished
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        _cleanup_registry(tmpdir)

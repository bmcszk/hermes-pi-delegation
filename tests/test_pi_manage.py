import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

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

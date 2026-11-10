import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from helpers import (
    _parse,
    _setup_registry,
    _teardown_registry,
    _wait_for_registry,
    _write_registry,
)

import __init__ as p


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

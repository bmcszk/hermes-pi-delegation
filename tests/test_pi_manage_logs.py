"""pi_manage steer (verified-pid control files) and logs (transcript resolution
via the registry row's log_path, falling back to the slug glob)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest
from helpers import (
    _parse,
    _setup_registry,
    _teardown_registry,
    _wait_for_registry,
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


def _registry_with_row(tmpdir, session, pid, status="running"):
    _setup_registry(tmpdir)
    with open(p.REGISTRY_PATH, "w") as f:
        f.write(
            json.dumps(
                {
                    "session": session,
                    "pid": pid,
                    "status": status,
                    "started": "2024-01-01T00:00:00",
                }
            )
            + "\n"
        )
    return p.REGISTRY_PATH


def test_pi_manage_steer_writes_control_file():
    """steer targets a verified live pi_rpc_client process and writes the file
    with the message and the queue disposition the caller chose."""
    tmpdir = tempfile.mkdtemp()
    decoy = FakeClientProc()
    try:
        _registry_with_row(tmpdir, "epicS-coder", decoy.proc.pid)
        result = _parse(
            p._pi_manage_impl(action="steer", session_id="epicS-coder", message="commit now")
        )
        steer_file = os.path.join(os.path.dirname(p.REGISTRY_PATH), "pi-steer", "epicS-coder.json")
        with open(steer_file) as f:
            payload = json.load(f)
        assert payload["message"] == "commit now" and payload["queue"] is False
        assert "steer queued" in result["status"], result

        os.unlink(steer_file)
        result = _parse(
            p._pi_manage_impl(
                action="steer", session_id="epicS-coder", message="commit now", queue=True
            )
        )
        with open(steer_file) as f:
            payload = json.load(f)
        assert payload["queue"] is True
        assert "follow_up queued" in result["status"], result

        # an empty message is refused
        result = _parse(p._pi_manage_impl(action="steer", session_id="epicS-coder"))
        assert "error" in result and "message required" in result["error"], result
    finally:
        decoy.stop()
        _teardown_registry()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.parametrize(
    ("session", "pid", "message", "expect_error"),
    [
        pytest.param("epicGhost-coder", None, "hi", "not found in registry", id="missing-session"),
        pytest.param("epicD-coder", 999999, "hi", "not running", id="dead-pid"),
        pytest.param("epicN-coder", "SLEEP", "hi", "pi_rpc_client", id="non-client-pid"),
    ],
)
def test_pi_manage_steer_refusals(session, pid, message, expect_error):
    """steer refuses sessions/pids it cannot verify and empty messages."""
    tmpdir = tempfile.mkdtemp()
    decoy = None
    try:
        if pid == "SLEEP":
            decoy = subprocess.Popen(["sleep", "30"])
            pid = decoy.pid
        if pid is None:
            _setup_registry(tmpdir)  # no row: the session is absent entirely
        else:
            _registry_with_row(tmpdir, session, pid)
        result = _parse(p._pi_manage_impl(action="steer", session_id=session, message=message))
        assert "error" in result, result
        assert expect_error in result["error"], result
    finally:
        if decoy is not None:
            decoy.terminate()
            decoy.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_pi_manage_list_reports_heartbeat_age():
    """Running agents get last_activity_s from the client's heartbeat stamp;
    finished agents never carry it."""
    tmpdir = tempfile.mkdtemp()
    decoy = subprocess.Popen(["sleep", "30"])
    try:
        _registry_with_row(tmpdir, "epicH-coder", decoy.pid)
        heartbeat = os.path.join(os.path.dirname(p.REGISTRY_PATH), "pi-heartbeat-epicH-coder.json")
        with open(heartbeat, "w") as f:
            json.dump({"ts": time.time() - 120, "frames": 5, "tools": 2}, f)

        agents = _parse(p._pi_manage_impl(action="list"))["agents"]
        entry = next(a for a in agents if a["session"] == "epicH-coder")
        assert entry["last_activity_s"] >= 119, entry

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
        shutil.rmtree(tmpdir, ignore_errors=True)


def _write_transcript(path, text=None, tool_call=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if tool_call:
        content = {
            "type": "message",
            "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash"}]},
        }
    else:
        content = {
            "type": "message",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    with open(path, "w") as f:
        f.write(json.dumps(content) + "\n")
    return path


@pytest.mark.parametrize(
    ("log_path", "expect_via"),
    [
        pytest.param(True, "registry-log-path", id="log-path-wins"),
        pytest.param(None, "slug-glob-fallback", id="glob-fallback"),
        pytest.param("missing", "no-transcript-error", id="no-path-no-glob"),
    ],
)
def test_pi_manage_logs_resolves_the_transcript(log_path, expect_via):
    """logs prefers the registry row's log_path; a pre-fix row without one
    resolves through the slug glob; neither gives the named error."""
    tmpdir = tempfile.mkdtemp()
    real_home = os.environ.get("HOME")
    os.environ["HOME"] = tmpdir
    _setup_registry(tmpdir)
    try:
        transcript = None
        row = {
            "session": "epicLogs-coder",
            "pid": 999999,
            "status": "killed",
            "started": "2024-01-01T00:00:00",
        }
        if expect_via == "registry-log-path":
            transcript = _write_transcript(
                os.path.join(tmpdir, "pi", "sessions", "other-slug", "2026-01-01T_a.jsonl"), "hi"
            )
            row["log_path"] = transcript
        elif expect_via == "slug-glob-fallback":
            transcript = _write_transcript(
                os.path.join(
                    tmpdir,
                    ".pi",
                    "agent",
                    "sessions",
                    "slugged",
                    "2026-01-01T_epicLogs-coder.jsonl",
                ),
                "old",
            )
        with open(p.REGISTRY_PATH, "w") as f:
            f.write(json.dumps(row) + "\n")
        result = _parse(p._pi_manage_impl("logs", session_id="epicLogs-coder", lines=10))
        if transcript:
            assert result.get("path") == transcript, result
        else:
            assert "error" in result and "no session log" in result["error"], result
    finally:
        if real_home is not None:
            os.environ["HOME"] = real_home
        _teardown_registry()
        shutil.rmtree(tmpdir, ignore_errors=True)


REAL_PI_SESSIONS = """#!/bin/sh
mkdir -p "$PI_FAKE_SESSIONS/probe-dir"
printf '%s\n' '{"role":"assistant","content":"swept 40 tiles"}' \
  > "$PI_FAKE_SESSIONS/probe-dir/2026-01-01T00-00-00-000Z_epicReal2-coder.jsonl"
echo '{"type":"ready"}'
exec sleep 30
"""


def test_client_records_transcript_path_in_registry():
    """After the first frames, the client's registry row carries log_path
    pointing at the REAL transcript file the (fake) pi wrote."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    p.REGISTRY_PATH = os.path.join(tmpdir, ".hermes", "logs", "pi-agents.jsonl")
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
            "epicReal2-coder",
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
    transcript = os.path.join(
        sessions_dir, "probe-dir", "2026-01-01T00-00-00-000Z_epicReal2-coder.jsonl"
    )
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
        assert os.path.exists(transcript), "fake pi never wrote the transcript"

        def _row_log_path():
            if not os.path.exists(p.REGISTRY_PATH):
                return None
            with open(p.REGISTRY_PATH) as f:
                return json.loads(f.read()).get("log_path")

        log_path = _wait_for_registry(_row_log_path)
        assert log_path == transcript, log_path
    finally:
        if child.poll() is None:
            child.terminate()

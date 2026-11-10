"""Tests for pi_rpc_client. No network, no model calls, no real pi process."""

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from pi_rpc_client import PiRpcClient, _find_pi_binary

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

FAKE_PI_PATH = "/usr/local/bin/pi"
REAL_WHICH = shutil.which  # captured before any test patches it


@pytest.fixture(scope="module", autouse=True)
def pi_binary_on_path():
    """Tests need no real pi; start() resolves one, so stub only the lookup."""

    def fake_which(name, *args, **kwargs):
        if name == "pi":
            return FAKE_PI_PATH
        return REAL_WHICH(name, *args, **kwargs)

    with patch("pi_rpc_client.shutil.which", side_effect=fake_which):
        yield


U2028 = "\u2028"
READY_LINE = b'{"type":"extension_ui_request","id":"x","method":"setStatus"}\n'


class TestPluginFreshness:
    """--plugin-fresh short-circuits the mtime staleness heuristic."""

    def test_fresh_flag_skips_heuristic(self, monkeypatch):
        from pi_rpc_client import _plugin_staleness_notice

        def boom():  # the certified-fresh path must never recompute
            raise AssertionError("heuristic ran despite --plugin-fresh")

        monkeypatch.setattr("pi_rpc_client._plugin_staleness_warning", boom)
        assert _plugin_staleness_notice(True) is None

    def test_missing_flag_runs_heuristic(self, monkeypatch):
        from pi_rpc_client import _plugin_staleness_notice

        monkeypatch.setattr("pi_rpc_client._plugin_staleness_warning", lambda: "S")
        assert _plugin_staleness_notice(False) == "S"


class FakeStdin:
    """Records written frames; keeps 'open' until close()."""

    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeStdout:
    """Raw byte iterator, one entry per scripted 'line' (split on b'\\n' only)."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self._closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        self._closed = True


class FakePiRpcProc:
    """Fake subprocess: scripted stdout frames, recorded stdin, closeable."""

    instances: list["FakePiRpcProc"] = []

    def __init__(self, lines: list[bytes]) -> None:
        self.pid = 424242  # killpg targets a nonexistent group → OSError path
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(lines)
        self.returncode: int | None = None
        self.term_called = False
        self.kill_called = False
        self.wait_calls: list[float | None] = []
        FakePiRpcProc.instances.append(self)

    def terminate(self) -> None:
        self.term_called = True
        self.returncode = 0

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -signal.SIGKILL

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.returncode is None:
            raise AssertionError("wait() before terminate/kill")
        return self.returncode


def make_client(lines: list[bytes]) -> tuple[PiRpcClient, FakePiRpcProc]:
    captured: dict = {}

    def factory(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        assert kwargs.get("cwd") == "/tmp/fakecwd"
        return FakePiRpcProc(lines)

    client = PiRpcClient(
        model="m",
        cwd="/tmp/fakecwd",
        proc_factory=factory,  # type: ignore[arg-type]
    )
    client.start()
    assert captured["argv"][1:5] == ["--mode", "rpc", "--model", "m"]
    assert isinstance(captured["argv"][0], str) and captured["argv"][0].endswith("pi")
    fake = client.proc
    assert isinstance(fake, FakePiRpcProc)
    return client, fake


def scripted_lines(assistant_text: str = "hello world") -> list[bytes]:
    ack = {
        "type": "response",
        "command": "prompt",
        "success": True,
        "data": {"disposition": "started"},
    }
    msg = {
        "type": "message_update",
        "role": "assistant",
        "content": [{"type": "text", "text": assistant_text}],
    }
    end = {
        "type": "agent_end",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "q"}]},
            {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
        ],
    }
    return [
        READY_LINE,
        (json.dumps(ack) + "\n").encode(),
        (json.dumps(msg) + "\n").encode(),
        (json.dumps(end) + "\n").encode(),
    ]


class TestPrompt:
    def test_extracts_assistant_text(self):
        client, fake = make_client(scripted_lines("hello world"))
        assert client.prompt("hi") == "hello world"
        sent = json.loads(fake.stdin.written.split(b"\n")[0])
        assert sent == {"type": "prompt", "message": "hi"}

    def test_agent_end_only_once(self):
        client, _ = make_client(scripted_lines("abc"))
        assert client.prompt("hi") == "abc"

    def test_disposition_handled_returns_immediately(self):
        # ack disposition "handled" = no run started; agent_end never comes.
        # Client must return at once, not burn the timeout.
        ack = {
            "type": "response",
            "command": "prompt",
            "success": True,
            "data": {"disposition": "handled"},
        }
        client, _ = make_client([READY_LINE, (json.dumps(ack) + "\n").encode()])
        assert client.prompt("hi", timeout_s=2) == ""

    def test_prompt_rejected_raises(self):
        ack = {
            "type": "response",
            "command": "prompt",
            "success": False,
            "data": {"error": "bad frame"},
        }
        client, _ = make_client([READY_LINE, (json.dumps(ack) + "\n").encode()])
        with pytest.raises(RuntimeError, match="prompt rejected"):
            client.prompt("hi", timeout_s=2)

    def test_streaming_behavior_forwarded(self):
        client, fake = make_client(scripted_lines("x"))
        client.prompt("hi", streaming_behavior="follow_up")
        sent = json.loads(fake.stdin.written.split(b"\n")[0])
        assert sent["streamingBehavior"] == "follow_up"


class TestFrameSplitting:
    def test_u2028_inside_json_string_does_not_split(self):
        # JSON string containing U+2028 must NOT split the frame: a false split
        # (universal newlines) would break the JSON into two skipped fragments
        # and prompt() would time out instead of returning the text intact.
        text_with_u2028 = f"line1{U2028}line2"
        end = {
            "type": "agent_end",
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": text_with_u2028}]},
            ],
        }
        line = (json.dumps(end, ensure_ascii=False) + "\n").encode("utf-8")
        assert U2028.encode("utf-8") in line  # sanity: raw U+2028 bytes present
        client, _ = make_client([READY_LINE, line])
        assert client.prompt("hi") == f"line1{U2028}line2"

    def test_non_json_lines_skipped_silently(self):
        lines = [b"WARNING: something human readable\n"] + scripted_lines("ok")
        client, _ = make_client(lines)
        assert client.prompt("hi") == "ok"


class TestLaunchFlags:
    def _argv(self, _omit_model=False, **kw) -> list:
        captured: dict = {}

        def factory(argv, **kwargs):
            captured["argv"] = argv
            return FakePiRpcProc(scripted_lines("x"))

        client_kw = {"cwd": "/tmp/fakecwd", "proc_factory": factory, **kw}
        if not _omit_model:
            client_kw.setdefault("model", "m")
        client = PiRpcClient(**client_kw)  # type: ignore[arg-type]
        client.start()
        return captured["argv"]

    def test_model_flag_present_when_given(self):
        argv = self._argv(model="m9")
        i = argv.index("--model")
        assert argv[i + 1] == "m9"

    def test_model_flag_omitted_when_none(self):
        # then pi falls back to its own defaultModel from settings.json
        assert "--model" not in self._argv(model=None)

    def test_model_flag_omitted_when_kwarg_absent(self):
        assert "--model" not in self._argv(_omit_model=True)

    def test_codemode_adds_plus_t(self):
        argv = self._argv(codemode=True)
        i = argv.index("-t")
        assert argv[i + 1] == "+codemode"

    def test_tools_override_codemode(self):
        argv = self._argv(codemode=True, tools=["read", "grep", "find", "ls"])
        i = argv.index("-t")
        assert argv[i + 1] == "read,grep,find,ls"

    def test_thinking_passthrough(self):
        argv = self._argv(thinking="high")
        i = argv.index("--thinking")
        assert argv[i + 1] == "high"

    def test_default_has_no_tool_flags(self):
        argv = self._argv()
        assert "-t" not in argv

    def test_no_session_flag(self):
        argv = self._argv(no_session=True)
        assert "--no-session" in argv


class TestSigterm:
    def test_sigterm_handler_registered(self):
        src = (SCRIPTS_DIR / "pi_rpc_client.py").read_text()
        assert "signal.signal(signal.SIGTERM, _on_sigterm)" in src
        assert "raise SystemExit(128 + signum)" in src


class TestCrashPrevention:
    def test_prompt_frame_uses_message_not_content(self):
        client, fake = make_client(scripted_lines("x"))
        client.prompt("hi")
        sent = json.loads(fake.stdin.written)
        assert "content" not in sent
        assert sent["message"] == "hi"
        assert sent["type"] == "prompt"


class TestSteerFollowUpAbort:
    def test_steer_fire_and_forget(self):
        client, fake = make_client(scripted_lines("x"))
        assert client.steer("stop thinking") is None
        assert json.loads(fake.stdin.written) == {"type": "steer", "message": "stop thinking"}

    def test_follow_up(self):
        client, fake = make_client(scripted_lines("x"))
        client.follow_up("and more")
        assert json.loads(fake.stdin.written) == {"type": "follow_up", "message": "and more"}

    def test_abort(self):
        client, fake = make_client(scripted_lines("x"))
        client.abort()
        assert json.loads(fake.stdin.written) == {"type": "abort"}


class TestAgentDir:
    def _capture(self, **client_kw):
        captured: dict = {}

        def factory(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return FakePiRpcProc(scripted_lines("x"))

        client = PiRpcClient(model="m", cwd="/tmp/fakecwd", proc_factory=factory, **client_kw)  # type: ignore[arg-type]
        client.start()
        return captured

    def test_agent_dir_flag_reaches_pi_argv(self):
        # pi has no --agent-dir CLI flag: PI_CODING_AGENT_DIR env var only
        captured = self._capture(agent_dir="/fake/agent-worker")
        assert "--agent-dir" not in captured["argv"]
        assert captured["env"]["PI_CODING_AGENT_DIR"] == "/fake/agent-worker"

    def test_agent_dir_exported_as_pi_env_var(self):
        captured = self._capture(agent_dir="/fake/agent-worker")
        assert captured["env"]["PI_CODING_AGENT_DIR"] == "/fake/agent-worker"

    def test_no_agent_dir_flag_by_default(self, monkeypatch):
        # the client must not invent PI_CODING_AGENT_DIR; clear any ambient one
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
        captured = self._capture()
        assert "PI_CODING_AGENT_DIR" not in captured["env"]

    def test_argv_never_carries_ns_flag(self):
        captured = self._capture(agent_dir="/fake/agent-worker")
        assert "-ns" not in captured["argv"]
        assert "--no-skills" not in captured["argv"]


class TestClose:
    def test_sigterm_then_wait(self):
        client, fake = make_client(scripted_lines("x"))
        client.close()
        assert fake.term_called
        assert not fake.kill_called
        assert fake.wait_calls == [5]
        assert client.proc is fake

    def test_sigterm_then_sigkill_if_alive(self):
        client, fake = make_client(scripted_lines("x"))

        def stubborn_wait(timeout=None):
            if fake.kill_called:
                return fake.returncode
            raise subprocess.TimeoutExpired(cmd="pi", timeout=timeout)

        fake.wait = stubborn_wait  # survives SIGTERM, forces SIGKILL path
        client.close()
        assert fake.term_called
        assert fake.kill_called

    def test_close_idempotent(self):
        client, fake = make_client(scripted_lines("x"))
        client.close()
        client.close()
        assert fake.wait_calls == [5]


class TestRegistryUpdate:
    def _write_registry(self, tmp_path, monkeypatch, entries):
        import pi_rpc_client as mod

        path = tmp_path / "pi-agents.jsonl"
        monkeypatch.setattr(mod, "REGISTRY_PATH", str(path))
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return mod

    def test_registry_kill_wins_no_phantom(self, tmp_path, monkeypatch):
        mod = self._write_registry(
            tmp_path,
            monkeypatch,
            [
                {"session": "S", "pid": 111, "model": "m", "status": "running", "started": "t0"},
            ],
        )
        # simulate external kill rewriting the line's status
        with open(mod.REGISTRY_PATH) as f:
            lines = [json.loads(line) for line in f]
        lines[0]["status"] = "killed"
        with open(mod.REGISTRY_PATH, "w") as f:
            for e in lines:
                f.write(json.dumps(e) + "\n")
        mod._registry_write("S", "m", "done", pid=111)
        with open(mod.REGISTRY_PATH) as f:
            rows = [json.loads(line) for line in f]
        assert len(rows) == 1
        assert rows[0]["status"] == "killed"

    def test_registry_append_when_no_line(self, tmp_path, monkeypatch):
        mod = self._write_registry(tmp_path, monkeypatch, [])
        mod._registry_write("S2", "m", "done", pid=222)
        with open(mod.REGISTRY_PATH) as f:
            rows = [json.loads(line) for line in f]
        assert len(rows) == 1
        assert rows[0]["status"] == "done"
        assert rows[0]["pid"] == 222

    def test_terminal_line_carries_known_exit_code(self, tmp_path, monkeypatch):
        mod = self._write_registry(tmp_path, monkeypatch, [])
        mod._registry_write("S3", "m", "done", pid=333, exit_code=0)
        with open(mod.REGISTRY_PATH) as f:
            rows = [json.loads(line) for line in f]
        assert rows[0]["exit_code"] == 0

    def test_running_line_has_no_exit_code_by_default(self, tmp_path, monkeypatch):
        mod = self._write_registry(tmp_path, monkeypatch, [])
        mod._registry_write("S4", "m", "running", pid=444)
        with open(mod.REGISTRY_PATH) as f:
            rows = [json.loads(line) for line in f]
        assert "exit_code" not in rows[0]

    def test_terminal_without_code_stays_absent_no_null(self, tmp_path, monkeypatch):
        mod = self._write_registry(tmp_path, monkeypatch, [])
        mod._registry_write("S5", "m", "timeout", pid=555, exit_code=None)
        with open(mod.REGISTRY_PATH) as f:
            rows = [json.loads(line) for line in f]
        assert "exit_code" not in rows[0]

    def test_killed_exit_code_maps_sigterm_and_interrupt(self):
        import pi_rpc_client as mod

        assert mod._killed_exit_code(SystemExit(143)) == 143
        assert mod._killed_exit_code(KeyboardInterrupt()) == 130
        assert mod._killed_exit_code(RuntimeError("x")) is None
        assert mod._killed_exit_code(SystemExit("128+15")) is None

    def test_registry_update_running_carries_log_path(self, tmp_path, monkeypatch):
        mod = self._write_registry(tmp_path, monkeypatch, [])
        mod._registry_write("S6", "m", "running", pid=666)
        mod._registry_patch("S6", 666, log_path="/pi/sessions/d/x.jsonl")
        mod._registry_write("S6", "m", "done", pid=666, exit_code=0)
        with open(mod.REGISTRY_PATH) as f:
            rows = [json.loads(line) for line in f]
        terminal = [r for r in rows if r["pid"] == 666]
        assert len(terminal) == 1
        assert terminal[0]["status"] == "done"
        assert terminal[0]["exit_code"] == 0
        assert terminal[0]["log_path"] == "/pi/sessions/d/x.jsonl"

    def test_registry_update_running_killed_row_keeps_log_path(self, tmp_path, monkeypatch):
        mod = self._write_registry(tmp_path, monkeypatch, [])
        mod._registry_write("S7", "m", "running", pid=777)
        mod._registry_patch("S7", 777, log_path="/pi/sessions/d/y.jsonl")
        mod._registry_write("S7", "m", "killed", pid=777, exit_code=143)
        with open(mod.REGISTRY_PATH) as f:
            rows = [json.loads(line) for line in f]
        terminal = [r for r in rows if r["pid"] == 777]
        assert len(terminal) == 1
        assert terminal[0]["status"] == "killed"
        assert terminal[0]["log_path"] == "/pi/sessions/d/y.jsonl"

    def test_discovered_log_path_prefers_content_id_match(self, tmp_path, monkeypatch):
        """Id-less filenames for two fresh transcripts: each client stamps the
        candidate whose leading bytes carry its own session id."""
        import pi_rpc_client as mod

        sessions = tmp_path / "sessions"
        (sessions / "d1").mkdir(parents=True)
        other = sessions / "d1" / "2026-01-01T_uuid-other.jsonl"
        mine = sessions / "d1" / "2026-01-01T_uuid-mine.jsonl"
        other.write_text(
            '{"type":"session","version":3,"id":"someone-else","timestamp":"t"}\n' + "x" * 20000
        )
        mine.write_text(
            '{"type":"session","version":3,"id":"my-worker","timestamp":"t"}\n' + "x" * 20000
        )
        # keep both firmly newer than any launch floor
        mtime = time.time() + 60
        os.utime(other, (mtime, mtime))
        os.utime(mine, (mtime + 1, mtime + 1))
        monkeypatch.setattr("pi_rpc_client._LAUNCH_MONOTONIC_SKEW_FLOOR", time.time() - 120)
        assert mod._discovered_log_path(tmp_path, "my-worker") == str(mine)


class TestPiBinaryDiscovery:
    """Binary discovery is machine-agnostic: PATH first, then per-user roots."""

    def test_path_hit_wins(self, monkeypatch):
        monkeypatch.setattr("pi_rpc_client.shutil.which", lambda _: "/opt/toolchain/bin/pi")
        assert _find_pi_binary() == "/opt/toolchain/bin/pi"

    def test_falls_back_to_home_roots(self, monkeypatch, tmp_path):
        # given a HOME whose nvm tree holds pi, and no pi on PATH
        nvm_bin = tmp_path / ".nvm" / "versions" / "node" / "v1" / "bin"
        nvm_bin.mkdir(parents=True)
        (nvm_bin / "pi").write_text("")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr("pi_rpc_client.shutil.which", lambda _: None)

        # then the home-relative glob finds it without any literal username
        assert _find_pi_binary() == str(nvm_bin / "pi")

    def test_missing_binary_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr("pi_rpc_client.shutil.which", lambda _: None)

        with pytest.raises(FileNotFoundError, match="pi binary not found"):
            _find_pi_binary()


class TestSteerFile:
    """pi_manage steer writes a control file; the client consumes it per frame."""

    def _session(self, tmp_path, monkeypatch):
        import pi_rpc_client as mod

        monkeypatch.setattr(mod, "REGISTRY_PATH", str(tmp_path / "pi-agents.jsonl"))
        client, fake = make_client(scripted_lines("x"))
        client.session_id = "sess-steer"
        return client, fake

    def test_drain_sends_steering_frame(self, tmp_path, monkeypatch):
        client, fake = self._session(tmp_path, monkeypatch)
        steer_dir = tmp_path / "pi-steer"
        steer_dir.mkdir()
        msg = {"message": "commit what you have", "queue": False, "ts": "now"}
        (steer_dir / "sess-steer.json").write_text(json.dumps(msg))

        client._drain_steer()

        assert json.loads(fake.stdin.written) == {
            "type": "steer",
            "message": "commit what you have",
        }
        assert client.steered == ["commit what you have"]
        assert not (steer_dir / "sess-steer.json").exists()  # consumed exactly once

    def test_drain_queue_true_sends_follow_up(self, tmp_path, monkeypatch):
        client, fake = self._session(tmp_path, monkeypatch)
        steer_dir = tmp_path / "pi-steer"
        steer_dir.mkdir()
        (steer_dir / "sess-steer.json").write_text(
            json.dumps({"message": "skip that test", "queue": True, "ts": "now"})
        )

        client._drain_steer()

        assert json.loads(fake.stdin.written) == {"type": "follow_up", "message": "skip that test"}

    def test_drain_without_file_is_noop(self, tmp_path, monkeypatch):
        client, fake = self._session(tmp_path, monkeypatch)
        client._drain_steer()  # tmp_path never gets a pi-steer dir
        assert fake.stdin.written == b""
        assert client.steered == []

    def test_no_session_skips_drain(self, tmp_path, monkeypatch):
        client, fake = self._session(tmp_path, monkeypatch)
        client.session_id = None
        client._drain_steer()
        assert fake.stdin.written == b""


class TestHeartbeat:
    def _session(self, tmp_path, monkeypatch):
        import pi_rpc_client as mod

        monkeypatch.setattr(mod, "REGISTRY_PATH", str(tmp_path / "pi-agents.jsonl"))
        client, fake = make_client(scripted_lines("x"))
        client.session_id = "sess-beat"
        return client

    def test_force_write_stamps_frames_and_tools(self, tmp_path, monkeypatch):
        client = self._session(tmp_path, monkeypatch)
        client.tool_calls = ["bash"]
        client._frames_seen = 7

        client._heartbeat_tick(force=True)

        data = json.loads((tmp_path / "pi-heartbeat-sess-beat.json").read_text())
        assert data["frames"] == 7
        assert data["tools"] == 1
        assert abs(time.time() - data["ts"]) < 5

    def test_throttle_skips_within_window(self, tmp_path, monkeypatch):
        client = self._session(tmp_path, monkeypatch)
        client._frames_seen = 0  # start() already consumed the scripted frames
        client._heartbeat_tick(force=True)
        client._frames_seen = 99
        client._heartbeat_tick()  # inside the 5 s window: must not rewrite

        data = json.loads((tmp_path / "pi-heartbeat-sess-beat.json").read_text())
        assert data["frames"] == 0  # first write's zero, not 99

    def test_force_bypasses_throttle(self, tmp_path, monkeypatch):
        client = self._session(tmp_path, monkeypatch)
        client._frames_seen = 0
        client._heartbeat_tick(force=True)
        client._frames_seen = 99
        client._heartbeat_tick(force=True)

        data = json.loads((tmp_path / "pi-heartbeat-sess-beat.json").read_text())
        assert data["frames"] == 99

    def test_without_session_writes_nothing(self, tmp_path, monkeypatch):
        client = self._session(tmp_path, monkeypatch)
        client.session_id = None
        client._heartbeat_tick(force=True)
        assert not list(tmp_path.glob("pi-heartbeat-*"))


def _terminal_registry_line(registry_path: Path, session_id: str) -> dict:
    """Newest registry entry for session_id, or AssertionError with all rows."""
    rows = [json.loads(line) for line in registry_path.read_text().splitlines() if line]
    matches = [r for r in rows if r.get("session") == session_id]
    assert matches, rows
    return sorted(matches, key=lambda r: r["started"])[-1]


FAKE_PI_SH = """#!/bin/sh
echo '{"type":"ready"}'
exec sleep 5
"""


class TestMainExitCode:
    """main() end to end: the process return value and the registry's terminal exit_code."""

    SESSION = "sess-exit"

    def _fake_pi_on_path(self, tmp_path: Path) -> str:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_pi = bin_dir / "pi"
        fake_pi.write_text(FAKE_PI_SH)
        fake_pi.chmod(0o755)
        return str(bin_dir)

    def _child(self, tmp_path: Path, fake_pi_sh: str | None = None) -> tuple[list[str], dict, Path]:
        # Run as a real script: only then does the __main__ block install the
        # SIGTERM handler the production path uses. HOME pins the registry
        # under tmp_path, so the subprocess never touches the machine's file.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_pi = bin_dir / "pi"
        fake_pi.write_text(fake_pi_sh or FAKE_PI_SH)
        fake_pi.chmod(0o755)
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        }
        argv = [
            sys.executable,
            str(SCRIPTS_DIR / "pi_rpc_client.py"),
            "--session-id",
            self.SESSION,
            "--model",
            "m",
            "--prompt",
            "p",
            "--timeout",
            "60",
        ]
        return argv, env, tmp_path / ".hermes" / "logs" / "pi-agents.jsonl"

    def test_sigterm_midrun_registry_carries_143(self, tmp_path):
        # given a client child blocked waiting for an agent_end that never comes
        argv, env, registry = self._child(tmp_path)
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        deadline = time.time() + 10
        while time.time() < deadline:
            if registry.exists() and "running" in registry.read_text():
                break
            time.sleep(0.05)
        assert registry.exists(), "child never wrote its running line"

        # when the parent SIGTERMs the child mid-run
        proc.terminate()
        rc = proc.wait(timeout=10)

        # then the child exits 143 and its terminal registry line says so
        assert rc == 143, rc
        entry = _terminal_registry_line(registry, self.SESSION)
        assert entry["status"] == "killed"
        assert entry["exit_code"] == 143

    def test_sigterm_killed_row_keeps_discovered_log_path(self, tmp_path):
        # given a client child whose fake pi leaves a transcript with an id-less
        # filename under the agent-dir sessions tree
        fake_pi_sh = """#!/bin/sh
mkdir -p "$HOME/.pi/agent/sessions/spawn-dir"
printf '%s\n' '{"type":"session","version":3,"id":"sess-exit","timestamp":"t"}' \
  > "$HOME/.pi/agent/sessions/spawn-dir/2026-01-01T_blank-id.jsonl"
echo '{"type":"ready"}'
exec sleep 60
"""
        argv, env, registry = self._child(tmp_path, fake_pi_sh=fake_pi_sh)
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        deadline = time.time() + 10
        while time.time() < deadline:
            if registry.exists():
                with open(registry) as f:
                    if any("log_path" in line for line in f):
                        break
            time.sleep(0.05)
        assert registry.exists(), "child never stamped log_path"

        # when the parent SIGTERMs the child mid-run
        proc.terminate()
        rc = proc.wait(timeout=10)

        # then the final killed row still carries the discovered transcript
        assert rc == 143, rc
        entry = _terminal_registry_line(registry, self.SESSION)
        assert entry["status"] == "killed"
        assert entry["exit_code"] == 143
        transcript = (
            tmp_path / ".pi" / "agent" / "sessions" / "spawn-dir" / "2026-01-01T_blank-id.jsonl"
        )
        assert entry["log_path"] == str(transcript), entry

    def test_done_run_registry_carries_0(self, tmp_path, monkeypatch):
        # given main() driving the scripted fake pi whose run makes no tool calls
        import pi_rpc_client as mod

        monkeypatch.setattr(mod, "REGISTRY_PATH", str(tmp_path / "pi-agents.jsonl"))
        real_init = mod.PiRpcClient.__init__

        def fake_init(self, *a, **kw):
            kw["proc_factory"] = lambda argv, **k: FakePiRpcProc(scripted_lines("ok"))
            real_init(self, *a, **kw)

        monkeypatch.setattr(mod.PiRpcClient, "__init__", fake_init)

        # when the dispatch completes normally
        rc = mod.main(
            [
                "--session-id",
                self.SESSION,
                "--model",
                "m",
                "--prompt",
                "p",
                "--timeout",
                "30",
                "--min-tool-calls",
                "0",
            ]
        )

        # then done exit 0 lands in both the return value and the registry
        assert rc == 0
        entry = _terminal_registry_line(tmp_path / "pi-agents.jsonl", self.SESSION)
        assert entry["status"] == "done"
        assert entry["exit_code"] == 0

    def test_incomplete_run_registry_carries_3(self, tmp_path, monkeypatch):
        # given min_tool_calls=1 while the scripted run makes zero tool calls
        import pi_rpc_client as mod

        monkeypatch.setattr(mod, "REGISTRY_PATH", str(tmp_path / "pi-agents.jsonl"))
        real_init = mod.PiRpcClient.__init__

        def fake_init(self, *a, **kw):
            kw["proc_factory"] = lambda argv, **k: FakePiRpcProc(scripted_lines("ok"))
            real_init(self, *a, **kw)

        monkeypatch.setattr(mod.PiRpcClient, "__init__", fake_init)

        # when the dispatch completes without work (nudges exhausted by max)
        rc = mod.main(
            [
                "--session-id",
                self.SESSION,
                "--model",
                "m",
                "--prompt",
                "p",
                "--timeout",
                "30",
                "--max-nudges",
                "0",
            ]
        )

        # then the incomplete verdict is return 3 and registry exit_code 3
        assert rc == 3
        entry = _terminal_registry_line(tmp_path / "pi-agents.jsonl", self.SESSION)
        assert entry["status"] == "incomplete"
        assert entry["exit_code"] == 3


class TestHeartbeatBudget:
    """A failed stamp never consumes the 5 s budget; the dir gets ensured."""

    def test_failed_write_does_not_consume_the_beat_budget(self, tmp_path, monkeypatch):
        # given the write path is broken: the heartbeat dir sits under a FILE,
        # so the ensure fails and the tick lands nowhere
        import pi_rpc_client as mod

        logs_dir = tmp_path / "blocked"
        logs_dir.write_text("this is a file, not a dir")
        monkeypatch.setattr(mod, "REGISTRY_PATH", str(logs_dir / "pi-agents.jsonl"))
        client, _ = make_client(scripted_lines("x"))
        client.session_id = "sess-budget"
        hb_path = logs_dir / "pi-heartbeat-sess-budget.json"
        client._heartbeat_tick(force=True)
        assert not hb_path.exists()
        # the failed write moved nothing: the budget stays at its initial value
        assert client._last_beat == 0.0, "failed write consumed the budget"

        # when the blocker is removed and the NEXT FRAME arrives immediately
        # (a real clock tick: real_now - the untouched budget >> 5 s)
        logs_dir.unlink()
        logs_dir.mkdir(parents=True)
        client._heartbeat_tick()

        # then the stamp lands
        assert hb_path.exists(), "second tick was silently dropped"

    def test_first_tick_creates_dir_and_stamps(self, tmp_path, monkeypatch):
        import pi_rpc_client as mod

        logs_dir = tmp_path / "logs"
        monkeypatch.setattr(mod, "REGISTRY_PATH", str(logs_dir / "pi-agents.jsonl"))
        client, _ = make_client(scripted_lines("x"))
        client.session_id = "sess-budget"
        hb_path = logs_dir / "pi-heartbeat-sess-budget.json"
        # given any construction-time leftovers are out of the way
        with contextlib.suppress(OSError):
            os.remove(hb_path)

        client._heartbeat_tick(force=True)

        assert hb_path.exists(), "fresh dispatch stamp never landed"
        data = json.loads(hb_path.read_text())
        assert data["frames"] == client._frames_seen

"""Tests for pi_rpc_client. No network, no model calls, no real pi process."""

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
from pi_rpc_client import PiRpcClient, _find_pi_binary, _killed_exit_code, _plugin_staleness_notice

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

    def __init__(self, lines: list[bytes]) -> None:
        self.pid = 424242  # killpg targets a nonexistent group → OSError path
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(lines)
        self.returncode: int | None = None
        self.term_called = False
        self.kill_called = False
        self.wait_calls: list[float | None] = []

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


def make_client(lines: list[bytes], **client_kw) -> tuple[PiRpcClient, FakePiRpcProc]:
    captured: dict = {}

    def factory(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakePiRpcProc(lines)

    client = PiRpcClient(
        model="m",
        cwd="/tmp/fakecwd",
        proc_factory=factory,  # type: ignore[arg-type]
        **client_kw,
    )
    client.start()
    return client, client.proc


def _sent_frame(fake, index=0):
    return json.loads(fake.stdin.written.split(b"\n")[index])


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


class TestPluginFreshness:
    """--plugin-fresh short-circuits the mtime staleness heuristic."""

    def test_staleness_notice(self, monkeypatch):
        def boom():  # the certified-fresh path must never recompute
            raise AssertionError("heuristic ran despite --plugin-fresh")

        monkeypatch.setattr("pi_rpc_client._plugin_staleness_warning", boom)
        assert _plugin_staleness_notice(True) is None

        monkeypatch.setattr("pi_rpc_client._plugin_staleness_warning", lambda: "S")
        assert _plugin_staleness_notice(False) == "S"


class TestPrompt:
    def test_extracts_assistant_text_and_sends_message_frame(self):
        client, fake = make_client(scripted_lines("hello world"))
        assert client.prompt("hi") == "hello world"
        sent = _sent_frame(fake)
        assert sent == {"type": "prompt", "message": "hi"}
        # optional kwargs forward onto the frame
        client2, fake2 = make_client(scripted_lines("x"))
        client2.prompt("hi", streaming_behavior="follow_up")
        assert _sent_frame(fake2)["streamingBehavior"] == "follow_up"
        # the prompt frame carries the message key, never "content"
        client3, fake3 = make_client(scripted_lines("x"))
        client3.prompt("hi")
        sent3 = json.loads(fake3.stdin.written)
        assert sent3["message"] == "hi" and "content" not in sent3 and sent3["type"] == "prompt"

    def test_ack_dispositions(self):
        # "handled" = no run started, agent_end never comes: return at once,
        # not burning the timeout. "rejected" raises.
        ack = {
            "type": "response",
            "command": "prompt",
            "success": True,
            "data": {"disposition": "handled"},
        }
        client, _ = make_client([READY_LINE, (json.dumps(ack) + "\n").encode()])
        assert client.prompt("hi", timeout_s=2) == ""

        ack = {
            "type": "response",
            "command": "prompt",
            "success": False,
            "data": {"error": "bad frame"},
        }
        client, _ = make_client([READY_LINE, (json.dumps(ack) + "\n").encode()])
        with pytest.raises(RuntimeError, match="prompt rejected"):
            client.prompt("hi", timeout_s=2)


class TestFrameSplitting:
    def test_frame_splitting_edge_cases(self):
        # A U+2028 inside a JSON string must NOT split the frame (a false split
        # would break the JSON into two skipped fragments and prompt() would
        # time out); human-readable noise lines are skipped silently.
        end = {
            "type": "agent_end",
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "line1\u2028line2"}]}
            ],
        }
        u2028_line = (json.dumps(end, ensure_ascii=False) + "\n").encode("utf-8")
        client, _ = make_client([READY_LINE, u2028_line] + scripted_lines("line1\u2028line2"))
        assert client.prompt("hi") == "line1\u2028line2"

        noisy = [b"WARNING: something human readable\n"] + scripted_lines("ok")
        client, _ = make_client(noisy)
        assert client.prompt("hi") == "ok"


class TestLaunchFlags:
    CASES = [
        ({"model": "m9"}, ["--model", "m9"]),
        ({"codemode": True}, ["-t", "+codemode"]),
        ({"codemode": True, "tools": ["read", "grep", "find", "ls"]}, ["-t", "read,grep,find,ls"]),
        ({"thinking": "high"}, ["--thinking", "high"]),
        ({"no_session": True}, ["--no-session"]),
    ]

    def _argv_for(self, client_kw):
        captured: dict = {}

        def factory(argv, **kwargs):
            captured["argv"] = argv
            return FakePiRpcProc(scripted_lines("x"))

        c = PiRpcClient(cwd="/tmp/fakecwd", proc_factory=factory, **client_kw)  # type: ignore[arg-type]
        c.start()
        return captured["argv"]

    def test_argv_flags(self):
        for client_kw, expect in self.CASES:
            argv = self._argv_for(client_kw)
            i = argv.index(expect[0])
            assert argv[i : i + len(expect)] == expect, (client_kw, argv)

        # model=None omits the flag (pi falls back to its defaultModel) and the
        # default client invents no tool flags at all
        captured: dict = {}

        def factory2(argv, **kwargs):
            captured["argv"] = argv
            return FakePiRpcProc(scripted_lines("x"))

        c = PiRpcClient(cwd="/tmp/fakecwd", proc_factory=factory2)  # type: ignore[arg-type]
        c.start()
        assert "--model" not in captured["argv"]
        assert "-t" not in captured["argv"] and "--tools" not in captured["argv"]


class TestSteerFollowUpAbort:
    def test_control_frames(self):
        client, fake = make_client(scripted_lines("x"))
        assert client.steer("stop thinking") is None
        assert json.loads(fake.stdin.written) == {"type": "steer", "message": "stop thinking"}
        fake.stdin.written = b""
        client.follow_up("and more")
        assert json.loads(fake.stdin.written) == {"type": "follow_up", "message": "and more"}
        fake.stdin.written = b""
        client.abort()
        assert json.loads(fake.stdin.written) == {"type": "abort"}


class TestAgentDir:
    def test_agent_dir_reaches_pi_as_env_var_never_argv(self, monkeypatch):
        # pi has no --agent-dir CLI flag: PI_CODING_AGENT_DIR env var only;
        # the client must not invent the env var when no agent dir is given.
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
        captured: dict = {}

        def factory(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return FakePiRpcProc(scripted_lines("x"))

        client = PiRpcClient(
            model="m",
            cwd="/tmp/fakecwd",
            agent_dir="/fake/agent-worker",
            proc_factory=factory,  # type: ignore[arg-type]
        )
        client.start()
        assert "--agent-dir" not in captured["argv"]
        assert "-ns" not in captured["argv"] and "--no-skills" not in captured["argv"]
        assert captured["env"]["PI_CODING_AGENT_DIR"] == "/fake/agent-worker"

        captured.clear()
        client = PiRpcClient(model="m", cwd="/tmp/fakecwd", proc_factory=factory)  # type: ignore[arg-type]
        client.start()
        assert "PI_CODING_AGENT_DIR" not in captured["env"]


class TestClose:
    def test_sigterm_then_wait(self):
        client, fake = make_client(scripted_lines("x"))
        client.close()
        assert fake.term_called and not fake.kill_called
        assert fake.wait_calls == [5]
        assert client.proc is fake

        # close is idempotent: the second close() must not wait again
        fake.wait_calls.clear()
        client.close()
        assert fake.wait_calls == []

        # a child that survives SIGTERM gets SIGKILLed
        client2, fake2 = make_client(scripted_lines("x"))

        def stubborn_wait(timeout=None):
            if fake2.kill_called:
                return fake2.returncode
            raise subprocess.TimeoutExpired(cmd="pi", timeout=timeout)

        fake2.wait = stubborn_wait
        client2.close()
        assert fake2.term_called and fake2.kill_called


class TestRegistryUpdate:
    def _write_registry(self, tmp_path, monkeypatch, entries):
        import pi_rpc_client as mod

        path = tmp_path / "pi-agents.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mod, "REGISTRY_PATH", str(path))
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return mod

    def _rows(self):
        with open(self.mod.REGISTRY_PATH) as f:
            return [json.loads(line) for line in f]

    @pytest.mark.parametrize(
        ("seed", "write_kw", "expect_rows"),
        [
            pytest.param(
                [{"session": "S", "pid": 111, "model": "m", "status": "killed", "started": "t0"}],
                {"session": "S", "pid": 111, "status": "done"},
                [{"status": "killed"}],
                id="kill-wins-no-phantom",
            ),
            pytest.param(
                [],
                {"session": "S2", "pid": 222, "status": "done"},
                [{"status": "done", "pid": 222}],
                id="append-when-no-line",
            ),
            pytest.param(
                [],
                {"session": "S3", "pid": 333, "status": "done", "exit_code": 0},
                [{"exit_code": 0}],
                id="terminal-carries-exit-code",
            ),
            pytest.param(
                [], {"session": "S4", "pid": 444, "status": "running"}, [{}], id="no-exit-code"
            ),
        ],
    )
    def test_registry_write_shapes(self, seed, write_kw, expect_rows, tmp_path, monkeypatch):
        self.mod = self._write_registry(tmp_path, monkeypatch, seed)
        self.mod._registry_write(write_kw.pop("session"), "m", write_kw.pop("status"), **write_kw)
        rows = self._rows()
        assert len(rows) == 1, rows
        for key, value in expect_rows[0].items():
            assert rows[0][key] == value, rows
        # the exit-code map: SIGTERM 143, KeyboardInterrupt 130, anything else none
        assert _killed_exit_code(KeyboardInterrupt()) == 130
        assert _killed_exit_code(RuntimeError("x")) is None
        assert _killed_exit_code(SystemExit("128+15")) is None

    def test_registry_update_running_carries_log_path(self, tmp_path, monkeypatch):
        for final_status, exit_code, pid in (("done", 0, 666), ("killed", 143, 667)):
            self.mod = self._write_registry(tmp_path / str(pid), monkeypatch, [])
            self.mod._registry_write("S6", "m", "running", pid=pid)
            self.mod._registry_patch("S6", pid, log_path="/pi/sessions/d/x.jsonl")
            self.mod._registry_write("S6", "m", final_status, pid=pid, exit_code=exit_code)
            terminal = [r for r in self._rows() if r["pid"] == pid]
            assert len(terminal) == 1, terminal
            assert terminal[0]["status"] == final_status
            assert terminal[0]["exit_code"] == exit_code
            assert terminal[0]["log_path"] == "/pi/sessions/d/x.jsonl"

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

    def test_discovery_order(self, monkeypatch, tmp_path):
        # PATH first; then the home nvm tree without any literal username;
        # no binary anywhere is a named FileNotFoundError.
        monkeypatch.setattr("pi_rpc_client.shutil.which", lambda _: "/opt/toolchain/bin/pi")
        assert _find_pi_binary() == "/opt/toolchain/bin/pi"

        nvm_bin = tmp_path / ".nvm" / "versions" / "node" / "v1" / "bin"
        nvm_bin.mkdir(parents=True)
        (nvm_bin / "pi").write_text("")
        monkeypatch.setattr("pi_rpc_client.shutil.which", lambda _: None)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _find_pi_binary() == str(nvm_bin / "pi")

        (nvm_bin / "pi").unlink()
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

    def test_drain(self, tmp_path, monkeypatch):
        client, fake = self._session(tmp_path, monkeypatch)
        # without file or session: noop
        client._drain_steer()
        assert fake.stdin.written == b""
        steer_dir = tmp_path / "pi-steer"
        steer_dir.mkdir()
        (steer_dir / "sess-steer.json").write_text(
            json.dumps({"message": "commit what you have", "queue": False, "ts": "now"})
        )
        client._drain_steer()
        assert json.loads(fake.stdin.written) == {
            "type": "steer",
            "message": "commit what you have",
        }
        assert client.steered == ["commit what you have"]
        assert not (steer_dir / "sess-steer.json").exists()  # consumed exactly once
        fake.stdin.written = b""
        (steer_dir / "sess-steer.json").write_text(
            json.dumps({"message": "and more", "queue": True, "ts": "now"})
        )
        client._drain_steer()
        assert json.loads(fake.stdin.written) == {"type": "follow_up", "message": "and more"}


class TestHeartbeat:
    def _session(self, tmp_path, monkeypatch):
        import pi_rpc_client as mod

        monkeypatch.setattr(mod, "REGISTRY_PATH", str(tmp_path / "pi-agents.jsonl"))
        client, _ = make_client(scripted_lines("x"))
        client.session_id = "sess-beat"
        return client

    def test_force_write_stamps_frames_and_tools(self, tmp_path, monkeypatch):
        # without a session the tick writes nothing
        client = self._session(tmp_path, monkeypatch)
        client.session_id = None
        client._heartbeat_tick(force=True)
        assert not list(tmp_path.glob("pi-heartbeat-*"))

        client.session_id = "sess-beat"
        client.tool_calls = ["bash"]
        client._frames_seen = 7

        client._heartbeat_tick(force=True)

        data = json.loads((tmp_path / "pi-heartbeat-sess-beat.json").read_text())
        assert data["frames"] == 7
        assert data["tools"] == 1
        assert abs(time.time() - data["ts"]) < 5

    def test_throttle(self, tmp_path, monkeypatch):
        client = self._session(tmp_path, monkeypatch)
        client._frames_seen = 0  # start() already consumed the scripted frames
        client._heartbeat_tick(force=True)
        client._frames_seen = 99
        # inside the 5 s window the tick is skipped; force bypasses the window
        client._heartbeat_tick()
        data = json.loads((tmp_path / "pi-heartbeat-sess-beat.json").read_text())
        assert data["frames"] == 0, data
        client._heartbeat_tick(force=True)
        data = json.loads((tmp_path / "pi-heartbeat-sess-beat.json").read_text())
        assert data["frames"] == 99, data


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

    @staticmethod
    def _wait_for(predicate, deadline_s=10):
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        return None

    def test_sigterm_midrun_registry_carries_143(self, tmp_path):
        # given a client child blocked waiting for an agent_end that never comes
        argv, env, registry = self._child(tmp_path)
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        assert self._wait_for(lambda: registry.exists() and "running" in registry.read_text()), (
            "child never wrote its running line"
        )

        # when the parent SIGTERMs the child mid-run
        proc.terminate()
        rc = proc.wait(timeout=10)

        # then the child exits 143 and its terminal registry line says so
        assert rc == 143, rc
        entry = _terminal_registry_line(registry, self.SESSION)
        assert entry["status"] == "killed" and entry["exit_code"] == 143

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
        assert self._wait_for(
            lambda: (
                registry.exists()
                and any("log_path" in line for line in registry.read_text().splitlines())
            )
        ), "child never stamped log_path"

        # when the parent SIGTERMs the child mid-run
        proc.terminate()
        rc = proc.wait(timeout=10)

        # then the final killed row still carries the discovered transcript
        assert rc == 143, rc
        entry = _terminal_registry_line(registry, self.SESSION)
        assert entry["status"] == "killed" and entry["exit_code"] == 143
        transcript = (
            tmp_path / ".pi" / "agent" / "sessions" / "spawn-dir" / "2026-01-01T_blank-id.jsonl"
        )
        assert entry["log_path"] == str(transcript), entry

    def test_main_run_verdicts(self, tmp_path, monkeypatch):
        # given main() driving the scripted fake pi whose run makes no tool calls
        import pi_rpc_client as mod

        real_init = mod.PiRpcClient.__init__

        def fake_init(self, *a, **kw):
            kw["proc_factory"] = lambda argv, **k: FakePiRpcProc(scripted_lines("ok"))
            real_init(self, *a, **kw)

        monkeypatch.setattr(mod.PiRpcClient, "__init__", fake_init)

        for extra_argv, expect_rc, expect_status in (
            (["--min-tool-calls", "0"], 0, "done"),
            (["--max-nudges", "0"], 3, "incomplete"),
        ):
            registry = tmp_path / f"pi-agents-{expect_status}.jsonl"
            monkeypatch.setattr(mod, "REGISTRY_PATH", str(registry))
            # when the dispatch completes normally / without work
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
                    *extra_argv,
                ]
            )
            # then the verdict lands in both the return value and the registry
            assert rc == expect_rc
            entry = _terminal_registry_line(registry, self.SESSION)
            assert entry["status"] == expect_status
            assert entry["exit_code"] == expect_rc


class TestSalvagePatch:
    """Terminal-not-done exits snapshot the dirty worktree into a size-capped
    git diff patch, best-effort; a clean tree writes nothing."""

    def _run_main_killed(self, tmp_path, monkeypatch, repo, fake_init_kwargs=None):
        import pi_rpc_client as mod

        real_init = mod.PiRpcClient.__init__

        def fake_init(self, *a, **kw):
            kw["proc_factory"] = lambda argv, **k: FakePiRpcProc(scripted_lines("ok"))
            real_init(self, *a, **kw)

        monkeypatch.setattr(mod.PiRpcClient, "__init__", fake_init)

        def boom():
            raise KeyboardInterrupt()

        real_run = mod.PiRpcClient.run

        def run_then_die(self, *a, **kw):
            real_run(self, *a, **kw)
            boom()

        monkeypatch.setattr(mod.PiRpcClient, "run", run_then_die)
        registry = tmp_path / "pi-agents.jsonl"
        monkeypatch.setattr(mod, "REGISTRY_PATH", str(registry))
        with pytest.raises(KeyboardInterrupt):
            mod.main(
                [
                    "--session-id",
                    "salvage-session",
                    "--model",
                    "m",
                    "--prompt",
                    "p",
                    "--timeout",
                    "30",
                    "--min-tool-calls",
                    "0",
                    "--cwd",
                    str(repo),
                ]
            )
        with open(registry) as f:
            entries = [json.loads(line) for line in f]
        return [e for e in entries if e.get("status") not in ("running", None) or "finished" in e]

    def _git_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        (repo / "f.txt").write_text("one\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True, capture_output=True
        )
        return repo

    def test_killed_dirty_tree_writes_patch_with_registry_pointer(self, tmp_path, monkeypatch):
        repo = self._git_repo(tmp_path)
        (repo / "dirty.txt").write_text("uncommitted work\n")
        terminal = self._run_main_killed(tmp_path, monkeypatch, repo)
        patch_path = repo / ".hermes" / "lane" / "salvage-session.salvage.patch"
        assert patch_path.is_file(), terminal
        content = patch_path.read_text()
        assert "dirty.txt" in content, content
        entry = next(e for e in terminal if e.get("salvage_patch"))
        assert entry["salvage_patch"] == str(patch_path), entry
        assert "dirty.txt" in entry.get("salvage_status", ""), entry

    def test_clean_tree_writes_no_patch(self, tmp_path, monkeypatch):
        repo = self._git_repo(tmp_path)
        terminal = self._run_main_killed(tmp_path, monkeypatch, repo)
        assert not (repo / ".hermes" / "lane" / "salvage-session.salvage.patch").exists()
        entry = next(e for e in terminal if e.get("status") == "killed")
        assert entry.get("salvage_patch") is None, entry

    def test_non_git_cwd_degrades_to_named_skip(self, tmp_path, monkeypatch):
        plain = tmp_path / "plain"
        plain.mkdir()
        terminal = self._run_main_killed(tmp_path, monkeypatch, plain)
        entry = next(e for e in terminal if e.get("status") == "killed")
        assert "not a git repo" in str(entry.get("salvage_patch")), entry


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

        # then the stamp lands (the first tick also created the dir itself)
        assert hb_path.exists(), "second tick was silently dropped"

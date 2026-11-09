"""Tests for pi_rpc_client. No network, no model calls, no real pi process."""

import json
import signal
import subprocess
from pathlib import Path

import pytest
from pi_rpc_client import PiRpcClient, _find_pi_binary

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

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
    # pi is resolved dynamically: PATH first, then the per-user install roots
    pi_bin = _find_pi_binary()
    assert captured["argv"][:5] == [pi_bin, "--mode", "rpc", "--model", "m"]
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
    def _argv(self, **kw) -> list:
        captured: dict = {}

        def factory(argv, **kwargs):
            captured["argv"] = argv
            return FakePiRpcProc(scripted_lines("x"))

        client = PiRpcClient(
            model="m",
            cwd="/tmp/fakecwd",
            proc_factory=factory,
            **kw,  # type: ignore[arg-type]
        )
        client.start()
        return captured["argv"]

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

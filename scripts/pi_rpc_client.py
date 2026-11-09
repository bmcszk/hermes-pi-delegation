#!/usr/bin/env python3
"""Minimal, tested Python client for pi's RPC mode (newline-delimited JSON over stdio).

Protocol facts verified against the installed pi bundle: do not "improve":
- Request frame is {"type":"prompt","message":"<text>"}: the field is "message",
  NOT "content" ("content" makes pi crash: Cannot read properties of undefined).
- stdin must be kept OPEN while waiting for responses: pi processes the prompt
  only while stdin lives.
- Frames are split on b"\\n" ONLY (bytes.split(b"\\n")); never universal
  newlines/readline, which also split on U+2028/U+2029 found inside JSON strings.
- Frame sequence for a prompt: {"type":"response","command":"prompt","success":true,
  "data":{"disposition":"started"}} ack, then message_update stream (usage +
  assistantMessageEvent), then terminal {"type":"agent_end","messages":[...]}.
- The prompt ack is ACCEPTANCE only; disposition "handled" means no run started
  (return "" at once, an agent_end will never come); success:false acks raise.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import hashlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable

# 120 s was too short: real sweep contracts were killed mid-run and reported
# as "no changes". Override per call with --timeout, or via PI_WORKER_TIMEOUT_S.
DEFAULT_TIMEOUT_S = float(os.environ.get("PI_WORKER_TIMEOUT_S", "900"))

# Best-effort registry of live agents (consumed by pi_manage list/kill).
REGISTRY_PATH = os.path.join(os.path.expanduser("~"), ".hermes", "logs", "pi-agents.jsonl")

# Per-frame idle budget only guards against a SILENT worker. A hung tool that
# still trickles progress frames resets it forever (2026-10-31: a runaway
# regex heredoc burned one core at 100% for 2 days). WALL_TIMEOUT_S is the
# hard ceiling on one dispatch regardless of frame traffic.
WALL_TIMEOUT_S = float(os.environ.get("PI_WORKER_WALL_TIMEOUT_S", "3600"))


def _registry_write(session_id, model, status, pid=None):
    """Append/update the agent registry line. Never raises, never blocks dispatch."""
    try:
        import datetime

        now = datetime.datetime.now().isoformat()
        entry = {
            "agent": session_id,
            "session": session_id,
            "pid": pid if pid is not None else os.getpid(),
            "model": model,
            "status": status,
            "started": now,
        }
        if status in ("done", "error", "killed", "timeout"):
            entry["finished"] = now
            _registry_update_running(session_id, entry)
        else:
            os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
            with open(REGISTRY_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _registry_update_running(session_id, entry):
    """Replace the running line matching session+pid.

    If the session+pid already has any terminal line (killed/dead/done), the
    external terminal state wins and we do nothing; append only when the
    session+pid has no line at all (crash-before-write case).
    """
    try:
        lines = []
        replaced = False
        exists = False
        if os.path.exists(REGISTRY_PATH):
            with open(REGISTRY_PATH) as f:
                for line in f:
                    try:
                        data = json.loads(line)
                    except Exception:
                        lines.append(line)
                        continue
                    if data.get("session") == session_id and data.get("pid") == entry["pid"]:
                        exists = True
                        if not replaced and data.get("status") == "running":
                            lines.append(json.dumps(entry) + "\n")
                            replaced = True
                            continue
                    lines.append(line)
        if exists and not replaced:
            return
        if not replaced:
            lines.append(json.dumps(entry) + "\n")
        with open(REGISTRY_PATH, "w") as f:
            f.writelines(lines)
    except Exception:
        pass


def _find_pi_binary() -> str:
    """Locate the pi executable: PATH first, then the usual per-user install roots."""
    found = shutil.which("pi")
    if found:
        return found
    roots = (
        "~/.nvm/versions/node/*/bin/pi",
        "~/.npm-global/bin/pi",
        "~/.local/bin/pi",
        "~/.bun/bin/pi",
    )
    for pattern in roots:
        for cand in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            if os.path.exists(cand):
                return cand
    raise FileNotFoundError(
        "pi binary not found in PATH or the per-user install roots "
        "(~/.nvm/versions/node/*/bin, ~/.npm-global/bin, ~/.local/bin, ~/.bun/bin)"
    )


class PiRpcClient:
    """Spawn `pi --mode rpc` and drive it over newline-delimited JSON on stdio."""

    def __init__(
        self,
        model: str | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
        skills: list[str] | None = None,
        system_prompts: list[str] | None = None,
        codemode: bool = False,
        thinking: str | None = None,
        tools: list | None = None,
        no_session: bool = False,
        agent_dir: str | None = None,
        proc_factory: Callable[..., subprocess.Popen] | None = None,
    ) -> None:
        self.model = model
        self.cwd = cwd
        self.session_id = session_id
        self.skills = skills or []
        self.system_prompts = system_prompts or []
        self.codemode = codemode
        self.agent_dir = agent_dir
        self.thinking = thinking
        self.tools = tools or []
        self.no_session = no_session
        self.proc_factory = proc_factory or subprocess.Popen
        self.proc: subprocess.Popen | None = None
        self._frames: queue.Queue[dict] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = False
        self.tool_calls: list[str] = []
        self._tool_ids: set = set()
        self.nudges = 0
        self.satisfied = True

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the pi process and wait for readiness."""
        pi_bin = _find_pi_binary()
        argv = [pi_bin, "--mode", "rpc"]
        if self.model:
            argv += ["--model", self.model]
        if self.session_id:
            argv += ["--session-id", self.session_id]
        for skill in self.skills:
            argv += ["--skill", skill]
        for sp in self.system_prompts:
            argv += ["--append-system-prompt", sp]
        if self.tools:
            argv += ["-t", ",".join(self.tools)]
        elif self.codemode:
            argv += ["-t", "+codemode"]
        if self.thinking:
            argv += ["--thinking", self.thinking]
        if self.no_session:
            argv += ["--no-session"]
        self.proc = self.proc_factory(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # pi stderr flows into the report file (diagnosability)
            cwd=self.cwd,
            # Own process group: pi's tool children (bash heredocs, node
            # helpers) die with it on close()/killpg. Without this a hung
            # tool child survives the client and burns CPU for days.
            process_group=0,
            # pi runs its tool commands through $SHELL. Under zsh -lic the
            # startup noise ("can't change option: zle", gitstatus errors)
            # lands in tool output, so the agent stalls after one sentence.
            env={
                **os.environ,
                "SHELL": "/bin/bash",
                **({"PI_CODING_AGENT_DIR": self.agent_dir} if self.agent_dir else {}),
            },
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._ready.wait(DEFAULT_TIMEOUT_S)
        if not self._ready.is_set():
            self.close()
            raise RuntimeError("pi RPC did not become ready")

    def _read_loop(self) -> None:
        """Consume raw stdout, split on b'\\n' ONLY, enqueue parsed frames."""
        assert self.proc is not None and self.proc.stdout is not None
        for raw_line in self.proc.stdout:  # raw bytes iterator: no universal newlines
            for chunk in raw_line.split(b"\n"):
                chunk = chunk.strip(b"\r")
                if not chunk:
                    continue
                try:
                    frame = json.loads(chunk)
                except (ValueError, UnicodeDecodeError):
                    continue  # skip non-JSON lines silently
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "ready" or frame.get("type") == "extension_ui_request":
                    self._ready.set()
                self._frames.put(frame)

    # -- protocol ----------------------------------------------------------

    def _log_frame(self, frame: dict) -> None:
        """Append every frame to PI_RPC_FRAMELOG (jsonl) when set: diagnosis aid."""
        import os

        path = os.environ.get("PI_RPC_FRAMELOG")
        if not path:
            return
        # Full frames only: truncation makes the line invalid json and hides
        # the terminal agent_end (stopReason lives there), which is the one
        # frame worth having when a worker dies silently.
        try:
            with open(path, "a") as f:
                f.write(json.dumps(frame, default=str) + "\n")
        except OSError:
            pass

    def _send(self, frame: dict) -> None:
        assert self.proc is not None
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(json.dumps(frame).encode("utf-8") + b"\n")
        stdin.flush()

    def prompt(
        self,
        text: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        streaming_behavior: str | None = None,
        deadline: float | None = None,
    ) -> str:
        """Send a prompt; collect until the TOP-LEVEL agent_end; return assistant text.

        streaming_behavior: "steer" (inject into current run) or "follow_up"
        (queue for next turn): pi ERRORS mid-run if omitted while streaming.
        The prompt ack is ACCEPTANCE only (disposition started/queued/handled);
        completion is the terminal agent_end frame. disposition "handled" means
        no run started: return "" immediately instead of waiting for agent_end.
        """
        frame: dict = {"type": "prompt", "message": text}
        if streaming_behavior:
            frame["streamingBehavior"] = streaming_behavior
        self._send(frame)
        parts: list[str] = []
        if deadline is None:
            deadline = time.monotonic() + WALL_TIMEOUT_S  # hard wall-clock ceiling
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                # timeout_s is the per-frame IDLE budget (worker silent too
                # long); the deadline above bounds the WHOLE dispatch even
                # when a hung tool keeps trickling progress frames.
                frame = self._frames.get(timeout=min(timeout_s, remaining))
            except queue.Empty as exc:
                raise TimeoutError(
                    f"no agent_end within {timeout_s}s idle / {WALL_TIMEOUT_S}s wall"
                ) from exc
            self._log_frame(frame)
            self._note_tools(frame)
            if frame.get("type") == "response" and frame.get("command") == "prompt":
                data = frame.get("data") or {}
                disposition = data.get("disposition")
                if frame.get("success") is False:
                    raise RuntimeError(f"prompt rejected: {frame}")
                if disposition == "handled":
                    # ack says no run started: agent_end will never come
                    return ""
            # message_update frames stream partial content; the terminal
            # agent_end carries the final messages, which is what we return.
            if frame.get("type") == "agent_end":
                for message in frame.get("messages") or []:
                    parts.extend(self._assistant_text_parts(message))
                break
        return "".join(parts)

    @staticmethod
    def _assistant_text_parts(frame: dict) -> list[str]:
        if frame.get("role") != "assistant":
            return []
        content = frame.get("content")
        if isinstance(content, list):
            return [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
        if isinstance(content, str):
            return [content]
        return []

    def _note_tools(self, frame: dict) -> None:
        """Record tool calls, deduped by id, from any message-bearing frame."""
        message = frame.get("message")
        if isinstance(message, dict):
            messages = [message]
        else:
            messages = [m for m in frame.get("messages") or [] if isinstance(m, dict)]
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "toolCall":
                    continue
                call_id = item.get("id") or ""
                if call_id in self._tool_ids:
                    continue
                self._tool_ids.add(call_id)
                self.tool_calls.append(str(item.get("name") or ""))

    def _digest(self, path: str) -> str:
        try:
            with open(os.path.join(self.cwd, path), "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except OSError:
            return ""

    def _satisfied(self, before: dict, min_tool_calls: int) -> bool:
        if len(self.tool_calls) < min_tool_calls:
            return False
        return all(self._digest(path) != digest for path, digest in before.items())

    @staticmethod
    def _nudge_text(targets: list[str]) -> str:
        what = ", ".join(targets) + " is still unchanged" if targets else "you changed nothing"
        return (
            f"You ended the turn without doing the work: {what}. "
            "Act now with tool calls only: read the target file, apply the edit, then run the "
            "exact verification command from the contract. Never reply with prose."
        )

    def run(
        self,
        text: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        require_edit: str | None = None,
        min_tool_calls: int = 1,
        max_nudges: int = 3,
    ) -> str:
        """Prompt, then verify that work happened; nudge in-session when it did not.

        A pi turn ends as soon as the model stops calling tools, so a confused
        worker returns with rc=0 having changed nothing. require_edit turns that
        into a detectable failure: the file hash must move, or the worker is told
        to act again in the same session.
        """
        targets = [p.strip() for p in (require_edit or "").split(",") if p.strip()]
        before = {path: self._digest(path) for path in targets}
        deadline = time.monotonic() + WALL_TIMEOUT_S  # whole run: prompt + nudges
        out = self.prompt(text, timeout_s=timeout_s, deadline=deadline)
        while not self._satisfied(before, min_tool_calls):
            if self.nudges >= max_nudges:
                break
            if time.monotonic() >= deadline:  # wall exhausted: no nudge retry
                break
            self.nudges += 1
            out += f"\n--- nudge {self.nudges} ---\n"
            out += self._nudge(self._nudge_text(targets), timeout_s, deadline)
        self.satisfied = self._satisfied(before, min_tool_calls)
        return out

    def _nudge(self, text: str, timeout_s: float, deadline: float | None = None) -> str:
        """Send a corrective prompt, tolerating a still-busy agent.

        A plain prompt is rejected with "Agent is already processing" while the
        previous run settles; followUp queues it for the turn that is starting
        instead of failing the whole dispatch.
        """
        try:
            return self.prompt(text, timeout_s=timeout_s, deadline=deadline)
        except RuntimeError as exc:
            if "already processing" not in str(exc):
                raise
            return self.prompt(
                text, timeout_s=timeout_s, deadline=deadline, streaming_behavior="follow_up"
            )

    def steer(self, text: str) -> None:
        """Fire-and-forget steering message."""
        self._send({"type": "steer", "message": text})

    def follow_up(self, text: str) -> None:
        self._send({"type": "follow_up", "message": text})

    def abort(self) -> None:
        self._send({"type": "abort"})

    # -- teardown ----------------------------------------------------------

    def close(self) -> None:
        """SIGTERM, wait 5s, then SIGKILL if still alive."""
        if self._stopped:
            return
        self._stopped = True
        proc = self.proc
        if proc is None:
            return
        with contextlib.suppress(OSError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)  # pi AND its tool children
        with contextlib.suppress(OSError):
            proc.terminate()  # SIGTERM fallback if the group is gone
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
            return
        with contextlib.suppress(OSError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)  # tool children incl.
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
            proc.wait(timeout=5)


def _plugin_staleness_notice(plugin_fresh: bool) -> str | None:
    """Staleness message for main(): None when --plugin-fresh is passed, since
    the launcher emitted that flag only after self-verifying that the module
    building the command hashes to the same bytes that were imported."""
    if plugin_fresh:
        return None
    return _plugin_staleness_warning()


def _plugin_staleness_warning() -> str | None:
    """Warn when the gateway serving pi_launch imported the plugin before its file changed.

    pi_launch only builds argv from a module the long-lived gateway process
    imported at startup. Editing __init__.py on disk does not reach it, so every
    launch keeps emitting the old flags until the gateway restarts. This client is
    spawned fresh per launch, so it is the only place that can notice.
    """
    plugin = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "__init__.py"
    )
    try:
        mtime = os.stat(plugin).st_mtime
        with open("/proc/stat", encoding="utf-8") as fh:
            btime = next(int(line.split()[1]) for line in fh if line.startswith("btime "))
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration):
        return None

    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            return None
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                stat = fh.read()
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return None

        fields = stat[stat.rfind(")") + 2 :].split()
        try:
            started = btime + int(fields[19]) / ticks
            parent = int(fields[1])
        except (IndexError, ValueError):
            return None

        if "hermes" in cmdline.lower() and started < mtime:
            return (
                "gateway started "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))} but the "
                "plugin file changed "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))}: pi_launch "
                "still serves the module imported at startup; restart the gateway"
            )
        pid = parent
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pi RPC mode client")
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--session-id", default=None, dest="session_id")
    parser.add_argument("--skill", action="append", default=[])
    parser.add_argument(
        "--append-system-prompt", action="append", default=[], dest="append_system_prompt"
    )
    parser.add_argument("--codemode", action="store_true")
    parser.add_argument(
        "--plugin-fresh",
        action="store_true",
        default=False,
        help=(
            "launcher self-verified: module that built the command matches disk; "
            "skip the mtime staleness heuristic"
        ),
    )
    parser.add_argument(
        "--agent-dir",
        default=None,
        dest="agent_dir",
        help="PI_CODING_AGENT_DIR for the pi process (isolated worker agent dir)",
    )
    parser.add_argument(
        "--thinking", default=None, help="pi thinking level: off|minimal|low|medium|high|xhigh|max"
    )
    parser.add_argument(
        "--tools", default=None, help="comma-separated -t allowlist (overrides --codemode)"
    )
    parser.add_argument(
        "--no-session", action="store_true", help="throwaway run: no pi session record"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        dest="timeout_s",
        help="prompt budget seconds (default 120)",
    )
    parser.add_argument(
        "--wall-timeout",
        type=float,
        default=None,
        dest="wall_timeout_s",
        help=(
            "hard wall-clock ceiling on the whole dispatch (default 3600; PI_WORKER_WALL_TIMEOUT_S)"
        ),
    )
    parser.add_argument(
        "--require-edit",
        default=None,
        dest="require_edit",
        help="repo-relative file that MUST change, else nudge the session (comma-separated ok)",
    )
    parser.add_argument(
        "--min-tool-calls",
        type=int,
        default=1,
        dest="min_tool_calls",
        help="tool calls required before the run counts as work (default 1)",
    )
    parser.add_argument(
        "--max-nudges",
        type=int,
        default=3,
        dest="max_nudges",
        help="in-session retries when the worker ends without working (default 3)",
    )
    args = parser.parse_args(argv)

    global WALL_TIMEOUT_S
    if args.wall_timeout_s:
        WALL_TIMEOUT_S = float(args.wall_timeout_s)

    stale = _plugin_staleness_notice(args.plugin_fresh)
    if stale:
        print(f"[pi] STALE-PLUGIN: {stale}", file=sys.stderr, flush=True)

    client = PiRpcClient(
        model=args.model,
        cwd=args.cwd,
        session_id=args.session_id,
        skills=args.skill,
        system_prompts=args.append_system_prompt,
        codemode=args.codemode,
        thinking=args.thinking,
        tools=[t.strip() for t in args.tools.split(",")] if args.tools else None,
        no_session=args.no_session,
        agent_dir=args.agent_dir,
    )
    client.start()
    _registry_write(args.session_id or "unnamed", args.model, "running")
    status = "done"
    try:
        text = client.run(
            args.prompt,
            timeout_s=args.timeout_s or DEFAULT_TIMEOUT_S,
            require_edit=args.require_edit,
            min_tool_calls=args.min_tool_calls,
            max_nudges=args.max_nudges,
        )
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stderr.write(
            f"[pi] tool_calls={len(client.tool_calls)} nudges={client.nudges} "
            f"satisfied={client.satisfied}\n"
        )
        if not client.satisfied:
            status = "incomplete"
    except TimeoutError:
        status = "timeout"
        raise
    except BaseException:  # SystemExit (SIGTERM), KeyboardInterrupt = killed
        status = "killed"
        raise
    finally:
        _registry_write(args.session_id or "unnamed", args.model, status)
        client.close()
    return 0 if status == "done" else 3


if __name__ == "__main__":

    def _on_sigterm(signum, frame):
        raise SystemExit(128 + signum)

    import signal

    signal.signal(signal.SIGTERM, _on_sigterm)
    sys.exit(main())

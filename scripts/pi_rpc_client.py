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
import tempfile
import threading
import time
from collections.abc import Callable

import registry_log

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

# Transcript discovery floor: sessions created before the client started are
# other workers' transcripts; wall-clock launch time bounds the scan window.
_LAUNCH_MONOTONIC_SKEW_FLOOR = time.time()


# Both live under the registry dir so test setups that monkeypatch
# REGISTRY_PATH relocate them for free.
def _steer_file(session_id):
    """Control file pi_manage(action="steer") writes; the client consumes it."""
    if not session_id:
        return None
    return os.path.join(os.path.dirname(REGISTRY_PATH), "pi-steer", f"{session_id}.json")


def _heartbeat_file(session_id):
    """Liveness stamp the parent reads through pi_manage list as last_activity_s."""
    if not session_id:
        return None
    return os.path.join(os.path.dirname(REGISTRY_PATH), f"pi-heartbeat-{session_id}.json")


def _take_steer(session_id):
    """Read one queued steer message and delete its file; None when absent.

    A parse failure leaves the file in place and returns None, so a message
    written while the file was mid-rename is consumed next frame instead of
    lost. Rename-on-write keeps the race window closed on the writer side.
    """
    path = _steer_file(session_id)
    if not path:
        return None
    try:
        with open(path) as f:
            msg = json.load(f)
        os.remove(path)
        if isinstance(msg, dict):
            return msg
    except (OSError, ValueError):
        pass
    return None


def _killed_exit_code(exc):
    """Client exit code for a killed run: SIGTERM 143 (SystemExit 128+signum),
    KeyboardInterrupt 130 (128+SIGINT), unknown otherwise."""
    if isinstance(exc, KeyboardInterrupt):
        return 130
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _registry_patch(session_id, pid, **fields):
    """Merge fields into this session+pid's RUNNING line. Best-effort, never
    raises. True when a line changed, so the caller can retry a stamp that
    raced its row into existence."""
    try:
        with registry_log.locked_registry(REGISTRY_PATH):
            rows, torn = registry_log.read_registry_lines(REGISTRY_PATH)
            changed = False
            lines = []
            for data in rows:
                if (
                    data.get("session") == session_id
                    and data.get("pid") == pid
                    and data.get("status") == "running"
                ):
                    merged = dict(data)
                    merged.update(fields)
                    if merged != data:
                        lines.append(json.dumps(merged) + "\n")
                        changed = True
                        continue
                lines.append(json.dumps(data) + "\n")
            lines.extend(line + "\n" for line in torn)
            if changed:
                registry_log.write_registry_lines(REGISTRY_PATH, lines)
    except Exception:
        pass


def _launch_base_git(cwd):
    """HEAD sha of the dispatch cwd at launch, best-effort (None when git is
    unavailable or cwd is not a repo)."""
    if not cwd:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or None
    except Exception:
        pass
    return None


def _discovered_log_path(agent_dir, session_id):
    """Best next transcript candidate from pi's sessions tree, or None.

    pi never announces the transcript path over RPC (verified against the real
    binary: no startup frame, no stderr line) and its filenames need not
    contain the session id, so the client collects .jsonl files created since
    launch, with this preference order:
    1. filename contains the session id;
    2. leading bytes (~16KB) contain the session id string (real pi writes
       {"type":"session","id":...} as the first line, verified);
    3. mtime-newest — the only tiebreak for transcripts that match neither;
       two concurrent id-less content-less candidates remain ambiguous.
    """
    base = agent_dir or os.path.join(os.path.expanduser("~"), ".pi", "agent")
    candidates = [
        p
        for p in glob.glob(os.path.join(base, "sessions", "*", "*.jsonl"))
        if os.stat(p).st_mtime >= _LAUNCH_MONOTONIC_SKEW_FLOOR
    ]
    if not candidates:
        return None
    matching = [p for p in candidates if session_id and session_id in os.path.basename(p)]
    if matching:
        return max(matching, key=os.path.getmtime)
    if session_id:
        content_matches = [p for p in candidates if _content_contains(p, session_id)]
        if content_matches:
            return max(content_matches, key=os.path.getmtime)
    return max(candidates, key=os.path.getmtime)


def _write_deliverable(path, text):
    """Auditor mode: the captured assistant text IS the observable output;
    write it into the first require_edit path. False when the write failed
    (the run then ends incomplete instead of claiming a delivery)."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return True
    except OSError:
        return False


def _deliverable_verdict(path, sentinel):
    """met / unmet / unchecked for one dispatch's deliverable file.

    A call count cannot prove a deliverable: a coder session made 35 research
    tool calls, changed nothing, and still exited satisfied. The sentinel line in
    the require_edit file is the claim; this is the check. A missing file is
    unmet, never met, and the sentinel matches literally (no regex).
    """
    if not sentinel:
        return "unchecked"
    if not path:
        return "unmet"
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return "met" if sentinel in handle.read() else "unmet"
    except OSError:
        return "unmet"


def _salvage_snapshot(session_id, cwd):
    """Snapshot the uncommitted worktree at a terminal-not-done exit.

    Writes <cwd>/.hermes/lane/<session>.salvage.patch (a plain `git diff`,
    size-capped, read-only toward the tree) and returns the registry fields:
    salvage_patch + salvage_status on success, a named skip otherwise. A clean
    tree writes nothing. Best-effort: never raises, never alters the exit code.
    """
    rel_patch = os.path.join(".hermes", "lane", f"{session_id}.salvage.patch")
    if not cwd:
        return {"salvage_patch": {"skipped": "no cwd recorded for this dispatch"}}
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain"], capture_output=True, text=True, timeout=10
        )
    except Exception as exc:
        return {"salvage_patch": {"skipped": f"git unavailable: {exc}"}}
    if proc.returncode != 0:
        return {"salvage_patch": {"skipped": f"cwd is not a git repo: {cwd}"}}
    lines = [ln for ln in proc.stdout.splitlines() if not ln.strip().endswith(".hermes/")]
    if not lines:
        return {"salvage_patch": None}
    diff = subprocess.run(
        ["git", "-C", cwd, "diff", "HEAD"], capture_output=True, text=True, timeout=30
    )
    body = [diff.stdout] if diff.returncode == 0 and diff.stdout.strip() else []
    for ln in lines:
        if not ln.startswith("??"):
            continue
        rel = ln[3:].strip()
        if rel.endswith("/"):
            continue
        try:
            with open(os.path.join(cwd, rel), encoding="utf-8", errors="replace") as f:
                content = f.read(1024 * 1024)
        except OSError:
            continue
        body.append(f"--- /dev/null\n+++ /dev/null (untracked: {rel})\n{content}")
    if not body:
        body = ["(no tracked diff; changes are staged-only)\n"]
    patch_text = "\n".join(body)[: 4 * 1024 * 1024]
    patch_path = os.path.join(cwd, rel_patch)
    try:
        os.makedirs(os.path.dirname(patch_path), exist_ok=True)
        with open(patch_path, "w") as f:
            f.write(patch_text)
    except OSError as exc:
        return {"salvage_patch": {"skipped": f"cannot write patch: {exc}"}}
    return {
        "salvage_patch": patch_path,
        "salvage_status": lines[0][:160],
    }


def _lane_lock_path(session_id):
    """Hold-point for one dispatch: <cwd>/.hermes/lane/LOCK/<session>.lock.

    The cwd is embedded WHERE the dispatch runs, so the helper cannot know it
    alone — callers pass the launch cwd. The dir lives under .hermes/lane,
    inside the .gitignore region.
    """
    if not session_id:
        return None
    return os.path.join(".hermes", "lane", "LOCK", f"{session_id}.lock")


def _lane_lock_acquire(session_id, cwd, model, pid):
    """Atomically write this dispatch's lane lock. Best-effort, never raises."""
    rel = _lane_lock_path(session_id)
    if not rel or not cwd:
        return
    path = os.path.join(cwd, rel)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import datetime

        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(
                {
                    "session_id": session_id,
                    "pid": pid,
                    "model": model,
                    "started_at": datetime.datetime.now().isoformat(),
                },
                f,
            )
        os.replace(tmp, path)
    except Exception:
        pass


def _lane_lock_release(session_id, cwd):
    """Remove this dispatch's own lock on any terminal exit. Best-effort."""
    rel = _lane_lock_path(session_id)
    if not rel or not cwd:
        return
    with contextlib.suppress(OSError):
        os.remove(os.path.join(cwd, rel))


def _content_contains(path, needle):
    """True when needle appears in the file's first ~16KB. Best-effort; any
    unreadable candidate simply cannot match."""
    try:
        with open(path, "rb") as f:
            return needle.encode("utf-8") in f.read(16384)
    except OSError:
        return False


def _registry_write(
    session_id,
    model,
    status,
    pid=None,
    exit_code=None,
    deliverable=None,
    require_sentinel=None,
    salvage_patch=None,
    salvage_status=None,
    agent_dir=None,
):
    """Append/update the agent registry line. Never raises, never blocks dispatch.

    salvage_patch/salvage_status: the terminal worktree snapshot fields written
    by _salvage_snapshot on terminal-not-done exits (None = clean tree).
    agent_dir: the resolved --agent-dir value, or "default" when none passed.
    """
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
        if exit_code is not None:
            entry["exit_code"] = int(exit_code)
        if deliverable is not None:
            entry["deliverable"] = deliverable
        if require_sentinel:
            entry["require_sentinel"] = require_sentinel
        if salvage_patch is not None:
            entry["salvage_patch"] = salvage_patch
        if salvage_status is not None:
            entry["salvage_status"] = salvage_status
        if agent_dir is not None:
            entry["agent_dir"] = agent_dir
        if status in ("done", "error", "killed", "timeout"):
            entry["finished"] = now
            _registry_update_running(session_id, entry)
        else:
            registry_log.append_registry_line(REGISTRY_PATH, json.dumps(entry))
    except Exception:
        pass


def _registry_update_running(session_id, entry):
    """Replace the running line matching session+pid.

    If the session+pid already has any terminal line (killed/dead/done), the
    external terminal state wins and we do nothing; the running row being
    replaced is merged with the terminal entry (additive keys like log_path
    survive the swap); append only when the session+pid has no line at all
    (crash-before-write case).
    """
    try:
        with registry_log.locked_registry(REGISTRY_PATH):
            rows, torn = registry_log.read_registry_lines(REGISTRY_PATH)
            replaced = False
            exists = False
            lines = []
            for data in rows:
                if data.get("session") == session_id and data.get("pid") == entry["pid"]:
                    exists = True
                    if not replaced and data.get("status") == "running":
                        merged = dict(data)
                        merged.update(entry)
                        lines.append(json.dumps(merged) + "\n")
                        replaced = True
                        continue
                lines.append(json.dumps(data) + "\n")
            lines.extend(line + "\n" for line in torn)
            if exists and not replaced:
                return
            if not replaced:
                lines.append(json.dumps(entry) + "\n")
            registry_log.write_registry_lines(REGISTRY_PATH, lines)
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
        self.steered: list[str] = []
        self._frames_seen = 0
        self._last_beat = 0.0
        self._send_lock = threading.Lock()
        self.log_path: str | None = None
        self._log_path_stamped = False

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
                self._frames_seen += 1
                self._drain_steer()
                self._record_log_path()
                self._heartbeat_tick()

    # -- protocol ----------------------------------------------------------

    def _log_frame(self, frame: dict) -> None:
        """Append every frame to PI_RPC_FRAMELOG (jsonl) when set: diagnosis aid."""
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
        # pi_manage steer injections arrive on the reader thread while the
        # main thread may be mid-nudge; both write proc.stdin.
        with self._send_lock:
            assert self.proc is not None
            stdin = self.proc.stdin
            assert stdin is not None
            stdin.write(json.dumps(frame).encode("utf-8") + b"\n")
            stdin.flush()

    def _drain_steer(self) -> None:
        """Inject one pi_manage-steered message into the live run.

        Best-effort: a failed send still consumed the file, which is fine for
        a fire-and-forget channel whose next verification is the worker's own
        output. Lands when the next frame arrives; a fully silent worker
        picks it up on its first output.
        """
        msg = _take_steer(self.session_id)
        if not msg:
            return
        kind = "follow_up" if msg.get("queue") else "steer"
        text = str(msg.get("message") or "")
        try:
            self._send({"type": kind, "message": text})
            self.steered.append(text)
        except Exception:
            pass

    def _record_log_path(self) -> None:
        """Locate pi's real transcript once and stamp it into the running row.

        Discovery runs until the first hit; the stamp retries while the running
        row has no log_path yet, because the first frame can arrive before
        main() has written that row. Every step is best-effort so discovery can
        never break the dispatch. --no-session runs find nothing.
        """
        if self.no_session or not self.session_id:
            return
        if self.log_path is None:
            found = _discovered_log_path(self.agent_dir, self.session_id)
            if not found:
                return
            self.log_path = found
        if self._log_path_stamped:
            return
        if _registry_patch(self.session_id, os.getpid(), log_path=self.log_path):
            self._log_path_stamped = True

    def stamp_log_path_into_registry(self) -> None:
        """Bounded retry of the running-row stamp after main() wrote that row.

        The ready frame usually beats the running write, so the first
        _record_log_path call finds no row to patch; this gives it one short
        window to land instead of waiting for frames the worker may never send.
        """
        deadline = time.monotonic() + 2.0
        while not self._log_path_stamped and time.monotonic() < deadline:
            self._record_log_path()
            if self._log_path_stamped:
                return
            time.sleep(0.05)

    def _heartbeat_tick(self, force: bool = False) -> None:
        """Stamp liveness for pi_manage list; one write per 5 s at most.

        The stamp must survive the race where the first tick fires before
        main() creates ~/.hermes/logs: the beat time advances only AFTER a
        successful write, and the dir is ensured best-effort, so a failed
        early tick is retried on the next frame instead of gone for 5 s.
        """
        path = _heartbeat_file(self.session_id)
        if not path:
            return
        now = time.time()
        if not force and now - self._last_beat < 5.0:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(
                    {"ts": now, "frames": self._frames_seen, "tools": len(self.tool_calls)}, f
                )
            self._last_beat = now
        except OSError:
            pass

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
        # broad catch: a wide OSError AND a cwd-less client (TypeError from
        # os.path.join); a hash miss must never take the dispatch down
        try:
            with open(os.path.join(self.cwd, path), "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception:
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
        deliverable: bool = False,
    ) -> str:
        """Prompt, then verify that work happened; nudge in-session when it did not.

        A pi turn ends as soon as the model stops calling tools, so a confused
        worker returns with rc=0 having changed nothing. require_edit turns that
        into a detectable failure: the file hash must move, or the worker is told
        to act again in the same session.
        deliverable=True is the auditor shape: min_tool_calls drops to 0 (text
        output is the work) and min_tool_calls-style hash checks are skipped.
        """
        targets = [p.strip() for p in (require_edit or "").split(",") if p.strip()]
        before = {path: self._digest(path) for path in targets}
        deadline = time.monotonic() + WALL_TIMEOUT_S  # whole run: prompt + nudges
        out = self.prompt(text, timeout_s=timeout_s, deadline=deadline)
        while not self._satisfied(before, min_tool_calls):
            if deliverable:
                break  # text IS the work; no hash gate, no nudges
            if self.nudges >= max_nudges:
                break
            if time.monotonic() >= deadline:  # wall exhausted: no nudge retry
                break
            self.nudges += 1
            out += f"\n--- nudge {self.nudges} ---\n"
            out += self._nudge(self._nudge_text(targets), timeout_s, deadline)
        self.satisfied = (
            self._satisfied(before, min_tool_calls) if not deliverable else bool(targets)
        )
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
        "--require-sentinel",
        default=None,
        dest="require_sentinel",
        help=(
            "string that must appear in the require_edit file at exit; the client records "
            "deliverable: met|unmet|unchecked on the registry row"
        ),
    )
    parser.add_argument(
        "--auditor-deliverable",
        action="store_true",
        default=False,
        dest="auditor_deliverable",
        help=(
            "text-deliverable run: final assistant text is written into the first "
            "require_edit path at completion; no nudge loop"
        ),
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
    deliverable_paths = [p.strip() for p in (args.require_edit or "").split(",") if p.strip()]
    is_auditor = bool(args.auditor_deliverable)
    sentinel_path = deliverable_paths[0] if deliverable_paths else None
    stale_steer = _steer_file(args.session_id)
    if stale_steer:
        # a fresh dispatch must not inherit a steer message aimed at a dead run
        with contextlib.suppress(OSError):
            os.remove(stale_steer)
    client.start()
    agent_dir_value = args.agent_dir or "default"
    _registry_write(
        args.session_id or "unnamed",
        args.model,
        "running",
        agent_dir=agent_dir_value,
    )
    _launch_facts = {"cwd": args.cwd, "base": _launch_base_git(args.cwd)}
    _registry_patch(
        args.session_id or "unnamed",
        os.getpid(),
        **{k: v for k, v in _launch_facts.items() if v},
    )
    _lane_lock_acquire(args.session_id or "unnamed", args.cwd, args.model, os.getpid())
    status = "done"
    exit_code = 0
    try:
        # Inside the guarded region: a SIGTERM during the bounded stamp loop
        # must still reach the finally below, or the killed line never lands.
        client.stamp_log_path_into_registry()
        text = client.run(
            args.prompt,
            timeout_s=args.timeout_s or DEFAULT_TIMEOUT_S,
            require_edit=args.require_edit,
            min_tool_calls=args.min_tool_calls,
            max_nudges=args.max_nudges,
            deliverable=is_auditor,
        )
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        steered_note = f" steered={len(client.steered)}" if client.steered else ""
        sys.stderr.write(
            f"[pi] tool_calls={len(client.tool_calls)} nudges={client.nudges} "
            f"satisfied={client.satisfied}{steered_note}\n"
        )
        if is_auditor:
            wrote = _write_deliverable(deliverable_paths[0], text) if deliverable_paths else False
            if not wrote:
                # the text was the deliverable: a failed write means nothing landed
                status = "incomplete"
                exit_code = 3
                sys.stderr.write("[pi] auditor deliverable write failed\n")
        if not client.satisfied:
            status = "incomplete"
            exit_code = 3
    except TimeoutError:
        status = "timeout"
        exit_code = None
        raise
    except BaseException as exc:  # SystemExit (SIGTERM), KeyboardInterrupt = killed
        status = "killed"
        exit_code = _killed_exit_code(exc)
        raise
    finally:
        _registry_write(
            args.session_id or "unnamed",
            args.model,
            status,
            exit_code=exit_code,
            deliverable=_deliverable_verdict(sentinel_path, args.require_sentinel),
            require_sentinel=args.require_sentinel,
            agent_dir=args.agent_dir or "default",
            **_salvage_fields(status, args.session_id or "unnamed", args.cwd),
        )
        client.close()
        _lane_lock_release(args.session_id or "unnamed", args.cwd)
    return 0 if status == "done" else 3


def _salvage_fields(status, session_id, cwd):
    """Salvage registry fields for terminal-not-done exits (timeout, killed,
    incomplete); done exits record nothing. Best-effort by construction."""
    if status in ("done", "running"):
        return {}
    return _salvage_snapshot(session_id, cwd)


if __name__ == "__main__":

    def _on_sigterm(signum, frame):
        raise SystemExit(128 + signum)

    import signal

    signal.signal(signal.SIGTERM, _on_sigterm)
    sys.exit(main())

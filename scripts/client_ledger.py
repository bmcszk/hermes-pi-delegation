#!/usr/bin/env python3
"""Registry ledger and lane-lock mechanics for one dispatch.

Every function takes the ledger path explicitly: the caller owns the location
(so a test can point one run at a temporary ledger), this module owns the
append/replace/lock mechanics and never raises out of a best-effort write.
"""

import contextlib
import datetime
import json
import os
import subprocess
import tempfile

import registry_log


def patch_running(ledger_path, session_id, pid, **fields):
    """Merge fields into this session+pid's RUNNING line. Best-effort, never
    raises. True when a line changed, so the caller can retry a stamp that
    raced its row into existence."""
    try:
        with registry_log.locked_registry(ledger_path):
            rows, torn = registry_log.read_registry_lines(ledger_path)
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
                registry_log.write_registry_lines(ledger_path, lines)
    except Exception:
        pass


def write_entry(
    ledger_path,
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
    by salvage_snapshot on terminal-not-done exits (None = clean tree).
    agent_dir: the resolved --agent-dir value, or "default" when none passed.
    """
    try:
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
            replace_running(ledger_path, session_id, entry)
        else:
            registry_log.append_registry_line(ledger_path, json.dumps(entry))
    except Exception:
        pass


def replace_running(ledger_path, session_id, entry):
    """Replace the running line matching session+pid.

    If the session+pid already has any terminal line (killed/dead/done), the
    external terminal state wins and we do nothing; the running row being
    replaced is merged with the terminal entry (additive keys like log_path
    survive the swap); append only when the session+pid has no line at all
    (crash-before-write case).
    """
    try:
        with registry_log.locked_registry(ledger_path):
            rows, torn = registry_log.read_registry_lines(ledger_path)
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
            registry_log.write_registry_lines(ledger_path, lines)
    except Exception:
        pass


def launch_base_git(cwd):
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


def write_deliverable(path, text):
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


def deliverable_verdict(path, sentinel):
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


def _salvage_body(cwd, lines):
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
    return body


def salvage_snapshot(session_id, cwd):
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
    body = _salvage_body(cwd, lines)
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


def lane_lock_path(session_id):
    """Hold-point for one dispatch: <cwd>/.hermes/lane/LOCK/<session>.lock.

    The cwd is embedded WHERE the dispatch runs, so the helper cannot know it
    alone — callers pass the launch cwd. The dir lives under .hermes/lane,
    inside the .gitignore region.
    """
    if not session_id:
        return None
    return os.path.join(".hermes", "lane", "LOCK", f"{session_id}.lock")


def lane_lock_acquire(session_id, cwd, model, pid):
    """Atomically write this dispatch's lane lock. Best-effort, never raises."""
    rel = lane_lock_path(session_id)
    if not rel or not cwd:
        return
    path = os.path.join(cwd, rel)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
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


def lane_lock_release(session_id, cwd):
    """Remove this dispatch's own lock on any terminal exit. Best-effort."""
    rel = lane_lock_path(session_id)
    if not rel or not cwd:
        return
    with contextlib.suppress(OSError):
        os.remove(os.path.join(cwd, rel))

"""Session registry: the line-delimited ledger, liveness and lane locks.

The registry is the plugin's only shared state: workers append, the parent
reads. Everything here is a mechanism (I/O, locking, timing, process probes),
best-effort by construction: an unreadable ledger degrades to a named skip, a
missing lane lock to no row, and neither ever raises.
"""

import datetime
import json
import logging
import os
import re
import subprocess
import time

import registry_log

from . import plugin_runtime as runtime


def read_entries():
    """Return list of parsed registry entries. Best-effort, never raises.

    A torn line (a write that landed in halves, or a trailing write caught
    mid-append) is skipped with a warning instead of hiding every other row:
    one bad line must never cost the whole registry.
    """
    try:
        entries, torn = registry_log.read_registry(runtime.registry_path())
    except Exception as exc:  # a missing or unreadable file answers an empty list
        runtime.set_registry_warning(f"registry unreadable: {exc}")
        return []
    warning = None
    if torn:
        warning = (
            f"skipped {len(torn)} torn line(s) in {runtime.registry_path()}; "
            "run pi_manage(action='doctor') or _registry_repair() to quarantine them"
        )
        logging.warning(warning)
    runtime.set_registry_warning(warning)
    return entries


def repair_entries(path=None):
    """Quarantine torn registry lines into <registry>.corrupt and rewrite clean.

    The rewrite is atomic, so a reader never sees the intermediate file. Lines
    that do not parse are preserved verbatim in the .corrupt file: they are the
    only record of whatever tore them.
    """
    path = path or runtime.registry_path()
    with registry_log.locked_registry(path):
        rows, torn = registry_log.read_registry_lines(path)
        corrupt_path = f"{path}.corrupt"
        if torn:
            with open(corrupt_path, "a") as corrupt:
                corrupt.writelines(line + "\n" for line in torn)
            registry_log.write_registry_lines(path, [json.dumps(row) + "\n" for row in rows])
    return {"kept": len(rows), "quarantined": len(torn), "corrupt_path": corrupt_path}


def update_entry(session_id, pid, **fields):
    """Read-modify-write the registry line matching session+pid. Best-effort."""
    try:
        with registry_log.locked_registry(runtime.registry_path()):
            rows, torn = registry_log.read_registry_lines(runtime.registry_path())
            changed = False
            lines = []
            for data in rows:
                if data.get("session") == session_id and data.get("pid") == pid:
                    data.update(fields)
                    changed = True
                lines.append(json.dumps(data) + "\n")
            lines.extend(line + "\n" for line in torn)
            if changed:
                registry_log.write_registry_lines(runtime.registry_path(), lines)
    except Exception:
        pass


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def live_session(session_id):
    """Return pid of a live registry entry for session_id, else None."""
    for entry in read_entries():
        if entry.get("session") == session_id and entry.get("status") == "running":
            pid = entry.get("pid")
            try:
                os.kill(pid, 0)
                return pid
            except Exception:
                continue
    return None


def lane_lock_info(cwd, session_id):
    """Lane lock state for one session's cwd: None when absent, else present
    plus stale (pid dead = crash leftover) and the lock's payload."""
    if not cwd:
        return None
    path = os.path.join(cwd, ".hermes", "lane", "LOCK", f"{session_id}.lock")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        payload = {}
    pid = payload.get("pid")
    return {
        "present": True,
        "stale": bool(pid) and not pid_alive(pid),
        "path": path,
        "payload": payload,
    }


def newest_entry(session_id):
    """Latest registry entry for session_id (by started, then finished), or None.

    A pid-bearing row always outranks a pending-dispatch row for the same
    session: a real run must never be shadowed by an older intent row, even
    on timestamp skew.
    """
    newest = None
    newest_pid_row = None
    for entry in read_entries():
        if entry.get("session") != session_id:
            continue
        if entry.get("pid") and (
            newest_pid_row is None or entry_stamp(entry) >= entry_stamp(newest_pid_row)
        ):
            newest_pid_row = entry
        if newest is None or entry_stamp(entry) >= entry_stamp(newest):
            newest = entry
    return newest_pid_row or newest


def entry_stamp(entry):
    """Sort key for registry recency; ISO timestamps compare as strings."""
    return str(entry.get("started") or entry.get("finished") or "")


def wait_finished(entry):
    """True when the entry's pid is gone; the registry status alone never settles it
    (a killed line can still carry a live pid). A gone pid settles a run even when
    the exit status write never landed. A pending-dispatch intent row is never
    finished: it has no pid to lose.
    """
    if entry.get("status") == "pending-dispatch":
        return False
    pid = entry.get("pid")
    return not (bool(pid) and pid_alive(pid))


def steer_file(session_id):
    """Control file for a live worker; client consumes it on the next frame.

    Must match scripts/pi_rpc_client.py::steer_file.
    """
    return os.path.join(os.path.dirname(runtime.registry_path()), "pi-steer", f"{session_id}.json")


def heartbeat_age(session_id):
    """Seconds since the worker last stamped its heartbeat file, or None.

    Read side of the stamp the client writes (must match
    scripts/pi_rpc_client.py::_heartbeat_file). A running pid with a large
    age is a stalled or silent worker; the caller decides what to do.
    """
    path = os.path.join(os.path.dirname(runtime.registry_path()), f"pi-heartbeat-{session_id}.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return round(time.time() - float(data.get("ts") or 0))
    except (OSError, ValueError, TypeError):
        return None


def steer_session(session_id, message, queue):
    """Queue a steer/follow_up message for a live worker via its control file."""
    if not session_id:
        return {"error": "session_id required for steer"}
    if not message:
        return {"error": "message required for steer"}
    entry = newest_entry(session_id)
    if entry is None:
        return {"error": f"Session {session_id} not found in registry"}
    pid = entry.get("pid")
    try:
        os.kill(pid, 0)
    except Exception:
        update_entry(session_id, pid, status="dead", finished=now_iso())
        return {"error": f"session {session_id} is not running (pid {pid} gone)"}
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
        if "pi_rpc_client" not in cmdline:
            return {"error": f"pid {pid} is not a pi_rpc_client process; refusing"}
    except Exception:
        return {"error": f"cannot verify cmdline of pid {pid}"}
    path = steer_file(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"message": message, "queue": bool(queue), "ts": now_iso()}, f)
        os.replace(tmp, path)  # atomic: the client never reads a partial file
    except OSError as exc:
        return {"error": f"cannot write steer file: {exc}"}
    kind = "follow_up" if queue else "steer"
    return {"status": f"{kind} queued for {session_id}", "path": path}


def pending_row(session_id):
    """The unresolved pending-dispatch row for session_id, or None."""
    for entry in read_entries():
        if entry.get("session") == session_id and entry.get("status") == "pending-dispatch":
            return entry
    return None


def record_pending_dispatch(session_id, cwd, mode):
    """Write the launch intent row, replacing any unresolved pending row for the
    same session (single-pending invariant). Best-effort: False on failure."""
    row = {
        "session": session_id,
        "status": "pending-dispatch",
        "pid": None,
        "mode": mode,
        "cwd": cwd,
        "started": now_iso(),
    }
    try:
        with registry_log.locked_registry(runtime.registry_path()):
            rows, torn = registry_log.read_registry_lines(runtime.registry_path())
            kept = []
            for data in rows:
                if data.get("session") == session_id and data.get("status") == "pending-dispatch":
                    continue
                kept.append(json.dumps(data) + "\n")
            kept.append(json.dumps(row) + "\n")
            kept.extend(t + "\n" for t in torn)
            registry_log.write_registry_lines(runtime.registry_path(), kept)
        return True
    except Exception:
        return False


def update_pending_dispatch(session_id, pid, log_path):
    """Stamp the spawned bash wrapper's pid and log path onto the pending row.
    Best-effort: never fails the dispatch."""
    try:
        with registry_log.locked_registry(runtime.registry_path()):
            rows, torn = registry_log.read_registry_lines(runtime.registry_path())
            changed = False
            lines = []
            for data in rows:
                if data.get("session") == session_id and data.get("status") == "pending-dispatch":
                    data["pid"] = pid
                    data["log_path"] = log_path
                    changed = True
                lines.append(json.dumps(data) + "\n")
            lines.extend(t + "\n" for t in torn)
            if changed:
                registry_log.write_registry_lines(runtime.registry_path(), lines)
    except Exception:
        pass


def now_iso():
    import datetime

    return datetime.datetime.now().isoformat()


def row_age_s(entry):
    """Seconds since the row's started stamp, best-effort; 0 when unreadable."""
    try:
        started = datetime.datetime.fromisoformat(entry.get("started"))
        return max(0, round((datetime.datetime.now() - started).total_seconds()))
    except (TypeError, ValueError):
        return 0


def git_readonly(cwd, *args):
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=30)
        return proc.returncode == 0, (proc.stdout if proc.returncode == 0 else proc.stderr)
    except Exception as exc:
        return False, str(exc)


def base_is_valid(cwd, base):
    return bool(base) and bool(git_readonly(cwd, "cat-file", "-e", f"{base}^{{commit}}")[0])


def client_pid_pattern(session_id):
    """pgrep -f regex matching this session's client command, boundary-anchored
    so task-foo-coder never matches task-foo-coder2."""
    escaped = re.escape(session_id)
    return rf"pi_rpc_client\.py --session-id {escaped}( |$)"


def client_pids(session_id):
    """Live pids running this session's pi_rpc_client, per pgrep -f.

    A missing or unreadable pgrep degrades to no-hit (the caller then gives
    the vaguer hint) instead of raising.
    """
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "--", client_pid_pattern(session_id)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    return [int(pid) for pid in proc.stdout.split() if pid.isdigit()]

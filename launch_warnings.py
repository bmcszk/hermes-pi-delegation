"""Launch warnings: facts about the world a dispatch is about to enter.

Warnings, never blocks: a reused session id, another live worker on the same
directory or agent dir, or a crash-leftover lane lock. Naming them before the
spawn is the difference between a diagnosable run and a mystery.
"""

import glob
import os

from .session_registry import entry_stamp, lane_lock_info, pid_alive, read_entries


def concurrency_warnings(session_id, cwd, agent_dir):
    """Registry-fact warnings for a dispatch about to launch: session-id reuse
    (pi resumes that session's context), another live worker on the same
    cwd (shared registry/transcript state), and another live worker on the
    same agent_dir (shared registry rewrite, heartbeat/steer namespace,
    transcript discovery). Warnings, never blocks."""
    warnings = []
    prior = None
    for entry in read_entries():
        if entry.get("session") == session_id and entry.get("status") not in (
            "running",
            "pending-dispatch",
        ):
            if prior is None or entry_stamp(entry) >= entry_stamp(prior):
                prior = entry
        elif (
            entry.get("cwd") == cwd
            and entry.get("status") == "running"
            and entry.get("session") != session_id
            and pid_alive(entry.get("pid"))
        ):
            warnings.append(
                f"preflight warn: session {entry.get('session')} (pid {entry.get('pid')}) is "
                "already running in this directory - one worker per project directory at a "
                "time; concurrent runs share registry and transcript state"
            )
        elif (
            entry.get("status") == "running"
            and pid_alive(entry.get("pid"))
            and entry.get("agent_dir")
            and entry.get("agent_dir") == (agent_dir or "default")
            and entry.get("session") != session_id
        ):
            warnings.append(
                f"preflight warn: session {entry.get('session')} (pid {entry.get('pid')}) "
                f"is running with the same agent dir ({entry.get('agent_dir')}); "
                "registry, heartbeat and transcript state are shared"
            )
    if prior is not None:
        when = prior.get("finished") or prior.get("started") or "an unknown time"
        warnings.append(
            f"preflight warn: session {session_id} has prior finished runs (last status "
            f"{prior.get('status')!r} at {when}); pi will resume that session's context - "
            "pass epic_id/task_slug for a fresh id"
        )
    return warnings


def stale_lane_lock_warnings(cwd):
    """Stale-lock visibility at launch: glob-scan <cwd>/.hermes/lane/LOCK for
    locks whose payload pid is dead and name the path, owning session, and the
    exact rm command (same wording salvage_report uses). The launch itself
    removes nothing - removal stays a human decision."""
    warnings = []
    lock_dir = os.path.join(cwd, ".hermes", "lane", "LOCK")
    try:
        lock_files = sorted(glob.glob(os.path.join(lock_dir, "*.lock")))
    except Exception:
        return warnings
    for path in lock_files:
        info = lane_lock_info(cwd, os.path.splitext(os.path.basename(path))[0])
        if info is None or not info["stale"]:
            continue
        owner = info["payload"].get("session_id") or "unknown session"
        warnings.append(
            f"preflight warn: stale lane lock in this directory (crash leftover from "
            f"{owner}): {path} - remove it with: rm {path}"
        )
    return warnings

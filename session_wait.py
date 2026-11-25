"""Wait state machine: whether a launch settled, stalled, or never started.

The parent polls through this module only. Every answer derives from the
registry row plus a live-process probe, so a session the ledger knows about
never reads as "unknown" - that answer is reserved for an id with no rows and
no process at all.
"""

import os

from . import plugin_runtime as runtime
from .plugin_constants import DISPATCH_GRACE_S
from .session_registry import (
    base_is_valid,
    git_readonly,
    heartbeat_age,
    newest_entry,
    pid_alive,
    row_age_s,
    wait_finished,
)


def tree_summary(cwd, base):
    """Read-only tree facts for one row's cwd: dirty count, commits since the
    row's launch base, unpushed count. Budget-bounded and best-effort: any leg
    that cannot be established degrades to a named skip instead of raising, so
    an answer that cannot read the tree never reads as "clean"."""
    if not cwd or not os.path.isdir(cwd) or not git_readonly(cwd, "status")[0]:
        return {"skipped": "no recorded cwd" if not cwd else f"cwd is not a git repo: {cwd}"}
    tree = {}
    ok, out = git_readonly(cwd, "status", "--porcelain")
    tree["dirty"] = len(out.splitlines()) if ok else {"skipped": out}
    if base_is_valid(cwd, base):
        ok, out = git_readonly(cwd, "rev-list", "--count", f"{base}..HEAD")
        tree["commits_since_base"] = int(out) if ok else {"skipped": out}
    else:
        tree["commits_since_base"] = {
            "skipped": "no base recorded for this row" if not base else "unknown revision"
        }
    ok_up, up_out = git_readonly(cwd, "rev-parse", "--abbrev-ref", "@{upstream}")
    if ok_up:
        ok, out = git_readonly(cwd, "rev-list", "--count", f"{up_out.strip()}..HEAD")
        tree["unpushed"] = int(out) if ok else {"skipped": out}
    else:
        tree["unpushed"] = {"skipped": "no upstream configured"}
    return tree


def wait_result(session_id, entry):
    """Derived wait answer for one registry row: running/terminal/last_activity_s.

    A pending-dispatch intent row surfaces its age as pending_age_s and is
    never derived into a terminal answer. unknown is reserved for a session
    with no registry rows at all; a known row always yields its derived
    fields, even when a concurrent whole-file rewrite makes one read land
    partial.
    """
    if entry.get("status") == "pending-dispatch":
        return {
            "finished": False,
            "status": "pending-dispatch",
            "pid": None,
            "pending_age_s": row_age_s(entry),
        }
    status = entry.get("status") or "running"
    pid = entry.get("pid")
    result = {"finished": wait_finished(entry), "status": status, "pid": pid}
    if not result["finished"] and pid_alive(pid):
        age = heartbeat_age(session_id)
        if age is not None:
            result["last_activity_s"] = age
    if entry.get("exit_code") is not None:
        result["exit_code"] = entry["exit_code"]
    if entry.get("deliverable") is not None:
        result["deliverable"] = entry["deliverable"]
    if result["finished"]:
        result["tree"] = tree_summary(entry.get("cwd"), entry.get("base"))
        status = entry.get("status") or "running"
        if status in ("incomplete", "timeout", "killed"):
            result["recovery"] = f"pi_manage(action='salvage_report', session_id='{session_id}')"
    return result


def wait_for_completion(session_id, budget, poll):
    """Probe the registry until the session settles or the budget runs out.

    Never answers unknown for a registered session: unknown comes only from
    the caller-side probe (no registry rows for this session at all), never
    from inside the loop. An aged pending row and a row-less session classify
    immediately via pgrep instead of burning the remaining budget.
    """
    clock = runtime.clock()
    deadline = clock.monotonic() + budget
    while True:
        entry = newest_entry(session_id)
        if entry is None:
            return classify_unregistered(session_id)
        result = wait_result(session_id, entry)
        if result["finished"] or deadline - clock.monotonic() < poll:
            return result
        if entry.get("status") == "pending-dispatch" and row_age_s(entry) >= DISPATCH_GRACE_S:
            return classify_aged_pending(session_id, entry)
        clock.sleep(poll)


def classify_unregistered(session_id):
    """Immediate wait answer for a session with no registry rows at all."""
    pids = runtime.client_pids(session_id)
    if pids:
        return {
            "finished": False,
            "status": "unregistered-client",
            "pid": pids[0],
            "hint": (
                "worker process alive but never registered; check "
                f"pi_manage(action='logs', session_id='{session_id}') or kill"
            ),
        }
    return {
        "finished": False,
        "status": "unknown",
        "hint": "no launch record and no process for this id - check session_id",
    }


def classify_aged_pending(session_id, entry):
    """Immediate wait answer for a pending-dispatch row past its grace."""
    age = row_age_s(entry)
    pids = runtime.client_pids(session_id)
    if pids:
        return {
            "finished": False,
            "status": "unregistered-client",
            "pid": pids[0],
            "hint": (
                "worker process alive but never registered; check "
                f"pi_manage(action='logs', session_id='{session_id}') or kill"
            ),
        }
    return {
        "finished": False,
        "status": "never-dispatched",
        "command_age_s": age,
        "hint": (
            f"command issued {age}s ago, no process found: run the returned command "
            f"in background, or pi_launch(auto=true); "
            f"pi_manage(action='kill', session_id='{session_id}') clears the pending row"
        ),
    }

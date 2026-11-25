"""pi_manage: one intent function per action, one mechanism per stage.

The actions are the business vocabulary the parent calls (list, wait, kill,
verify, doctor, logs, steer, salvage_report). Each intent function reads as the
decision sequence for its action; the pid, ledger and tree mechanics live in
the session modules this one calls.
"""

import os
import signal

from . import plugin_runtime as runtime
from .plugin_constants import WAIT_CEILING_S
from .plugin_health import doctor
from .session_registry import (
    heartbeat_age,
    lane_lock_info,
    now_iso,
    pid_alive,
    row_age_s,
    steer_session,
)
from .session_report import salvage_report, session_logs, verify_session
from .session_wait import tree_summary, wait_finished, wait_for_completion


def pi_manage_impl(
    action,
    session_id=None,
    only_running=False,
    lines=40,
    timeout_s=None,
    poll_s=None,
    message=None,
    queue=False,
    go_tests=False,
    **_,
):
    """Answer one pi_manage call; an unrecognised action answers nothing."""
    if action == "steer":
        return steer_session(session_id, message or "", queue)
    if action == "wait":
        return wait_answer(session_id, timeout_s, poll_s)
    if action == "verify":
        return verify_session(session_id)
    if action == "doctor":
        return doctor()
    if action == "logs":
        return session_logs(session_id, lines)
    if action == "salvage_report":
        return salvage_report(session_id, lines, go_tests)
    if action == "list":
        return list_agents(only_running)
    if action == "kill":
        return kill_session(session_id)
    return None


def wait_answer(session_id, timeout_s, poll_s):
    """Clamp the requested budget to the plugin ceiling, then probe."""
    if not session_id:
        return {"error": "session_id required for wait"}
    requested = max(0, int(timeout_s or 0))
    budget = min(requested, WAIT_CEILING_S)
    poll = max(1, int(poll_s or 1))
    result = wait_for_completion(session_id, budget, poll)
    result["requested_timeout_s"] = requested
    result["timeout_s"] = budget
    result["clamped"] = budget < requested
    if result["clamped"] and not result["finished"]:
        result["resumed_by"] = "call-wait-again"
    return result


def list_agents(only_running):
    """Every registry row with its live facts: heartbeat age, lane lock, tree."""
    agents = runtime.read_registry()
    if only_running:
        agents = [e for e in agents if e.get("status") == "running" and pid_alive(e.get("pid"))]
    for entry in agents:
        if entry.get("status") != "running" or not pid_alive(entry.get("pid")):
            continue
        age = heartbeat_age(entry.get("session"))
        if age is not None:
            entry["last_activity_s"] = age
    # every registered entry surfaces its lane lock when one sits on disk: a
    # stale lock (pid dead, crash leftover) belongs on the DEAD row too, which
    # is exactly where the crashed worker's row is still running.
    for agent in agents:
        if agent.get("status") == "pending-dispatch":
            agent["pending_age_s"] = row_age_s(agent)
        lock = lane_lock_info(agent.get("cwd"), agent.get("session"))
        if lock is not None:
            agent["lane_lock"] = {"present": True, "stale": lock["stale"]}
        if wait_finished(agent):
            agent["tree"] = tree_summary(agent.get("cwd"), agent.get("base"))
    agents.sort(key=lambda e: e.get("started", ""), reverse=True)
    return {"agents": agents}


def kill_session(session_id):
    """Terminate one session's worker, verifying the pid before signalling.

    Steps run in the order that keeps the most specific fact first: a live
    registered row, then an unresolved intent row, then an already-dead row,
    then a process that never registered.
    """
    if not session_id:
        return {"error": "session_id required for kill"}
    attempt = {"session_id": session_id, "dead_pid": None, "pending": False}
    for step in (
        _kill_registered_row,
        _kill_pending_dispatch,
        _kill_already_dead,
        _kill_unregistered_client,
    ):
        answer = step(attempt)
        if answer is not None:
            return answer
    return {
        "error": (
            f"Session {session_id} not found in registry and no process found: "
            "run the returned launch command in background"
        )
    }


def _kill_registered_row(attempt):
    """Signal the session's live registered pid; note dead and pending rows."""
    session_id = attempt["session_id"]
    for entry in runtime.read_registry():
        if entry.get("session") != session_id:
            continue
        if entry.get("status") == "pending-dispatch":
            attempt["pending"] = True
            continue
        pid = entry.get("pid")
        try:
            os.kill(pid, 0)
        except Exception:
            runtime.registry_update(session_id, pid, status="dead", finished=now_iso())
            attempt["dead_pid"] = pid
            continue
        refusal = _client_cmdline_refusal(pid, session_id, must_name_session=False)
        if refusal is not None:
            return refusal
        os.killpg(pid, signal.SIGTERM)  # client AND its pi + tool children
        runtime.registry_update(session_id, pid, status="killed", exit_code=143, finished=now_iso())
        return {
            "status": f"SIGTERM sent to pgid {pid} ({session_id})",
            "recovery": f"pi_manage(action='salvage_report', session_id='{session_id}')",
        }
    return None


def _kill_pending_dispatch(attempt):
    """Cancel or signal a dispatch that never wrote a pid of its own."""
    session_id = attempt["session_id"]
    if not attempt["pending"]:
        return None
    live = runtime.client_pids(session_id)
    if not live:
        runtime.registry_update(session_id, None, status="aborted", finished=now_iso())
        return {"status": f"pending dispatch cancelled for {session_id}"}
    pid = live[0]
    refusal = _client_cmdline_refusal(pid, session_id, must_name_session=True)
    if refusal is not None:
        return refusal
    os.killpg(pid, signal.SIGTERM)
    runtime.registry_update(session_id, None, status="killed", exit_code=143, finished=now_iso())
    return {
        "status": f"SIGTERM sent to pgid {pid} (unregistered client, {session_id})",
        "recovery": f"pi_manage(action='salvage_report', session_id='{session_id}')",
    }


def _kill_already_dead(attempt):
    """Report a session whose row was just reaped, so the caller stops waiting."""
    if attempt["dead_pid"] is None:
        return None
    session_id = attempt["session_id"]
    return {
        "status": (f"session {session_id} already dead (pid {attempt['dead_pid']}, marked dead)"),
        "recovery": f"pi_manage(action='salvage_report', session_id='{session_id}')",
    }


def _kill_unregistered_client(attempt):
    """Signal a client process that is alive but has no registry row."""
    session_id = attempt["session_id"]
    pids = runtime.client_pids(session_id)
    if not pids:
        return None
    pid = pids[0]
    refusal = _client_cmdline_refusal(pid, session_id, must_name_session=True)
    if refusal is not None:
        return refusal
    os.killpg(pid, signal.SIGTERM)
    return {
        "status": f"SIGTERM sent to pgid {pid} (unregistered client, {session_id})",
        "recovery": f"pi_manage(action='salvage_report', session_id='{session_id}')",
    }


def _client_cmdline_refusal(pid, session_id, must_name_session):
    """Refuse a pid that is not this plugin's client; None when it is safe."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except Exception:
        return {"error": f"cannot verify cmdline of pid {pid}"}
    if "pi_rpc_client" not in cmdline:
        return {"error": f"pid {pid} is not a pi_rpc_client process; refusing to signal"}
    if must_name_session and session_id not in cmdline:
        return {"error": f"pid {pid} is not a pi_rpc_client process; refusing to signal"}
    return None

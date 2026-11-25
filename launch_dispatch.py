"""Dispatch: turn a launch request into a running worker, or a named refusal.

`pi_launch_impl` is the intent function and reads as the business sequence:
validate the request, resolve its skills, refuse a double run, preflight the
inputs, compose the command, record the intent, then spawn or echo. The stages
below it hold the mechanics.
"""

import os
import subprocess

from . import plugin_runtime as runtime
from .launch_command import LaunchRefusedError, build_launch_command
from .launch_preflight import preflight, session_id_for
from .launch_warnings import concurrency_warnings, stale_lane_lock_warnings
from .plugin_constants import _UNSET, WORKER_TYPES
from .session_registry import live_session, update_pending_dispatch
from .skill_discovery import resolve_skill_name, skill_discoverable


def pi_launch_impl(
    contract_path,
    cwd,
    worker_type="default",
    skills=None,
    model=None,
    epic_id=None,
    task_slug=None,
    mode="standard",
    require_edit=None,
    min_tool_calls=_UNSET,
    max_nudges=_UNSET,
    timeout_s=None,
    wall_timeout_s=None,
    require_sentinel=None,
    create_anchor=False,
    auto=True,
    **_,
):
    """Dispatch a pi worker as a detached background process.

    The default launch (auto absent, auto true) spawns the worker and returns
    dispatched/pid/session_id/log; the answer carries no command string.

    auto=false is the explicit diagnostic echo: it returns the equivalent
    command and mode "echo-only", spawns nothing, and carries no wait hint -
    never wait on the session an echo-only answer names.

    cwd is the required absolute project dir: pi is project-scoped (skill
    discovery and relative paths resolve from it).

    model is an optional per-dispatch worker model alias; omitted, the command
    carries no --model at all and pi uses its own defaultModel. The plugin
    carries no model names of its own.

    session_id = <epic-id>-<worker-type> when epic_id given,
                 task-<task_slug>-<worker-type> when task_slug given,
                 task-<basename of cwd>-<worker-type> when neither: the same
                 cwd with the same worker_type reuses that session id, and pi
                 reuses the session state behind it.

    Non-empty skills -> returns "system_prompt" (compliance block) and the
    command carries --append-system-prompt. Missing/empty skills -> neither.
    """
    request = {
        "contract_path": contract_path,
        "cwd": cwd,
        "worker_type": worker_type,
        "skills": skills,
        "model": model,
        "epic_id": epic_id,
        "task_slug": task_slug,
        "mode": mode,
        "require_edit": require_edit,
        "min_tool_calls": min_tool_calls,
        "max_nudges": max_nudges,
        "timeout_s": timeout_s,
        "wall_timeout_s": wall_timeout_s,
        "require_sentinel": require_sentinel,
        "create_anchor": create_anchor,
        "auto": auto,
    }
    refusal = _refuse_bad_request(request)
    if refusal is not None:
        return refusal
    session_id = session_id_for(request["cwd"], worker_type, epic_id, task_slug)
    resolved_skills, skill_warnings, skill_failures = _resolve_skills(
        request["skills"], request["cwd"]
    )
    live_pid = live_session(session_id)
    if live_pid is not None:
        return {"error": f"session {session_id} already running (pid {live_pid})"}
    failures, warnings = _preflight_checks(request, resolved_skills)
    failures += skill_failures
    if failures:
        return {"error": "preflight: " + "; ".join(failures)}
    plan = _launch_plan(request, session_id, resolved_skills)
    try:
        command, system_prompt, command_warnings = build_launch_command(plan)
    except LaunchRefusedError as refusal_exc:
        return {"error": str(refusal_exc)}
    warnings = (
        warnings
        + skill_warnings
        + concurrency_warnings(session_id, request["cwd"], plan["agent_dir"])
        + command_warnings
        + stale_lane_lock_warnings(request["cwd"])
    )
    result = {"session_id": session_id, "worker_type": worker_type, "mode": request["mode"]}
    if request["require_sentinel"]:
        result["require_sentinel"] = request["require_sentinel"]
    pending_mode = "auto" if request["auto"] else "manual"
    if not runtime.record_pending_dispatch(session_id, request["cwd"], pending_mode):
        warnings.append(
            "preflight warn: could not record the pending-dispatch intent row; "
            "wait cannot detect a never-dispatched command for this session"
        )
    if request["auto"]:
        return dispatch_command(command, request["cwd"], session_id)
    return echo_answer(result, command, system_prompt, warnings)


def _preflight_checks(request, resolved_skills):
    """Run the input checks for one request; (failures, warnings)."""
    return preflight(
        request["cwd"],
        request["contract_path"],
        request["require_edit"],
        request["create_anchor"],
        resolved_skills,
        request["timeout_s"],
        request["wall_timeout_s"],
        request["min_tool_calls"],
        request["max_nudges"],
        request["worker_type"],
        request["model"],
        request["epic_id"],
        request["task_slug"],
        request["require_sentinel"],
    )


def _refuse_bad_request(request):
    """Named refusals for a request that cannot be honoured at all."""
    worker_type = request["worker_type"]
    cwd = request["cwd"]
    if worker_type not in WORKER_TYPES:
        return {"error": f"Unknown worker_type: {worker_type}. Valid: {list(WORKER_TYPES.keys())}"}
    if request["mode"] == "legacy":
        return {
            "error": "legacy mode removed: it ran workers with -ns (skills "
            "disabled); use the default standard mode"
        }
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return {"error": f"cwd must be an existing directory (absolute project dir), got: {cwd!r}"}
    return None


def _resolve_skills(skills, cwd):
    """(resolved paths, discovery warnings, unresolvable entries) for one launch."""
    resolved, warnings, failures = [], [], []
    for skill in skills or []:
        expanded = os.path.expanduser(skill)
        if os.path.exists(expanded):
            resolved_path = expanded
        else:
            found = resolve_skill_name(skill, cwd)
            if found is None:
                failures.append(f"skills - not resolved: {skill}")
                continue
            resolved_path = found
        if not skill_discoverable(resolved_path, cwd):
            warnings.append(
                f"preflight warn: {skill} is outside pi skill discovery "
                "(.agents/.pi under cwd ancestors); worker reads it only via the absolute "
                "path, so the contract must quote this exact path"
            )
        resolved.append(resolved_path)
    return resolved, warnings, failures


def _launch_plan(request, session_id, resolved_skills):
    """The resolved facts the command builder needs, in one record."""
    agent_dir = runtime.worker_agent_dir()
    return {
        "client_path": runtime.client_path(),
        "session_id": session_id,
        "cwd": request["cwd"],
        "contract_path": request["contract_path"],
        "model": request["model"],
        "skills": resolved_skills,
        "worker_type": request["worker_type"],
        "worker_config": WORKER_TYPES[request["worker_type"]],
        "require_edit": request["require_edit"],
        "min_tool_calls": request["min_tool_calls"],
        "max_nudges": request["max_nudges"],
        "timeout_s": request["timeout_s"],
        "wall_timeout_s": request["wall_timeout_s"],
        "require_sentinel": request["require_sentinel"],
        "agent_dir": agent_dir if os.path.isdir(agent_dir) else "default",
    }


def dispatch_command(command, cwd, session_id):
    """The default launch: run the generated one-liner detached, log to the lane dir.

    bash -c receives the EXACT emitted command (preflight echo + budget echo
    + client) with stdout/stderr into <cwd>/.hermes/lane/<session_id>.log;
    the redirect keeps the parent's stdout clean. start_new_session detaches
    the worker's process group so the kill path (client pid pgid) stays exact
    and a gateway restart cannot take the worker down. Best-effort: a spawn
    failure returns the error shape instead of raising.
    """
    log_dir = os.path.join(cwd, ".hermes", "lane")
    log_path = os.path.join(log_dir, f"{session_id}.log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(log_path, "w") as log:
            proc = subprocess.Popen(
                ["bash", "-c", command],
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                start_new_session=True,
            )
    except OSError as exc:
        return {"error": f"$dispatch failed to spawn: {exc}", "log": log_path}
    update_pending_dispatch(session_id, proc.pid, log_path)
    return {
        "dispatched": True,
        "pid": proc.pid,
        "session_id": session_id,
        "log": log_path,
        "next": f"pi_manage(action='wait', session_id='{session_id}', timeout_s<=1500)",
    }


def echo_answer(result, command, system_prompt, warnings):
    """auto=false: the diagnostic printout, named as such and carrying no wait
    hint - nothing was spawned, so no session exists to wait on."""
    result["mode"] = "echo-only"
    result["command"] = command
    if system_prompt is not None:
        result["system_prompt"] = system_prompt
    if warnings:
        result["warnings"] = warnings
    return result

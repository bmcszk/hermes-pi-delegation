import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time

import registry_log

# Constants
REGISTRY_PATH = os.path.expanduser("~/.hermes/logs/pi-agents.jsonl")
CLIENT_PATH = os.path.join(os.path.dirname(__file__), "scripts", "pi_rpc_client.py")
# Isolated pi agent dir for workers: same providers/auth as ~/.pi/agent but
# without npm:pi-subagents and with a no-delegation AGENTS.md.
WORKER_AGENT_DIR = os.path.join(os.path.expanduser("~"), ".pi", "agent-worker")

# Worker tool policy. default is a passthrough: no tool/codemode/thinking
# policy attached. reasoner is read-only. brainer may write docs and task
# files (write/edit) but has no shell. coder/auditor get codemode.
# No model key: the model is an optional per-dispatch argument, and pi falls
# back to its own defaultModel when the caller passes none.
WORKER_TYPES = {
    "default": {"tools": None, "use_codemode": False, "thinking": None},
    "reasoner": {"tools": "read,grep,find,ls", "use_codemode": False, "thinking": "high"},
    "brainer": {
        "tools": "read,grep,find,ls,write,edit",
        "use_codemode": False,
        "thinking": "high",
    },
    "coder": {"tools": None, "use_codemode": True, "thinking": None},
    # text-deliverable review jobs: writes its report to the require_edit
    # deliverable file; no shell, no codemode. nudge gate off by default.
    "auditor": {
        "tools": "read,grep,find,ls,write,edit",
        "use_codemode": False,
        "thinking": None,
        "text_deliverable": True,
    },
}

# Parent-side git-mutation deny: only when the command targets a git binary
# (word boundary before "git"), followed by a mutating subcommand.
GIT_MUTATION_RE = re.compile(
    r"\bgit\s+(stash|reset|checkout|switch|restore|merge|rebase|revert|clean|"
    r"branch\s+-[dDmM]|push\s+--force|push\s+-f)\b"
)

TOOLSETS = {
    "pi_manage": "pi-delegation",
    "pi_launch": "pi-delegation",
}

# Prompt budget handed to the worker. The client default (120 s) is far too
# short for a real sweep: workers were being killed mid-contract.
WORKER_TIMEOUT_S = int(os.environ.get("PI_WORKER_TIMEOUT_S", "900"))
# wait clamps its own budget below the host's 1600 s tool-call ceiling: a wait
# that rides that limit loses the whole turn instead of answering.
WAIT_CEILING_S = 1500
_UNSET = object()  # distinguishes "caller passed it" from the signature default

# why the last registry read skipped a line; the next read resets it
_last_registry_warning = None


def _read_registry():
    """Return list of parsed registry entries. Best-effort, never raises.

    A torn line (a write that landed in halves, or a trailing write caught
    mid-append) is skipped with a warning instead of hiding every other row:
    one bad line must never cost the whole registry.
    """
    global _last_registry_warning
    _last_registry_warning = None
    try:
        entries, torn = registry_log.read_registry(REGISTRY_PATH)
    except Exception as exc:  # a missing or unreadable file answers an empty list
        _last_registry_warning = f"registry unreadable: {exc}"
        return []
    if torn:
        _last_registry_warning = (
            f"skipped {len(torn)} torn line(s) in {REGISTRY_PATH}; "
            f"run pi_manage(action='doctor') or _registry_repair() to quarantine them"
        )
        logging.warning(_last_registry_warning)
    return entries


def _registry_repair(path=None):
    """Quarantine torn registry lines into <registry>.corrupt and rewrite clean.

    The rewrite is atomic, so a reader never sees the intermediate file. Lines
    that do not parse are preserved verbatim in the .corrupt file: they are the
    only record of whatever tore them.
    """
    path = path or REGISTRY_PATH
    with registry_log.locked_registry(path):
        rows, torn = registry_log.read_registry_lines(path)
        corrupt_path = f"{path}.corrupt"
        if torn:
            with open(corrupt_path, "a") as corrupt:
                corrupt.writelines(line + "\n" for line in torn)
            registry_log.write_registry_lines(path, [json.dumps(row) + "\n" for row in rows])
    return {"kept": len(rows), "quarantined": len(torn), "corrupt_path": corrupt_path}


def _registry_update(session_id, pid, **fields):
    """Read-modify-write the registry line matching session+pid. Best-effort."""
    try:
        with registry_log.locked_registry(REGISTRY_PATH):
            rows, torn = registry_log.read_registry_lines(REGISTRY_PATH)
            changed = False
            lines = []
            for data in rows:
                if data.get("session") == session_id and data.get("pid") == pid:
                    data.update(fields)
                    changed = True
                lines.append(json.dumps(data) + "\n")
            lines.extend(line + "\n" for line in torn)
            if changed:
                registry_log.write_registry_lines(REGISTRY_PATH, lines)
    except Exception:
        pass


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _live_session(session_id):
    """Return pid of a live registry entry for session_id, else None."""
    for entry in _read_registry():
        if entry.get("session") == session_id and entry.get("status") == "running":
            pid = entry.get("pid")
            try:
                os.kill(pid, 0)
                return pid
            except Exception:
                continue
    return None


def _lane_lock_info(cwd, session_id):
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
        "stale": bool(pid) and not _pid_alive(pid),
        "path": path,
        "payload": payload,
    }


def _newest_registry_entry(session_id):
    """Latest registry entry for session_id (by started, then finished), or None."""
    newest = None
    for entry in _read_registry():
        if entry.get("session") != session_id:
            continue
        if newest is None or _entry_stamp(entry) >= _entry_stamp(newest):
            newest = entry
    return newest


def _entry_stamp(entry):
    """Sort key for registry recency; ISO timestamps compare as strings."""
    return str(entry.get("started") or entry.get("finished") or "")


def _wait_finished(entry):
    """True when the entry's pid is gone; the registry status alone never settles it
    (a killed line can still carry a live pid). A gone pid settles a run even when
    the exit status write never landed.
    """
    pid = entry.get("pid")
    return not (bool(pid) and _pid_alive(pid))


def _steer_file(session_id):
    """Control file for a live worker; client consumes it on the next frame.

    Must match scripts/pi_rpc_client.py::_steer_file.
    """
    return os.path.join(os.path.dirname(REGISTRY_PATH), "pi-steer", f"{session_id}.json")


def _heartbeat_age(session_id):
    """Seconds since the worker last stamped its heartbeat file, or None.

    Read side of the stamp the client writes (must match
    scripts/pi_rpc_client.py::_heartbeat_file). A running pid with a large
    age is a stalled or silent worker; the caller decides what to do.
    """
    path = os.path.join(os.path.dirname(REGISTRY_PATH), f"pi-heartbeat-{session_id}.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return round(time.time() - float(data.get("ts") or 0))
    except (OSError, ValueError, TypeError):
        return None


def _steer_session(session_id, message, queue):
    """Queue a steer/follow_up message for a live worker via its control file."""
    if not session_id:
        return {"error": "session_id required for steer"}
    if not message:
        return {"error": "message required for steer"}
    entry = _newest_registry_entry(session_id)
    if entry is None:
        return {"error": f"Session {session_id} not found in registry"}
    pid = entry.get("pid")
    try:
        os.kill(pid, 0)
    except Exception:
        _registry_update(session_id, pid, status="dead", finished=_now_iso())
        return {"error": f"session {session_id} is not running (pid {pid} gone)"}
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
        if "pi_rpc_client" not in cmdline:
            return {"error": f"pid {pid} is not a pi_rpc_client process; refusing"}
    except Exception:
        return {"error": f"cannot verify cmdline of pid {pid}"}
    path = _steer_file(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"message": message, "queue": bool(queue), "ts": _now_iso()}, f)
        os.replace(tmp, path)  # atomic: the client never reads a partial file
    except OSError as exc:
        return {"error": f"cannot write steer file: {exc}"}
    kind = "follow_up" if queue else "steer"
    return {"status": f"{kind} queued for {session_id}", "path": path}


def _tool_schema(name, description, properties, required):
    """Build a JSON-schema dict for ctx.register_tool."""
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


LAUNCH_PARAMS = {
    "auto": {
        "type": "boolean",
        "description": (
            "run the generated command yourself as a detached background dispatch and "
            "return dispatched/pid/session_id/log; default false keeps the paste-able "
            "command output (no process spawned)"
        ),
    },
    "create_anchor": {
        "type": "boolean",
        "description": (
            "require_edit paths that do not exist get created as append-only lane-log "
            "anchors (header: '## <slug> lane log (append-only)') instead of failing "
            "preflight"
        ),
    },
    "worker_type": {
        "type": "string",
        "description": (
            "optional, defaults to 'default' (passthrough: no tool/codemode/thinking "
            "policy attached). reasoner: read-only tools; brainer: read+write+edit, no "
            "shell; coder and auditor: pi defaults plus codemode"
        ),
    },
    "skills": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "optional but recommended; skill entries: dirs (~ or absolute) or bare names; "
            "names resolve as "
            ".agents/.pi under cwd ancestors -> ~/.agents/skills -> ~/.hermes/skills/* "
            "-> ~/.pi/skills; discovered paths are emitted absolute; pi does not "
            "inherit parent skills"
        ),
    },
    "contract_path": {"type": "string", "description": "path to the contract prompt file"},
    "cwd": {"type": "string", "description": "absolute project dir; pi is project-scoped"},
    "epic_id": {
        "type": "string",
        "description": (
            "optional; session id becomes <epic_id>-<worker_type>. Omit both epic_id and "
            "task_slug and the session id is task-<basename of cwd>-<worker_type>: the "
            "same cwd with the same worker_type reuses that session id, and pi reuses "
            "the session state behind it"
        ),
    },
    "task_slug": {
        "type": "string",
        "description": (
            "optional; session id becomes task-<slug>-<worker_type>. Omit both epic_id "
            "and task_slug and the session id is task-<basename of cwd>-<worker_type>: "
            "the same cwd with the same worker_type reuses that session id, and pi "
            "reuses the session state behind it"
        ),
    },
    "model": {
        "type": "string",
        "description": (
            "optional model alias for the worker upstream; omitted, the command carries "
            "no --model at all and pi uses its own defaultModel. The caller's deployment "
            "defines the aliases, the plugin never hardcodes any"
        ),
    },
    "mode": {"type": "string", "enum": ["standard"]},
    "require_edit": {
        "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
        "description": (
            "paths that must change for the run to count as done; enables the nudge gate"
        ),
    },
    "min_tool_calls": {
        "type": "integer",
        "minimum": 1,
        "description": "tool calls required before the run counts as done; needs require_edit",
    },
    "max_nudges": {
        "type": "integer",
        "minimum": 0,
        "description": "corrective prompts before giving up; needs require_edit",
    },
    "require_sentinel": {
        "type": "string",
        "description": (
            "optional string that must appear in the require_edit file at exit; the "
            "registry row then records deliverable: met|unmet|unchecked (exit code "
            "unchanged) and wait/list surface it. Needs require_edit. Closes the "
            "satisfied-by-call-count hole: tool calls cannot prove a deliverable"
        ),
    },
    "timeout_s": {
        "type": "integer",
        "minimum": 30,
        "maximum": 21600,
        "description": "worker timeout in seconds; defaults to PI_WORKER_TIMEOUT_S",
    },
    "wall_timeout_s": {
        "type": "integer",
        "minimum": 60,
        "maximum": 86400,
        "description": (
            "hard wall-clock ceiling for the whole dispatch (survives streaming "
            "frames); defaults to max(3600, timeout_s+900)"
        ),
    },
}


def _launch_properties():
    return LAUNCH_PARAMS


def _launch_required():
    """Return the impl params that have no default, so the schema cannot drift."""
    params = inspect.signature(_pi_launch_impl).parameters
    return [
        name
        for name, p in params.items()
        if p.default is inspect.Parameter.empty and p.kind is not inspect.Parameter.VAR_KEYWORD
    ]


def _assert_schema_parity():
    """Fail at import when the registered schema and the impl signature disagree."""
    impl = {
        n
        for n, p in inspect.signature(_pi_launch_impl).parameters.items()
        if p.kind is not inspect.Parameter.VAR_KEYWORD
    }
    missing = sorted(impl - set(LAUNCH_PARAMS))
    extra = sorted(set(LAUNCH_PARAMS) - impl)
    if missing or extra:
        raise RuntimeError(
            f"pi_launch schema drift: missing from LAUNCH_PARAMS={missing}, not in impl={extra}"
        )


def _file_sha(path):
    """SHA-256 of a file's bytes, read through a context manager."""
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


_LOADED_SHA = _file_sha(__file__)


def _plugin_health():
    """Report whether the running module is older than the file on disk."""
    health = {"stale": False, "loaded_sha": _LOADED_SHA, "disk_sha": _LOADED_SHA, "detail": ""}
    try:
        with open(__file__, "rb") as handle:
            disk_sha = hashlib.sha256(handle.read()).hexdigest()
    except OSError as exc:
        health["detail"] = f"health unavailable: {exc}"
        return health
    health["disk_sha"] = disk_sha
    health["stale"] = disk_sha != _LOADED_SHA
    if health["stale"]:
        health["detail"] = (
            "plugin file changed since the gateway imported it - run pi_manage(action='doctor'), "
            "then restart the gateway; the registered schema is behind"
        )
    return health


def _fresh_impl(name):
    """Return `name` from the plugin file as it is on disk right now."""
    # Only code is hot-reloaded; the registered schema still needs the restart.
    spec = importlib.util.spec_from_file_location(f"pi_delegation_fresh_{time.time_ns()}", __file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def register(ctx):
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_tool(
        name="pi_manage",
        toolset=TOOLSETS["pi_manage"],
        schema=_tool_schema(
            "pi_manage",
            "List, steer or kill pi subagent sessions",
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "kill",
                        "doctor",
                        "logs",
                        "wait",
                        "steer",
                        "salvage_report",
                    ],
                },
                "session_id": {"type": "string"},
                "message": {
                    "type": "string",
                    "description": "steer: text injected into the worker",
                },
                "queue": {
                    "type": "boolean",
                    "description": (
                        "steer: true queues the message for the worker's next turn "
                        "(follow_up) instead of the current run"
                    ),
                },
                "only_running": {"type": "boolean"},
                "lines": {"type": "integer", "minimum": 1},
                "timeout_s": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "wait: seconds to keep probing; 0 means a single probe. "
                        "Clamped to 1500s (WAIT_CEILING_S), safely under the host's "
                        "1600s tool-call ceiling; a clamped answer carries "
                        "clamped/resumed_by=call-wait-again"
                    ),
                },
                "poll_s": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "wait: seconds between probes",
                },
                "go_tests": {
                    "type": "boolean",
                    "description": (
                        "salvage_report: for a recorded cwd with go.mod, also run "
                        "'go test ./...' (off by default: it dominates the report's runtime)"
                    ),
                },
            },
            ["action"],
        ),
        handler=pi_manage,
    )
    ctx.register_tool(
        name="pi_launch",
        toolset=TOOLSETS["pi_launch"],
        schema=_tool_schema(
            "pi_launch",
            "Generate a launch command for a pi subagent worker",
            _launch_properties(),
            _launch_required(),
        ),
        handler=pi_launch,
    )
    logging.info("pi-delegation plugin registered with management tools")


def _call_impl(name, args):
    """Call the on-disk impl when the gateway's copy is stale, else the local one."""
    health = _plugin_health()
    detail = health["detail"]
    hot_reloaded = False
    impl = globals()[name]
    if health["stale"]:
        try:
            impl = _fresh_impl(name)
            hot_reloaded = True
        except Exception as exc:  # a broken reload must not take the tool down
            detail = f"{detail} (fresh reload failed: {exc})".strip()
    try:
        result = impl(**args)
    except Exception as exc:  # report, never raise
        return json.dumps({"error": str(exc), "plugin": dict(health, detail=detail)})
    if not isinstance(result, dict):
        result = {"result": result}
    if hot_reloaded:
        # The code that just ran came from disk, so the answer is current: report
        # it as such instead of replaying the stale module's verdict forever.
        health = {
            "stale": False,
            "loaded_sha": health["disk_sha"],
            "disk_sha": health["disk_sha"],
            "detail": "",
            "hot_reloaded": True,
            "note": "code reloaded from disk; a SCHEMA change still needs a gateway restart",
        }
        detail = ""
    result["plugin"] = dict(health, detail=detail)
    if health["stale"]:
        result["warning"] = health["detail"]
    return json.dumps(result)


def pi_manage(args: dict, **_):
    """Manage pi sessions plus plugin freshness (doctor/logs)."""
    return _call_impl("_pi_manage_impl", args)


def _pi_manage_impl(
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
    if action == "steer":
        return _steer_session(session_id, message or "", queue)
    if action == "wait":
        if not session_id:
            return {"error": "session_id required for wait"}
        requested = max(0, int(timeout_s or 0))
        budget = min(requested, WAIT_CEILING_S)
        poll = max(1, int(poll_s or 1))
        result = _wait_for_completion(session_id, budget, poll)
        result["requested_timeout_s"] = requested
        result["timeout_s"] = budget
        result["clamped"] = budget < requested
        if result["clamped"] and not result["finished"]:
            result["resumed_by"] = "call-wait-again"
        return result
    if action == "doctor":
        return _doctor()
    if action == "logs":
        return _session_logs(session_id, lines)
    if action == "salvage_report":
        return _salvage_report(session_id, lines, go_tests)

    if action == "list":
        agents = _read_registry()
        if only_running:
            agents = [
                e for e in agents if e.get("status") == "running" and _pid_alive(e.get("pid"))
            ]
        for entry in agents:
            if entry.get("status") != "running" or not _pid_alive(entry.get("pid")):
                continue
            age = _heartbeat_age(entry.get("session"))
            if age is not None:
                entry["last_activity_s"] = age
        # every registered entry surfaces its lane lock when one sits on disk:
        # a stale lock (pid dead, crash leftover) belongs on the DEAD row too,
        # which is exactly where the crashed worker's row is still running.
        for agent in agents:
            lock = _lane_lock_info(agent.get("cwd"), agent.get("session"))
            if lock is not None:
                agent["lane_lock"] = {"present": True, "stale": lock["stale"]}
        agents.sort(key=lambda e: e.get("started", ""), reverse=True)
        return {"agents": agents}

    if action == "kill":
        if not session_id:
            return {"error": "session_id required for kill"}
        seen_dead_pid = None
        for entry in _read_registry():
            if entry.get("session") == session_id:
                pid = entry.get("pid")
                # Defensive: verify pid exists and is a pi_rpc_client process.
                try:
                    os.kill(pid, 0)
                except Exception:
                    _registry_update(session_id, pid, status="dead", finished=_now_iso())
                    seen_dead_pid = pid
                    continue
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        cmdline = f.read().decode("utf-8", "replace")
                    if "pi_rpc_client" not in cmdline:
                        return {
                            "error": f"pid {pid} is not a pi_rpc_client process; refusing to signal"
                        }
                except Exception:
                    return {"error": f"cannot verify cmdline of pid {pid}"}
                os.killpg(pid, signal.SIGTERM)  # client AND its pi + tool children
                _registry_update(
                    session_id, pid, status="killed", exit_code=143, finished=_now_iso()
                )
                return {"status": f"SIGTERM sent to pgid {pid} ({session_id})"}
        if seen_dead_pid is not None:
            return {
                "status": f"session {session_id} already dead (pid {seen_dead_pid}, marked dead)"
            }
        return {"error": f"Session {session_id} not found in registry"}


def _wait_result(session_id, entry):
    """Derived wait answer for one registry row: running/terminal/last_activity_s.

    unknown is reserved for a session with no registry rows at all; a known
    row always yields its derived fields, even when a concurrent whole-file
    rewrite makes one read land partial.
    """
    status = entry.get("status") or "running"
    pid = entry.get("pid")
    result = {"finished": _wait_finished(entry), "status": status, "pid": pid}
    if not result["finished"] and _pid_alive(pid):
        age = _heartbeat_age(session_id)
        if age is not None:
            result["last_activity_s"] = age
    if entry.get("exit_code") is not None:
        result["exit_code"] = entry["exit_code"]
    if entry.get("deliverable") is not None:
        result["deliverable"] = entry["deliverable"]
    return result


def _wait_for_completion(session_id, budget, poll):
    """Probe the registry until the session settles or the budget runs out.

    Never answers unknown for a registered session: unknown comes only from
    the caller-side probe (no registry rows for this session at all), never
    from inside the loop.
    """
    deadline = time.monotonic() + budget
    while True:
        entry = _newest_registry_entry(session_id)
        if entry is None:
            if time.monotonic() >= deadline:
                return {"finished": False, "status": "unknown"}
            time.sleep(poll)
            continue
        result = _wait_result(session_id, entry)
        if result["finished"] or deadline - time.monotonic() < poll:
            return result
        time.sleep(poll)


def _git_readonly(cwd, *args):
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=30)
        return proc.returncode == 0, (proc.stdout if proc.returncode == 0 else proc.stderr)
    except Exception as exc:
        return False, str(exc)


def _base_is_valid(cwd, base):
    return bool(base) and bool(_git_readonly(cwd, "cat-file", "-e", f"{base}^{{commit}}")[0])


# Quick gates per project type, chosen from the RECORDED cwd's root markers. A
# worker in another repo must never be graded by the plugin's own checks: one
# live Go session surfaced 23 pre-existing lint errors from this tree.
PROJECT_MARKERS = (
    ("go.mod", "go"),
    ("pyproject.toml", "python"),
    ("plugin.yaml", "plugin"),
)


def _project_type(cwd):
    """Project type of the recorded cwd: go, python, plugin, or unknown."""
    if not cwd:
        return "unknown"
    for marker, kind in PROJECT_MARKERS:
        if os.path.isfile(os.path.join(cwd, marker)):
            return kind
    return "unknown"


def _gate_commands(project_type, go_tests):
    """(name, command) pairs for one project type; go test stays opt-in."""
    if project_type == "go":
        gates = [
            (
                "gofmt",
                "unformatted=$(gofmt -l .) || exit 1; "
                '[ -z "$unformatted" ] || { echo "$unformatted"; exit 1; }',
            ),
            ("go-build", "go build ./..."),
        ]
        if go_tests:
            gates.append(("go-test", "go test ./..."))
        return gates
    if project_type == "python":
        return [("ruff-check", "uvx ruff check ."), ("compileall", "python -m compileall -q .")]
    if project_type == "plugin":
        return [
            ("uvx-ruff-check", "uvx ruff check ."),
            ("compileall-plugin", "python -m compileall -q __init__.py scripts tests"),
        ]
    return []


def _project_gates(cwd, go_tests=False):
    """Gate rows for the recorded cwd's project type, plus a named skip when
    the type cannot be established, so an empty gate list never reads as pass."""
    project_type = _project_type(cwd)
    commands = _gate_commands(project_type, go_tests)
    if not commands:
        reason = (
            "no recorded cwd for this row"
            if not cwd
            else f"no project marker (go.mod, pyproject.toml, plugin.yaml) in {cwd}"
        )
        return [{"name": "project-type", "command": "", "ok": False, "skipped": reason}]
    return [_gate_row(name, command, cwd) for name, command in commands]


def _gate_row(name, command, cwd):
    """One salvage gate row: ok plus a short output tail or a named reason.

    cwd comes from the registry row: the gate must judge THE SESSION'S tree,
    not the caller's. A failing invocation (command absent, unwritable cwd,
    120 s timeout cap) degrades to a skipped row."""
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120, cwd=cwd
        )
        tail = (proc.stdout + proc.stderr)[-2000:]
        return {"name": name, "command": command, "ok": proc.returncode == 0, "output_tail": tail}
    except Exception as exc:
        return {
            "name": name,
            "command": command,
            "ok": False,
            "output_tail": "",
            "skipped": str(exc),
        }


def _salvage_report(session_id, lines=12, go_tests=False):
    """Post-exit state collection for one session, in one call, shaped to
    paste as the on-disk state section of a continuation contract. Read-only,
    best-effort: every leg degrades to a named skip instead of raising.
    """
    entry = _newest_registry_entry(session_id)
    if entry is None:
        return {"finished": False, "status": "unknown"}
    cwd = entry.get("cwd")
    base = entry.get("base")
    valid_base = _base_is_valid(cwd, base) if cwd else False

    git_report = {}
    if cwd and _git_readonly(cwd, "status")[0]:
        ok_status, status_out = _git_readonly(cwd, "status", "--porcelain")
        git_report["status"] = status_out if ok_status else ""
        ok_head, head_out = _git_readonly(cwd, "log", "--oneline", "-1")
        ok_sha, sha_out = _git_readonly(cwd, "rev-parse", "HEAD")
        git_report["branch_head"] = "\n".join(x.strip() for x in (head_out, sha_out) if x)
        if valid_base:
            ok_log, log_out = _git_readonly(cwd, "log", "--oneline", f"{base}..HEAD")
            git_report["commits_since_base"] = (
                log_out.splitlines() if ok_log else {"skipped": log_out}
            )
            ok_diff, diff_out = _git_readonly(cwd, "diff", f"{base}..HEAD", "--stat")
            git_report["diff_stat"] = (
                diff_out.splitlines()[-6:] if ok_diff else {"skipped": diff_out}
            )
        else:
            reason = (
                "no base recorded for this row"
                if not base
                else "unknown revision: base not a commit of this repo"
            )
            git_report["commits_since_base"] = {"skipped": reason}
            git_report["diff_stat"] = {"skipped": f"{reason}; uncommitted facts visible via status"}
    else:
        reason = "no recorded cwd for this row" if not cwd else f"cwd is not a git repo: {cwd}"
        git_report["status"] = ""
        git_report["branch_head"] = ""
        git_report["commits_since_base"] = {"skipped": reason}
        git_report["diff_stat"] = {"skipped": reason}

    frames = _session_logs(session_id, lines)

    lock = _lane_lock_info(cwd, session_id)
    if lock is not None:
        lane_lock_row = {
            "path": lock["path"],
            "stale": lock["stale"],
            "payload": lock["payload"],
            "removal": f"rm {lock['path']}" + ("  # stale: pid gone" if lock["stale"] else ""),
        }
    else:
        lane_lock_row = {"present": False}

    return {
        "session": session_id,
        "cwd": cwd,
        "base": base,
        "collected_at": _now_iso(),
        "git": git_report,
        "frames": {
            k: frames[k]
            for k in ("error", "path", "mtime", "tool_calls", "last_text", "tail")
            if k in frames
        },
        "lane_lock": lane_lock_row,
        "project_type": _project_type(cwd),
        "gates": _project_gates(cwd, go_tests),
    }


def _dispatch_command(command, cwd, session_id):
    """auto=true: run the generated one-liner detached, log to the lane dir.

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
    return {"dispatched": True, "pid": proc.pid, "session_id": session_id, "log": log_path}


def _doctor():
    """Read-only snapshot of plugin-vs-gateway state; the first-class staleness answer."""
    health = _plugin_health()
    impl_params = sorted(
        n
        for n, param in inspect.signature(_pi_launch_impl).parameters.items()
        if param.kind is not inspect.Parameter.VAR_KEYWORD
    )
    registered = sorted(LAUNCH_PARAMS)
    try:
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.stat(__file__).st_mtime))
    except OSError:
        mtime = ""
    report = {
        "plugin_path": __file__,
        "plugin_mtime": mtime,
        "loaded_sha": health["loaded_sha"],
        "disk_sha": health["disk_sha"],
        "stale": health["stale"],
        "registered_props": registered,
        "impl_params": impl_params,
        "schema_missing": sorted(set(impl_params) - set(registered)),
        "schema_extra": sorted(set(registered) - set(impl_params)),
        "client_path": CLIENT_PATH,
        "client_exists": os.path.isfile(CLIENT_PATH),
        "worker_timeout_s": WORKER_TIMEOUT_S,
        "python": sys.version.split()[0],
    }
    if report["schema_missing"] or report["stale"]:
        report["fix"] = health["detail"] or "restart the gateway to re-register the schema"
    return report


def _transcript_for(session_id):
    """Resolve a session's transcript path, or None.

    Registry log_path first (the client learned pi's real transcript even when
    its filename lacks the session slug), then the legacy slug glob for rows
    recorded before that fix.
    """
    import glob

    entry = _newest_registry_entry(session_id)
    if entry is not None:
        registered = entry.get("log_path")
        if registered and os.path.isfile(registered):
            return registered
    pattern = os.path.expanduser(f"~/.pi/agent/sessions/*/*{session_id}*.jsonl")
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return matches[0] if matches else None


def _session_logs(session_id, lines=40):
    """Tail the newest pi session log for a session id."""
    if not session_id:
        return {"error": "logs requires session_id"}
    path = _transcript_for(session_id)
    if not path:
        return {"error": f"no session log for {session_id}"}
    tail, tool_calls, last_text = [], 0, ""
    try:
        with open(path) as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                message = row.get("message") or {}
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    kind = part.get("type")
                    if kind == "toolCall":
                        tool_calls += 1
                        tail.append("toolCall: {}".format(part.get("name", "")))
                    elif kind == "text":
                        last_text = part.get("text", "")
                        tail.append("text: {}".format(last_text[:160].replace("\n", " ")))
    except OSError as exc:
        return {"error": f"cannot read {path}: {exc}"}
    return {
        "session_id": session_id,
        "path": path,
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
        "tool_calls": tool_calls,
        "last_text": last_text[:400],
        "tail": tail[-int(lines) :],
    }


def _now_iso():
    import datetime

    return datetime.datetime.now().isoformat()


def _compliance_block(skills):
    """Deterministic skill-compliance prompt text injected via --append-system-prompt.

    Single line so the emitted shell command stays one line (gate: shlex
    round-trip must remain single-line parseable).
    """
    skill_list = ", ".join(sorted(skills))
    return (
        "[IMMUTABLE PI LAW] 1. NO NARRATION. 2. CITATIONS REQUIRED. "
        f"3. SKILL COMPLIANCE: You loaded these skills: {skill_list}. "
        "For EACH skill you MUST: a) READ the full SKILL.md (use read tool on the path) "
        "b) OUTPUT: skill name + 3 binding rules you will follow c) ONLY THEN execute edits. "
        "VIOLATION = CONTRACT BREACH. Parent verifies via tool trace."
    )


def pi_launch(args: dict, **_):
    """Generate a pi worker launch command and report plugin freshness."""
    if not args.get("cwd"):
        return json.dumps({"error": "cwd is required (absolute project dir)"})
    return _call_impl("_pi_launch_impl", args)


def _launch_session_id(cwd, worker_type, epic_id, task_slug):
    """Session naming: explicit ids win, else derive one from the project directory."""
    if epic_id:
        return f"{epic_id}-{worker_type}"
    if task_slug:
        return f"task-{task_slug}-{worker_type}"
    return f"task-{os.path.basename(os.path.normpath(cwd))}-{worker_type}"


def _deployment_models(agent_dir):
    """(known_aliases, default_model, readable) from one pi agent dir, for
    preflight model validation. readable=False when the list cannot be
    established at all; the caller then degrades to a warning."""
    known, default = [], None
    try:
        with open(os.path.join(agent_dir, "models.json")) as f:
            catalog = json.load(f)
        for provider in (catalog.get("providers") or {}).values():
            for entry in (provider or {}).get("models") or []:
                alias = (entry or {}).get("id")
                if isinstance(alias, str) and alias:
                    known.append(alias)
    except (OSError, ValueError, AttributeError):
        return [], None, False
    try:
        with open(os.path.join(agent_dir, "settings.json")) as f:
            default = json.load(f).get("defaultModel")
    except (OSError, ValueError, AttributeError):
        pass
    return known, default, True


def _model_check_rows(model, agent_dir):
    """(failures, warnings) for the launch model argument against agent_dir."""
    failures, warnings = [], []
    known, default, readable = _deployment_models(agent_dir)
    if model:
        if readable and known and model not in known and model != default:
            sample = ", ".join(sorted(known)[:5])
            failures.append(
                f"model - unknown alias {model!r} for this deployment "
                f"({agent_dir}); valid ids include: {sample}"
            )
        elif not (readable and known):
            warnings.append(
                f"preflight warn: model {model} not verifiable against the deployment list"
            )
        return failures, warnings
    warning = "preflight warn: no model passed - the worker runs pi's defaultModel"
    if readable and default:
        warning += f" ({default})"
    warnings.append(warning)
    return failures, warnings


def _anchor_header(path):
    """Standard append-only lane-log header for a require_edit anchor."""
    slug = os.path.splitext(os.path.basename(path))[0]
    return f"## {slug} lane log (append-only)"


def _preflight(
    cwd,
    contract_path,
    require_edit,
    create_anchor,
    resolved_skills,
    timeout_s,
    wall_timeout_s,
    min_tool_calls,
    max_nudges,
    worker_type="default",
    model=None,
    epic_id=None,
    task_slug=None,
    require_sentinel=None,
):
    """Fail-fast contract, budget, model and naming checks before a worker spawns.

    Returns (failures, warnings). Every check names itself, so the aggregated
    error lists ALL failed checks, not just the first; warnings carry the
    degraded model checks.
    """
    failures = []
    warnings = []
    if epic_id and task_slug:
        failures.append(
            f"naming - epic_id and task_slug are mutually exclusive "
            f"(epic_id wins the session id); drop one: epic_id={epic_id}, task_slug={task_slug}"
        )
    model_failures, model_warnings = _model_check_rows(model, WORKER_AGENT_DIR)
    failures.extend(model_failures)
    warnings.extend(model_warnings)

    if not os.path.isfile(os.path.expanduser(contract_path)):
        failures.append(f"contract - not found or unreadable: {contract_path}")

    if require_edit:
        targets = [require_edit] if isinstance(require_edit, str) else list(require_edit)
        for anchor in targets:
            if not anchor:
                failures.append("require_edit anchors - empty path entry")
                continue
            anchored = anchor if os.path.isabs(anchor) else os.path.join(cwd, anchor)
            if os.path.exists(anchored):
                continue
            if create_anchor:
                try:
                    with open(anchored, "w") as f:
                        f.write(_anchor_header(anchored) + "\n")
                except OSError as exc:
                    failures.append(f"require_edit anchors - cannot create {anchor}: {exc}")
            else:
                failures.append(
                    f"require_edit anchors - missing: {anchored} "
                    "(exists-check; create_anchor=true to create it)"
                )

    for skill in resolved_skills:
        if not os.path.isdir(skill):
            failures.append(f"skills - not a directory: {skill}")
        elif not os.path.isfile(os.path.join(skill, "SKILL.md")):
            failures.append(f"skills - directory lacks SKILL.md: {skill}")

    if (
        wall_timeout_s is not None
        and timeout_s is not None
        and int(wall_timeout_s) <= int(timeout_s)
    ):
        failures.append(
            f"budgets - wall_timeout_s ({wall_timeout_s}) must exceed timeout_s ({timeout_s})"
        )
    min_max_passed = min_tool_calls is not _UNSET or max_nudges is not _UNSET
    if worker_type == "auditor" and not require_edit:
        failures.append(
            "auditor - mode aims at no deliverable: require_edit names the file the "
            "final text is written into"
        )
    min_calls = 0 if min_tool_calls is _UNSET else int(min_tool_calls or 0)
    max_nudges = 0 if max_nudges is _UNSET else int(max_nudges or 0)
    if max_nudges > 0 and min_calls < 1:
        failures.append(
            "budgets - min_tool_calls < 1 while max_nudges > 0: the nudge gate "
            "cannot tell work from luck"
        )
    if min_max_passed and not require_edit:
        failures.append(
            "budgets - min_tool_calls/max_nudges set without require_edit: the gate has no aim"
        )
    if require_sentinel and not require_edit:
        failures.append(
            "require_sentinel - set without require_edit: the sentinel check has no file to "
            "grep; name the deliverable file in require_edit"
        )

    if not (
        os.path.isfile(os.path.join(cwd, ".beans.yml"))
        or os.path.isfile(os.path.join(cwd, "AGENTS.md"))
    ):
        failures.append(
            "cwd - right-repo guard: launch cwd has neither .beans.yml nor AGENTS.md at "
            "its root; add one of them (e.g. AGENTS.md)"
        )

    return failures, warnings


def _pi_launch_impl(
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
    auto=False,
    **_,
):
    """Generate a single-line POSIX shell command for pi_rpc_client.py.

    The caller wraps the returned command in a Hermes terminal tool call with
    background=true; the command itself is plain shell, no tool-DSL.

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
    if worker_type not in WORKER_TYPES:
        return {"error": f"Unknown worker_type: {worker_type}. Valid: {list(WORKER_TYPES.keys())}"}

    cfg = WORKER_TYPES[worker_type]
    skills = list(skills or [])
    if mode == "legacy":
        return {
            "error": "legacy mode removed: it ran workers with -ns (skills "
            "disabled); use the default standard mode"
        }

    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return {"error": f"cwd must be an existing directory (absolute project dir), got: {cwd!r}"}

    session_id = _launch_session_id(cwd, worker_type, epic_id, task_slug)

    warnings = []
    resolved_skills = []
    preflight_skill_failures = []
    for skill in skills:
        expanded = os.path.expanduser(skill)
        if os.path.exists(expanded):
            resolved = expanded
        else:
            found = _resolve_skill_name(skill, cwd)
            if found is None:
                preflight_skill_failures.append(f"skills - not resolved: {skill}")
                continue
            resolved = found
        if not _skill_discoverable(resolved, cwd):
            warnings.append(
                f"preflight warn: {skill} is outside pi skill discovery "
                "(.agents/.pi under cwd ancestors); worker reads it only via the absolute "
                "path, so the contract must quote this exact path"
            )
        resolved_skills.append(resolved)
    skills = resolved_skills

    live_pid = _live_session(session_id)
    if live_pid is not None:
        return {"error": f"session {session_id} already running (pid {live_pid})"}

    failures, preflight_warnings = _preflight(
        cwd,
        contract_path,
        require_edit,
        create_anchor,
        resolved_skills,
        timeout_s,
        wall_timeout_s,
        min_tool_calls,
        max_nudges,
        worker_type,
        model,
        epic_id,
        task_slug,
        require_sentinel,
    )
    failures += preflight_skill_failures
    if failures:
        return {"error": "preflight: " + "; ".join(failures)}
    warnings = preflight_warnings + warnings

    # Single-line POSIX command, shlex-quoted throughout. Double quotes around
    # $(cat ...) so command substitution happens at shell level.
    parts = [
        "python3",
        CLIENT_PATH,
        "--session-id",
        session_id,
        "--cwd",
        cwd,
    ]
    if model:
        parts.extend(["--model", model])
    # NOTE: no --ns anywhere: pi's -ns/--no-skills disables skill loading
    # outright, and every worker must run with its skills. Legacy mode is
    # rejected above.
    # The bytes we were imported from still match disk, so this module IS the
    # disk code (only hit if Hermes fails to hot-reload): tell the client to
    # skip its mtime staleness heuristic. A mid-call edit makes the hash
    # mismatch and drops the flag; the heuristic then applies.
    if _file_sha(__file__) == _LOADED_SHA:
        parts.append("--plugin-fresh")
    for skill in skills:
        parts.extend(["--skill", skill])
    system_prompt = None
    if skills:
        system_prompt = _compliance_block(skills)
        parts.extend(["--append-system-prompt", system_prompt])
    # Worker agent dir: excludes npm:pi-subagents (workers must not delegate)
    # and swaps in a no-delegation AGENTS.md; auth/models come along via
    # symlink, so model catalogs and providers are unchanged.
    if os.path.isdir(WORKER_AGENT_DIR):
        parts.extend(["--agent-dir", WORKER_AGENT_DIR])
    else:
        warnings.append(
            f"worker agent dir {WORKER_AGENT_DIR} missing: pi-subagents NOT disabled and default "
            "AGENTS.md in effect; run the setup from the pi-delegation skill"
        )
    if cfg["use_codemode"]:
        parts.append("--codemode")
    if cfg.get("text_deliverable"):
        parts.append("--auditor-deliverable")
    if cfg["tools"]:
        parts.extend(["--tools", cfg["tools"]])
    if cfg["thinking"]:
        parts.extend(["--thinking", cfg["thinking"]])
    try:
        resolved_timeout = WORKER_TIMEOUT_S if timeout_s is None else int(timeout_s)
    except (TypeError, ValueError):
        return {"error": f"timeout_s must be an integer, got {timeout_s!r}"}
    if not 30 <= resolved_timeout <= 21600:
        return {"error": f"timeout_s must be between 30 and 21600, got {timeout_s!r}"}
    parts.extend(["--timeout", str(resolved_timeout)])
    try:
        resolved_wall = None if wall_timeout_s is None else int(wall_timeout_s)
    except (TypeError, ValueError):
        return {"error": f"wall_timeout_s must be an integer, got {wall_timeout_s!r}"}
    if resolved_wall is None:
        # Wall must exceed the idle budget or long sweeps die mid-contract.
        resolved_wall = max(3600, resolved_timeout + 900)
    elif resolved_wall < resolved_timeout:
        failures = [
            f"budgets - wall_timeout_s ({resolved_wall}) must be >= timeout_s ({resolved_timeout})"
        ]
        return {"error": "preflight: " + "; ".join(failures)}
    parts.extend(["--wall-timeout", str(resolved_wall)])

    if require_edit:
        targets = require_edit if isinstance(require_edit, str) else ",".join(require_edit)
        if worker_type == "auditor":
            min_calls = 1 if min_tool_calls is _UNSET else int(min_tool_calls)
            max_nudges_resolved = 0 if max_nudges is _UNSET else int(max_nudges)
        else:
            min_calls = 3 if min_tool_calls is _UNSET else int(min_tool_calls)
            max_nudges_resolved = 4 if max_nudges is _UNSET else int(max_nudges)
        nudge_clause = (
            "nudge: 0 - no correction loop"
            if max_nudges_resolved == 0
            else f"nudge: >={min_calls} tool calls, <={max_nudges_resolved} nudges then exit 3"
        )
        parts.extend(["--require-edit", targets])
        parts.extend(["--min-tool-calls", str(min_calls)])
        parts.extend(["--max-nudges", str(max_nudges_resolved)])
        if require_sentinel:
            parts.extend(["--require-sentinel", require_sentinel])
    else:
        # preflight already refuses min/max without require_edit
        nudge_clause = "nudge: 0 - no correction loop"
    parts.extend(["--prompt", f"$(cat {shlex.quote(contract_path)})"])

    command = "echo 'preflight ok: contract, anchors, skills, budgets, cwd, model, naming' && "
    budget_note = (
        f"budget: per-call <= {resolved_timeout}s, wall <= {resolved_wall}s, {nudge_clause}"
    )
    if require_sentinel:
        budget_note += f", deliverable: sentinel {require_sentinel!r}"
    command += f"echo {shlex.quote(budget_note)} && "
    command += " ".join(shlex.quote(p) for p in parts[:-1])
    # Last part keeps $(cat ...) unquoted at the outer level: shlex.quote would
    # suppress substitution, so emit it double-quoted manually.
    command += f' "$(cat {shlex.quote(contract_path)})"'

    result = {
        "command": command,
        "session_id": session_id,
        "worker_type": worker_type,
        "mode": mode,
    }
    if require_sentinel:
        result["require_sentinel"] = require_sentinel
    if auto:
        return _dispatch_command(command, cwd, session_id)
    if system_prompt is not None:
        result["system_prompt"] = system_prompt
    if warnings:
        result["warnings"] = warnings
    return result


_assert_schema_parity()


def _skill_discoverable(skill_path, cwd):
    """True if skill_path's realpath lies under a .agents/skills or .pi/skills
    dir at cwd or any ancestor of cwd (pi's discovery roots)."""
    real = os.path.realpath(skill_path)
    root = os.path.realpath(cwd)
    while True:
        for sub in (".agents/skills", ".pi/skills"):
            base = os.path.join(root, sub)
            if os.path.isdir(base) and (real == base or real.startswith(base + os.sep)):
                return True
        parent = os.path.dirname(root)
        if parent == root:
            return False
        root = parent


def _resolve_skill_name(name, cwd):
    """Resolve a bare skill NAME to a directory path, or None.

    Search order, first hit wins: .agents/skills and .pi/skills at cwd and
    every ancestor (pi's discovery roots, nearest first), then the global
    ~/.agents/skills, then ~/.hermes/skills/<category>/ (sorted), then
    ~/.pi/skills. Path-form entries never reach this function.
    """
    home = os.path.expanduser("~")
    root = os.path.realpath(cwd)
    while True:
        for sub in (".agents/skills", ".pi/skills"):
            cand = os.path.join(root, sub, name)
            if os.path.isdir(cand):
                return cand
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    cand = os.path.join(home, ".agents", "skills", name)
    if os.path.isdir(cand):
        return cand
    base = os.path.join(home, ".hermes", "skills")
    if os.path.isdir(base):
        cand = os.path.join(base, name)
        if os.path.isdir(cand):
            return cand
        for cat in sorted(os.listdir(base)):
            cand = os.path.join(base, cat, name)
            if os.path.isdir(cand):
                return cand
    cand = os.path.join(home, ".pi", "skills", name)
    if os.path.isdir(cand):
        return cand
    return None


def on_pre_tool_call(tool_name, args, **_):
    """Parent-side enforcement ONLY.

    Worker-side tool calls never pass through Hermes hooks: workers run as
    standalone python3 processes started from a pasted shell command. Worker
    enforcement lives in sweep_runner.py and the contract text, not here.
    """
    if tool_name == "terminal":
        cmd = args.get("command", "")
        if GIT_MUTATION_RE.search(cmd):
            return {
                "action": "block",
                "message": "GIT SAFETY: git mutation blocked in parent terminal command.",
            }

    return None

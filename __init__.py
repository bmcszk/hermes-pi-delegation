import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import shlex
import signal
import sys
import time

# Constants
REGISTRY_PATH = os.path.expanduser("~/.hermes/logs/pi-agents.jsonl")
CLIENT_PATH = os.path.join(os.path.dirname(__file__), "scripts", "pi_rpc_client.py")
# Isolated pi agent dir for workers: same providers/auth as ~/.pi/agent but
# without npm:pi-subagents and with a no-delegation AGENTS.md.
WORKER_AGENT_DIR = os.path.join(os.path.expanduser("~"), ".pi", "agent-worker")

# Worker tool policy. reasoner is read-only. brainer may write docs and
# task files (write/edit) but has no shell. coder/auditor get codemode.
# No model key: the model is a per-dispatch argument supplied by the caller.
WORKER_TYPES = {
    "reasoner": {"tools": "read,grep,find,ls", "use_codemode": False, "thinking": "high"},
    "brainer": {
        "tools": "read,grep,find,ls,write,edit",
        "use_codemode": False,
        "thinking": "high",
    },
    "coder": {"tools": None, "use_codemode": True, "thinking": None},
    "auditor": {"tools": None, "use_codemode": True, "thinking": None},
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
_UNSET = object()  # distinguishes "caller passed it" from the signature default


def _read_registry():
    """Return list of parsed registry entries. Best-effort, never raises."""
    entries = []
    try:
        if not os.path.exists(REGISTRY_PATH):
            return entries
        with open(REGISTRY_PATH) as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return entries


def _registry_update(session_id, pid, **fields):
    """Read-modify-write the registry line matching session+pid. Best-effort."""
    try:
        if not os.path.exists(REGISTRY_PATH):
            return
        lines = []
        changed = False
        with open(REGISTRY_PATH) as f:
            for line in f:
                try:
                    data = json.loads(line)
                except Exception:
                    lines.append(line)
                    continue
                if data.get("session") == session_id and data.get("pid") == pid:
                    data.update(fields)
                    lines.append(json.dumps(data) + "\n")
                    changed = True
                else:
                    lines.append(line)
        if changed:
            with open(REGISTRY_PATH, "w") as f:
                f.writelines(lines)
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
    "worker_type": {
        "type": "string",
        "enum": list(WORKER_TYPES.keys()),
        "description": "reasoner/brainer are read-only-ish; coder/auditor get codemode",
    },
    "skills": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "skill entries: dirs (~ or absolute) or bare names; names resolve as "
            ".agents/.pi under cwd ancestors -> ~/.agents/skills -> ~/.hermes/skills/* "
            "-> ~/.pi/skills; discovered paths are emitted absolute; pi does not "
            "inherit parent skills"
        ),
    },
    "contract_path": {"type": "string", "description": "path to the contract prompt file"},
    "cwd": {"type": "string", "description": "absolute project dir; pi is project-scoped"},
    "epic_id": {"type": "string", "description": "session id becomes <epic_id>-<worker_type>"},
    "task_slug": {"type": "string", "description": "session id becomes task-<slug>-<worker_type>"},
    "model": {
        "type": "string",
        "description": (
            "model alias for the worker upstream; the caller's deployment defines "
            "aliases, the plugin never hardcodes any"
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
            "List or kill pi subagent sessions",
            {
                "action": {"type": "string", "enum": ["list", "kill", "doctor", "logs", "wait"]},
                "session_id": {"type": "string"},
                "only_running": {"type": "boolean"},
                "lines": {"type": "integer", "minimum": 1},
                "timeout_s": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "wait: seconds to keep probing; 0 means a single probe",
                },
                "poll_s": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "wait: seconds between probes",
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
    action, session_id=None, only_running=False, lines=40, timeout_s=None, poll_s=None, **_
):
    if action == "wait":
        if not session_id:
            return {"error": "session_id required for wait"}
        budget = max(0, int(timeout_s or 0))
        poll = max(1, int(poll_s or 1))
        if _newest_registry_entry(session_id) is None:
            return {"finished": False, "status": "unknown"}
        return _wait_for_completion(session_id, budget, poll)
    if action == "doctor":
        return _doctor()
    if action == "logs":
        return _session_logs(session_id, lines)

    if action == "list":
        agents = _read_registry()
        if only_running:
            agents = [
                e for e in agents if e.get("status") == "running" and _pid_alive(e.get("pid"))
            ]
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
                _registry_update(session_id, pid, status="killed", finished=_now_iso())
                return {"status": f"SIGTERM sent to pgid {pid} ({session_id})"}
        if seen_dead_pid is not None:
            return {
                "status": f"session {session_id} already dead (pid {seen_dead_pid}, marked dead)"
            }
        return {"error": f"Session {session_id} not found in registry"}


def _wait_for_completion(session_id, budget, poll):
    """Probe the registry until the session settles or the budget runs out."""
    deadline = time.monotonic() + budget
    while True:
        entry = _newest_registry_entry(session_id)
        if entry is None:
            return {"finished": False, "status": "unknown"}
        status = entry.get("status") or "running"
        pid = entry.get("pid")
        if _wait_finished(entry):
            return {"finished": True, "status": status, "pid": pid}
        remaining = deadline - time.monotonic()
        if remaining < poll:
            return {"finished": False, "status": status, "pid": pid}
        time.sleep(poll)


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


def _session_logs(session_id, lines=40):
    """Tail the newest pi session log for a session id."""
    if not session_id:
        return {"error": "logs requires session_id"}
    import glob

    pattern = os.path.expanduser(f"~/.pi/agent/sessions/*/*{session_id}*.jsonl")
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not matches:
        return {"error": f"no session log for {session_id}"}
    path = matches[0]
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


def _pi_launch_impl(
    worker_type,
    skills,
    contract_path,
    cwd,
    model,
    epic_id=None,
    task_slug=None,
    mode="standard",
    require_edit=None,
    min_tool_calls=_UNSET,
    max_nudges=_UNSET,
    timeout_s=None,
    wall_timeout_s=None,
    **_,
):
    """Generate a single-line POSIX shell command for pi_rpc_client.py.

    The caller wraps the returned command in a Hermes terminal tool call with
    background=true; the command itself is plain shell, no tool-DSL.

    cwd is the required absolute project dir: pi is project-scoped (skill
    discovery and relative paths resolve from it).

    model is the required per-dispatch worker model alias; the plugin carries
    no model names of its own, the caller's deployment defines the aliases.

    session_id = <epic-id>-<worker-type> when epic_id given,
                 task-<task_slug>-<worker-type> otherwise.

    Non-empty skills -> returns "system_prompt" (compliance block) and the
    command carries --append-system-prompt. Empty skills -> neither.
    """
    if worker_type not in WORKER_TYPES:
        return {"error": f"Unknown worker_type: {worker_type}. Valid: {list(WORKER_TYPES.keys())}"}

    if not epic_id and not task_slug:
        return {"error": "Either epic_id or task_slug is required (e.g. task_slug='lint-fix')"}

    cfg = WORKER_TYPES[worker_type]
    session_id = f"{epic_id}-{worker_type}" if epic_id else f"task-{task_slug}-{worker_type}"
    if mode == "legacy":
        return {
            "error": "legacy mode removed: it ran workers with -ns (skills "
            "disabled); use the default standard mode"
        }

    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return {"error": f"cwd must be an existing directory (absolute project dir), got: {cwd!r}"}

    if not os.path.isfile(os.path.expanduser(contract_path)):
        return {"error": f"contract_path not found: {contract_path}"}

    warnings = []
    resolved_skills = []
    for skill in skills:
        expanded = os.path.expanduser(skill)
        if os.path.exists(expanded):
            resolved = expanded
        else:
            found = _resolve_skill_name(skill, cwd)
            if found is None:
                return {"error": f"skill not found: {skill}"}
            resolved = found
        if not _skill_discoverable(resolved, cwd):
            warnings.append(
                f"{skill} is outside pi skill discovery (.agents/.pi under cwd ancestors); "
                "worker reads it only via the absolute path, so the contract must "
                "quote this exact path"
            )
        resolved_skills.append(resolved)
    skills = resolved_skills

    live_pid = _live_session(session_id)
    if live_pid is not None:
        return {"error": f"session {session_id} already running (pid {live_pid})"}

    # Single-line POSIX command, shlex-quoted throughout. Double quotes around
    # $(cat ...) so command substitution happens at shell level.
    parts = [
        "python3",
        CLIENT_PATH,
        "--model",
        model,
        "--session-id",
        session_id,
        "--cwd",
        cwd,
    ]
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
        return {
            "error": f"wall_timeout_s ({resolved_wall}) must be >= timeout_s ({resolved_timeout})"
        }
    parts.extend(["--wall-timeout", str(resolved_wall)])

    if require_edit:
        targets = require_edit if isinstance(require_edit, str) else ",".join(require_edit)
        min_calls = 3 if min_tool_calls is _UNSET else int(min_tool_calls)
        max_nudges_resolved = 4 if max_nudges is _UNSET else int(max_nudges)
        parts.extend(["--require-edit", targets])
        parts.extend(["--min-tool-calls", str(min_calls)])
        parts.extend(["--max-nudges", str(max_nudges_resolved)])
    elif min_tool_calls is not _UNSET or max_nudges is not _UNSET:
        return {
            "error": (
                "min_tool_calls/max_nudges require require_edit "
                "(the gate is inert without an edit target)"
            )
        }
    parts.extend(["--prompt", f"$(cat {shlex.quote(contract_path)})"])

    command = " ".join(shlex.quote(p) for p in parts[:-1])
    # Last part keeps $(cat ...) unquoted at the outer level: shlex.quote would
    # suppress substitution, so emit it double-quoted manually.
    command += f' "$(cat {shlex.quote(contract_path)})"'

    result = {
        "command": command,
        "session_id": session_id,
        "worker_type": worker_type,
        "mode": mode,
    }
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

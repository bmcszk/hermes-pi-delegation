"""Hermes plugin entry: the loader contract, the schemas, and the wiring.

The loader imports this file as a synthetic package whose ``__path__`` is the
plugin directory, so the logic lives in sibling modules. What stays here is
what the host needs from the entry module itself: the registered tools, the
schema/parity assertion, the hot-reload seam, and the surface the plugin is
addressed by.
"""

import importlib
import inspect
import json
import logging
import os
import sys
import time
import types

# The plugin directory is not on sys.path under the host loader, and scripts/
# never is: both are prepended before anything is imported from them.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(_PLUGIN_DIR, "scripts")
for _entry in (_SCRIPTS_DIR, _PLUGIN_DIR):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
# Both loaders name this file as a package: the host as hermes_plugins.<slug>,
# the test runner as plain "__init__". Siblings import relatively, so the
# parent, path and spec must describe that package before the first import.
__package__ = __name__
__path__ = [_PLUGIN_DIR]
if __spec__ is not None:
    __spec__.name = __name__
    __spec__.submodule_search_locations = [_PLUGIN_DIR]
# A dotted name ("hermes_plugins.<slug>") needs its parent present too, or the
# first relative import resolves the parent and dies on it; the host namespace
# carries nothing else, so an empty package is the whole contract.
_parent_name = __package__.rpartition(".")[0]
if _parent_name and _parent_name not in sys.modules:
    _parent = types.ModuleType(_parent_name)
    _parent.__path__ = []
    sys.modules[_parent_name] = _parent


def _sibling(name):
    """Load one module of this plugin package by its own name."""
    return importlib.import_module(f"{__name__}.{name}")


# Loaded through the package rather than a relative import statement: the
# package bootstrap above has to run first, and an import statement placed
# after code is not a module-level import at all.
display_preview = _sibling("display_preview")
launch_dispatch = _sibling("launch_dispatch")
launch_preflight = _sibling("launch_preflight")
launch_warnings = _sibling("launch_warnings")
manage_actions = _sibling("manage_actions")
plugin_constants = _sibling("plugin_constants")
plugin_health = _sibling("plugin_health")
schemas = _sibling("schemas")
session_registry = _sibling("session_registry")

# Configuration the host and the caller read off the entry module.
CLIENT_PATH = plugin_constants.CLIENT_PATH
REGISTRY_PATH = plugin_constants.REGISTRY_PATH
TOOLSETS = plugin_constants.TOOLSETS
GIT_MUTATION_RE = plugin_constants.GIT_MUTATION_RE
WAIT_CEILING_S = plugin_constants.WAIT_CEILING_S
WORKER_AGENT_DIR = plugin_constants.WORKER_AGENT_DIR
WORKER_TIMEOUT_S = plugin_constants.WORKER_TIMEOUT_S
WORKER_TYPES = plugin_constants.WORKER_TYPES
LAUNCH_PARAMS = schemas.LAUNCH_PARAMS

# The staleness digest of the code the gateway imported; a test may substitute it.
_LOADED_SHA = plugin_health.LOADED_SHA
# Why the last registry read skipped a line; the next read resets it.
_last_registry_warning = None

# Test- and caller-facing aliases: the surface the plugin is addressed by.
_pi_launch_impl = launch_dispatch.pi_launch_impl
_pi_manage_impl = manage_actions.pi_manage_impl
_read_registry = session_registry.read_entries
_registry_repair = session_registry.repair_entries
_registry_update = session_registry.update_entry
_record_pending_dispatch = session_registry.record_pending_dispatch
_client_pids = session_registry.client_pids
_client_pid_pattern = session_registry.client_pid_pattern
_newest_registry_entry = session_registry.newest_entry
_pid_alive = session_registry.pid_alive
_launch_concurrency_warnings = launch_warnings.concurrency_warnings
_deployment_models = launch_preflight.deployment_models
_fresh_impl = plugin_health.fresh_impl
_plugin_health = plugin_health.plugin_health
_doctor = plugin_health.doctor

# The plugin's addressed surface: the tool handlers, the configuration above,
# the hot-reload seam and the collaborators a test substitutes.
__all__ = [
    "CLIENT_PATH",
    "LAUNCH_PARAMS",
    "REGISTRY_PATH",
    "TOOLSETS",
    "WAIT_CEILING_S",
    "WORKER_AGENT_DIR",
    "WORKER_TIMEOUT_S",
    "WORKER_TYPES",
    "_LOADED_SHA",
    "_client_pid_pattern",
    "_client_pids",
    "_deployment_models",
    "_doctor",
    "_fresh_impl",
    "_last_registry_warning",
    "_launch_concurrency_warnings",
    "_newest_registry_entry",
    "_pid_alive",
    "_pi_launch_impl",
    "_pi_manage_impl",
    "_plugin_health",
    "_read_registry",
    "_record_pending_dispatch",
    "_registry_repair",
    "_registry_update",
    "on_pre_tool_call",
    "pi_launch",
    "pi_manage",
    "register",
    "time",
]


def _import_agent_display():
    """Return the host's agent.display module, or raise when absent."""
    from agent import display

    return display


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


def _manage_schema():
    return schemas.tool_schema(
        "pi_manage",
        "List, steer or kill pi subagent sessions",
        schemas.manage_properties(),
        ["action"],
    )


def _launch_schema():
    return schemas.tool_schema(
        "pi_launch",
        "Generate a launch command for a pi subagent worker",
        _launch_properties(),
        _launch_required(),
    )


def _register_guarded(ctx, name, toolset, build_schema, handler):
    """Register one tool; a schema failure costs its own tool, not the others."""
    try:
        ctx.register_tool(name=name, toolset=toolset, schema=build_schema(), handler=handler)
    except Exception as exc:  # the surviving tools are the diagnostic surface
        logging.warning(
            "pi-delegation: %s registration failed (%s); the other tools still work",
            name,
            exc,
        )
        return False
    return True


def register(ctx):
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    registered = [
        name
        for name, toolset, build_schema, handler in (
            ("pi_manage", TOOLSETS["pi_manage"], _manage_schema, pi_manage),
            ("pi_launch", TOOLSETS["pi_launch"], _launch_schema, pi_launch),
        )
        if _register_guarded(ctx, name, toolset, build_schema, handler)
    ]
    logging.info("pi-delegation plugin registered: %s", ", ".join(registered) or "none")
    try:
        display_preview.register_display_preview(
            import_display=_import_agent_display,
            builders={
                "pi_launch": display_preview.preview_launch,
                "pi_manage": display_preview.preview_manage,
            },
        )
    except Exception as exc:
        logging.warning("pi-delegation: display preview registration skipped (%s)", exc)


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


def pi_launch(args: dict, **_):
    """Dispatch a pi worker and report plugin freshness."""
    if not args.get("cwd"):
        return json.dumps({"error": "cwd is required (absolute project dir)"})
    return _call_impl("_pi_launch_impl", args)


def on_pre_tool_call(tool_name, args, **_):
    """Parent-side enforcement ONLY.

    Worker-side tool calls never pass through Hermes hooks: workers run as
    standalone python3 processes started outside this hook. Worker
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


_assert_schema_parity()

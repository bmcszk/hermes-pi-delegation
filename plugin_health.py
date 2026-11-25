"""Plugin freshness: the whole-directory digest, the health answer, the doctor.

The gateway imports the plugin once and keeps serving that copy; this module is
how the plugin tells a caller that the code on disk has moved on. The digest
covers every plugin ``*.py`` file, so a change under ``scripts/`` is as visible
as a change to the entry module.
"""

import hashlib
import importlib.util
import inspect
import os
import sys
import time

from . import plugin_runtime as runtime
from .plugin_constants import CLIENT_PATH
from .schemas import LAUNCH_PARAMS


def file_sha(path):
    """SHA-256 of a file's bytes, read through a context manager."""
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def plugin_digest():
    """Deterministic digest over every plugin *.py file (entry + scripts)."""
    plugin_dir = os.path.dirname(os.path.abspath(runtime.entry_path()))
    entries = []
    for dirpath, dirnames, filenames in os.walk(plugin_dir):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for filename in filenames:
            if filename.endswith(".py"):
                full = os.path.join(dirpath, filename)
                entries.append((os.path.relpath(full, plugin_dir), file_sha(full)))
    entries.sort()
    return hashlib.sha256(repr(entries).encode()).hexdigest()


LOADED_SHA = plugin_digest()


def plugin_health():
    """Report whether the running plugin code is older than the code on disk."""
    loaded = runtime.loaded_digest()
    health = {"stale": False, "loaded_sha": loaded, "disk_sha": loaded, "detail": ""}
    try:
        disk_sha = plugin_digest()
    except OSError as exc:
        health["detail"] = f"health unavailable: {exc}"
        return health
    health["disk_sha"] = disk_sha
    health["stale"] = disk_sha != loaded
    if health["stale"]:
        health["detail"] = (
            "plugin file changed since the gateway imported it - run pi_manage(action='doctor'), "
            "then restart the gateway; the registered schema is behind"
        )
    return health


def fresh_impl(name):
    """Return `name` from the plugin entry file as it is on disk right now."""
    # Only code is hot-reloaded; the registered schema still needs the restart.
    entry = runtime.entry_path()
    spec = importlib.util.spec_from_file_location(f"pi_delegation_fresh_{time.time_ns()}", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def doctor():
    """Read-only snapshot of plugin-vs-gateway state; the staleness answer."""
    health = plugin_health()
    impl_params = sorted(
        n
        for n, param in inspect.signature(runtime.launch_impl()).parameters.items()
        if param.kind is not inspect.Parameter.VAR_KEYWORD
    )
    registered = sorted(LAUNCH_PARAMS)
    entry = runtime.entry_path()
    try:
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.stat(entry).st_mtime))
    except OSError:
        mtime = ""
    report = {
        "plugin_path": entry,
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
        "worker_timeout_s": runtime.worker_timeout_s(),
        "python": sys.version.split()[0],
    }
    if report["schema_missing"] or report["stale"]:
        report["fix"] = health["detail"] or "restart the gateway to re-register the schema"
    return report

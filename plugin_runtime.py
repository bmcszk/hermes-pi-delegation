"""The plugin's runtime seam: configuration read from the package at call time.

The test suite substitutes ``REGISTRY_PATH``, ``WORKER_AGENT_DIR``,
``WORKER_TIMEOUT_S``, ``time``, ``_LOADED_SHA`` and a few collaborators on the
package module. Sibling modules resolve those names through the package instead
of holding a copy, so a substitution on the entry module is seen everywhere.
"""

import sys


def _package():
    return sys.modules[__name__.rsplit(".", 1)[0]]


def entry_path():
    """The plugin entry file: the gateway's copy, and the file the digest covers."""
    return _package().__file__


def clock():
    """The process clock; a test substitutes it as ``<package>.time``."""
    return _package().time


def registry_path():
    return _package().REGISTRY_PATH


def client_path():
    return _package().CLIENT_PATH


def worker_agent_dir():
    return _package().WORKER_AGENT_DIR


def worker_timeout_s():
    return _package().WORKER_TIMEOUT_S


def loaded_digest():
    return _package()._LOADED_SHA


def launch_impl():
    return _package()._pi_launch_impl


def read_registry():
    return _package()._read_registry()


def registry_update(session_id, pid, **fields):
    return _package()._registry_update(session_id, pid, **fields)


def set_registry_warning(text):
    _package()._last_registry_warning = text


def client_pids(session_id):
    return _package()._client_pids(session_id)


def record_pending_dispatch(session_id, cwd, mode):
    return _package()._record_pending_dispatch(session_id, cwd, mode)


def deployment_models(agent_dir):
    return _package()._deployment_models(agent_dir)

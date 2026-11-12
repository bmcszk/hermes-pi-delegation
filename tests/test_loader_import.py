"""The plugin must import the way the Hermes plugin loader imports it.

hermes_cli/plugin_host_child.py loads the plugin __init__.py with
submodule_search_locations=[<plugin_dir>], so scripts/ is not on sys.path and
a bare `import registry_log` resolves nowhere. pytest's conftest.py prepends
scripts/, which masked the failure.
"""

import importlib.util
import pathlib
import sys

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
LOADER_MODULE_NAME = "hermes_plugins.probe_pi_delegation"


def _load_as_loader_does():
    spec = importlib.util.spec_from_file_location(
        LOADER_MODULE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = LOADER_MODULE_NAME
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[LOADER_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe_sandbox():
    """Strip pytest's path/module setup so the probe sees only loader context."""
    path_before = list(sys.path)
    hidden_paths = {str(PLUGIN_DIR), str(PLUGIN_DIR / "scripts")}
    registry_log_module = sys.modules.pop("registry_log", None)
    sys.path[:] = [entry for entry in path_before if entry not in hidden_paths]
    modules_before = set(sys.modules)
    yield
    for name in set(sys.modules) - modules_before:
        del sys.modules[name]
    if registry_log_module is not None:
        sys.modules["registry_log"] = registry_log_module
    sys.path[:] = path_before


def test_plugin_imports_under_loader_context(probe_sandbox):
    # when
    module = _load_as_loader_does()

    # then
    assert module.WAIT_CEILING_S == 1500
    assert "require_sentinel" in module.LAUNCH_PARAMS

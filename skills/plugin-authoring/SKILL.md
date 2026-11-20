---
description: 'Write a Hermes plugin that survives core version drift.'
metadata:
    github-path: skills/plugin-authoring
    github-ref: refs/heads/master
    github-repo: https://github.com/bmcszk/hermes-pi-delegation
    github-tree-sha: 0000000000000000000000000000000000000000
name: plugin-authoring
---
# Plugin authoring skill

How to build a Hermes plugin that survives core upgrades. Every rule is a
practice observed in shipped plugins or verified in the loader source (P-numbers
cite the practice survey).

## Layout: thin entry, logic in modules

A plugin is a directory with `plugin.yaml` and `__init__.py`; the loader skips
a directory missing either, silently (P1, P8). `__init__.py` holds
`register(ctx)` and little else - shipped plugins keep it at 10-57 lines, with
the manifest saying what the plugin is, `schemas.py` what the model reads and
`tools.py` what runs (P1).

The loader imports the directory as a synthetic package whose `__package__` is
`hermes_plugins.<slug>` and whose `__path__` is the plugin directory. So:

- Import your own modules **relatively** (`from . import tools`).
- A bare `import helper` for a file in a *subdirectory* resolves nowhere: those
  are not on `sys.path`. Keep modules at the plugin root, or prepend the
  subdirectory to `sys.path` before importing.
- Never import a sibling plugin: one plugin, one repo, one directory (P12).

## Registration: fail one tool, not all of them

`register(ctx)` runs once at startup, and an exception disables the whole
plugin - one bad schema build would take every tool down (P9). Guard each
registration, log one warning naming the failure, keep going:

```python
def _register_guarded(ctx, name, toolset, build_schema, handler):
    try:
        ctx.register_tool(name=name, toolset=toolset, schema=build_schema(), handler=handler)
    except Exception as exc:
        logging.warning("my-plugin: %s registration failed (%s); other tools work", name, exc)
        return False
    return True
```

Register the tools you diagnose with first, so a partial load leaves something
usable, and log the final registered set so a half load shows in one line (P9).

## Schemas: make invalid values unrepresentable

A free string parameter costs a round trip per typo. For a closed set put an
`enum` in the schema, derived from the table the runtime reads so it cannot
drift (P2):

```python
MODES = {"fast": ..., "thorough": ...}
"mode": {"type": "string", "enum": sorted(MODES), "description": "..."}
```

Keep the runtime rejection too: a host holding a stale schema still lets an
invalid value through, and a silent fallback hides the mistake instead of
naming the valid set (P11).

```python
if mode not in MODES:
    return {"error": f"Unknown mode: {mode}. Valid: {sorted(MODES)}"}
```

Assert schema/implementation parity at import, so a schema listing a parameter
the handler rejects fails at load, not at the first call. Keep that assert
key-level: a value-level change (an enum) is safe; adding or removing a
parameter to satisfy it is not.

## Manifest: version floor and display name

Declare the oldest core that certainly provides the seam you use (P8):

```yaml
name: my-plugin
label: My Plugin          # human-readable name for catalogs
version: 1.0.0
requires_hermes: ">=0.21.6"
provides_tools: [my_tool]
provides_hooks: [pre_tool_call]
```

The loader refuses a plugin whose `requires_hermes` rejects the running core,
with `requires hermes <spec>, running <version>`: a clean refusal instead of a
broken load (P4). Pick the floor from the API you actually call - too high
locks out working installs, too low is harmless, since the floor is only ever
a message.

## Failure behaviour: degrade with a warning

- **Auxiliary** registrations (bundled skill, optional integration) go inside
  `try/except`, log one warning, and never take the main capability down (P9).
- **Availability gating** uses `check_fn` on `ctx.register_tool`: with the
  optional dependency missing, the tool drops out of the model's toolset
  instead of erroring at call time (P3).

Never swallow an exception with no log line: a partial load that logs nothing
is indistinguishable from a complete one.

## Hot reload: what a restart actually changes

- Modules are imported once per load. Editing an already-imported file changes
  nothing until the plugin reloads or the host restarts.
- A module added *after* load is not visible at all: editing an existing module
  and adding a new one are different operations, and only a reload covers both.
- Anything the host copied out of your module (schemas, handler bindings, hook
  callbacks) keeps its loaded value even after the code reloads from disk, so a
  behaviour fix can hot-reload while a schema change needs a restart.
- If the plugin reports its own freshness, hash **every** file it ships, not
  just `__init__.py`: one-file hashing reports `fresh` while edits elsewhere run
  stale, worse than no check at all.

## Test the loader wiring, not just the code

Importing the plugin from the test runner's `sys.path` proves nothing about how
the host imports it. Reproduce the loader (P5) with those paths stripped:

```python
spec = importlib.util.spec_from_file_location(
    "hermes_plugins.probe_my_plugin",
    PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
module = importlib.util.module_from_spec(spec)
module.__package__, module.__path__ = "hermes_plugins.probe_my_plugin", [str(PLUGIN_DIR)]
spec.loader.exec_module(module)
```

Drive `register(ctx)` against a recording fake `ctx` too, so schemas are
exercised as the host sees them.

## State, logging, and paths

- Durable state goes under the plugin data root, never the plugin directory (an
  update or remove deletes it) and never a temp dir (P10). No absolute machine
  paths, no credentials in code: read config or environment.
- `logger = logging.getLogger(__name__)` per module: a plugin that logs nothing
  useful cannot be debugged from a running host.

## Verification checklist

- [ ] `plugin.yaml` and `__init__.py` exist; `register(ctx)` is defined; the
      entry stays thin and imports siblings relatively.
- [ ] Loader-shaped import test passes with the plugin dirs off `sys.path`.
- [ ] Every closed-set parameter carries an enum derived from its source table.
- [ ] Schema/implementation parity assert passes at import.
- [ ] `requires_hermes` names the oldest core version you verified.
- [ ] Each tool registers inside its own guard; a failure logs one warning and
      the final log line names what did register.
- [ ] No state written inside the plugin directory.
- [ ] `hermes plugins doctor <path>` loads it clean in a throwaway home.

"""Tool schemas: the payload tables the model reads, authored once.

`LAUNCH_PARAMS` is the pi_launch payload table; the entry module builds the
registered schema from it and asserts it against the impl signature at import,
so a parameter cannot exist in one and not the other.
"""

from .plugin_constants import WORKER_TYPES


def tool_schema(name, description, properties, required):
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
            "default true: dispatches the generated command as a detached background "
            "worker and returns dispatched/pid/session_id/log with no command string; "
            "false = diagnostic printout of the equivalent command, spawns nothing, "
            "do NOT wait on the named session"
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
        "enum": sorted(WORKER_TYPES),
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


def manage_properties():
    return {
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
                "verify",
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
    }

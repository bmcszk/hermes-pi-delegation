"""Plugin configuration: paths, worker policy, budgets and matching tables."""

import os
import re

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
# Age at which an unresolved pending-dispatch row stops meaning "slow client"
# and starts meaning "never dispatched": wait then classifies via pgrep
# instead of burning the rest of its budget.
DISPATCH_GRACE_S = 25
_UNSET = object()  # distinguishes "caller passed it" from the signature default

_SLUG_PREVIEW_CAP = 32

_MANAGE_ACTIONS = {
    "list",
    "kill",
    "doctor",
    "logs",
    "wait",
    "steer",
    "salvage_report",
    "verify",
}

PROJECT_MARKERS = (
    ("go.mod", "go"),
    ("pyproject.toml", "python"),
    ("plugin.yaml", "plugin"),
)

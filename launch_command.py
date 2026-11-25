"""Launch command assembly: one intent function, one stage per argv block.

The emitted command is a single POSIX line that runs pi_rpc_client with the
worker's whole policy on it. Each stage owns one block of that line and the
warnings that block can raise, so the caller reads the order the flags land in.
"""

import os
import shlex

from . import plugin_runtime as runtime
from .launch_preflight import compliance_block
from .plugin_constants import _UNSET
from .plugin_health import plugin_digest

PREFLIGHT_OK_LINE = (
    "echo 'preflight ok: contract, anchors, skills, budgets, cwd, model, naming' && "
)


class LaunchRefusedError(Exception):
    """A launch argument that cannot produce a valid command."""


def build_launch_command(plan):
    """Compose (command, system_prompt, warnings) for one resolved launch plan."""
    head, system_prompt, head_warnings = _client_argv(plan)
    budget = _resolve_budget(plan["timeout_s"], plan["wall_timeout_s"])
    budget_parts, budget_warnings = _budget_argv(plan, budget)
    gate = _resolve_gate(plan)
    gate_parts, gate_warnings = _gate_argv(plan, gate)
    parts = head + budget_parts + gate_parts
    parts.extend(["--prompt", f"$(cat {shlex.quote(plan['contract_path'])})"])
    command = _compose_command(parts, plan, budget, gate)
    return command, system_prompt, head_warnings + budget_warnings + gate_warnings


def _client_argv(plan):
    """The client invocation up to its budget flags, plus the system prompt."""
    agent_dir = runtime.worker_agent_dir()
    parts = [
        "python3",
        plan["client_path"],
        "--session-id",
        plan["session_id"],
        "--cwd",
        plan["cwd"],
    ]
    if plan["model"]:
        parts.extend(["--model", plan["model"]])
    # NOTE: no --ns anywhere: pi's -ns/--no-skills disables skill loading
    # outright, and every worker must run with its skills. Legacy mode is
    # rejected before this point.
    # The bytes we were imported from still match disk, so this module IS the
    # disk code (only hit if Hermes fails to hot-reload): tell the client to
    # skip its mtime staleness heuristic. A mid-call edit makes the hash
    # mismatch and drops the flag; the heuristic then applies.
    if plugin_digest() == runtime.loaded_digest():
        parts.append("--plugin-fresh")
    for skill in plan["skills"]:
        parts.extend(["--skill", skill])
    system_prompt = None
    if plan["skills"]:
        system_prompt = compliance_block(plan["skills"])
        parts.extend(["--append-system-prompt", system_prompt])
    warnings = []
    # Worker agent dir: excludes npm:pi-subagents (workers must not delegate)
    # and swaps in a no-delegation AGENTS.md; auth/models come along via
    # symlink, so model catalogs and providers are unchanged.
    if os.path.isdir(agent_dir):
        parts.extend(["--agent-dir", agent_dir])
    else:
        warnings.append(
            f"worker agent dir {agent_dir} missing: pi-subagents NOT disabled and default "
            "AGENTS.md in effect; run the setup from the pi-delegation skill"
        )
    parts.extend(_worker_policy_flags(plan["worker_config"]))
    return parts, system_prompt, warnings


def _worker_policy_flags(worker_config):
    parts = []
    if worker_config["use_codemode"]:
        parts.append("--codemode")
    if worker_config.get("text_deliverable"):
        parts.append("--auditor-deliverable")
    if worker_config["tools"]:
        parts.extend(["--tools", worker_config["tools"]])
    if worker_config["thinking"]:
        parts.extend(["--thinking", worker_config["thinking"]])
    return parts


def _resolve_budget(timeout_s, wall_timeout_s):
    """(per-call, wall) seconds; a wall shorter than the idle budget is refused."""
    try:
        per_call = runtime.worker_timeout_s() if timeout_s is None else int(timeout_s)
    except (TypeError, ValueError):
        raise LaunchRefusedError(f"timeout_s must be an integer, got {timeout_s!r}") from None
    if not 30 <= per_call <= 21600:
        raise LaunchRefusedError(f"timeout_s must be between 30 and 21600, got {timeout_s!r}")
    try:
        wall = None if wall_timeout_s is None else int(wall_timeout_s)
    except (TypeError, ValueError):
        raise LaunchRefusedError(
            f"wall_timeout_s must be an integer, got {wall_timeout_s!r}"
        ) from None
    if wall is None:
        # Wall must exceed the idle budget or long sweeps die mid-contract.
        wall = max(3600, per_call + 900)
    elif wall < per_call:
        raise LaunchRefusedError(
            f"preflight: budgets - wall_timeout_s ({wall}) must be >= timeout_s ({per_call})"
        )
    return {"per_call": per_call, "wall": wall}


def _budget_argv(plan, budget):
    parts = ["--timeout", str(budget["per_call"]), "--wall-timeout", str(budget["wall"])]
    warnings = []
    if plan["timeout_s"] is None:
        source = (
            "PI_WORKER_TIMEOUT_S" if "PI_WORKER_TIMEOUT_S" in os.environ else "built-in default"
        )
        warnings.append(
            f"budget warn: timeout_s not set - defaulting to {budget['per_call']}s ({source})"
        )
    if plan["wall_timeout_s"] is None:
        warnings.append(
            f"budget warn: wall_timeout_s not set - defaulting to {budget['wall']}s "
            "(max(3600, timeout_s + 900))"
        )
    return parts, warnings


def _resolve_gate(plan):
    """Resolve the nudge gate: enforced call floor, nudge ceiling, budget clause.

    Without require_edit the gate has no aim, so it stays off and the budget
    line says so; preflight already refused min/max in that configuration.
    """
    if not plan["require_edit"]:
        return {"armed": False, "clause": "nudge: 0 - no correction loop", "min_calls": 0}
    defaults = (1, 0) if plan["worker_type"] == "auditor" else (3, 4)
    min_calls = defaults[0] if plan["min_tool_calls"] is _UNSET else int(plan["min_tool_calls"])
    max_nudges = defaults[1] if plan["max_nudges"] is _UNSET else int(plan["max_nudges"])
    clause = (
        "nudge: 0 - no correction loop"
        if max_nudges == 0
        else f"nudge: >={min_calls} tool calls, <={max_nudges} nudges then exit 3"
    )
    gate_off = plan["min_tool_calls"] is _UNSET and plan["max_nudges"] is _UNSET
    return {
        "armed": True,
        "clause": clause,
        "min_calls": min_calls,
        "max_nudges": max_nudges,
        "defaults_used": gate_off,
    }


def _gate_argv(plan, gate):
    if not gate["armed"]:
        return [], []
    targets = (
        plan["require_edit"]
        if isinstance(plan["require_edit"], str)
        else ",".join(plan["require_edit"])
    )
    parts = [
        "--require-edit",
        targets,
        "--min-tool-calls",
        str(gate["min_calls"]),
        "--max-nudges",
        str(gate["max_nudges"]),
    ]
    warnings = []
    if gate["defaults_used"]:
        warnings.append(
            f"budget warn: min_tool_calls/max_nudges not set - nudge gate off ({gate['clause']})"
        )
    if plan["require_sentinel"]:
        parts.extend(["--require-sentinel", plan["require_sentinel"]])
    return parts, warnings


def _compose_command(parts, plan, budget, gate):
    """Join argv into one shell line, with the preflight echo and budget echo."""
    budget_note = (
        f"budget: per-call <= {budget['per_call']}s, wall <= {budget['wall']}s, {gate['clause']}"
    )
    if plan["require_sentinel"]:
        budget_note += f", deliverable: sentinel {plan['require_sentinel']!r}"
    command = PREFLIGHT_OK_LINE
    command += f"echo {shlex.quote(budget_note)} && "
    command += " ".join(shlex.quote(p) for p in parts[:-1])
    # Last part keeps $(cat ...) unquoted at the outer level: shlex.quote would
    # suppress substitution, so emit it double-quoted manually.
    contract_quote = shlex.quote(plan["contract_path"])
    command += f' "$(cat {contract_quote})"'
    return command

"""Launch preflight: fail fast, name every failed check, then build the command.

A dispatch that would waste a worker run is refused here, before anything is
spawned. Each stage names itself in the failure text, so one answer lists every
broken input instead of only the first.
"""

import json
import os

from . import plugin_runtime as runtime
from .plugin_constants import _UNSET


def preflight(
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
    """Run every launch check in order; return (failures, warnings)."""
    failures = []
    warnings = []
    _check_naming(failures, epic_id, task_slug)
    model_failures, model_warnings = model_check_rows(model, runtime.worker_agent_dir())
    failures.extend(model_failures)
    warnings.extend(model_warnings)
    _check_contract(failures, contract_path)
    _check_anchors(failures, cwd, require_edit, create_anchor)
    _check_skills(failures, resolved_skills)
    _check_budget_pair(failures, timeout_s, wall_timeout_s)
    _check_worker_aim(failures, warnings, worker_type, require_edit)
    _check_nudge_budget(failures, min_tool_calls, max_nudges, require_edit)
    _check_sentinel(failures, warnings, require_sentinel, require_edit, contract_path)
    _check_repo_marker(failures, cwd)
    return failures, warnings


def _check_naming(failures, epic_id, task_slug):
    if epic_id and task_slug:
        failures.append(
            f"naming - epic_id and task_slug are mutually exclusive "
            f"(epic_id wins the session id); drop one: epic_id={epic_id}, task_slug={task_slug}"
        )


def _check_contract(failures, contract_path):
    if not os.path.isfile(os.path.expanduser(contract_path)):
        failures.append(f"contract - not found or unreadable: {contract_path}")


def _check_anchors(failures, cwd, require_edit, create_anchor):
    """require_edit targets must exist, or be created as append-only anchors."""
    if not require_edit:
        return
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
                    f.write(anchor_header(anchored) + "\n")
            except OSError as exc:
                failures.append(f"require_edit anchors - cannot create {anchor}: {exc}")
        else:
            failures.append(
                f"require_edit anchors - missing: {anchored} "
                "(exists-check; create_anchor=true to create it)"
            )


def _check_skills(failures, resolved_skills):
    for skill in resolved_skills:
        if not os.path.isdir(skill):
            failures.append(f"skills - not a directory: {skill}")
        elif not os.path.isfile(os.path.join(skill, "SKILL.md")):
            failures.append(f"skills - directory lacks SKILL.md: {skill}")


def _check_budget_pair(failures, timeout_s, wall_timeout_s):
    if (
        wall_timeout_s is not None
        and timeout_s is not None
        and int(wall_timeout_s) <= int(timeout_s)
    ):
        failures.append(
            f"budgets - wall_timeout_s ({wall_timeout_s}) must exceed timeout_s ({timeout_s})"
        )


def _check_worker_aim(failures, warnings, worker_type, require_edit):
    """An auditor without a deliverable cannot land; a coder without one can cheat."""
    if worker_type == "auditor" and not require_edit:
        failures.append(
            "auditor - mode aims at no deliverable: require_edit names the file the "
            "final text is written into"
        )
    if worker_type == "coder" and not require_edit:
        warnings.append(
            "preflight warn: coder without require_edit - a worker that ends its turn "
            "early yields exit 0 with zero edits and no gate catches it; pass require_edit "
            "to arm the nudge gate"
        )


def _check_nudge_budget(failures, min_tool_calls, max_nudges, require_edit):
    min_max_passed = min_tool_calls is not _UNSET or max_nudges is not _UNSET
    min_calls = 0 if min_tool_calls is _UNSET else int(min_tool_calls or 0)
    max_nudges_resolved = 0 if max_nudges is _UNSET else int(max_nudges or 0)
    if max_nudges_resolved > 0 and min_calls < 1:
        failures.append(
            "budgets - min_tool_calls < 1 while max_nudges > 0: the nudge gate "
            "cannot tell work from luck"
        )
    if min_max_passed and not require_edit:
        failures.append(
            "budgets - min_tool_calls/max_nudges set without require_edit: the gate has no aim"
        )


def _check_sentinel(failures, warnings, require_sentinel, require_edit, contract_path):
    if require_sentinel and not require_edit:
        failures.append(
            "require_sentinel - set without require_edit: the sentinel check has no file to "
            "grep; name the deliverable file in require_edit"
        )
    elif require_sentinel:
        _warn_sentinel_absent_from_contract(warnings, require_sentinel, contract_path)


def _warn_sentinel_absent_from_contract(warnings, require_sentinel, contract_path):
    try:
        with open(os.path.expanduser(contract_path), encoding="utf-8", errors="replace") as f:
            contract_text = f.read()
    except OSError:
        return  # the exists-check above already fails the launch for this file
    if require_sentinel not in contract_text:
        warnings.append(
            f"preflight warn: sentinel {require_sentinel!r} never appears in the "
            "contract; the worker has no instruction to write it"
        )


def _check_repo_marker(failures, cwd):
    if not (
        os.path.isfile(os.path.join(cwd, ".beans.yml"))
        or os.path.isfile(os.path.join(cwd, "AGENTS.md"))
    ):
        failures.append(
            "cwd - right-repo guard: launch cwd has neither .beans.yml nor AGENTS.md at "
            "its root; add one of them (e.g. AGENTS.md)"
        )


def model_check_rows(model, agent_dir):
    """(failures, warnings) for the launch model argument against agent_dir."""
    failures, warnings = [], []
    known, default, readable = runtime.deployment_models(agent_dir)
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


def deployment_models(agent_dir):
    """(known_aliases, default_model, readable) from one pi agent dir.

    readable=False when the list cannot be established at all; the caller then
    degrades to a warning instead of refusing the launch.
    """
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


def session_id_for(cwd, worker_type, epic_id, task_slug):
    """Session naming: explicit ids win, else derive one from the project dir."""
    if epic_id:
        return f"{epic_id}-{worker_type}"
    if task_slug:
        return f"task-{task_slug}-{worker_type}"
    return f"task-{os.path.basename(os.path.normpath(cwd))}-{worker_type}"


def anchor_header(path):
    """Standard append-only lane-log header for a require_edit anchor."""
    slug = os.path.splitext(os.path.basename(path))[0]
    return f"## {slug} lane log (append-only)"


def compliance_block(skills):
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

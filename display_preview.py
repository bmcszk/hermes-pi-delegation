"""Display previews: the progress-bubble label a tool call shows in the UI.

Pure label composition, never raising: a preview is presentation only, so a
malformed call must degrade to a readable name instead of failing the tool.
"""

import logging

from .plugin_constants import _MANAGE_ACTIONS, _SLUG_PREVIEW_CAP, WORKER_TYPES


def preview_launch(args, max_len):
    """Compose the pi_launch progress-bubble label; never raises.

    Shape: <slug cap 32> · <worker_type> · <model when set> · "codemode" when
    the worker_type policy enables it · "echo-only" when mode != standard.
    """
    try:
        parts = [str(args.get("task_slug") or "")[:_SLUG_PREVIEW_CAP]]
        worker_type = str(args.get("worker_type") or "")
        parts.append(worker_type)
        model = str(args.get("model") or "")
        if model:
            parts.append(model)
        if WORKER_TYPES.get(worker_type, {}).get("use_codemode"):
            parts.append("codemode")
        if args.get("mode", "standard") != "standard":
            parts.append("echo-only")
        return " · ".join(part for part in parts if part)
    except Exception:
        return None


def preview_manage(args, max_len):
    """Compose the pi_manage progress-bubble label; never raises.

    Shape: <action> · <session_id cap 32> · "t <timeout_s>" when waiting ·
    "+prompt"/"+empty" when steering. Returns "manage" for unknown calls.
    """
    try:
        action = str(args.get("action") or "")
        if action not in _MANAGE_ACTIONS:
            return "manage"
        parts = [action]
        session_id = str(args.get("session_id") or "")
        if session_id:
            parts.append(session_id[:_SLUG_PREVIEW_CAP])
        if action == "wait":
            timeout_s = args.get("timeout_s")
            if timeout_s is not None:
                parts.append(f"t {timeout_s}")
        if action == "steer":
            parts.append("+prompt" if args.get("prompt") else "+empty")
        return " · ".join(parts)
    except Exception:
        return "manage"


def register_display_preview(import_display, builders):
    """Teach agent.display to render this plugin's bubbles.

    The display module and its two registries belong to the host, so they are
    parameters: an absent or reshaped host module degrades to the caller's
    warning instead of breaking tool registration.
    """
    display = import_display()
    display._PREVIEW_BUILDERS["pi_launch"] = builders["pi_launch"]
    display._PRIMARY_ARGS["pi_launch"] = "worker_type"
    display._PREVIEW_BUILDERS["pi_manage"] = builders["pi_manage"]
    display._PRIMARY_ARGS["pi_manage"] = "action"
    logging.info("pi-delegation: display preview registered for pi_launch and pi_manage")

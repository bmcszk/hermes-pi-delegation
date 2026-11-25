"""Exit reports: salvage_report, verify, logs, and the per-project quick gates.

Read-only and best-effort throughout: every leg that cannot be established
degrades to a named skip, so a report that could not read a fact never reads
like a clean tree.
"""

import glob
import json
import os
import subprocess
import time

from .plugin_constants import PROJECT_MARKERS
from .session_registry import (
    base_is_valid,
    git_readonly,
    lane_lock_info,
    newest_entry,
    now_iso,
    row_age_s,
)
from .session_wait import tree_summary, wait_finished


def project_type(cwd):
    """Project type of the recorded cwd: go, python, plugin, or unknown."""
    if not cwd:
        return "unknown"
    for marker, kind in PROJECT_MARKERS:
        if os.path.isfile(os.path.join(cwd, marker)):
            return kind
    return "unknown"


def gate_commands(kind, go_tests):
    """(name, command) pairs for one project type; go test stays opt-in."""
    if kind == "go":
        gates = [
            (
                "gofmt",
                "unformatted=$(gofmt -l .) || exit 1; "
                '[ -z "$unformatted" ] || { echo "$unformatted"; exit 1; }',
            ),
            ("go-build", "go build ./..."),
        ]
        if go_tests:
            gates.append(("go-test", "go test ./..."))
        return gates
    if kind == "python":
        return [("ruff-check", "uvx ruff check ."), ("compileall", "python -m compileall -q .")]
    if kind == "plugin":
        return [
            ("uvx-ruff-check", "uvx ruff check ."),
            ("compileall-plugin", "python -m compileall -q __init__.py scripts tests"),
        ]
    return []


def project_gates(cwd, go_tests=False):
    """Gate rows for the recorded cwd's project type, plus a named skip when
    the type cannot be established, so an empty gate list never reads as pass."""
    kind = project_type(cwd)
    commands = gate_commands(kind, go_tests)
    if not commands:
        reason = (
            "no recorded cwd for this row"
            if not cwd
            else f"no project marker (go.mod, pyproject.toml, plugin.yaml) in {cwd}"
        )
        return [{"name": "project-type", "command": "", "ok": False, "skipped": reason}]
    return [gate_row(name, command, cwd) for name, command in commands]


def gate_row(name, command, cwd):
    """One salvage gate row: ok plus a short output tail or a named reason.

    cwd comes from the registry row: the gate must judge THE SESSION'S tree,
    not the caller's. A failing invocation (command absent, unwritable cwd,
    120 s timeout cap) degrades to a skipped row."""
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120, cwd=cwd
        )
        tail = (proc.stdout + proc.stderr)[-2000:]
        return {"name": name, "command": command, "ok": proc.returncode == 0, "output_tail": tail}
    except Exception as exc:
        return {
            "name": name,
            "command": command,
            "ok": False,
            "output_tail": "",
            "skipped": str(exc),
        }


def salvage_report(session_id, lines=12, go_tests=False):
    """Post-exit state collection for one session, in one call, shaped to
    paste as the on-disk state section of a continuation contract. Read-only,
    best-effort: every leg degrades to a named skip instead of raising.
    """
    entry = newest_entry(session_id)
    if entry is None:
        return {"finished": False, "status": "unknown"}
    if entry.get("status") == "pending-dispatch":
        return {
            "finished": False,
            "status": "pending-dispatch",
            "session": session_id,
            "pending_age_s": row_age_s(entry),
        }
    cwd = entry.get("cwd")
    base = entry.get("base")
    valid_base = base_is_valid(cwd, base) if cwd else False

    git_report = {}
    if cwd and git_readonly(cwd, "status")[0]:
        ok_status, status_out = git_readonly(cwd, "status", "--porcelain")
        git_report["status"] = status_out if ok_status else ""
        ok_head, head_out = git_readonly(cwd, "log", "--oneline", "-1")
        ok_sha, sha_out = git_readonly(cwd, "rev-parse", "HEAD")
        git_report["branch_head"] = "\n".join(x.strip() for x in (head_out, sha_out) if x)
        if valid_base:
            ok_log, log_out = git_readonly(cwd, "log", "--oneline", f"{base}..HEAD")
            git_report["commits_since_base"] = (
                log_out.splitlines() if ok_log else {"skipped": log_out}
            )
            ok_diff, diff_out = git_readonly(cwd, "diff", f"{base}..HEAD", "--stat")
            git_report["diff_stat"] = (
                diff_out.splitlines()[-6:] if ok_diff else {"skipped": diff_out}
            )
        else:
            reason = (
                "no base recorded for this row"
                if not base
                else "unknown revision: base not a commit of this repo"
            )
            git_report["commits_since_base"] = {"skipped": reason}
            git_report["diff_stat"] = {"skipped": f"{reason}; uncommitted facts visible via status"}
    else:
        reason = "no recorded cwd for this row" if not cwd else f"cwd is not a git repo: {cwd}"
        git_report["status"] = ""
        git_report["branch_head"] = ""
        git_report["commits_since_base"] = {"skipped": reason}
        git_report["diff_stat"] = {"skipped": reason}

    frames = session_logs(session_id, lines)

    lock = lane_lock_info(cwd, session_id)
    if lock is not None:
        lane_lock_row = {
            "path": lock["path"],
            "stale": lock["stale"],
            "payload": lock["payload"],
            "removal": f"rm {lock['path']}" + ("  # stale: pid gone" if lock["stale"] else ""),
        }
    else:
        lane_lock_row = {"present": False}

    return {
        "session": session_id,
        "cwd": cwd,
        "base": base,
        "collected_at": now_iso(),
        "git": git_report,
        "frames": {
            k: frames[k]
            for k in ("error", "path", "mtime", "tool_calls", "last_text", "tail")
            if k in frames
        },
        "lane_lock": lane_lock_row,
        "project_type": project_type(cwd),
        "gates": project_gates(cwd, go_tests),
    }


def verify_session(session_id):
    """One-call, read-only exit drill for a recorded session: the registry's
    exit facts plus the tree facts that override them, combined into a single
    ok verdict. Exit codes lie in both directions - exit 3 with a fully
    committed and pushed tree is a completed run, exit 0 with uncommitted
    residue is not - so the verdict judges the tree, and names whichever
    contradiction it found. Every leg degrades to a named skip, never raises.
    Distinct from salvage_report: that one is the fast post-exit state dump
    with per-project gates; verify adds the upstream comparison and one verdict.
    """
    entry = newest_entry(session_id)
    if entry is None:
        return {"finished": False, "status": "unknown"}
    if entry.get("status") == "pending-dispatch":
        return {
            "finished": False,
            "status": "pending-dispatch",
            "session": session_id,
            "pending_age_s": row_age_s(entry),
        }
    cwd = entry.get("cwd")
    tree = tree_summary(cwd, entry.get("base"))
    uncommitted = tree["dirty"] if isinstance(tree["dirty"], int) else None
    verdict = deliverable_verdict_for_row(entry, cwd)
    result = {
        "session": session_id,
        "finished": wait_finished(entry),
        "status": entry.get("status") or "running",
        "exit_code": entry.get("exit_code"),
        "uncommitted_files": uncommitted,
        "commits_since_base": tree["commits_since_base"],
        "unpushed_commits": tree["unpushed"],
        "tree": tree,
        "deliverable": verdict,
    }
    result["ok"], result["note"] = verify_verdict(result, tree)
    return result


def deliverable_verdict_for_row(entry, cwd):
    """Re-check the row's sentinel against its deliverable file, or pass the
    recorded verdict through when the row never armed a sentinel. A relative
    require_edit path resolves against the row's recorded cwd."""
    sentinel = entry.get("require_sentinel")
    if not sentinel:
        return entry.get("deliverable") or "unchecked"
    path = entry.get("require_edit")
    if isinstance(path, list):
        path = path[0] if path else None
    import pi_rpc_client

    if path and not os.path.isabs(path):
        path = os.path.join(cwd or ".", path)
    return pi_rpc_client._deliverable_verdict(path, sentinel)


def verify_verdict(result, tree):
    """(ok, note) from the verify legs: tree clean, nothing unpushed, and
    either client-satisfied or the done-anyway contradiction named."""
    residue = [
        name
        for name in ("dirty", "commits_since_base", "unpushed")
        if isinstance(tree.get(name), dict)
    ]
    note_parts = []
    if result["uncommitted_files"]:
        note_parts.append(
            f"exit {result['exit_code']} but "
            f"{result['uncommitted_files']} uncommitted file(s) remain"
        )
    if isinstance(tree.get("unpushed"), int) and tree["unpushed"]:
        note_parts.append(f"{tree['unpushed']} unpushed commit(s)")
    exit_zero_unmet = result["exit_code"] == 0 and result["deliverable"] == "unmet"
    if exit_zero_unmet:
        note_parts.append("exit 0 but the sentinel is absent from the deliverable")
    if note_parts:
        return False, "; ".join(note_parts)
    if result["exit_code"] not in (0, None) and not residue:
        return True, (
            f"exit {result['exit_code']} yet the tree is committed, clean and pushed: "
            "the work is done despite the client's own verdict"
        )
    return True, ""


def transcript_for(session_id):
    """Resolve a session's transcript path, or None.

    Registry log_path first (the client learned pi's real transcript even when
    its filename lacks the session slug), then the legacy slug glob for rows
    recorded before that fix. A pending-dispatch row resolves nothing: its
    log_path, if any, points at the dispatch log, not a transcript.
    """
    entry = newest_entry(session_id)
    if entry is not None and entry.get("status") != "pending-dispatch":
        registered = entry.get("log_path")
        if registered and os.path.isfile(registered):
            return registered
    pattern = os.path.expanduser(f"~/.pi/agent/sessions/*/*{session_id}*.jsonl")
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return matches[0] if matches else None


def session_logs(session_id, lines=40):
    """Tail the newest pi session log for a session id."""
    if not session_id:
        return {"error": "logs requires session_id"}
    path = transcript_for(session_id)
    if not path:
        return {"error": f"no session log for {session_id}"}
    tail, tool_calls, last_text = [], 0, ""
    try:
        with open(path) as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                message = row.get("message") or {}
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    kind = part.get("type")
                    if kind == "toolCall":
                        tool_calls += 1
                        tail.append("toolCall: {}".format(part.get("name", "")))
                    elif kind == "text":
                        last_text = part.get("text", "")
                        tail.append("text: {}".format(last_text[:160].replace("\n", " ")))
    except OSError as exc:
        return {"error": f"cannot read {path}: {exc}"}
    return {
        "session_id": session_id,
        "path": path,
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
        "tool_calls": tool_calls,
        "last_text": last_text[:400],
        "tail": tail[-int(lines) :],
    }

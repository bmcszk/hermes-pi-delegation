import json
import os
import subprocess
import time

from helpers import (
    _parse,
    _setup_registry,
    _spawn_real_client,
    _teardown_registry,
    _write_registry,
)

import __init__ as p

GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "salvage",
    "GIT_AUTHOR_EMAIL": "salvage@test",
    "GIT_COMMITTER_NAME": "salvage",
    "GIT_COMMITTER_EMAIL": "salvage@test",
}


def _make_repo(pwd):
    """Init a tmp git repo: one base commit, then one later 'work' commit."""
    env = {**os.environ, **GIT_IDENTITY_ENV}
    subprocess.run(["git", "init", "-q", "-b", "main", pwd], check=True, env=env)
    subprocess.run(["git", "config", "user.email", "s@t"], cwd=pwd, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "s"], cwd=pwd, check=True, env=env)
    with open(os.path.join(pwd, "a.txt"), "w") as f:
        f.write("base\n")
    subprocess.run(["git", "add", "a.txt"], cwd=pwd, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "base commit"], cwd=pwd, check=True, env=env)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=pwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()
    with open(os.path.join(pwd, "a.txt"), "w") as f:
        f.write("changed\n")
    subprocess.run(["git", "add", "a.txt"], cwd=pwd, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "work commit"], cwd=pwd, check=True, env=env)
    with open(os.path.join(pwd, "a.txt"), "w") as f:
        f.write("uncommitted\n")
    return base


def test_salvage_report_collects_one_call(tmp_path):
    """One kill-shaped row (cwd/base/log_path stamped): the whole salvage
    order in a single answer, shaped for a continuation contract."""
    repo = str(tmp_path / "repo")
    base = _make_repo(repo)
    transcript = tmp_path / "pi" / "sessions" / "anywhere" / "t_epicSal-coder.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash"}]},
            }
        )
        + "\n"
    )
    _setup_registry(str(tmp_path))
    try:
        _write_registry(
            {
                "session": "epicSal-coder",
                "pid": 999999,
                "status": "killed",
                "exit_code": 143,
                "cwd": repo,
                "base": base,
                "log_path": str(transcript),
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicSal-coder", lines=5))
        assert "error" not in result, result
        assert result["session"] == "epicSal-coder"
        assert result["cwd"] == repo
        assert result["base"] == base
        assert "collected_at" in result
        assert result["git"]["status"].strip(), result["git"]
        assert result["git"]["commits_since_base"][0].endswith("work commit"), result["git"]
        assert any("a.txt" in line for line in result["git"]["diff_stat"]), result["git"]
        assert result["git"]["branch_head"], result["git"]
        assert result["frames"]["path"] == str(transcript), result
        assert result["frames"]["tool_calls"] == 1, result["frames"]
        gates = {row["name"]: row for row in result["gates"]}
        assert set(gates) == {"uvx-ruff-check", "compileall-plugin"}, gates
        for row in result["gates"]:
            assert row["ok"] in (True, False), row
            assert row["command"], row
    finally:
        _teardown_registry()


def test_salvage_report_git_facts_from_recorded_cwd(tmp_path):
    """The git facts describe the row's recorded cwd, not the process cwd."""
    repo = str(tmp_path / "repo")
    base = _make_repo(repo)
    other = str(tmp_path / "elsewhere-empty")
    os.makedirs(other)
    _setup_registry(str(tmp_path))
    old_cwd = os.getcwd()
    os.chdir(other)
    try:
        _write_registry(
            {
                "session": "epicCwd-coder",
                "pid": 999999,
                "status": "killed",
                "cwd": repo,
                "base": base,
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicCwd-coder"))
        assert "error" not in result, result
        # an unrelated empty cwd would make "git log" fail; the recorded cwd works
        assert result["git"]["commits_since_base"][0].endswith("work commit"), result["git"]
        assert result["cwd"] == repo
    finally:
        os.chdir(old_cwd)
        _teardown_registry()


def test_salvage_report_missing_base_reports_named_skips(tmp_path):
    """No base row field: commits_since_base/diff_stat carry skip reasons,
    the rest of the report still lands."""
    repo = str(tmp_path / "repo")
    _make_repo(repo)
    _setup_registry(str(tmp_path))
    try:
        _write_registry(
            {
                "session": "epicNoBase-coder",
                "pid": 999999,
                "status": "killed",
                "cwd": repo,
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicNoBase-coder"))
        assert "error" not in result, result
        assert result["base"] is None, result["base"]
        assert "skipped" in result["git"]["commits_since_base"], result["git"]
        assert "no base recorded" in result["git"]["commits_since_base"]["skipped"].lower(), result[
            "git"
        ]
        assert "skipped" in result["git"]["diff_stat"], result["git"]
        assert result["git"]["status"], result["git"]
    finally:
        _teardown_registry()


def test_salvage_report_invalid_base_reports_named_skips(tmp_path):
    """A base the repo does not know: named skip, not an exception."""
    repo = str(tmp_path / "repo")
    _make_repo(repo)
    _setup_registry(str(tmp_path))
    try:
        _write_registry(
            {
                "session": "epicBadBase-coder",
                "pid": 999999,
                "status": "killed",
                "cwd": repo,
                "base": "0" * 40,
                "started": "2024-01-01T00:00:00",
            }
        )
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicBadBase-coder"))
        assert "error" not in result, result
        assert "skipped" in result["git"]["commits_since_base"], result["git"]
        assert "unknown revision" in result["git"]["commits_since_base"]["skipped"].lower(), result[
            "git"
        ]
    finally:
        _teardown_registry()


def test_salvage_report_unknown_session_error_unchanged(tmp_path):
    """No registry rows for the session: the existing unknown shape."""
    _setup_registry(str(tmp_path))
    try:
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicGhost-coder"))
        assert result["finished"] is False and result["status"] == "unknown", result
    finally:
        _teardown_registry()


def _lane_lock_path(cwd, session_id):
    return os.path.join(cwd, ".hermes", "lane", "LOCK", f"{session_id}.lock")


def test_lane_lock_lifecycle_live_and_exit(tmp_path):
    """Real client subprocess: lock present while running (list surfaces
    present:true stale:false), lock gone when the client exits."""
    proj = str(tmp_path / "proj")
    os.makedirs(proj)
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("marker\n")
    session = "task-lockfix-default"
    # ONE world: the child's HOME carries the registry (like prod), list reads
    # that p.REGISTRY_PATH, and the LOCK lands per-cwd under proj/.hermes/lane.
    child_home = str(tmp_path / "home")
    _setup_registry(os.path.join(child_home, ".hermes", "logs"))
    p.REGISTRY_PATH = os.path.join(child_home, ".hermes", "logs", "pi-agents.jsonl")
    child = _spawn_real_client(child_home, session, cwd=proj)
    lock_path = _lane_lock_path(proj, session)
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(lock_path):
            time.sleep(0.05)
        assert os.path.exists(lock_path), "client never wrote its lane lock"
        with open(lock_path) as f:
            payload = json.load(f)
        assert payload["session_id"] == session
        assert payload["pid"] == child.pid

        agents = _parse(p._pi_manage_impl("list"))["agents"]
        mine = next(a for a in agents if a["session"] == session)
        assert mine["lane_lock"] == {"present": True, "stale": False}, mine

        child.terminate()
        assert child.wait(timeout=10) == 143
        assert not os.path.exists(lock_path), "lock file survived the client exit"
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        _teardown_registry()


def test_lane_lock_stale_after_crash_surfaced_and_reported(tmp_path):
    """kill -9 shaped crash: lock persists; list marks stale:true;
    salvage_report names the file + the rm command and deletes nothing."""
    proj = str(tmp_path / "proj")
    os.makedirs(proj)
    with open(os.path.join(proj, "AGENTS.md"), "w") as f:
        f.write("marker\n")
    session = "task-lockstale-default"
    _setup_registry(str(tmp_path))
    lock_dir = os.path.join(proj, ".hermes", "lane", "LOCK")
    os.makedirs(lock_dir)
    lock_path = os.path.join(lock_dir, f"{session}.lock")
    dead_pid = 999999
    with open(lock_path, "w") as f:
        json.dump({"session_id": session, "pid": dead_pid, "model": "m", "started_at": "now"}, f)
    _write_registry(
        {
            "session": session,
            "pid": dead_pid,
            "status": "running",
            "cwd": proj,
            "started": "2024-01-01T00:00:00",
        }
    )

    agents = _parse(p._pi_manage_impl("list"))["agents"]
    mine = next(a for a in agents if a["session"] == session)
    assert mine["lane_lock"] == {"present": True, "stale": True}, mine

    result = _parse(p._pi_manage_impl("salvage_report", session_id=session))
    lock_row = result.get("lane_lock")
    assert lock_row, result
    assert lock_row["path"] == lock_path
    assert lock_row["stale"] is True
    assert f"rm {lock_path}" in lock_row["removal"], lock_row
    # the action itself deletes nothing
    assert os.path.exists(lock_path)
    _teardown_registry()


def _salvage_row_setup(tmp_path, cwd, **extra):
    """Informal helper: registry row wired for salvage cwd tests."""
    _setup_registry(str(tmp_path))
    _write_registry(
        {
            "session": "epicRev-coder",
            "pid": 999999,
            "status": "killed",
            "cwd": cwd,
            "started": "2024-01-01T00:00:00",
            **extra,
        }
    )


def test_salvage_gates_run_in_recorded_cwd(tmp_path, monkeypatch):
    """A tmp repo whose gate command fails, while the process cwd passes:
    the row must report the tmp repo's failure, not the caller's."""
    import pathlib

    repo = str(tmp_path / "proj")
    pathlib.Path(repo).mkdir()
    with open(os.path.join(repo, "AGENTS.md"), "w") as f:
        f.write("m\n")
    # the tmp repo FAILS the compileall gate deterministically: broken syntax
    with open(os.path.join(repo, "__init__.py"), "w") as f:
        f.write("def broken(:\n")
    _salvage_row_setup(tmp_path, repo)
    old_cwd = os.getcwd()
    os.chdir(str(tmp_path))  # process cwd is OUTSIDE the row's cwd
    try:
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicRev-coder"))
        assert "error" not in result, result
        compile_row = next(r for r in result["gates"] if r["name"] == "compileall-plugin")
        assert compile_row["ok"] is False, compile_row
        assert "broken" in compile_row["output_tail"], compile_row
    finally:
        os.chdir(old_cwd)
        _teardown_registry()


def test_salvage_git_legs_work_in_worktree(tmp_path):
    """git worktree checkout as the row cwd: git legs populated, not skipped."""
    repo = str(tmp_path / "seed")
    env = {**os.environ, **GIT_IDENTITY_ENV}
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True, env=env)
    subprocess.run(["git", "-C", repo, "config", "user.email", "s@t"], check=True, env=env)
    subprocess.run(["git", "-C", repo, "config", "user.name", "s"], check=True, env=env)
    with open(os.path.join(repo, "a.md"), "w") as f:
        f.write("seed\n")
    subprocess.run(["git", "-C", repo, "add", "a.md"], check=True, env=env)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "base commit"], check=True, env=env)
    base = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    wt = str(tmp_path / "lane-wt")
    subprocess.run(["git", "-C", repo, "worktree", "add", "-q", wt], check=True, env=env)
    with open(os.path.join(wt, "a.md"), "w") as f:
        f.write("worktree edit\n")
    subprocess.run(["git", "-C", wt, "add", "a.md"], check=True, env=env)
    subprocess.run(["git", "-C", wt, "commit", "-qm", "work commit"], check=True, env=env)
    with open(os.path.join(wt, "a.md"), "w") as f:
        f.write("worktree edit uncommitted\n")

    _salvage_row_setup(tmp_path, wt, base=base)
    try:
        result = _parse(p._pi_manage_impl("salvage_report", session_id="epicRev-coder"))
        assert "error" not in result, result
        assert result["git"]["status"].strip(), result["git"]
        csb = result["git"]["commits_since_base"]
        assert not (isinstance(csb, dict) and "skipped" in csb), csb
        assert csb[0].endswith("work commit"), csb
    finally:
        subprocess.run(
            ["git", "-C", repo, "worktree", "remove", "--force", wt], check=True, env=env
        )
        _teardown_registry()

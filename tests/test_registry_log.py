"""The registry append log is line-delimited JSON end to end.

Eight concurrent dispatches each wrote running/patch/terminal rows, and the
update path rewrote the whole file while another writer appended to it, which
tears lines: the audit found 4 unparseable lines in 791. These tests seed the
tear (a torn trailing line) and the race (concurrent rapid writers plus a
concurrent reader) and demand one complete record per row.
"""

import json
import os
import subprocess
import sys
import threading

from helpers import _parse, _write_registry

import __init__ as p

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(REPO_ROOT, "scripts", "pi_rpc_client.py")


def _rows(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _raw_lines(path):
    with open(path) as f:
        return [line for line in f if line.strip()]


def _assert_every_line_parses(path):
    for index, line in enumerate(_raw_lines(path)):
        try:
            json.loads(line)
        except ValueError:
            raise AssertionError(f"torn registry line {index}: {line[:120]!r}") from None


def _torn_registry(tmp_path, torn_in_middle=False):
    p.REGISTRY_PATH = str(tmp_path / "pi-agents.jsonl")
    with open(p.REGISTRY_PATH, "w") as f:
        f.write('{"session": "a", "pid": 1, "status": "done"}\n')
        if torn_in_middle:
            f.write('{"session": "b", "pid": 2, "sta\n')
        f.write('{"session": "c", "pid": 3, "status": "done"}\n')
        f.write('{"session": "d", "pid"')


def test_torn_trailing_line_is_skipped_and_warned(tmp_path):
    # given a registry whose last line was cut mid-write (no trailing newline)
    _torn_registry(tmp_path)

    # when
    entries = p._read_registry()

    # then the complete rows survive, the torn tail never raises, and the
    # reader names the skip instead of hiding it
    assert [e["session"] for e in entries] == ["a", "c"], entries
    warning = getattr(p, "_last_registry_warning", None)
    assert warning is not None and "torn" in warning, warning

    # and pi_manage list answers from the surviving rows alone
    result = _parse(p._pi_manage_impl("list"))
    assert "error" not in result, result
    assert [a["session"] for a in result["agents"]] == ["a", "c"], result


def test_repair_quarantines_torn_lines_and_rewrites_clean(tmp_path):
    # given a registry with a torn middle line and a torn tail
    _torn_registry(tmp_path, torn_in_middle=True)

    # when
    repair = getattr(p, "_registry_repair", None)
    assert repair is not None, "no registry repair path exists"
    report = repair()

    # then every parseable row is rewritten clean and the torn text is kept aside
    assert report["kept"] == 2 and report["quarantined"] == 2, report
    assert os.path.exists(report["corrupt_path"]), report
    assert [r["session"] for r in _rows(p.REGISTRY_PATH)] == ["a", "c"], report
    with open(report["corrupt_path"]) as f:
        corrupt = f.read()
    assert '"session": "b"' in corrupt and '"session": "d"' in corrupt, corrupt

    # and the repaired registry reads clean without a warning
    entries = p._read_registry()
    assert [e["session"] for e in entries] == ["a", "c"], entries
    assert getattr(p, "_last_registry_warning", None) is None, p._last_registry_warning

    # and a fully parseable registry is left untouched
    clean_path = str(tmp_path / "clean.jsonl")
    p.REGISTRY_PATH = clean_path
    _write_registry({"session": "a", "pid": 1, "status": "done"})
    before = os.stat(clean_path).st_mtime_ns
    report = p._registry_repair()
    assert report["kept"] == 1 and report["quarantined"] == 0, report
    assert os.stat(clean_path).st_mtime_ns == before

    # the recovery path is documented where the registry is
    with open(os.path.join(REPO_ROOT, "README.md")) as f:
        readme = f.read()
    assert ".corrupt" in readme
    assert "registry" in readme.lower()


def test_concurrent_appends_stay_parseable(tmp_path, monkeypatch):
    """Eight clients hammering the append path concurrently: every line parses."""
    path = str(tmp_path / "pi-agents.jsonl")
    env = {**os.environ, "HOME": str(tmp_path), "PYTHONPATH": os.path.dirname(CLIENT)}
    hammer = f"""
import os
import sys
sys.path.insert(0, {os.path.dirname(CLIENT)!r})
import pi_rpc_client as m
m.REGISTRY_PATH = {path!r}
for i in range(25):
    m._registry_write("S" + sys.argv[1], "m", "running", pid=os.getpid())
    m._registry_patch("S" + sys.argv[1], os.getpid(), log_path="/x/" + sys.argv[1])
    m._registry_write("S" + sys.argv[1], "m", "done", pid=os.getpid(), exit_code=0)
"""
    procs = [subprocess.Popen([sys.executable, "-c", hammer, str(n)], env=env) for n in range(8)]
    for proc in procs:
        assert proc.wait(timeout=120) == 0

    # then: not one torn line, and every session's rows landed
    _assert_every_line_parses(path)
    sessions = {row["session"] for row in _rows(path)}
    assert sessions == {f"S{n}" for n in range(8)}, sessions

    # and a reader polling while in-process writers rewrite the file never
    # observes half a record
    import pi_rpc_client as mod

    thread_path = str(tmp_path / "threaded.jsonl")
    old_mod_path = mod.REGISTRY_PATH
    old_p_path = p.REGISTRY_PATH
    mod.REGISTRY_PATH = thread_path
    p.REGISTRY_PATH = thread_path
    try:
        stop = threading.Event()
        seen_bad = []

        def reader():
            while not stop.is_set():
                if not os.path.exists(thread_path):
                    continue
                for entry in p._read_registry():
                    if not isinstance(entry, dict):
                        seen_bad.append(entry)
                        return

        def writer(n):
            for i in range(30):
                mod._registry_write(f"W{n}", "m", "running", pid=90000 + n)
                mod._registry_patch(f"W{n}", 90000 + n, log_path=f"/x/{n}-{i}")
                mod._registry_write(f"W{n}", "m", "done", pid=90000 + n, exit_code=0)

        watcher = threading.Thread(target=reader, daemon=True)
        watcher.start()
        writers = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in writers:
            t.start()
        for t in writers:
            t.join(timeout=120)
        stop.set()
        watcher.join(timeout=10)
        assert seen_bad == [], seen_bad
        _assert_every_line_parses(thread_path)
    finally:
        mod.REGISTRY_PATH = old_mod_path
        p.REGISTRY_PATH = old_p_path

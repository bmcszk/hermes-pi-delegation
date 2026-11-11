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


def test_torn_trailing_line_is_skipped_without_crashing_the_reader(tmp_path):
    # given a registry whose last line was cut mid-write (no trailing newline)
    p.REGISTRY_PATH = str(tmp_path / "pi-agents.jsonl")
    good = {"session": "epicGood-coder", "pid": 1, "status": "done", "started": "t0"}
    with open(p.REGISTRY_PATH, "w") as f:
        f.write(json.dumps(good) + "\n")
        f.write('{"session": "epicTorn-coder", "pid": 2, "sta')  # torn, no newline

    # when
    entries = p._read_registry()

    # then the complete row survives and the torn tail never raises
    assert [e["session"] for e in entries] == ["epicGood-coder"], entries


def test_torn_trailing_line_is_reported_as_a_warning(tmp_path):
    # given one torn tail
    p.REGISTRY_PATH = str(tmp_path / "pi-agents.jsonl")
    with open(p.REGISTRY_PATH, "w") as f:
        f.write('{"session": "epicGood-coder", "pid": 1, "status": "done"}\n')
        f.write('{"session": "epicTorn-coder", "pid"')

    # when
    entries = p._read_registry()

    # then the reader names the skip instead of hiding it
    warning = getattr(p, "_last_registry_warning", None)
    assert warning is not None, "reader reported no warning for the torn line"
    assert "torn" in warning, warning
    assert len(entries) == 1, entries


def test_repair_quarantines_torn_lines_and_rewrites_clean(tmp_path):
    # given a registry with a torn middle line and a torn tail
    p.REGISTRY_PATH = str(tmp_path / "pi-agents.jsonl")
    with open(p.REGISTRY_PATH, "w") as f:
        f.write('{"session": "a", "pid": 1, "status": "done"}\n')
        f.write('{"session": "b", "pid": 2, "sta\n')
        f.write('{"session": "c", "pid": 3, "status": "done"}\n')
        f.write('{"session": "d", "pid"')

    # when
    repair = getattr(p, "_registry_repair", None)
    assert repair is not None, "no registry repair path exists"
    report = repair()

    # then every parseable row is rewritten clean and the torn text is kept aside
    assert report["kept"] == 2, report
    assert report["quarantined"] == 2, report
    assert os.path.exists(report["corrupt_path"]), report
    rows = _rows(p.REGISTRY_PATH)
    assert [r["session"] for r in rows] == ["a", "c"], rows
    with open(report["corrupt_path"]) as f:
        corrupt = f.read()
    assert '"session": "b"' in corrupt, corrupt
    assert '"session": "d"' in corrupt, corrupt


def test_repair_leaves_a_clean_registry_untouched(tmp_path):
    # given a fully parseable registry
    p.REGISTRY_PATH = str(tmp_path / "pi-agents.jsonl")
    _write_registry({"session": "a", "pid": 1, "status": "done"})
    before = os.stat(p.REGISTRY_PATH).st_mtime_ns

    # when
    repair = getattr(p, "_registry_repair", None)
    assert repair is not None, "no registry repair path exists"
    report = repair()

    # then
    assert report["kept"] == 1 and report["quarantined"] == 0, report
    assert os.stat(p.REGISTRY_PATH).st_mtime_ns == before


def test_repaired_registry_reads_clean_without_warning(tmp_path):
    # given a torn registry that has been repaired
    p.REGISTRY_PATH = str(tmp_path / "pi-agents.jsonl")
    with open(p.REGISTRY_PATH, "w") as f:
        f.write('{"session": "a", "pid": 1, "status": "done"}\n')
        f.write('{"session": "b", "pid"')
    repair = getattr(p, "_registry_repair", None)
    assert repair is not None, "no registry repair path exists"
    repair()

    # when
    entries = p._read_registry()

    # then
    assert [e["session"] for e in entries] == ["a"], entries
    assert getattr(p, "_last_registry_warning", None) is None, p._last_registry_warning


def test_concurrent_process_appends_stay_parseable(tmp_path):
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


def test_concurrent_appends_and_reads_never_expose_half_a_line(tmp_path):
    """The registry reader polling while writers rewrite the file: every row it
    ever observes is a whole record."""
    import pi_rpc_client as mod

    path = str(tmp_path / "pi-agents.jsonl")
    monkeypatch_target = mod.REGISTRY_PATH
    previous = p.REGISTRY_PATH
    mod.REGISTRY_PATH = path
    p.REGISTRY_PATH = path
    try:
        stop = threading.Event()
        seen_bad = []

        def reader():
            while not stop.is_set():
                if not os.path.exists(path):
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

        # then
        assert seen_bad == [], seen_bad
        _assert_every_line_parses(path)
    finally:
        mod.REGISTRY_PATH = monkeypatch_target
        p.REGISTRY_PATH = previous


def test_manage_list_survives_a_torn_registry_line(tmp_path):
    # given a registry carrying a torn tail next to a finished row
    p.REGISTRY_PATH = str(tmp_path / "pi-agents.jsonl")
    with open(p.REGISTRY_PATH, "w") as f:
        f.write(json.dumps({"session": "epicR-coder", "pid": 999999, "status": "done"}) + "\n")
        f.write('{"session": "epicTorn-coder", "pid": 12')

    # when
    result = _parse(p._pi_manage_impl("list"))

    # then
    assert "error" not in result, result
    assert [a["session"] for a in result["agents"]] == ["epicR-coder"], result


def test_readme_documents_the_registry_recovery_path():
    with open(os.path.join(REPO_ROOT, "README.md")) as f:
        readme = f.read()

    assert ".corrupt" in readme
    assert "registry" in readme.lower()

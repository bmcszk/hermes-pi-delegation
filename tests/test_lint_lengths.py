import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import lint_lengths  # noqa: E402


# given: a python file with a function over the limit
# when: find_violations runs over it
# then: exactly one violation naming the function, its line, and its length
def test_over_limit_function_flagged(tmp_path):
    body = "\n".join("    pass" for _ in range(100))
    target = tmp_path / "big.py"
    target.write_text("def huge():\n" + body + "\n")

    violations = lint_lengths.find_violations([target])

    assert len(violations) == 1
    assert str(target) in violations[0]
    assert ":1: huge - 101 lines" in violations[0]


# given: a function exactly at the limit
# when: find_violations runs
# then: no violation
def test_function_at_limit_passes(tmp_path):
    body = "\n".join("    pass" for _ in range(99))
    target = tmp_path / "edge.py"
    target.write_text("def at_limit():\n" + body + "\n")

    assert lint_lengths.find_violations([target]) == []


# given: a python file over 1000 lines with small functions
# when: find_violations runs
# then: the file itself is flagged with its length
def test_over_limit_file_flagged(tmp_path):
    lines = ["def small():", "    pass", ""]
    target = tmp_path / "long_file.py"
    target.write_text("\n".join(lines * 400))

    violations = lint_lengths.find_violations([target])

    assert len(violations) == 1
    assert str(target) in violations[0]
    assert "1199 lines" in violations[0]


# given: a clean tree of small files
# when: find_violations runs
# then: empty list
def test_clean_tree_passes(tmp_path):
    target = tmp_path / "clean.py"
    target.write_text("def tiny():\n    return 1\n")

    assert lint_lengths.find_violations([target]) == []


# given: a file whose outer function holds an oversized nested function
# when: find_violations runs
# then: the nested function is flagged by its own name and length
def test_nested_function_counted(tmp_path):
    inner = "\n".join("        pass" for _ in range(100))
    target = tmp_path / "nested.py"
    target.write_text("def outer():\n    def inner():\n" + inner + "\n")

    violations = lint_lengths.find_violations([target])

    inner_hits = [v for v in violations if "inner" in v]
    assert len(inner_hits) == 1
    assert ":2: inner - 101 lines" in inner_hits[0]


# given: an async def over the limit
# when: find_violations runs
# then: it is flagged like a sync def
def test_async_def_counted(tmp_path):
    body = "\n".join("    pass" for _ in range(100))
    target = tmp_path / "async_big.py"
    target.write_text("async def big_async():\n" + body + "\n")

    violations = lint_lengths.find_violations([target])

    assert len(violations) == 1
    assert "big_async - 101 lines" in violations[0]


# given: a directory tree with hidden/cache dirs holding oversized files
# when: find_violations runs over the root
# then: skipped dirs contribute no violations
def test_skipped_dirs_not_scanned(tmp_path):
    for skip in (".git", "__pycache__", ".hermes"):
        d = tmp_path / skip
        d.mkdir()
        body = "\n".join("    pass" for _ in range(100))
        (d / "junk.py").write_text("def huge():\n" + body + "\n")

    assert lint_lengths.find_violations([tmp_path]) == []


# given: the checker run as a script on a clean tree
# when: it exits
# then: exit code 0 and silent
def test_main_exits_zero_when_clean(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["lint_lengths.py", str(tmp_path)])
    assert lint_lengths.main() == 0
    assert capsys.readouterr().out == ""


# given: the checker run as a script on a violating tree
# when: it exits
# then: exit code 1 with one line per violation
def test_main_exits_one_on_violation(tmp_path, capsys, monkeypatch):
    body = "\n".join("    pass" for _ in range(100))
    (tmp_path / "big.py").write_text("def huge():\n" + body + "\n")
    monkeypatch.setattr(sys, "argv", ["lint_lengths.py", str(tmp_path)])

    exit_code = lint_lengths.main()

    assert exit_code == 1
    assert "huge - 101 lines" in capsys.readouterr().out

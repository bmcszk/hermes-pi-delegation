"""Tests for sweep_runner: citation audit + reconciliation (mechanical gates)."""

import importlib.util
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

spec = importlib.util.spec_from_file_location("sweep_runner", SCRIPTS_DIR / "sweep_runner.py")
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)


@pytest.mark.parametrize(
    ("report", "expect_error_fragment", "expect_fixed"),
    [
        pytest.param(
            "FILE: a/b.go VERDICT: FIXED reordered consts\n",
            "without rule citation",
            ["a/b.go"],
            id="fixed-without-citation-fails",
        ),
        pytest.param(
            "FILE: a/b.go VERDICT: FIXED R14 narration a/b.go:42 removed\n",
            None,
            ["a/b.go"],
            id="fixed-with-citation-passes",
        ),
        pytest.param("FILE: x.go VERDICT: CLEAN\n", None, [], id="clean-report-no-fixed"),
    ],
)
def test_citation_audit(report, expect_error_fragment, expect_fixed):
    errors, fixed = sr.citation_audit(report)
    assert fixed == expect_fixed, (errors, fixed)
    if expect_error_fragment:
        assert expect_error_fragment in errors[0], errors
    else:
        assert all("without rule citation" not in e for e in errors), errors


@pytest.mark.parametrize(
    ("report", "manifest", "expect_error_fragment"),
    [
        pytest.param(
            "FILE: a.go VERDICT: CLEAN\n",
            ["a.go", "b.go"],
            "missing from verdicts: b.go",
            id="missing-manifest-file-detected",
        ),
        pytest.param(
            "FILE: a.go VERDICT: CLEAN\nFILE: z.go VERDICT: CLEAN\n",
            ["a.go"],
            "outside manifest: z.go",
            id="outside-manifest-detected",
        ),
    ],
)
def test_reconciliation(report, manifest, expect_error_fragment):
    errs = sr.reconciliation(report, "/tmp", manifest)
    if expect_error_fragment:
        assert any(expect_error_fragment in e for e in errs), errs
    else:
        assert errs == [], errs
    # a full manifest/report match is clean
    assert (
        sr.reconciliation(
            "FILE: a.go VERDICT: CLEAN\nFILE: b.go VERDICT: CLEAN\n", "/tmp", ["a.go", "b.go"]
        )
        == []
    )


def test_sweep_cmd_has_no_ns(monkeypatch):
    seen = {}

    class _Result:
        stdout = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(sr.subprocess, "run", fake_run)
    report, code = sr.dispatch(["a.go"], lambda b: "contract", "sweep", "_worker", 10)
    assert code == 0 and report == ""
    cmd = seen["cmd"]
    assert "--ns" not in cmd
    assert "--codemode" in cmd
    assert cmd.count("--prompt") == 1

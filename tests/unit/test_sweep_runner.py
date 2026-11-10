"""Tests for sweep_runner: citation audit + reconciliation (mechanical gates)."""

import importlib.util
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

spec = importlib.util.spec_from_file_location("sweep_runner", SCRIPTS_DIR / "sweep_runner.py")
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)


class TestCitationAudit:
    def test_fixed_without_citation_fails(self):
        errors, fixed = sr.citation_audit("FILE: a/b.go VERDICT: FIXED reordered consts\n")
        assert errors and "without rule citation" in errors[0]
        assert fixed == ["a/b.go"]

    def test_fixed_with_citation_passes(self):
        rep = "FILE: a/b.go VERDICT: FIXED R14 narration a/b.go:42 removed\n"
        errors, fixed = sr.citation_audit(rep)
        # path not in manifest repo=/tmp -> only the untracked error may appear
        assert all("without rule citation" not in e for e in errors)
        assert fixed == ["a/b.go"]

    def test_clean_report_no_fixed(self):
        errors, fixed = sr.citation_audit("FILE: x.go VERDICT: CLEAN\n")
        assert errors == [] and fixed == []


class TestReconciliation:
    def test_missing_manifest_file_detected(self):
        errs = sr.reconciliation("FILE: a.go VERDICT: CLEAN\n", "/tmp", ["a.go", "b.go"])
        assert any("missing from verdicts: b.go" in e for e in errs)

    def test_outside_manifest_detected(self):
        errs = sr.reconciliation(
            "FILE: a.go VERDICT: CLEAN\nFILE: z.go VERDICT: CLEAN\n", "/tmp", ["a.go"]
        )
        assert any("outside manifest: z.go" in e for e in errs)

    def test_full_match_clean(self):
        errs = sr.reconciliation(
            "FILE: a.go VERDICT: CLEAN\nFILE: b.go VERDICT: CLEAN\n", "/tmp", ["a.go", "b.go"]
        )
        assert errs == []


class TestDispatchCmd:
    def test_sweep_cmd_has_no_ns(self, monkeypatch):
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

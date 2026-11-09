#!/usr/bin/env python3
"""Sweep runner with an enforced canary gate.

The canary is not advisory: batch 2 never dispatches until batch 1's report
passes the citation audit and its diff passes the parent gate. A runner that
wants to skip the gate has to edit this file, which is a reviewable act.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

FIXED_RE = re.compile(r"^FILE: (.+?) VERDICT: FIXED (.*)$", re.M)
CITE_RE = re.compile(
    r"\b([A-Z]+[0-9]+|R[0-9]+|[a-z-]+-rule|phase-marker[a-z-]*)\b.*?(\S+\.(?:go|py|ts|js|rs|java|c|cpp|h|md):\d+)"
)
PATH_RE = re.compile(r"^FILE: (\S+)", re.M)


def sh(*cmd: str, cwd: str | None = None) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[:400]}")
    return r.stdout


def manifest(repo: str, glob_pattern: str) -> list[str]:
    out = sh("git", "-C", repo, "ls-files", glob_pattern)
    return sorted(line for line in out.splitlines() if line.strip())


def citation_audit(report_text: str) -> tuple[list[str], list[str]]:
    """Mechanical audit of a sweep report: every FIXED verdict names a rule
    id and a file:line. Returns (errors, fixed_files)."""
    errors: list[str] = []
    fixed: list[str] = []
    for m in FIXED_RE.finditer(report_text):
        path = m.group(1)
        fixed.append(path)
        if not CITE_RE.search(m.group(2)):
            errors.append(f"FIXED verdict without rule citation: {path}")
    return errors, fixed


def reconciliation(report_text: str, repo: str, manifest_paths: list[str]) -> list[str]:
    errors: list[str] = []
    seen = set(PATH_RE.findall(report_text))
    for p in manifest_paths:
        if p not in seen:
            errors.append(f"manifest file missing from verdicts: {p}")
    for p in seen:
        if p not in manifest_paths:
            errors.append(f"verdict for file outside manifest: {p}")
    return errors


def dispatch(
    batch: list[str], contract_fn, session_prefix: str, model: str, timeout_s: int
) -> tuple[str, int]:
    """One RPC dispatch for a batch. Returns (report_text, exit_code)."""
    contract = contract_fn(batch)
    cid = f"{session_prefix}-b{abs(hash(tuple(batch))) % 10000}"
    cmd = [
        "python3",
        str(Path.home() / ".hermes/skills/dev/pi-delegation/scripts/pi_rpc_client.py"),
        "--session-id",
        cid,
        "--model",
        model,
        "--codemode",
        "--timeout",
        str(timeout_s),
        "--prompt",
        contract,
    ]
    r = subprocess.run(
        cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout_s + 30
    )
    return r.stdout, r.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--glob", default="*.go")
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--session-prefix", required=True)
    ap.add_argument("--model", default="_worker")
    ap.add_argument("--timeout", type=int, default=550)
    ap.add_argument(
        "--contract-file", required=True, help="file with a {files} placeholder; rendered per batch"
    )
    args = ap.parse_args()

    paths = manifest(args.repo, args.glob)
    if not paths:
        print("empty manifest", file=sys.stderr)
        return 2
    template = Path(args.contract_file).read_text()

    def contract_fn(batch):
        return template.replace("{files}", "\n".join(batch))

    # CANARY: first batch is one file. Wave is dead on its failure.
    canary = paths[:1]
    print(f"[canary] dispatching 1/{len(paths)}: {canary[0]}", flush=True)
    report, code = dispatch(canary, contract_fn, args.session_prefix, args.model, args.timeout)
    Path("/tmp", f"{args.session_prefix}-canary-report.md").write_text(report)
    errors, fixed = citation_audit(report)
    if code != 0:
        print(f"[canary] dispatch exit {code}; fix the contract before any wave", file=sys.stderr)
        return 3
    if errors:
        print("[canary] citation audit FAILED:", *errors, sep="\n  ", file=sys.stderr)
        return 4
    print(f"[canary] audit clean ({len(fixed)} FIXED verdicts); releasing wave", flush=True)

    remaining = paths[1:]
    batches = [
        remaining[i : i + args.batch_size] for i in range(0, len(remaining), args.batch_size)
    ]
    all_reports: list[str] = [report]
    for n, batch in enumerate(batches, start=2):
        rep, code = dispatch(batch, contract_fn, args.session_prefix, args.model, args.timeout)
        Path("/tmp", f"{args.session_prefix}-b{n:03d}-report.md").write_text(rep)
        all_reports.append(rep)
        errs, _ = citation_audit(rep)
        if code != 0 or errs:
            print(
                f"[batch {n}] FAILED (exit {code}, {len(errs)} audit errors); stopping",
                file=sys.stderr,
            )
            for e in errs:
                print("  ", e, file=sys.stderr)
            return 5
        print(f"[batch {n}/{len(batches) + 1}] clean", flush=True)

    full = "\n".join(all_reports)
    recon = reconciliation(full, args.repo, paths)
    if recon:
        print("[reconciliation] FAILED:", *recon, sep="\n  ", file=sys.stderr)
        return 6
    print(
        f"[done] {len(paths)}/{len(paths)} files reconciled; parent gate + closure next", flush=True
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

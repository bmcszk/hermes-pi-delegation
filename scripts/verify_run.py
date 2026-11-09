#!/usr/bin/env python3
"""Verified pi RPC dispatch.

A run SUCCEEDS only if every required artifact exists with at least its minimum
byte size AND the completion marker appears in the reply or in an artifact.
Interrupted runs (a network flake yields an early agent_end with thinking-only
text) are detected and retried with a fresh client.

Usage:
  verify_run.py <contract.md> <model> <cwd> <marker> [--artifact PATH:MINB]...
                [--attempts N] [--timeout S] [--report PATH]

Prints VERIFIED or FAILED. Exit code 0 or 1.
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pi_rpc_client import PiRpcClient


def parse_artifact(spec):
    """Split a PATH:MINB spec into (path, minimum bytes)."""
    if ":" in spec:
        path, minimum = spec.rsplit(":", 1)
    else:
        path, minimum = spec, "1"
    return path, int(minimum)


def dispatch_once(contract, model, cwd, timeout_s):
    """Run one dispatch and return the worker reply text (empty on exception)."""
    client = PiRpcClient(model=model, cwd=cwd)
    try:
        client.start()
        return client.prompt(contract, timeout_s=timeout_s)
    finally:
        client.close()


def marker_found(marker, reply, checks):
    """True when the marker appears in the reply or in any checked artifact."""
    if marker in reply:
        return True
    for path, _minimum in checks:
        if os.path.exists(path) and marker in Path(path).read_text(errors="ignore"):
            return True
    return False


def artifacts_satisfied(checks):
    """True when every artifact exists and meets its minimum size."""
    return all(
        os.path.exists(path) and os.path.getsize(path) >= minimum for path, minimum in checks
    )


def run_verified_attempts(options):
    """Retry the dispatch until a verified run completes or attempts run out."""
    contract = Path(options.contract).read_text()
    checks = [parse_artifact(spec) for spec in options.artifact]
    for attempt in range(1, options.attempts + 1):
        reply = ""
        try:
            reply = dispatch_once(contract, options.model, options.cwd, options.timeout)
        except Exception as exc:
            print(f"attempt {attempt}: EXC {type(exc).__name__}: {exc}")
        if options.report:
            Path(options.report).write_text(reply)
        ok_marker = marker_found(options.marker, reply, checks)
        ok_files = artifacts_satisfied(checks)
        if ok_marker and ok_files:
            print(f"attempt {attempt}: VERIFIED (marker + {len(checks)} artifacts)")
            return 0
        print(
            f"attempt {attempt}: FAILED marker={ok_marker} files={ok_files} replylen={len(reply)}"
        )
        time.sleep(8)
    print("ALL ATTEMPTS FAILED")
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("contract")
    parser.add_argument("model")
    parser.add_argument("cwd")
    parser.add_argument("marker")
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=2400)
    parser.add_argument("--report", default=None)
    return run_verified_attempts(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())

"""Shared test machinery: registry fakes, fake-pi harness, world constants.

Imported explicitly by the split test files; carries no tests.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import subprocess
import sys
import tempfile
import time

import __init__ as p

TMP_CWD = tempfile.mkdtemp(prefix="pi-delegation-test-cwd-")
with open(os.path.join(TMP_CWD, "AGENTS.md"), "w") as _f:
    _f.write("test repo marker\n")
CONTRACT_MD = os.path.join(TMP_CWD, "contract.md")
with open(CONTRACT_MD, "w") as _f:
    _f.write("test contract\n")

_registry_backup = p.REGISTRY_PATH

SKILLS = os.path.join(TMP_CWD, "skills")
CODING_RULES = os.path.join(SKILLS, "coding-rules")
GO_DEV = os.path.join(SKILLS, "go-dev")
for _name in (CODING_RULES, GO_DEV):
    os.makedirs(_name, exist_ok=True)
    with open(os.path.join(_name, "SKILL.md"), "w") as _f:
        _f.write(f"---\nname: {os.path.basename(_name)}\n---\ntest\n")

with open(os.path.join(TMP_CWD, "AGENTS.md"), "w") as _f:
    _f.write("test repo marker\n")
CONTRACT_MD = os.path.join(TMP_CWD, "contract.md")
with open(CONTRACT_MD, "w") as _f:
    _f.write("test contract\n")

_registry_backup = p.REGISTRY_PATH

SKILLS = os.path.join(TMP_CWD, "skills")
CODING_RULES = os.path.join(SKILLS, "coding-rules")
GO_DEV = os.path.join(SKILLS, "go-dev")
for _name in (CODING_RULES, GO_DEV):
    os.makedirs(_name, exist_ok=True)
    with open(os.path.join(_name, "SKILL.md"), "w") as _f:
        _f.write(f"---\nname: {os.path.basename(_name)}\n---\ntest\n")


def _parse(result):
    """Helper: parse JSON string from handler returns."""
    return json.loads(result) if isinstance(result, str) else result


def _setup_registry(tmpdir):
    global _registry_backup
    _registry_backup = p.REGISTRY_PATH
    p.REGISTRY_PATH = os.path.join(tmpdir, "pi-agents.jsonl")


def _teardown_registry():
    p.REGISTRY_PATH = _registry_backup


def _write_registry(*entries):
    with open(p.REGISTRY_PATH, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


FAKE_PI_SH = """#!/bin/sh
echo '{"type":"ready"}'
exec sleep 5
"""


def _spawn_real_client(tmpdir, session_id, cwd=None):
    """Spawn the real scripts/pi_rpc_client.py against a fake sleeping pi."""
    bin_dir = os.path.join(tmpdir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    fake_pi = os.path.join(bin_dir, "pi")
    with open(fake_pi, "w") as f:
        f.write(FAKE_PI_SH)
    os.chmod(fake_pi, 0o755)
    env = {
        **os.environ,
        "HOME": tmpdir,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [
            os.path.dirname(os.path.abspath(__file__)),
            os.path.dirname(p.CLIENT_PATH),
            env.get("PYTHONPATH", ""),
        ]
    )
    return subprocess.Popen(
        [
            sys.executable,
            os.path.join(os.path.dirname(p.CLIENT_PATH), "pi_rpc_client.py"),
            "--session-id",
            session_id,
            "--model",
            "m",
            "--prompt",
            "p",
            "--timeout",
            "60",
            *(["--cwd", str(cwd)] if cwd else []),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )


def _wait_for_registry(fn, deadline=10):
    end = time.time() + deadline
    while time.time() < end:
        value = fn()
        if value is not None:
            return value
        time.sleep(0.05)
    return None

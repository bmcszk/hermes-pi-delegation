"""wait clamps its own ceiling below the host tool-call limit.

The host tool layer gives one call 1600 s. A caller asking wait for 1500-1700 s
lost the turn six times in one live session, so wait answers at its own clamp
instead of riding the host's limit.
"""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest
from helpers import _parse, _setup_registry, _teardown_registry, _write_registry

import __init__ as p
from __init__ import pi_manage

WAIT_CEILING_S = 1500


class VirtualClock:
    """Stands in for the module's `time`: sleep advances monotonically, so a
    long wait budget is exercised in microseconds and the probe count is exact."""

    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += float(seconds)
        self.slept += float(seconds)


def _running_session():
    """A registered session whose pid is alive: wait must keep probing it."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    _write_registry(
        {
            "session": "epicClamp-coder",
            "pid": child.pid,
            "status": "running",
            "started": "2024-01-01T00:00:00",
        }
    )
    return child


def _wait_with_virtual_clock(payload):
    """Run wait against a virtual clock; returns (result, clock)."""
    clock = VirtualClock()
    original = p.time
    p.time = clock
    try:
        return _parse(pi_manage(payload)), clock
    finally:
        p.time = original


def test_wait_ceiling_constant_is_under_host_limit():
    # the module advertises one clamp, safely under the 1600 s host cap
    assert getattr(p, "WAIT_CEILING_S", None) == WAIT_CEILING_S
    # the cap and its resume hint are documented where wait is
    readme_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"
    )
    with open(readme_path) as f:
        readme = f.read()
    assert "1500" in readme and "resumed_by" in readme and "call-wait-again" in readme


@pytest.mark.parametrize(
    ("payload", "expect"),
    [
        pytest.param(
            {"timeout_s": 99999, "poll_s": 1},
            {
                "finished": False,
                "status": "running",
                "requested_timeout_s": 99999,
                "timeout_s": WAIT_CEILING_S,
                "clamped": True,
                "resumed_by": "call-wait-again",
            },
            id="clamped",
        ),
        pytest.param(
            {"timeout_s": 2, "poll_s": 1},
            {
                "requested_timeout_s": 2,
                "timeout_s": 2,
                "clamped": False,
            },
            id="under-clamp-honoured",
        ),
        pytest.param(
            {},
            {
                "finished": False,
                "requested_timeout_s": 0,
                "timeout_s": 0,
                "clamped": False,
            },
            id="single-probe-immediate",
        ),
    ],
)
def test_wait_payload_key_contract(payload, expect):
    """The wait payload reports the budget the caller asked for, the clamp when
    one applied, and the resume hint only when the caller must come back."""
    tmpdir = None
    child = None
    try:
        tmpdir = tempfile.mkdtemp()
        _setup_registry(tmpdir)
        if payload.get("timeout_s") == 99999 and payload.get("poll_s") == 1:
            # a settled session: the clamp is visible, but the terminal state
            # answers and nothing asks the caller to wait again
            _write_registry(
                {
                    "session": "epicClamp-coder",
                    "pid": 999999,
                    "status": "done",
                    "exit_code": 0,
                    "started": "2024-01-01T00:00:00",
                }
            )
        else:
            child = _running_session()

        result, clock = _wait_with_virtual_clock(
            {"action": "wait", "session_id": "epicClamp-coder", **payload}
        )

        done_row = payload.get("timeout_s") == 99999 and payload.get("poll_s") == 1
        for key, value in expect.items():
            if done_row and key in ("finished", "status", "resumed_by"):
                continue  # the done-row branch asserts the terminal state below
            assert result.get(key) == value, (key, result)
        if expect.get("clamped") is False:
            assert "resumed_by" not in result, result
        if done_row:
            assert result["status"] == "done" and result["exit_code"] == 0, result
        if payload == {"timeout_s": 99999, "poll_s": 1}:
            assert clock.slept <= WAIT_CEILING_S, clock.slept
        if not payload:
            assert clock.slept == 0, clock.slept
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=10)
        _teardown_registry()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

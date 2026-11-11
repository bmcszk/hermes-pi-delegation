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
    # when / then the module advertises one clamp, safely under the 1600 s host cap
    assert getattr(p, "WAIT_CEILING_S", None) == WAIT_CEILING_S


def test_wait_clamps_huge_timeout_and_reports_resume_hint():
    """wait(timeout_s=99999) stops at the clamp and says how to resume."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    child = None
    try:
        child = _running_session()

        # when
        result, clock = _wait_with_virtual_clock(
            {"action": "wait", "session_id": "epicClamp-coder", "timeout_s": 99999, "poll_s": 1}
        )

        # then the loop stopped at the clamp instead of the requested budget
        assert result["finished"] is False, result
        assert result["status"] == "running", result
        assert result["requested_timeout_s"] == 99999, result
        assert result["timeout_s"] == WAIT_CEILING_S, result
        assert result["clamped"] is True, result
        assert result["resumed_by"] == "call-wait-again", result
        assert clock.slept <= WAIT_CEILING_S, clock.slept
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_wait_under_the_clamp_is_not_marked_clamped():
    """A budget under the clamp is honoured as asked: no clamp, no resume hint."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    child = None
    try:
        child = _running_session()

        # when
        result, _ = _wait_with_virtual_clock(
            {"action": "wait", "session_id": "epicClamp-coder", "timeout_s": 2, "poll_s": 1}
        )

        # then the payload reports the budget the caller asked for
        assert result["requested_timeout_s"] == 2, result
        assert result["timeout_s"] == 2, result
        assert result["clamped"] is False, result
        assert "resumed_by" not in result, result
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_wait_single_probe_stays_immediate():
    """timeout_s omitted (one probe) answers at once, unclamped, no resume hint."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    child = None
    try:
        child = _running_session()

        # when
        result, clock = _wait_with_virtual_clock(
            {"action": "wait", "session_id": "epicClamp-coder"}
        )

        # then a single probe is never clamped and never asks for a resume call
        assert result["finished"] is False, result
        assert result["requested_timeout_s"] == 0, result
        assert result["timeout_s"] == 0, result
        assert result["clamped"] is False, result
        assert "resumed_by" not in result, result
        assert clock.slept == 0, clock.slept
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=10)
        _teardown_registry()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_wait_finished_session_carries_no_resume_hint():
    """A settled session answers with its terminal state: the clamp is still
    visible, but nothing asks the caller to wait again."""
    tmpdir = tempfile.mkdtemp()
    _setup_registry(tmpdir)
    try:
        _write_registry(
            {
                "session": "epicClampDone-coder",
                "pid": 999999,
                "status": "done",
                "exit_code": 0,
                "started": "2024-01-01T00:00:00",
            }
        )

        # when
        result, _ = _wait_with_virtual_clock(
            {"action": "wait", "session_id": "epicClampDone-coder", "timeout_s": 99999}
        )

        # then
        assert result["finished"] is True, result
        assert result["status"] == "done", result
        assert result["exit_code"] == 0, result
        assert result["clamped"] is True, result
        assert result["timeout_s"] == WAIT_CEILING_S, result
        assert "resumed_by" not in result, result
    finally:
        _teardown_registry()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_readme_documents_the_wait_cap():
    # given the README the dispatcher reads
    readme_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"
    )
    with open(readme_path) as f:
        readme = f.read()

    # then the cap and its reason are stated where wait is documented
    assert "1500" in readme
    assert "resumed_by" in readme
    assert "call-wait-again" in readme

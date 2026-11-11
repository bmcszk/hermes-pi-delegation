"""Registry log I/O shared by the plugin (reader) and the RPC client (writer).

The registry is one line-delimited JSON file appended by every dispatch and
rewritten whenever a row changes. Plain `open(..., "a").write()` under
concurrency tears lines: a second writer lands mid-record, and a whole-file
rewrite swaps in a shorter file while an append sits at the old offset. The
audit found 4 unparseable lines of 791 that way.

So every mutation takes one exclusive lock (`<registry>.lock`) and replaces the
file with a temp file + `os.replace`, which makes each record whole from any
reader's point of view. Appends keep the single `write()` call of the whole
record plus its newline.
"""

from __future__ import annotations

import contextlib
import json
import os

try:
    import fcntl
except ImportError:  # non-POSIX: no lock, appends stay whole-line
    fcntl = None


def registry_lock_path(path):
    return f"{path}.lock"


@contextlib.contextmanager
def locked_registry(path):
    """Hold the registry's exclusive lock for a read-modify-write."""
    with _flock(path, fcntl.LOCK_EX if fcntl else None):
        yield


@contextlib.contextmanager
def shared_registry(path):
    """Hold the registry's shared lock around a pure read.

    Without it a reader tailing the file mid-append sees the first half of a
    record as a trailing line and its second half as the next one. The lock
    makes a read whole; the tolerant parser below still covers files left torn
    by a crash or by an older writer.
    """
    with _flock(path, fcntl.LOCK_SH if fcntl else None):
        yield


@contextlib.contextmanager
def _flock(path, mode):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if mode is None:
        yield
        return
    with open(registry_lock_path(path), "a+") as handle:
        fcntl.flock(handle, mode)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_registry_lines(path):
    """(rows, torn) for the registry: parsed dicts plus the unparseable texts.

    Lock-free by design: every caller either holds `locked_registry` already or
    wraps this in `read_registry`, so the lock is taken once per operation.
    """
    try:
        with open(path) as handle:
            lines = [line for line in handle if line.strip()]
    except FileNotFoundError:
        return [], []
    rows, torn = [], []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            torn.append(line.rstrip("\n"))
    return rows, torn


def read_registry(path):
    """Registry rows plus torn lines, read under the shared lock."""
    with shared_registry(path):
        return read_registry_lines(path)


def write_registry_lines(path, lines):
    """Replace the registry with exactly these lines, atomically."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.swap-{os.getpid()}-{os.urandom(4).hex()}"
    with open(tmp, "w") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def append_registry_line(path, line):
    """Append one complete record in a single write() call.

    Holds the same lock as the rewrite path: a rewriter that snapshotted the
    file mid-append would otherwise merge a half record into its clean copy,
    which is exactly how a torn line survives an atomic rewrite.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with locked_registry(path), open(path, "a") as handle:
        handle.write(line if line.endswith("\n") else line + "\n")
        handle.flush()
        os.fsync(handle.fileno())

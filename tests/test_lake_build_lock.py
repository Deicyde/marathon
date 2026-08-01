"""Focused cross-process tests for shared Lake build coordination."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from marathon.lake_build_lock import (
    LakeBuildLockCancelled,
    lake_build_lock,
    lake_build_lock_path,
    lake_build_lock_supported,
)


pytestmark = pytest.mark.skipif(
    not lake_build_lock_supported(), reason="POSIX inherited flock"
)


def test_repo_lock_is_nonblocking_when_busy_and_reusable_after_release(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    with lake_build_lock(repo) as owner:
        assert owner is not None
        assert lake_build_lock_path(repo).is_file()
        with lake_build_lock(repo / ".", blocking=False) as contender:
            assert contender is None

    with lake_build_lock(repo, blocking=False) as next_owner:
        assert next_owner is not None


def test_blocking_wait_is_cancellable(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()

    with lake_build_lock(repo):
        cancel_at = time.monotonic() + 0.05
        with pytest.raises(LakeBuildLockCancelled):
            with lake_build_lock(
                repo,
                timeout=5,
                cancelled=lambda: time.monotonic() >= cancel_at,
            ):
                raise AssertionError("cancelled waiter must not acquire")


def test_inherited_descriptor_keeps_lock_after_parent_crash(tmp_path: Path):
    """A surviving Lake child remains the owner if its Python parent dies."""

    repo = tmp_path / "repo"
    repo.mkdir()
    holder_code = """
import os
import subprocess
import sys
from marathon.lake_build_lock import lake_build_lock

guard = lake_build_lock(sys.argv[1])
lock_fd = guard.__enter__()
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(1)"],
    pass_fds=(lock_fd,),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
os._exit(0)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(repo)],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    child_pid = int(holder.stdout.readline().strip())
    assert child_pid > 0
    assert holder.wait(timeout=5) == 0

    # The context manager never unwound in the crashed holder. Its child has
    # inherited the same open file description, so the lock remains busy.
    with lake_build_lock(repo, blocking=False) as contender:
        assert contender is None

    deadline = time.monotonic() + 5
    acquired = False
    while time.monotonic() < deadline:
        with lake_build_lock(repo, blocking=False) as contender:
            if contender is not None:
                acquired = True
                break
        time.sleep(0.02)
    assert acquired, "lock was not released after the inherited child exited"

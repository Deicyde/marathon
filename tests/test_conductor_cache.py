"""``_prime_mathlib_cache`` — seeding a refine worktree's Mathlib oleans via
``lake exe cache get`` so the build skips the from-scratch Mathlib compile.

Unit-level: a PATH-stubbed ``lake`` records its argv / cwd / env and is scripted
to exit 0, exit non-zero, or hang past the timeout. No real lake / network /
worktree. The fail-safe contract is the point: a prime must NEVER raise and must
return False on anything but a clean cache-get, so a dispatch never aborts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from marathon import conductor


def _write_lake_stub(bindir: Path, record: Path, *, exit_code: int = 0,
                     sleep: float = 0) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "lake"
    stub.write_text(
        "#!/bin/sh\n"
        f'{{ echo "ARGV: $*"; echo "CWD: $(pwd)"; '
        f'echo "HOME=$HOME"; }} > "{record}"\n'
        + (f"sleep {sleep}\n" if sleep else "")
        + f"exit {exit_code}\n"
    )
    stub.chmod(0o755)


def _put_on_path(monkeypatch, bindir: Path) -> None:
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


def test_prime_runs_cache_get_in_worktree_with_inherited_env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    record = tmp_path / "rec.txt"
    _write_lake_stub(bindir, record, exit_code=0)
    _put_on_path(monkeypatch, bindir)
    monkeypatch.setenv("HOME", "/home/marathon-test")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert conductor._prime_mathlib_cache(worktree) is True

    text = record.read_text()
    assert "ARGV: exe cache get" in text          # `lake exe cache get`
    assert f"CWD: {worktree}" in text              # cwd = the worktree
    assert "HOME=/home/marathon-test" in text      # env inherited (store key)


def test_prime_nonzero_exit_returns_false_no_raise(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    _write_lake_stub(bindir, tmp_path / "rec.txt", exit_code=1)
    _put_on_path(monkeypatch, bindir)
    assert conductor._prime_mathlib_cache(tmp_path / "wt") is False


def test_prime_timeout_returns_false_no_raise(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    _write_lake_stub(bindir, tmp_path / "rec.txt", exit_code=0, sleep=3)
    _put_on_path(monkeypatch, bindir)
    # timeout well under the stub's sleep → TimeoutExpired, swallowed → False.
    assert conductor._prime_mathlib_cache(tmp_path / "wt", timeout=1) is False


def test_prime_skip_env_noops_without_calling_lake(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    record = tmp_path / "rec.txt"
    _write_lake_stub(bindir, record, exit_code=0)
    _put_on_path(monkeypatch, bindir)
    monkeypatch.setenv("MARATHON_SKIP_CACHE_GET", "1")
    assert conductor._prime_mathlib_cache(tmp_path / "wt") is False
    assert not record.exists()  # lake was never invoked


def test_prime_no_lake_on_path_returns_false(tmp_path, monkeypatch):
    # A PATH with no `lake` at all.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("MARATHON_SKIP_CACHE_GET", raising=False)
    assert conductor._prime_mathlib_cache(tmp_path / "wt") is False


def test_mathlib_already_built_marker(tmp_path):
    wt = tmp_path / "wt"
    assert conductor._mathlib_already_built(wt) is False
    marker = wt / conductor._MATHLIB_BUILT_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("olean")
    assert conductor._mathlib_already_built(wt) is True


def test_reuse_branch_primes_only_when_not_built(tmp_path, monkeypatch):
    """On the kept-worktree reuse path, _prime_mathlib_cache is called when the
    worktree has no Mathlib build, and SKIPPED when the built-marker is present
    (so a retry doesn't pay a redundant decompress)."""
    from types import SimpleNamespace
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    wt = tmp_path / "kept-wt"
    wt.mkdir()
    primed: list[str] = []
    monkeypatch.setattr(conductor, "_prime_mathlib_cache",
                        lambda w, **k: primed.append(str(w)) or False)
    monkeypatch.setattr(conductor.subprocess, "Popen",
                        lambda cmd, **kw: SimpleNamespace(pid=4001))

    # not built → prime runs on the reused worktree
    conductor._dispatch_job(cfg, 22, 14, tmp_path / "runs", {22: str(wt)})
    assert primed == [str(wt)]

    # built-marker present → prime skipped on the next retry
    marker = wt / conductor._MATHLIB_BUILT_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("olean")
    primed.clear()
    conductor._dispatch_job(cfg, 22, 14, tmp_path / "runs", {22: str(wt)})
    assert primed == []

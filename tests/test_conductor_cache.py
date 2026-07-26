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


@pytest.fixture(autouse=True)
def _complete_fake_worker_handshake(monkeypatch):
    monkeypatch.setattr(
        conductor,
        "_await_worker_handshake",
        lambda _wt, proc, **_kw: {
            "pid": proc.pid, "pgid": proc.pid, "status": "running",
        },
    )


def _write_lake_stub(bindir: Path, record: Path, *, exit_code: int = 0,
                     sleep: float = 0, before_exit: str = "") -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "lake"
    stub.write_text(
        "#!/bin/sh\n"
        f'{{ echo "ARGV: $*"; echo "CWD: $(pwd)"; '
        f'echo "HOME=$HOME"; }} > "{record}"\n'
        + (before_exit + "\n" if before_exit else "")
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
    worktree = tmp_path / "wt"
    worktree.mkdir()
    # timeout well under the stub's sleep → TimeoutExpired, swallowed → False.
    assert conductor._prime_mathlib_cache(worktree, timeout=0.1) is False


def test_prime_preflight_quarantines_retained_locked_dependency_only(
    tmp_path, monkeypatch,
):
    bindir = tmp_path / "bin"
    _write_lake_stub(bindir, tmp_path / "rec.txt")
    _put_on_path(monkeypatch, bindir)
    worktree = tmp_path / "wt"
    locked_dependency = worktree / ".lake/packages/mathlib"
    (locked_dependency / ".git").mkdir(parents=True)
    (locked_dependency / ".git/index.lock").write_text("interrupted checkout")
    (locked_dependency / "partial-source").write_text("partial")
    build_output = worktree / ".lake/build/lib/lean/Project.olean"
    build_output.parent.mkdir(parents=True)
    build_output.write_text("keep")
    project_source = worktree / "Project.lean"
    project_source.write_text("def keep := true")

    assert conductor._prime_job_mathlib_cache(worktree) is True

    assert not locked_dependency.exists()  # Lake cannot reuse the poisoned path.
    quarantined = list(
        (worktree / conductor._INTERRUPTED_PACKAGES_RELPATH).glob(
            "mathlib-*/mathlib"
        )
    )
    assert len(quarantined) == 1
    assert (quarantined[0] / "partial-source").read_text() == "partial"
    assert (
        quarantined[0] / ".git/index.lock"
    ).read_text() == "interrupted checkout"
    assert build_output.read_text() == "keep"
    assert project_source.read_text() == "def keep := true"


def test_prime_timeout_quarantines_dependency_locked_by_killed_clone(
    tmp_path, monkeypatch,
):
    bindir = tmp_path / "bin"
    _write_lake_stub(
        bindir,
        tmp_path / "rec.txt",
        before_exit=(
            "mkdir -p .lake/packages/mathlib/.git\n"
            "echo partial > .lake/packages/mathlib/source\n"
            "echo locked > .lake/packages/mathlib/.git/index.lock"
        ),
        sleep=3,
    )
    _put_on_path(monkeypatch, bindir)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    build_output = worktree / ".lake/build/keep"
    build_output.parent.mkdir(parents=True)
    build_output.write_text("compiled output")
    project_source = worktree / "Project.lean"
    project_source.write_text("def keep := true")

    assert conductor._prime_job_mathlib_cache(
        worktree, timeout=0.5
    ) is False

    assert not (worktree / ".lake/packages/mathlib").exists()
    quarantined = list(
        (worktree / conductor._INTERRUPTED_PACKAGES_RELPATH).glob(
            "mathlib-*/mathlib"
        )
    )
    assert len(quarantined) == 1
    assert (quarantined[0] / "source").read_text() == "partial\n"
    assert build_output.read_text() == "compiled output"
    assert project_source.read_text() == "def keep := true"


def test_prime_nonzero_quarantines_lock_but_preserves_unlocked_dependency(
    tmp_path, monkeypatch,
):
    bindir = tmp_path / "bin"
    _write_lake_stub(
        bindir,
        tmp_path / "rec.txt",
        exit_code=1,
        before_exit=(
            "mkdir -p .lake/packages/mathlib/.git\n"
            "echo locked > .lake/packages/mathlib/.git/index.lock"
        ),
    )
    _put_on_path(monkeypatch, bindir)
    worktree = tmp_path / "wt"
    unlocked = worktree / ".lake/packages/batteries"
    (unlocked / ".git").mkdir(parents=True)
    (unlocked / "intentional-edit").write_text("keep")

    assert conductor._prime_job_mathlib_cache(worktree) is False

    assert not (worktree / ".lake/packages/mathlib").exists()
    quarantined = list(
        (worktree / conductor._INTERRUPTED_PACKAGES_RELPATH).glob(
            "mathlib-*/mathlib"
        )
    )
    assert len(quarantined) == 1
    assert (
        quarantined[0] / ".git/index.lock"
    ).read_text() == "locked\n"
    assert (unlocked / "intentional-edit").read_text() == "keep"


def test_prime_timeout_env_override(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    _write_lake_stub(
        bindir, tmp_path / "rec.txt", exit_code=0, sleep=0.3
    )
    _put_on_path(monkeypatch, bindir)
    monkeypatch.setenv(conductor.CACHE_GET_TIMEOUT_ENV_VAR, "0.05")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert conductor._prime_mathlib_cache(worktree) is False


def test_cache_timeout_default_and_invalid_env_fallback(monkeypatch):
    monkeypatch.delenv(conductor.CACHE_GET_TIMEOUT_ENV_VAR, raising=False)
    assert (
        conductor._cache_get_timeout_seconds()
        == conductor.CACHE_GET_TIMEOUT_SECONDS
        == 1200
    )

    monkeypatch.setenv(conductor.CACHE_GET_TIMEOUT_ENV_VAR, "not-a-number")
    assert (
        conductor._cache_get_timeout_seconds()
        == conductor.CACHE_GET_TIMEOUT_SECONDS
    )

    monkeypatch.setenv(conductor.CACHE_GET_TIMEOUT_ENV_VAR, "0")
    assert (
        conductor._cache_get_timeout_seconds()
        == conductor.CACHE_GET_TIMEOUT_SECONDS
    )


def test_quarantine_never_overwrites_an_existing_capture(
    tmp_path, monkeypatch,
):
    worktree = tmp_path / "wt"
    dependency = worktree / ".lake/packages/mathlib"
    (dependency / ".git").mkdir(parents=True)
    (dependency / ".git/index.lock").write_text("new interrupted checkout")
    quarantine_root = (
        worktree / conductor._INTERRUPTED_PACKAGES_RELPATH
    )
    collision = quarantine_root / "mathlib-collision"
    collision.mkdir(parents=True)
    (collision / "sentinel").write_text("older capture")
    tokens = iter(("collision", "unique"))
    monkeypatch.setattr(
        conductor.secrets, "token_hex", lambda _size: next(tokens)
    )

    quarantined = conductor._quarantine_interrupted_lake_dependency(
        worktree, dependency
    )

    assert quarantined == quarantine_root / "mathlib-unique/mathlib"
    assert (collision / "sentinel").read_text() == "older capture"
    assert (
        quarantined / ".git/index.lock"
    ).read_text() == "new interrupted checkout"


def test_prime_defers_when_locked_dependency_cannot_be_quarantined(
    tmp_path, monkeypatch,
):
    bindir = tmp_path / "bin"
    record = tmp_path / "rec.txt"
    _write_lake_stub(bindir, record)
    _put_on_path(monkeypatch, bindir)
    worktree = tmp_path / "wt"
    dependency = worktree / ".lake/packages/mathlib"
    (dependency / ".git").mkdir(parents=True)
    (dependency / ".git/index.lock").write_text("keep")
    quarantine_root = (
        worktree / conductor._INTERRUPTED_PACKAGES_RELPATH
    )
    quarantine_root.parent.mkdir(parents=True, exist_ok=True)
    quarantine_root.write_text("blocks quarantine directory creation")

    assert (
        conductor._prime_job_mathlib_cache(worktree)
        is conductor._CACHE_PRIME_UNSAFE
    )

    assert (dependency / ".git/index.lock").read_text() == "keep"
    assert not record.exists()


def test_live_checkout_prime_does_not_move_locked_dependency(
    tmp_path, monkeypatch,
):
    bindir = tmp_path / "bin"
    record = tmp_path / "rec.txt"
    _write_lake_stub(bindir, record)
    _put_on_path(monkeypatch, bindir)
    live_checkout = tmp_path / "operator-checkout"
    dependency = live_checkout / ".lake/packages/mathlib"
    (dependency / ".git").mkdir(parents=True)
    (dependency / ".git/index.lock").write_text("operator state")

    assert conductor._prime_mathlib_cache(live_checkout) is True

    assert (dependency / ".git/index.lock").read_text() == "operator state"
    assert not (
        live_checkout / conductor._INTERRUPTED_PACKAGES_RELPATH
    ).exists()


def test_job_prime_refuses_symlinked_lake_root(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    record = tmp_path / "rec.txt"
    _write_lake_stub(bindir, record)
    _put_on_path(monkeypatch, bindir)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    shared_lake = tmp_path / "shared-lake"
    dependency = shared_lake / "packages/mathlib"
    (dependency / ".git").mkdir(parents=True)
    lock = dependency / ".git/index.lock"
    lock.write_text("shared state")
    (worktree / ".lake").symlink_to(shared_lake, target_is_directory=True)

    assert (
        conductor._prime_job_mathlib_cache(worktree)
        is conductor._CACHE_PRIME_UNSAFE
    )

    assert lock.read_text() == "shared state"
    assert dependency.parent == shared_lake / "packages"
    assert not record.exists()


def test_communicate_error_kills_and_reaps_before_quarantine(
    tmp_path, monkeypatch,
):
    events: list[str] = []

    class BrokenCommunicate:
        pid = 4321
        returncode = None
        calls = 0

        def communicate(self, *, timeout):
            self.calls += 1
            if self.calls == 1:
                events.append("communicate-error")
                raise RuntimeError("pipe failed")
            events.append("reap")
            self.returncode = -9
            return "", ""

        def poll(self):
            return self.returncode

    proc = BrokenCommunicate()
    monkeypatch.setattr(
        conductor, "find_lake_executable", lambda: "/fake/lake"
    )
    monkeypatch.setattr(
        conductor.subprocess, "Popen", lambda *_a, **_kw: proc
    )
    monkeypatch.setattr(conductor.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(
        conductor.os,
        "killpg",
        lambda _pgid, _signal: events.append("kill"),
    )

    def fake_quarantine(_worktree):
        events.append("quarantine")
        return True

    monkeypatch.setattr(
        conductor,
        "_quarantine_interrupted_lake_dependencies",
        fake_quarantine,
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert conductor._prime_job_mathlib_cache(worktree) is False
    assert events == [
        "quarantine",
        "communicate-error",
        "kill",
        "reap",
        "quarantine",
    ]


def test_unreaped_cache_process_is_unsafe_and_never_quarantined_afterward(
    tmp_path, monkeypatch,
):
    events: list[str] = []

    class Unreaped:
        pid = 9876
        returncode = None

        def communicate(self, *, timeout):
            events.append("communicate")
            raise RuntimeError("still live")

        def poll(self):
            return None

    monkeypatch.setattr(
        conductor, "find_lake_executable", lambda: "/fake/lake"
    )
    monkeypatch.setattr(
        conductor.subprocess, "Popen", lambda *_a, **_kw: Unreaped()
    )
    monkeypatch.setattr(conductor.os, "getpgid", lambda _pid: 9876)
    monkeypatch.setattr(
        conductor.os,
        "killpg",
        lambda _pgid, _signal: events.append("kill"),
    )

    def fake_quarantine(_worktree):
        events.append("quarantine")
        return True

    monkeypatch.setattr(
        conductor,
        "_quarantine_interrupted_lake_dependencies",
        fake_quarantine,
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert (
        conductor._prime_job_mathlib_cache(worktree)
        is conductor._CACHE_PRIME_UNSAFE
    )
    assert events == ["quarantine", "communicate", "kill", "communicate"]


def test_prime_skip_env_noops_without_calling_lake(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    record = tmp_path / "rec.txt"
    _write_lake_stub(bindir, record, exit_code=0)
    _put_on_path(monkeypatch, bindir)
    monkeypatch.setenv("MARATHON_SKIP_CACHE_GET", "1")
    worktree = tmp_path / "wt"
    locked_dependency = worktree / ".lake/packages/mathlib"
    (locked_dependency / ".git").mkdir(parents=True)
    (locked_dependency / ".git/index.lock").write_text("stale")

    assert conductor._prime_job_mathlib_cache(worktree) is False

    assert not record.exists()  # lake was never invoked
    assert not locked_dependency.exists()  # fallback was still made safe
    quarantined = list(
        (worktree / conductor._INTERRUPTED_PACKAGES_RELPATH).glob(
            "mathlib-*/mathlib"
        )
    )
    assert len(quarantined) == 1
    assert (
        quarantined[0] / ".git/index.lock"
    ).read_text() == "stale"


def test_prime_no_lake_on_path_returns_false(tmp_path, monkeypatch):
    # A PATH with no `lake` at all.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("MARATHON_SKIP_CACHE_GET", raising=False)
    monkeypatch.setattr(conductor, "find_lake_executable", lambda: None)
    assert conductor._prime_mathlib_cache(tmp_path / "wt") is False


def test_prime_uses_resolved_elan_lake_when_path_omits_it(
    tmp_path, monkeypatch,
):
    bindir = tmp_path / "home" / ".elan" / "bin"
    record = tmp_path / "rec.txt"
    _write_lake_stub(bindir, record, exit_code=0)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("MARATHON_SKIP_CACHE_GET", raising=False)
    monkeypatch.setattr(
        conductor, "find_lake_executable", lambda: str(bindir / "lake")
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert conductor._prime_mathlib_cache(worktree) is True
    assert "ARGV: exe cache get" in record.read_text()


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
    monkeypatch.setattr(
        conductor,
        "_refine_branch_for_dispatch",
        lambda *a, **k: "marathon/refine-c14-i22",
    )
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


def test_reuse_branch_defers_when_cache_poison_cannot_be_quarantined(
    tmp_path, monkeypatch,
):
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    wt = tmp_path / "kept-wt"
    wt.mkdir()
    kept = {22: str(wt)}
    monkeypatch.setattr(
        conductor,
        "_prime_job_mathlib_cache",
        lambda _w: conductor._CACHE_PRIME_UNSAFE,
    )
    monkeypatch.setattr(
        conductor,
        "_refine_branch_for_dispatch",
        lambda *_a, **_kw: "marathon/refine-c14-i22",
    )

    def fail_popen(*_args, **_kwargs):
        raise AssertionError("unsafe worktree must not launch a worker")

    monkeypatch.setattr(conductor.subprocess, "Popen", fail_popen)

    assert (
        conductor._dispatch_job(
            cfg, 22, 14, tmp_path / "runs", kept
        )
        is None
    )
    assert kept[22] == str(wt)

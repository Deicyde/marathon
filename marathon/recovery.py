"""Operational recovery helpers for review/refine jobs.

The first recovery rung is deliberately cheap: re-run Marathon's local Lake
build for an *existing* pull-request head without asking Aristotle to do any
new work and without creating a replacement commit.  The result is attached
to that immutable commit through the trusted ``marathon/lake-build`` GitHub
commit-status context.

This module has a small standalone CLI so the conductor can run it in an
isolated worktree without importing or configuring the Aristotle runtime::

    python -m marathon.recovery retry-build \
      --repo-dir /path/to/worktree \
      --repo owner/name --pr 123 \
      --expected-head <full-sha> \
      --expected-branch marathon/refine-c15-i123 \
      --target GeometricAnalysis/LeeSM/Chapter15

The expected SHA is checked against both the local worktree and the live PR
before the build.  The live PR is checked again after the build, so a status
from a stale/racing worker can never attest to a newer PR head.

``update-continue`` is the next rung. It first checkpoints any unpublished
files in the selected PR branch, joins an ahead or diverged local checkpoint
with the exact selected PR head, then merges the PR's current base.  A green
tree publishes immediately. Conflicts or a red build continue the matching
saved Aristotle session when one exists; otherwise they use the explicitly
provided fallback refine command.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

try:  # POSIX advisory locks (Linux/macOS).
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX hosts
    _fcntl = None

try:  # Windows advisory byte-range locks.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised on non-Windows hosts
    _msvcrt = None

from marathon.post_pipeline import (
    BuildResult,
    _target_lean_modules,
    lean_paths_to_targets,
    run_lake_build,
)
from marathon.review.automerge import TRUSTED_BUILD_CONTEXT
from marathon.review.github import gh
from marathon.state import (
    RefineState,
    load_refine_state,
    now_iso,
    save_refine_state,
)


EXIT_BUILD_FAILED = 1
EXIT_RECOVERY_ABORTED = 2
UPDATE_RECOVERY_MARKER_FILENAME = "marathon-update-recovery-prepared.json"
UPDATE_RECOVERY_MARKER_LOCK_FILENAME = "marathon-update-recovery-prepared.lock"
UPDATE_RECOVERY_SCHEMA_VERSION = 2
UPDATE_RECOVERY_PHASE_STARTING = "starting"
UPDATE_RECOVERY_PHASE_MERGED = "merged"
UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED = "fallback_prepared"
UPDATE_RECOVERY_PHASE_PUSHING = "pushing"
UPDATE_RECOVERY_PHASE_PUBLISHED = "published"
UPDATE_RECOVERY_PHASES = frozenset({
    UPDATE_RECOVERY_PHASE_STARTING,
    UPDATE_RECOVERY_PHASE_MERGED,
    UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED,
    UPDATE_RECOVERY_PHASE_PUSHING,
    UPDATE_RECOVERY_PHASE_PUBLISHED,
})
MAX_UPDATE_BASE_ROLLFORWARDS = 8
_FULL_GIT_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class RecoveryError(RuntimeError):
    """A safe preflight/publication abort, rather than a failed Lake build."""


class _UpdateMarkerLockState:
    """One path's thread-reentrant lock plus its outermost OS lock."""

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.handle = None


_UPDATE_MARKER_LOCKS_GUARD = threading.Lock()
_UPDATE_MARKER_LOCKS: dict[str, _UpdateMarkerLockState] = {}


def _update_marker_lock_state(path: Path) -> _UpdateMarkerLockState:
    key = str(path.resolve())
    with _UPDATE_MARKER_LOCKS_GUARD:
        return _UPDATE_MARKER_LOCKS.setdefault(key, _UpdateMarkerLockState())


def _lock_update_marker_file(handle) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows-only branch
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
        return
    raise RecoveryError(
        "update recovery journals require an operating-system file lock"
    )


def _unlock_update_marker_file(handle) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows-only branch
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


@contextmanager
def _update_recovery_marker_lock(marker: Path):
    """Serialize every update-journal mutation across threads and processes.

    The stable sibling lock file is never replaced.  Nested helpers share the
    outer descriptor, avoiding the self-deadlock caused by opening and flocking
    the same inode twice in one thread.
    """
    lock_path = marker.with_name(UPDATE_RECOVERY_MARKER_LOCK_FILENAME)
    state = _update_marker_lock_state(lock_path)
    with state.rlock:
        if state.depth == 0:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            state.handle = lock_path.open("a+b")
            try:
                _lock_update_marker_file(state.handle)
            except BaseException:
                state.handle.close()
                state.handle = None
                raise
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0:
                handle = state.handle
                state.handle = None
                assert handle is not None
                try:
                    _unlock_update_marker_file(handle)
                finally:
                    handle.close()


@dataclass(frozen=True)
class PullRequestRevision:
    number: int
    state: str
    head_branch: str
    head_sha: str
    base_branch: str
    base_sha: str
    url: str


@dataclass(frozen=True)
class RetryBuildConfig:
    repo_dir: Path
    github_repo: str
    pr_number: int
    expected_head_sha: str
    target_path: Path
    expected_branch: Optional[str] = None
    expected_base: Optional[str] = None
    build_timeout: int = 600


@dataclass(frozen=True)
class RetryBuildResult:
    ok: bool
    exit_code: int
    head_sha: str
    branch: str
    base_sha: str
    targets: tuple[str, ...]
    build: Optional[BuildResult]
    message: str


@dataclass(frozen=True)
class UpdateContinueConfig:
    repo_dir: Path
    github_repo: str
    pr_number: int
    expected_head_sha: str
    target_path: Path
    workdir: Path
    fallback_command: tuple[str, ...]
    expected_branch: Optional[str] = None
    expected_base: Optional[str] = None
    build_timeout: int = 600
    operation_id: Optional[str] = None


@dataclass(frozen=True)
class UpdateContinueResult:
    ok: bool
    exit_code: int
    fast_path: bool
    head_sha: str
    branch: str
    build: Optional[BuildResult]
    fallback_invoked: bool
    message: str


def _git(repo_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RecoveryError(f"could not run git in {repo_dir}: {exc}") from exc
    if check and proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout) or "unknown error").strip()
        raise RecoveryError(
            f"git {' '.join(args[:3])} failed (exit {proc.returncode}): "
            f"{detail[:500]}"
        )
    return proc


def _normalise_full_oid(value: str, *, label: str) -> str:
    oid = value.strip().lower()
    if not _FULL_GIT_OID_RE.fullmatch(oid):
        raise RecoveryError(
            f"{label} must be a full 40- or 64-character hexadecimal Git OID"
        )
    return oid


def _read_pr_revision(github_repo: str, pr_number: int) -> PullRequestRevision:
    """Read the PR's exact head/base OIDs from GitHub's REST representation."""
    try:
        proc = gh(
            "api",
            f"repos/{github_repo}/pulls/{pr_number}",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError(f"could not read PR #{pr_number}: {exc}") from exc
    if proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout) or "unknown error").strip()
        raise RecoveryError(
            f"could not read PR #{pr_number} (gh exit {proc.returncode}): "
            f"{detail[:500]}"
        )
    try:
        data = json.loads(proc.stdout)
        head = data["head"]
        base = data["base"]
        return PullRequestRevision(
            number=int(data["number"]),
            state=str(data["state"]).lower(),
            head_branch=str(head["ref"]),
            head_sha=_normalise_full_oid(str(head["sha"]), label="PR head SHA"),
            base_branch=str(base["ref"]),
            base_sha=_normalise_full_oid(str(base["sha"]), label="PR base SHA"),
            url=str(data.get("html_url") or ""),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"GitHub returned malformed metadata for PR #{pr_number}") from exc


def _validate_live_pr(
    config: RetryBuildConfig | UpdateContinueConfig,
    pr: PullRequestRevision,
    *,
    expected_head_sha: Optional[str] = None,
    expected_base_sha: Optional[str] = None,
) -> None:
    expected = _normalise_full_oid(
        expected_head_sha or config.expected_head_sha,
        label="expected head SHA",
    )
    if pr.number != config.pr_number:
        raise RecoveryError(
            f"GitHub returned PR #{pr.number}, expected #{config.pr_number}"
        )
    if pr.state != "open":
        raise RecoveryError(f"PR #{config.pr_number} is {pr.state}, not open")
    if pr.head_sha != expected:
        raise RecoveryError(
            f"PR #{config.pr_number} head changed: expected {expected}, "
            f"found {pr.head_sha}; refusing to build or publish a stale result"
        )
    if config.expected_branch and pr.head_branch != config.expected_branch:
        raise RecoveryError(
            f"PR #{config.pr_number} branch changed: expected "
            f"{config.expected_branch!r}, found {pr.head_branch!r}"
        )
    if config.expected_base and pr.base_branch != config.expected_base:
        raise RecoveryError(
            f"PR #{config.pr_number} base changed: expected "
            f"{config.expected_base!r}, found {pr.base_branch!r}"
        )
    if expected_base_sha is not None:
        expected_base_oid = _normalise_full_oid(
            expected_base_sha, label="expected base SHA"
        )
        if pr.base_sha != expected_base_oid:
            raise RecoveryError(
                f"PR #{config.pr_number} base moved: expected "
                f"{expected_base_oid}, found {pr.base_sha}"
            )


def _validate_local_checkout(
    config: RetryBuildConfig | UpdateContinueConfig,
    pr: PullRequestRevision,
) -> Path:
    repo_dir = config.repo_dir.expanduser().resolve()
    if not repo_dir.is_dir():
        raise RecoveryError(f"repository worktree does not exist: {repo_dir}")

    top = Path(_git(repo_dir, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != repo_dir:
        raise RecoveryError(
            f"--repo-dir must name the worktree root ({top}), not {repo_dir}"
        )

    local_head = _normalise_full_oid(
        _git(repo_dir, "rev-parse", "HEAD").stdout,
        label="local HEAD",
    )
    if local_head != pr.head_sha:
        raise RecoveryError(
            f"local HEAD {local_head} does not match PR #{pr.number} head "
            f"{pr.head_sha}"
        )

    # Do not require the PR's named branch here. The continuation recovery may
    # already hold that branch in a dirty worktree, so the conductor should be
    # free to create a second, detached clean worktree at the same immutable
    # SHA for this build-only action. The live GitHub branch is validated by
    # _validate_live_pr; local HEAD + cleanliness are the attestation boundary.

    dirty = _git(
        repo_dir,
        "status",
        "--porcelain",
        "--untracked-files=no",
    ).stdout.rstrip("\n")
    if dirty:
        lines = dirty.splitlines()
        preview = "; ".join(lines[:5])
        raise RecoveryError(
            "tracked worktree changes would make a SHA-bound build dishonest; "
            f"refusing retry-build ({preview})"
        )

    target = config.target_path.expanduser()
    if not target.is_absolute():
        target = repo_dir / target
    target = target.resolve()
    try:
        target.relative_to(repo_dir)
    except ValueError as exc:
        raise RecoveryError(f"target path is outside the repository: {target}") from exc
    if not target.is_dir():
        raise RecoveryError(f"target path is not a directory: {target}")
    return target


def _ensure_commit_available(repo_dir: Path, oid: str, remote_ref: str) -> None:
    probe = _git(
        repo_dir,
        "cat-file",
        "-e",
        f"{oid}^{{commit}}",
        check=False,
    )
    if probe.returncode == 0:
        return

    # The PR base may not be in a shallow/new worktree yet. Fetching its named
    # branch is non-destructive and normally brings the queried base commit
    # into the object database even if the base advanced after our API read.
    fetch = _git(
        repo_dir,
        "fetch",
        "--no-tags",
        "origin",
        remote_ref,
        check=False,
    )
    if fetch.returncode != 0:
        detail = ((fetch.stderr or fetch.stdout) or "unknown error").strip()
        raise RecoveryError(
            f"PR base commit {oid} is unavailable locally and fetching "
            f"origin/{remote_ref} failed: {detail[:500]}"
        )
    if _git(
        repo_dir,
        "cat-file",
        "-e",
        f"{oid}^{{commit}}",
        check=False,
    ).returncode != 0:
        raise RecoveryError(
            f"PR base commit {oid} is still unavailable after fetching "
            f"origin/{remote_ref}"
        )


def plan_retry_build_targets(
    repo_dir: Path,
    *,
    base_sha: str,
    head_sha: str,
    target_path: Path,
) -> list[str]:
    """Plan the same explicit Lake targets used by the post-pipeline.

    A retry worktree is clean, so the post-pipeline's working-tree diff is
    necessarily empty.  We instead feed its existing Lean path-to-target
    planner the PR commit diff.  If the PR has no buildable changed Lean file
    (for example it only deleted one), use the existing target-directory
    fallback so the chapter, rather than an unrelated default closure, is
    compiled.
    """
    merge_base = _git(repo_dir, "merge-base", base_sha, head_sha).stdout.strip()
    if not merge_base:
        raise RecoveryError(
            f"could not find a merge base between {base_sha} and {head_sha}"
        )
    changed = _git(
        repo_dir,
        "diff",
        "--name-only",
        "--diff-filter=ACMRT",
        merge_base,
        head_sha,
        "--",
        "*.lean",
    ).stdout.splitlines()
    targets = lean_paths_to_targets(repo_dir, changed)
    if not targets:
        targets = _target_lean_modules(repo_dir, target_path)
    return targets


def _post_build_status(
    config: RetryBuildConfig | UpdateContinueConfig,
    *,
    sha: str,
    state: str,
    description: str,
    target_url: str,
) -> None:
    if state not in {"pending", "success", "failure"}:
        raise ValueError(f"unsupported commit status state: {state}")
    args = [
        "api",
        "--method",
        "POST",
        f"repos/{config.github_repo}/statuses/{sha}",
        "-f",
        f"state={state}",
        "-f",
        f"context={TRUSTED_BUILD_CONTEXT}",
        "-f",
        f"description={description[:140]}",
    ]
    if target_url:
        args.extend(("-f", f"target_url={target_url}"))
    try:
        proc = gh(*args, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError(
            f"could not post {TRUSTED_BUILD_CONTEXT}={state} on {sha}: {exc}"
        ) from exc
    if proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout) or "unknown error").strip()
        raise RecoveryError(
            f"could not post {TRUSTED_BUILD_CONTEXT}={state} on {sha}: "
            f"{detail[:500]}"
        )


def _failure_description(build: BuildResult, timeout: int) -> str:
    if build.skipped_reason:
        return f"lake build could not run: {build.skipped_reason}"
    if build.timed_out:
        return f"lake build timed out after {timeout}s"
    return "lake build failed"


def _best_effort_failure_status(
    config: RetryBuildConfig,
    *,
    sha: str,
    description: str,
    target_url: str,
) -> Optional[str]:
    try:
        _post_build_status(
            config,
            sha=sha,
            state="failure",
            description=description,
            target_url=target_url,
        )
    except RecoveryError as exc:
        return str(exc)
    return None


def retry_build(config: RetryBuildConfig) -> RetryBuildResult:
    """Rebuild one immutable PR head without invoking Aristotle."""
    expected = _normalise_full_oid(
        config.expected_head_sha, label="expected head SHA"
    )
    if config.pr_number <= 0:
        raise RecoveryError("PR number must be positive")
    if config.build_timeout <= 0:
        raise RecoveryError("build timeout must be positive")
    if "/" not in config.github_repo:
        raise RecoveryError("GitHub repository must be in owner/name form")

    repo_dir = config.repo_dir.expanduser().resolve()
    pr = _read_pr_revision(config.github_repo, config.pr_number)
    _validate_live_pr(config, pr)
    target = _validate_local_checkout(config, pr)
    _ensure_commit_available(repo_dir, pr.base_sha, pr.base_branch)
    targets = plan_retry_build_targets(
        repo_dir,
        base_sha=pr.base_sha,
        head_sha=expected,
        target_path=target,
    )
    if not targets:
        raise RecoveryError(
            "no buildable Lean target could be derived from the PR diff or "
            f"target directory {target}; refusing to publish a vacuous "
            "default-build attestation"
        )

    scope = f"{len(targets)} explicit target(s)"
    _post_build_status(
        config,
        sha=expected,
        state="pending",
        description=f"Marathon retrying lake build ({scope})",
        target_url=pr.url,
    )

    try:
        build = run_lake_build(
            repo_dir,
            config.build_timeout,
            targets=targets,
        )
    except BaseException as exc:
        status_error = _best_effort_failure_status(
            config,
            sha=expected,
            description="lake build runner crashed",
            target_url=pr.url,
        )
        suffix = f"; status publication also failed: {status_error}" if status_error else ""
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise RecoveryError(f"lake build runner crashed: {exc}{suffix}") from exc

    # Check both sides of the SHA attestation again after the potentially long
    # build.  A concurrent branch push or local commit invalidates this result.
    try:
        current_pr = _read_pr_revision(config.github_repo, config.pr_number)
        _validate_live_pr(
            config,
            current_pr,
            expected_base_sha=pr.base_sha,
        )
        _validate_local_checkout(config, current_pr)
    except RecoveryError as exc:
        status_error = _best_effort_failure_status(
            config,
            sha=expected,
            description="PR head changed; stale build result discarded",
            target_url=pr.url,
        )
        suffix = f"; failure status also failed: {status_error}" if status_error else ""
        return RetryBuildResult(
            ok=False,
            exit_code=EXIT_RECOVERY_ABORTED,
            head_sha=expected,
            branch=pr.head_branch,
            base_sha=pr.base_sha,
            targets=tuple(targets),
            build=build,
            message=f"{exc}{suffix}",
        )

    if build.ok is True and not build.timed_out and not build.skipped_reason:
        duration = (
            f" in {build.duration_seconds:.1f}s"
            if build.duration_seconds is not None
            else ""
        )
        _post_build_status(
            config,
            sha=expected,
            state="success",
            description=f"lake build passed{duration} ({scope})",
            target_url=pr.url,
        )
        return RetryBuildResult(
            ok=True,
            exit_code=0,
            head_sha=expected,
            branch=pr.head_branch,
            base_sha=pr.base_sha,
            targets=tuple(targets),
            build=build,
            message=f"lake build passed for PR #{config.pr_number} at {expected}",
        )

    description = _failure_description(build, config.build_timeout)
    _post_build_status(
        config,
        sha=expected,
        state="failure",
        description=description,
        target_url=pr.url,
    )
    return RetryBuildResult(
        ok=False,
        exit_code=EXIT_BUILD_FAILED,
        head_sha=expected,
        branch=pr.head_branch,
        base_sha=pr.base_sha,
        targets=tuple(targets),
        build=build,
        message=f"{description} for PR #{config.pr_number} at {expected}",
    )


def _validate_update_checkout(
    config: UpdateContinueConfig,
    pr: PullRequestRevision,
) -> tuple[Path, str]:
    """Bind and checkpoint the named local branch selected for recovery.

    Build-only recovery must attest to an immutable clean PR head.  Update
    recovery has a different job: preserve a dirty/ahead local checkpoint and
    join it with that immutable PR head before publication.  The branch name,
    live PR identity, and exact remote lease are therefore the ownership
    boundary; every non-runtime worktree change is committed locally before a
    crash-safe journal is created.
    """
    repo_dir = config.repo_dir.expanduser().resolve()
    if not repo_dir.is_dir():
        raise RecoveryError(f"repository worktree does not exist: {repo_dir}")
    top = Path(_git(repo_dir, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != repo_dir:
        raise RecoveryError(
            f"--repo-dir must name the worktree root ({top}), not {repo_dir}"
        )
    branch = _git(
        repo_dir,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    actual = (branch.stdout or "").strip()
    if branch.returncode != 0 or not actual:
        raise RecoveryError("update-continue requires a named local branch")
    if actual != pr.head_branch:
        raise RecoveryError(
            f"local branch {actual!r} does not match PR head branch "
            f"{pr.head_branch!r}"
        )

    target = _update_target_path(config)
    try:
        target.relative_to(repo_dir)
    except ValueError as exc:
        raise RecoveryError(f"target path is outside the repository: {target}") from exc
    if not target.is_dir():
        raise RecoveryError(f"target path is not a directory: {target}")

    unmerged = _git(
        repo_dir,
        "diff",
        "--name-only",
        "--diff-filter=U",
        check=False,
    )
    if unmerged.returncode != 0 or (unmerged.stdout or "").strip():
        raise RecoveryError(
            "cannot adopt a worktree with a pre-existing unresolved merge; "
            "resolve or abort that merge first"
        )

    # The conductor's liveness marker lives under .marathon/.  It must remain
    # on disk, but it is runtime identity rather than formalization work and
    # must never be swept into a recovery commit.  All other visible files,
    # including newly-created Lean modules, are part of the checkpoint.
    pathspec = (".", ":(exclude).marathon", ":(exclude).marathon/**")
    status = _git(
        repo_dir,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        *pathspec,
    ).stdout.strip()
    if status:
        added = _git(repo_dir, "add", "-A", "--", *pathspec, check=False)
        if added.returncode != 0:
            detail = ((added.stderr or added.stdout) or "unknown error").strip()
            raise RecoveryError(
                f"could not stage the local recovery checkpoint: {detail[:500]}"
            )
        committed = _git(
            repo_dir,
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "-m",
            _adoption_checkpoint_subject(pr.number),
            check=False,
        )
        if committed.returncode != 0:
            detail = (
                (committed.stderr or committed.stdout) or "unknown error"
            ).strip()
            raise RecoveryError(
                f"could not commit the local recovery checkpoint: {detail[:500]}"
            )
    remaining = _git(
        repo_dir,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        *pathspec,
    ).stdout.strip()
    if remaining:
        raise RecoveryError(
            "local recovery checkpoint remained dirty: "
            + "; ".join(remaining.splitlines()[:5])
        )
    return target, _head_sha(repo_dir)


def _head_sha(repo_dir: Path) -> str:
    return _normalise_full_oid(
        _git(repo_dir, "rev-parse", "HEAD").stdout,
        label="local HEAD",
    )


def _merge_base_checkpoint(
    repo_dir: Path,
    *,
    base_sha: str,
    pr_number: int,
    conflict_subject: Optional[str] = None,
    ancestor_label: str = "current PR base",
) -> tuple[str, bool]:
    """Merge one required revision and commit conflict markers when needed.

    Returns ``(new_head, had_conflicts)``. A non-conflict merge error is
    aborted and reported instead of being mistaken for code that Aristotle
    should repair (authentication, hooks, disk failures, and corrupt indexes
    are operational failures, not formalization tasks).
    """
    merge = _git(
        repo_dir,
        "-c",
        "commit.gpgSign=false",
        "merge",
        "--no-edit",
        "--no-stat",
        "--no-verify",
        base_sha,
        check=False,
    )
    had_conflicts = False
    if merge.returncode != 0:
        merge_head = _git(
            repo_dir,
            "rev-parse",
            "--verify",
            "--quiet",
            "MERGE_HEAD",
            check=False,
        )
        unmerged = _git(
            repo_dir,
            "diff",
            "--name-only",
            "--diff-filter=U",
            check=False,
        )
        if merge_head.returncode != 0 or not (unmerged.stdout or "").strip():
            detail = ((merge.stderr or merge.stdout) or "unknown error").strip()
            _git(repo_dir, "merge", "--abort", check=False)
            raise RecoveryError(
                f"git merge of PR base {base_sha} failed without resolvable "
                f"conflicts: {detail[:500]}"
            )

        # Record the conflict-marker tree as an ordinary local merge commit.
        # This both preserves every side of the conflict and gives refine's
        # preserve_local_branch guard one immutable continuation SHA.
        # Resolve every tracked unmerged path without sweeping conductor PID
        # markers or other unrelated untracked runtime files into the commit.
        added = _git(repo_dir, "add", "-u", "--", check=False)
        if added.returncode != 0:
            detail = ((added.stderr or added.stdout) or "unknown error").strip()
            raise RecoveryError(
                f"could not stage merge-conflict checkpoint: {detail[:500]}"
            )
        committed = _git(
            repo_dir,
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "-m",
            conflict_subject or _conflict_checkpoint_subject(pr_number),
            check=False,
        )
        if committed.returncode != 0:
            detail = (
                (committed.stderr or committed.stdout) or "unknown error"
            ).strip()
            raise RecoveryError(
                f"could not commit merge-conflict checkpoint: {detail[:500]}"
            )
        had_conflicts = True

    head = _head_sha(repo_dir)
    dirty = _git(
        repo_dir,
        "status",
        "--porcelain",
        "--untracked-files=no",
    ).stdout.strip()
    if dirty:
        raise RecoveryError(
            "base-update checkpoint is not a clean committed tree: "
            + "; ".join(dirty.splitlines()[:5])
        )
    ancestor = _git(
        repo_dir,
        "merge-base",
        "--is-ancestor",
        base_sha,
        head,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RecoveryError(
            f"updated checkpoint {head} does not contain the {ancestor_label} "
            f"{base_sha}"
        )
    return head, had_conflicts


def _normalise_fallback(command: Sequence[str]) -> tuple[str, ...]:
    fallback = tuple(str(part) for part in command)
    if fallback[:1] == ("--",):
        fallback = fallback[1:]
    if not fallback:
        raise RecoveryError(
            "update-continue requires a fallback command after `--`"
        )
    return fallback


def _normalise_operation_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    operation_id = str(value).strip()
    if not operation_id:
        raise RecoveryError("operation ID must not be blank")
    return operation_id


def _marker_json_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync after a marker rename/link.

    The journal is still atomically visible on platforms which do not permit
    opening a directory descriptor; fsync merely strengthens power-loss
    durability on the POSIX hosts Marathon normally runs on.
    """
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_marker_temp(marker: Path, payload: dict) -> Path:
    tmp = marker.with_name(
        f".{marker.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with tmp.open("xb") as handle:
            handle.write(_marker_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RecoveryError(f"could not persist update recovery marker: {exc}") from exc
    return tmp


def _atomic_create_update_recovery_marker(marker: Path, payload: dict) -> None:
    with _update_recovery_marker_lock(marker):
        _atomic_create_update_recovery_marker_unlocked(marker, payload)


def _atomic_create_update_recovery_marker_unlocked(
    marker: Path, payload: dict
) -> None:
    """Publish the initial marker without ever replacing another operation.

    A fully-written temp file is hard-linked into place.  The link is an atomic
    create-if-absent operation, unlike ``Path.replace`` which could silently
    overwrite a marker won by a concurrent worker.
    """
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RecoveryError(
            f"could not create update recovery workdir {marker.parent}: {exc}"
        ) from exc
    tmp = _write_marker_temp(marker, payload)
    try:
        os.link(tmp, marker)
        _fsync_directory(marker.parent)
    except FileExistsError as exc:
        raise RecoveryError(
            f"update recovery marker appeared concurrently at {marker}; retry "
            "through the conductor"
        ) from exc
    except OSError as exc:
        raise RecoveryError(f"could not create update recovery marker: {exc}") from exc
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _atomic_replace_update_recovery_marker(marker: Path, payload: dict) -> None:
    with _update_recovery_marker_lock(marker):
        _atomic_replace_update_recovery_marker_unlocked(marker, payload)


def _atomic_replace_update_recovery_marker_unlocked(
    marker: Path, payload: dict
) -> None:
    tmp = _write_marker_temp(marker, payload)
    try:
        os.replace(tmp, marker)
        _fsync_directory(marker.parent)
    except OSError as exc:
        raise RecoveryError(f"could not update recovery marker: {exc}") from exc
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _load_update_recovery_marker(marker: Path) -> dict:
    try:
        payload = json.loads(marker.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"could not read update recovery marker {marker}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecoveryError(f"update recovery marker {marker} is not an object")
    if payload.get("schema_version") != UPDATE_RECOVERY_SCHEMA_VERSION:
        raise RecoveryError(
            f"unsupported update recovery marker schema "
            f"{payload.get('schema_version')!r} at {marker}"
        )
    phase = payload.get("phase")
    if phase not in UPDATE_RECOVERY_PHASES:
        raise RecoveryError(f"invalid update recovery phase {phase!r} at {marker}")
    try:
        payload["pr"] = int(payload["pr"])
        payload["original_expected_head"] = _normalise_full_oid(
            str(payload["original_expected_head"]),
            label="marker original expected head",
        )
        payload["expected_remote_head"] = _normalise_full_oid(
            str(
                payload.get(
                    "expected_remote_head", payload["original_expected_head"]
                )
            ),
            label="marker expected remote head",
        )
        payload["base_sha"] = _normalise_full_oid(
            str(payload["base_sha"]), label="marker base SHA"
        )
        payload["current_local_head"] = _normalise_full_oid(
            str(payload["current_local_head"]), label="marker current local head"
        )
        payload["adopted_local_head"] = _normalise_full_oid(
            str(
                payload.get(
                    "adopted_local_head", payload["original_expected_head"]
                )
            ),
            label="marker adopted local head",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError(f"malformed update recovery identity at {marker}") from exc
    for field in ("github_repo", "branch", "base_branch", "target_path"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise RecoveryError(
                f"update recovery marker {marker} has invalid {field}"
            )
    operation_id = payload.get("operation_id")
    if operation_id is not None and (
        not isinstance(operation_id, str) or not operation_id.strip()
    ):
        raise RecoveryError(
            f"update recovery marker {marker} has invalid operation_id"
        )
    return payload


def _update_target_path(config: UpdateContinueConfig) -> Path:
    repo_dir = config.repo_dir.expanduser().resolve()
    target = config.target_path.expanduser()
    if not target.is_absolute():
        target = repo_dir / target
    return target.resolve()


def _validate_update_marker_identity(
    config: UpdateContinueConfig,
    marker: dict,
) -> None:
    expected_head = _normalise_full_oid(
        config.expected_head_sha, label="expected head SHA"
    )
    expected_operation = _normalise_operation_id(config.operation_id)
    checks = (
        (marker["pr"], config.pr_number, "PR"),
        (marker["github_repo"], config.github_repo, "GitHub repository"),
        (marker["original_expected_head"], expected_head, "original head"),
        (marker.get("operation_id"), expected_operation, "operation ID"),
        (marker["target_path"], str(_update_target_path(config)), "target path"),
    )
    for actual, expected, label in checks:
        if actual != expected:
            raise RecoveryError(
                f"update recovery {label} mismatch: marker has {actual!r}, "
                f"request expects {expected!r}"
            )
    if config.expected_branch and marker["branch"] != config.expected_branch:
        raise RecoveryError(
            f"update recovery branch mismatch: marker has {marker['branch']!r}, "
            f"request expects {config.expected_branch!r}"
        )
    if config.expected_base and marker["base_branch"] != config.expected_base:
        raise RecoveryError(
            f"update recovery base mismatch: marker has "
            f"{marker['base_branch']!r}, request expects {config.expected_base!r}"
        )


def _new_update_recovery_marker(
    config: UpdateContinueConfig,
    pr: PullRequestRevision,
    target: Path,
    adopted_local_head: str,
    *,
    base_sha: Optional[str] = None,
) -> dict:
    stamp = now_iso()
    return {
        "schema_version": UPDATE_RECOVERY_SCHEMA_VERSION,
        "phase": UPDATE_RECOVERY_PHASE_STARTING,
        "created_at": stamp,
        "updated_at": stamp,
        "operation_id": _normalise_operation_id(config.operation_id),
        "github_repo": config.github_repo,
        "pr": config.pr_number,
        "branch": pr.head_branch,
        "original_expected_head": pr.head_sha,
        "expected_remote_head": pr.head_sha,
        "adopted_local_head": adopted_local_head,
        "base_branch": pr.base_branch,
        "base_sha": _normalise_full_oid(
            base_sha or pr.base_sha, label="recovery base SHA"
        ),
        "current_local_head": adopted_local_head,
        "target_path": str(target),
        "had_conflicts": False,
        "reason": None,
    }


def _transition_update_recovery_marker(
    marker_path: Path,
    marker: dict,
    phase: str,
    *,
    current_local_head: Optional[str] = None,
    **updates: object,
) -> dict:
    allowed = {
        UPDATE_RECOVERY_PHASE_STARTING: {UPDATE_RECOVERY_PHASE_MERGED},
        UPDATE_RECOVERY_PHASE_MERGED: {
            UPDATE_RECOVERY_PHASE_STARTING,
            UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED,
            UPDATE_RECOVERY_PHASE_PUSHING,
        },
        UPDATE_RECOVERY_PHASE_PUSHING: {
            UPDATE_RECOVERY_PHASE_STARTING,
            UPDATE_RECOVERY_PHASE_PUBLISHED,
        },
    }
    current_phase = marker["phase"]
    if phase not in allowed.get(current_phase, set()):
        raise RecoveryError(
            f"invalid update recovery transition {current_phase!r} -> {phase!r}"
        )
    updates["phase"] = phase
    if current_local_head is not None:
        updates["current_local_head"] = _normalise_full_oid(
            current_local_head, label="current local head"
        )
    return _replace_update_recovery_marker_fields(
        marker_path,
        marker,
        **updates,
    )


def _replace_update_recovery_marker_fields(
    marker_path: Path,
    marker: dict,
    **updates: object,
) -> dict:
    """CAS-like atomic replacement for a marker phase or checkpoint field."""
    with _update_recovery_marker_lock(marker_path):
        on_disk = _load_update_recovery_marker(marker_path)
        if on_disk != marker:
            raise RecoveryError(
                f"update recovery marker changed concurrently at {marker_path}"
            )
        payload = dict(marker)
        payload.update(updates)
        payload["updated_at"] = now_iso()
        _atomic_replace_update_recovery_marker_unlocked(marker_path, payload)
        return payload


def _prepare_fresh_update_fallback(
    config: UpdateContinueConfig,
    *,
    marker_path: Path,
    marker: dict,
    target: Path,
    local_head: str,
    reason: str,
) -> tuple[tuple[str, ...], dict]:
    """Prepare fallback while retaining a matching Aristotle checkpoint."""
    fallback = _normalise_fallback(config.fallback_command)
    workdir = config.workdir.expanduser().resolve()
    if workdir.exists() and not workdir.is_dir():
        raise RecoveryError(f"--workdir exists but is not a directory: {workdir}")
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RecoveryError(f"could not create --workdir {workdir}: {exc}") from exc
    state_path = workdir / "marathon-refine-state.json"
    try:
        state = load_refine_state(state_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            f"could not safely read the preserved refine checkpoint: {exc}"
        ) from exc
    if state is not None:
        try:
            same_target = Path(state.target_folder).resolve() == target.resolve()
        except OSError:
            same_target = False
        if not same_target:
            raise RecoveryError(
                f"preserved refine checkpoint targets {state.target_folder!r}, "
                f"not {str(target)!r}; refusing to overwrite its session IDs"
            )
        if state.pending_publication is not None:
            raise RecoveryError(
                "preserved refine checkpoint already has an unpublished "
                "commit; resume that exact publication before updating base"
            )
        state.iterations_completed = 0
        state.current_iteration_idx = 0
        state.force_continuation = bool(state.project_id)
        state.preserve_local_branch = True
        state.continuation_commit_sha = local_head
    else:
        state = RefineState(
            target_folder=str(target),
            preserve_local_branch=True,
            continuation_commit_sha=local_head,
        )
    try:
        save_refine_state(state_path, state)
    except OSError as exc:
        raise RecoveryError(f"could not persist fresh refine checkpoint: {exc}") from exc
    marker = _transition_update_recovery_marker(
        marker_path,
        marker,
        UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED,
        current_local_head=local_head,
        reason=reason,
    )
    print(
        f"update-continue: {reason}; continuing refine from "
        f"{local_head[:12]}"
        + (
            f" with preserved Aristotle project {state.project_id}"
            if state.project_id
            else " with a new Aristotle project"
        ),
        flush=True,
    )
    return fallback, marker


def _run_fallback(command: Sequence[str], *, cwd: Path) -> int:
    try:
        proc = subprocess.run(list(command), cwd=str(cwd), check=False)
    except OSError as exc:
        raise RecoveryError(f"could not execute update fallback: {exc}") from exc
    if proc.returncode < 0:
        return 128 + abs(proc.returncode)
    return proc.returncode


def _run_prepared_fallback(
    config: UpdateContinueConfig,
    *,
    marker_path: Path,
    marker: dict,
    pr: PullRequestRevision,
    target: Path,
    local_head: str,
    reason: str,
    build: Optional[BuildResult],
) -> Optional[UpdateContinueResult]:
    local_head = _commit_fallback_checkpoint(
        config.repo_dir.expanduser().resolve(),
        expected_head=local_head,
        pr_number=config.pr_number,
    )
    # Do not launch a fresh paid session against a stale branch/base snapshot.
    # The eventual refine push also has a force-with-lease, but catching the
    # race here avoids spending Aristotle compute that cannot be published.
    current_pr = _read_pr_revision(config.github_repo, config.pr_number)
    _validate_live_pr(
        config,
        current_pr,
        expected_head_sha=pr.head_sha,
    )
    remote_base = _remote_branch_head(
        config.repo_dir.expanduser().resolve(), marker["base_branch"]
    )
    remote_head = _remote_branch_head(
        config.repo_dir.expanduser().resolve(), marker["branch"]
    )
    expected_remote = marker["expected_remote_head"]
    if remote_head != expected_remote:
        raise RecoveryError(
            f"remote branch {marker['branch']} moved to unexpected head "
            f"{remote_head}; expected {expected_remote}; refusing to spend an "
            "Aristotle continuation"
        )
    if remote_base != marker["base_sha"]:
        repo_dir = config.repo_dir.expanduser().resolve()
        _ensure_commit_available(repo_dir, remote_base, marker["base_branch"])
        if not _is_ancestor(repo_dir, marker["base_sha"], remote_base):
            raise RecoveryError(
                f"remote base branch {marker['base_branch']} was rewritten "
                f"from {marker['base_sha']} to {remote_base}; refusing to "
                "combine unrelated base histories"
            )
        previous_base = marker["base_sha"]
        _transition_update_recovery_marker(
            marker_path,
            marker,
            UPDATE_RECOVERY_PHASE_STARTING,
            current_local_head=local_head,
            base_sha=remote_base,
            expected_remote_head=remote_head,
            build_duration_seconds=None,
            reason=(
                f"base advanced from {previous_base} to {remote_base}; "
                "rebuilding before fallback"
            ),
        )
        print(
            f"update-continue: {marker['base_branch']} advanced to "
            f"{remote_base[:12]}; rebuilding before spending Aristotle",
            flush=True,
        )
        return None
    fallback, _marker = _prepare_fresh_update_fallback(
        config,
        marker_path=marker_path,
        marker=marker,
        target=target,
        local_head=local_head,
        reason=reason,
    )
    exit_code = _run_fallback(
        fallback,
        cwd=config.repo_dir.expanduser().resolve(),
    )
    return UpdateContinueResult(
        ok=exit_code == 0,
        exit_code=exit_code,
        fast_path=False,
        head_sha=local_head,
        branch=pr.head_branch,
        build=build,
        fallback_invoked=True,
        message=(
            f"fresh update fallback exited {exit_code} from checkpoint "
            f"{local_head}"
        ),
    )


def _commit_fallback_checkpoint(
    repo_dir: Path,
    *,
    expected_head: str,
    pr_number: int,
) -> str:
    """Ensure every fallback starts from a clean, committed local tree."""
    if _head_sha(repo_dir) != expected_head:
        raise RecoveryError(
            "local HEAD changed while preparing the update fallback"
        )
    status = _git(
        repo_dir,
        "status",
        "--porcelain",
        "--untracked-files=no",
    ).stdout.strip()
    if not status:
        return expected_head

    added = _git(repo_dir, "add", "-u", "--", check=False)
    if added.returncode != 0:
        detail = ((added.stderr or added.stdout) or "unknown error").strip()
        raise RecoveryError(
            f"could not stage update fallback checkpoint: {detail[:500]}"
        )
    committed = _git(
        repo_dir,
        "-c",
        "commit.gpgSign=false",
        "commit",
        "--no-verify",
        "-m",
        _fallback_checkpoint_subject(pr_number),
        check=False,
    )
    if committed.returncode != 0:
        detail = (
            (committed.stderr or committed.stdout) or "unknown error"
        ).strip()
        raise RecoveryError(
            f"could not commit update fallback checkpoint: {detail[:500]}"
        )
    head = _head_sha(repo_dir)
    if _git(
        repo_dir,
        "status",
        "--porcelain",
        "--untracked-files=no",
    ).stdout.strip():
        raise RecoveryError("update fallback checkpoint remained dirty after commit")
    return head


def _push_updated_head(
    repo_dir: Path,
    *,
    branch: str,
    expected_remote_head: str,
    new_head: str,
) -> None:
    if not _is_ancestor(repo_dir, expected_remote_head, new_head):
        raise RecoveryError(
            f"refusing to replace {branch}: recovery head {new_head} does not "
            f"descend from selected remote head {expected_remote_head}"
        )
    lease = f"--force-with-lease=refs/heads/{branch}:{expected_remote_head}"
    destination = f"{new_head}:refs/heads/{branch}"
    push = _git(
        repo_dir,
        "push",
        "--porcelain",
        lease,
        "origin",
        destination,
        check=False,
    )
    if push.returncode != 0:
        detail = ((push.stderr or push.stdout) or "unknown error").strip()
        raise RecoveryError(
            f"exact-lease push of {branch} failed; expected remote "
            f"{expected_remote_head}: {detail[:500]}"
        )


def _await_published_revision(
    config: UpdateContinueConfig,
    *,
    original_head: str,
    published_head: str,
) -> PullRequestRevision:
    """Wait briefly for GitHub's PR metadata to expose a successful push."""
    last_error: Optional[RecoveryError] = None
    for attempt in range(5):
        try:
            candidate = _read_pr_revision(
                config.github_repo,
                config.pr_number,
            )
            # Validate every invariant except the head first. Seeing the old
            # head immediately after a successful push is normal API lag;
            # seeing a third SHA is a real race and must fail immediately.
            _validate_live_pr(
                config,
                candidate,
                expected_head_sha=candidate.head_sha,
            )
            if candidate.head_sha == published_head:
                return candidate
            if candidate.head_sha != original_head:
                raise RecoveryError(
                    f"PR #{config.pr_number} moved to unexpected head "
                    f"{candidate.head_sha} after the exact-lease push"
                )
            last_error = RecoveryError(
                f"PR #{config.pr_number} still reports pre-push head "
                f"{original_head}"
            )
        except RecoveryError as exc:
            last_error = exc
            # A concrete third head is not eventual consistency; do not hide
            # a competing writer behind retries.
            if "unexpected head" in str(exc):
                raise
        if attempt < 4:
            time.sleep(0.25)
    raise RecoveryError(
        f"could not revalidate PR #{config.pr_number} at published head "
        f"{published_head}: {last_error}"
    )


def _merge_head(repo_dir: Path) -> Optional[str]:
    probe = _git(
        repo_dir,
        "rev-parse",
        "--verify",
        "--quiet",
        "MERGE_HEAD",
        check=False,
    )
    if probe.returncode != 0:
        return None
    values = (probe.stdout or "").splitlines()
    if len(values) != 1:
        raise RecoveryError("update-continue found an unsupported octopus merge")
    return _normalise_full_oid(values[0], label="MERGE_HEAD")


def _tracked_status(repo_dir: Path) -> str:
    return _git(
        repo_dir,
        "status",
        "--porcelain",
        "--untracked-files=no",
    ).stdout.strip()


def _validate_named_update_worktree(
    config: UpdateContinueConfig,
    marker: dict,
) -> tuple[Path, str]:
    repo_dir = config.repo_dir.expanduser().resolve()
    if not repo_dir.is_dir():
        raise RecoveryError(f"repository worktree does not exist: {repo_dir}")
    top = Path(_git(repo_dir, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != repo_dir:
        raise RecoveryError(
            f"--repo-dir must name the worktree root ({top}), not {repo_dir}"
        )
    branch = _git(
        repo_dir,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    actual_branch = (branch.stdout or "").strip()
    if branch.returncode != 0 or not actual_branch:
        raise RecoveryError("update-continue requires a named local branch")
    if actual_branch != marker["branch"]:
        raise RecoveryError(
            f"local branch {actual_branch!r} does not match recovery branch "
            f"{marker['branch']!r}"
        )
    target = _update_target_path(config)
    try:
        target.relative_to(repo_dir)
    except ValueError as exc:
        raise RecoveryError(f"target path is outside the repository: {target}") from exc
    if not target.is_dir():
        raise RecoveryError(f"target path is not a directory: {target}")
    return target, _head_sha(repo_dir)


def _is_ancestor(repo_dir: Path, ancestor: str, descendant: str) -> bool:
    return _git(
        repo_dir,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        check=False,
    ).returncode == 0


def _validate_completed_update_head(
    repo_dir: Path,
    *,
    original_head: str,
    base_sha: str,
    current_head: str,
    adopted_head: Optional[str] = None,
) -> None:
    if not _is_ancestor(repo_dir, original_head, current_head):
        raise RecoveryError(
            f"base update did not preserve original PR head {original_head} in "
            f"checkpoint {current_head}"
        )
    if not _is_ancestor(repo_dir, base_sha, current_head):
        raise RecoveryError(
            f"updated checkpoint {current_head} does not contain current PR "
            f"base {base_sha}"
        )
    if adopted_head is not None and not _is_ancestor(
        repo_dir, adopted_head, current_head
    ):
        raise RecoveryError(
            f"updated checkpoint {current_head} does not preserve adopted "
            f"local work {adopted_head}"
        )


def _conflict_checkpoint_subject(pr_number: int) -> str:
    return f"chore(marathon): checkpoint base update for PR #{pr_number}"


def _adoption_checkpoint_subject(pr_number: int) -> str:
    return f"chore(marathon): preserve local checkpoint for PR #{pr_number}"


def _adoption_conflict_subject(pr_number: int) -> str:
    return f"chore(marathon): join local checkpoint with PR #{pr_number}"


def _fallback_checkpoint_subject(pr_number: int) -> str:
    return f"chore(marathon): preserve update fallback for PR #{pr_number}"


def _completed_merge_had_conflicts(
    repo_dir: Path,
    *,
    head: str,
    pr_number: int,
) -> bool:
    subject = _git(repo_dir, "show", "-s", "--format=%s", head).stdout.strip()
    return subject == _conflict_checkpoint_subject(pr_number)


def _finish_interrupted_merge(
    repo_dir: Path,
    *,
    base_sha: str,
    pr_number: int,
    conflict_subject: Optional[str] = None,
) -> tuple[str, bool]:
    merge_head = _merge_head(repo_dir)
    if merge_head != base_sha:
        raise RecoveryError(
            f"in-progress merge targets {merge_head}, expected PR base {base_sha}"
        )
    unmerged = _git(
        repo_dir,
        "diff",
        "--name-only",
        "--diff-filter=U",
        check=False,
    ).stdout.strip()
    # Reaching this helper means a previous recovery process died with
    # MERGE_HEAD present. Even when every conflicted path was already staged,
    # ``diff --diff-filter=U`` is then empty; treating that as a clean merge
    # would skip the mandatory Aristotle conflict-repair fallback. Conservatively
    # classify every resumed MERGE_HEAD as conflicted. A genuinely clean
    # auto-merge commits inside ``git merge`` and is recovered by ancestry with
    # no MERGE_HEAD instead.
    had_conflicts = True
    if unmerged:
        added = _git(repo_dir, "add", "-u", "--", check=False)
        if added.returncode != 0:
            detail = ((added.stderr or added.stdout) or "unknown error").strip()
            raise RecoveryError(
                f"could not stage interrupted merge checkpoint: {detail[:500]}"
            )
    subject = conflict_subject or _conflict_checkpoint_subject(pr_number)
    committed = _git(
        repo_dir,
        "-c",
        "commit.gpgSign=false",
        "commit",
        "--no-verify",
        "-m",
        subject,
        check=False,
    )
    if committed.returncode != 0:
        detail = ((committed.stderr or committed.stdout) or "unknown error").strip()
        raise RecoveryError(
            f"could not complete interrupted base update: {detail[:500]}"
        )
    head = _head_sha(repo_dir)
    if _merge_head(repo_dir) is not None or _tracked_status(repo_dir):
        raise RecoveryError("interrupted base update did not finish as a clean commit")
    return head, had_conflicts


def _read_marker_pr(
    config: UpdateContinueConfig,
    marker: dict,
    *,
    allowed_heads: set[str],
) -> PullRequestRevision:
    pr = _read_pr_revision(config.github_repo, config.pr_number)
    # GitHub's pull payload can retain the base SHA from an earlier mergeability
    # calculation even after the named base ref advances.  Head identity and
    # both branch names still come from the PR; the exact base publication
    # boundary is read directly from origin by the phase finishers.
    _validate_live_pr(
        config,
        pr,
        expected_head_sha=pr.head_sha,
    )
    if pr.head_branch != marker["branch"]:
        raise RecoveryError(
            f"PR #{config.pr_number} branch changed: expected "
            f"{marker['branch']!r}, found {pr.head_branch!r}"
        )
    if pr.base_branch != marker["base_branch"]:
        raise RecoveryError(
            f"PR #{config.pr_number} base branch changed: expected "
            f"{marker['base_branch']!r}, found {pr.base_branch!r}"
        )
    if pr.head_sha not in allowed_heads:
        expected = " or ".join(sorted(allowed_heads))
        raise RecoveryError(
            f"PR #{config.pr_number} head changed: expected {expected}, "
            f"found {pr.head_sha}"
        )
    return pr


def _resume_starting_phase(
    config: UpdateContinueConfig,
    marker_path: Path,
    marker: dict,
) -> tuple[dict, PullRequestRevision, Path]:
    original = marker["original_expected_head"]
    selected_remote = marker.get("expected_remote_head", original)
    adopted = marker.get("adopted_local_head", original)
    base_sha = marker["base_sha"]
    pr = _read_marker_pr(config, marker, allowed_heads={selected_remote})
    target, local_head = _validate_named_update_worktree(config, marker)
    repo_dir = config.repo_dir.expanduser().resolve()
    _ensure_commit_available(repo_dir, original, marker["branch"])
    _ensure_commit_available(repo_dir, selected_remote, marker["branch"])
    _ensure_commit_available(repo_dir, base_sha, marker["base_branch"])
    had_conflicts = bool(marker.get("had_conflicts"))

    merge_head = _merge_head(repo_dir)
    if merge_head == selected_remote:
        # A previous worker died while joining a diverged local checkpoint to
        # the exact selected PR head. HEAD is still the pre-merge local commit;
        # finish only this expected merge and durably record the joined head
        # before attempting the base update.
        if not _is_ancestor(repo_dir, adopted, local_head):
            raise RecoveryError(
                "interrupted PR-head adoption no longer contains the selected "
                f"local checkpoint {adopted}"
            )
        local_head, adoption_conflicts = _finish_interrupted_merge(
            repo_dir,
            base_sha=selected_remote,
            pr_number=config.pr_number,
            conflict_subject=_adoption_conflict_subject(config.pr_number),
        )
        had_conflicts = had_conflicts or adoption_conflicts
        marker = _replace_update_recovery_marker_fields(
            marker_path,
            marker,
            current_local_head=local_head,
            had_conflicts=had_conflicts,
        )
        merge_head = None
    elif merge_head not in (None, base_sha):
        raise RecoveryError(
            f"in-progress merge targets {merge_head}, expected selected PR "
            f"head {selected_remote} or current base {base_sha}"
        )

    if merge_head is None:
        dirty = _tracked_status(repo_dir)
        if dirty:
            raise RecoveryError(
                "tracked worktree changed after update recovery started: "
                + "; ".join(dirty.splitlines()[:5])
            )

        contains_adopted = _is_ancestor(repo_dir, adopted, local_head)
        contains_selected = _is_ancestor(repo_dir, selected_remote, local_head)
        if not contains_adopted:
            raise RecoveryError(
                f"local HEAD {local_head} no longer contains the adopted "
                f"checkpoint {adopted}"
            )
        if not contains_selected:
            local_head, adoption_conflicts = _merge_base_checkpoint(
                repo_dir,
                base_sha=selected_remote,
                pr_number=config.pr_number,
                conflict_subject=_adoption_conflict_subject(config.pr_number),
                ancestor_label="selected PR head",
            )
            had_conflicts = had_conflicts or adoption_conflicts
            marker = _replace_update_recovery_marker_fields(
                marker_path,
                marker,
                current_local_head=local_head,
                had_conflicts=had_conflicts,
            )
        elif local_head != marker["current_local_head"]:
            # Crash after the PR-head join committed but before the marker
            # replacement, or an additional clean checkpoint committed in the
            # selected dead worktree. Preserve it and bind it to this operation.
            adoption_conflicts = (
                _git(repo_dir, "show", "-s", "--format=%s", local_head)
                .stdout.strip()
                == _adoption_conflict_subject(config.pr_number)
            )
            had_conflicts = had_conflicts or adoption_conflicts
            marker = _replace_update_recovery_marker_fields(
                marker_path,
                marker,
                current_local_head=local_head,
                had_conflicts=had_conflicts,
            )

    # If MERGE_HEAD names the base, adoption was already complete when the
    # prior worker began that merge. Validate both histories before finishing.
    if not _is_ancestor(repo_dir, adopted, local_head) or not _is_ancestor(
        repo_dir, selected_remote, local_head
    ):
        raise RecoveryError(
            "base update would discard the selected PR head or adopted local "
            "checkpoint"
        )

    if merge_head == base_sha:
        updated_head, base_conflicts = _finish_interrupted_merge(
            repo_dir,
            base_sha=base_sha,
            pr_number=config.pr_number,
        )
        had_conflicts = had_conflicts or base_conflicts
    elif _is_ancestor(repo_dir, base_sha, local_head):
        # This also recognizes a worker killed after a successful base merge
        # but before the phase transition.
        updated_head = local_head
        had_conflicts = had_conflicts or _completed_merge_had_conflicts(
            repo_dir,
            head=updated_head,
            pr_number=config.pr_number,
        )
    else:
        updated_head, base_conflicts = _merge_base_checkpoint(
            repo_dir,
            base_sha=base_sha,
            pr_number=config.pr_number,
        )
        had_conflicts = had_conflicts or base_conflicts

    _validate_completed_update_head(
        repo_dir,
        original_head=original,
        base_sha=base_sha,
        current_head=updated_head,
        adopted_head=adopted,
    )
    marker = _transition_update_recovery_marker(
        marker_path,
        marker,
        UPDATE_RECOVERY_PHASE_MERGED,
        current_local_head=updated_head,
        had_conflicts=had_conflicts,
    )
    return marker, pr, target


def _validate_resumed_local_head(
    config: UpdateContinueConfig,
    marker: dict,
) -> Path:
    target, local_head = _validate_named_update_worktree(config, marker)
    if local_head != marker["current_local_head"]:
        raise RecoveryError(
            f"local HEAD changed after recovery phase {marker['phase']}: "
            f"expected {marker['current_local_head']}, found {local_head}"
        )
    if _merge_head(config.repo_dir.expanduser().resolve()) is not None:
        raise RecoveryError(
            f"unexpected in-progress merge during recovery phase {marker['phase']}"
        )
    dirty = _tracked_status(config.repo_dir.expanduser().resolve())
    if dirty:
        raise RecoveryError(
            f"tracked worktree changed during recovery phase {marker['phase']}: "
            + "; ".join(dirty.splitlines()[:5])
        )
    _validate_completed_update_head(
        config.repo_dir.expanduser().resolve(),
        original_head=marker["original_expected_head"],
        base_sha=marker["base_sha"],
        current_head=local_head,
        adopted_head=marker.get("adopted_local_head"),
    )
    return target


def _resume_merged_phase(
    config: UpdateContinueConfig,
    marker_path: Path,
    marker: dict,
) -> tuple[dict, PullRequestRevision, Path]:
    """Resume a build/fallback from a durable merged checkpoint.

    A red build can leave tracked diagnostic/source changes which Marathon
    commits before preparing the fresh RefineState. If the process dies between
    that commit and the ``fallback_prepared`` marker update, recognise only that
    exact internal checkpoint subject and advance ``current_local_head`` without
    repeating or discarding the commit.
    """
    target, local_head = _validate_named_update_worktree(config, marker)
    repo_dir = config.repo_dir.expanduser().resolve()
    if _merge_head(repo_dir) is not None:
        raise RecoveryError("unexpected in-progress merge during merged recovery")
    dirty = _tracked_status(repo_dir)
    if dirty:
        raise RecoveryError(
            "tracked worktree changed during merged recovery: "
            + "; ".join(dirty.splitlines()[:5])
        )
    if local_head != marker["current_local_head"]:
        prior_head = marker["current_local_head"]
        subject = _git(
            repo_dir, "show", "-s", "--format=%s", local_head
        ).stdout.strip()
        if (
            subject != _fallback_checkpoint_subject(config.pr_number)
            or not _is_ancestor(repo_dir, prior_head, local_head)
        ):
            raise RecoveryError(
                f"local HEAD changed after merged recovery: expected "
                f"{prior_head}, found {local_head}"
            )
        _validate_completed_update_head(
            repo_dir,
            original_head=marker["original_expected_head"],
            base_sha=marker["base_sha"],
            current_head=local_head,
            adopted_head=marker.get("adopted_local_head"),
        )
        marker = _replace_update_recovery_marker_fields(
            marker_path,
            marker,
            current_local_head=local_head,
        )
    else:
        _validate_completed_update_head(
            repo_dir,
            original_head=marker["original_expected_head"],
            base_sha=marker["base_sha"],
            current_head=local_head,
            adopted_head=marker.get("adopted_local_head"),
        )
    pr = _read_marker_pr(
        config,
        marker,
        allowed_heads={marker["expected_remote_head"]},
    )
    return marker, pr, target


def _remote_branch_head(repo_dir: Path, branch: str) -> str:
    ref = f"refs/heads/{branch}"
    remote = _git(
        repo_dir,
        "ls-remote",
        "--heads",
        "origin",
        ref,
        check=False,
    )
    if remote.returncode != 0:
        detail = ((remote.stderr or remote.stdout) or "unknown error").strip()
        raise RecoveryError(f"could not read remote branch {branch}: {detail[:500]}")
    rows = [line.split() for line in (remote.stdout or "").splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref:
        raise RecoveryError(
            f"remote branch {branch!r} is missing or ambiguous; refusing recovery push"
        )
    return _normalise_full_oid(rows[0][0], label="remote branch head")


def _finish_pushing_phase(
    config: UpdateContinueConfig,
    marker_path: Path,
    marker: dict,
    *,
    build: Optional[BuildResult],
) -> Optional[UpdateContinueResult]:
    _validate_resumed_local_head(config, marker)
    lease_head = marker["expected_remote_head"]
    published_head = marker["current_local_head"]
    _read_marker_pr(
        config,
        marker,
        allowed_heads={lease_head, published_head},
    )
    remote_head = _remote_branch_head(
        config.repo_dir.expanduser().resolve(), marker["branch"]
    )
    remote_base = _remote_branch_head(
        config.repo_dir.expanduser().resolve(), marker["base_branch"]
    )
    if remote_head not in {lease_head, published_head}:
        raise RecoveryError(
            f"remote branch {marker['branch']} moved to unexpected head "
            f"{remote_head}; expected {lease_head} or {published_head}"
        )
    if remote_base != marker["base_sha"]:
        repo_dir = config.repo_dir.expanduser().resolve()
        _ensure_commit_available(repo_dir, remote_base, marker["base_branch"])
        if not _is_ancestor(repo_dir, marker["base_sha"], remote_base):
            raise RecoveryError(
                f"remote base branch {marker['base_branch']} was rewritten "
                f"from {marker['base_sha']} to {remote_base}; refusing to "
                "combine unrelated base histories"
            )
        previous_base = marker["base_sha"]
        marker = _transition_update_recovery_marker(
            marker_path,
            marker,
            UPDATE_RECOVERY_PHASE_STARTING,
            base_sha=remote_base,
            expected_remote_head=remote_head,
            build_duration_seconds=None,
            reason=(
                f"base advanced from {previous_base} to {remote_base}; "
                "rebuilding before publication"
            ),
        )
        print(
            f"update-continue: {marker['base_branch']} advanced to "
            f"{remote_base[:12]}; merging and rebuilding before publication",
            flush=True,
        )
        return None
    if remote_head == lease_head:
        _push_updated_head(
            config.repo_dir.expanduser().resolve(),
            branch=marker["branch"],
            expected_remote_head=lease_head,
            new_head=published_head,
        )

    published = _await_published_revision(
        config,
        original_head=lease_head,
        published_head=published_head,
    )
    # GitHub acknowledging the pushed PR head is not an atomic snapshot with
    # the two remote refs. Re-read both after that acknowledgement so a base
    # commit landing during the push/API wait cannot receive a stale success
    # status or terminal journal marker.
    confirmed_head = _remote_branch_head(
        config.repo_dir.expanduser().resolve(), marker["branch"]
    )
    if confirmed_head != published_head:
        raise RecoveryError(
            f"remote branch {marker['branch']} moved to unexpected head "
            f"{confirmed_head} after publication; expected {published_head}"
        )
    confirmed_base = _remote_branch_head(
        config.repo_dir.expanduser().resolve(), marker["base_branch"]
    )
    if confirmed_base != marker["base_sha"]:
        repo_dir = config.repo_dir.expanduser().resolve()
        _ensure_commit_available(repo_dir, confirmed_base, marker["base_branch"])
        if not _is_ancestor(repo_dir, marker["base_sha"], confirmed_base):
            raise RecoveryError(
                f"remote base branch {marker['base_branch']} was rewritten "
                f"from {marker['base_sha']} to {confirmed_base}; refusing to "
                "combine unrelated base histories"
            )
        previous_base = marker["base_sha"]
        marker = _transition_update_recovery_marker(
            marker_path,
            marker,
            UPDATE_RECOVERY_PHASE_STARTING,
            base_sha=confirmed_base,
            expected_remote_head=published_head,
            build_duration_seconds=None,
            reason=(
                f"base advanced from {previous_base} to {confirmed_base} "
                "during publication; rebuilding before success"
            ),
        )
        print(
            f"update-continue: {marker['base_branch']} advanced to "
            f"{confirmed_base[:12]} during publication; merging and "
            "rebuilding before success",
            flush=True,
        )
        return None
    duration_seconds = marker.get("build_duration_seconds")
    duration = (
        f" in {float(duration_seconds):.1f}s"
        if isinstance(duration_seconds, (int, float))
        else ""
    )
    _post_build_status(
        config,
        sha=published_head,
        state="success",
        description=(
            f"lake build passed{duration} after updating from "
            f"{marker['base_branch']}"
        ),
        target_url=published.url,
    )
    marker = _transition_update_recovery_marker(
        marker_path,
        marker,
        UPDATE_RECOVERY_PHASE_PUBLISHED,
    )
    return UpdateContinueResult(
        ok=True,
        exit_code=0,
        fast_path=True,
        head_sha=published_head,
        branch=marker["branch"],
        build=build,
        fallback_invoked=False,
        message=(
            f"updated PR #{config.pr_number} from {marker['base_branch']}, "
            f"built, and published {published_head} without Aristotle"
        ),
    )


def _update_continue_once(
    config: UpdateContinueConfig,
) -> Optional[UpdateContinueResult]:
    """Crash-safe base update of an existing PR, continuing only when needed."""
    _normalise_full_oid(config.expected_head_sha, label="expected head SHA")
    if config.pr_number <= 0:
        raise RecoveryError("PR number must be positive")
    if config.build_timeout <= 0:
        raise RecoveryError("build timeout must be positive")
    if "/" not in config.github_repo:
        raise RecoveryError("GitHub repository must be in owner/name form")
    _normalise_fallback(config.fallback_command)
    _normalise_operation_id(config.operation_id)

    repo_dir = config.repo_dir.expanduser().resolve()
    workdir = config.workdir.expanduser().resolve()
    marker_path = workdir / UPDATE_RECOVERY_MARKER_FILENAME
    if marker_path.exists():
        marker = _load_update_recovery_marker(marker_path)
        _validate_update_marker_identity(config, marker)
    else:
        state_path = workdir / "marathon-refine-state.json"
        try:
            existing_state = load_refine_state(state_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RecoveryError(
                f"could not safely read the preserved refine checkpoint: {exc}"
            ) from exc
        if (
            existing_state is not None
            and existing_state.pending_publication is not None
        ):
            raise RecoveryError(
                "preserved refine checkpoint already has an unpublished "
                "commit; resume that exact publication before updating base"
            )
        pr = _read_pr_revision(config.github_repo, config.pr_number)
        _validate_live_pr(config, pr)
        _ensure_commit_available(repo_dir, pr.head_sha, pr.head_branch)
        target, adopted_local_head = _validate_update_checkout(config, pr)
        # Bind the first durable marker to the branch refs themselves. In
        # particular, do not seed it from PullRequestRevision.base_sha: GitHub
        # may keep that value frozen while origin/main has already advanced.
        remote_head = _remote_branch_head(repo_dir, pr.head_branch)
        if remote_head != pr.head_sha:
            raise RecoveryError(
                f"remote branch {pr.head_branch} moved to {remote_head}; "
                f"expected exact PR head {pr.head_sha}"
            )
        remote_base = _remote_branch_head(repo_dir, pr.base_branch)
        _ensure_commit_available(repo_dir, remote_base, pr.base_branch)
        if workdir.exists() and not workdir.is_dir():
            raise RecoveryError(f"--workdir exists but is not a directory: {workdir}")
        try:
            workdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RecoveryError(f"could not create --workdir {workdir}: {exc}") from exc
        marker = _new_update_recovery_marker(
            config,
            pr,
            target,
            adopted_local_head,
            base_sha=remote_base,
        )
        _atomic_create_update_recovery_marker(marker_path, marker)

    phase = marker["phase"]
    if phase == UPDATE_RECOVERY_PHASE_PUBLISHED:
        _validate_resumed_local_head(config, marker)
        return UpdateContinueResult(
            ok=True,
            exit_code=0,
            fast_path=True,
            head_sha=marker["current_local_head"],
            branch=marker["branch"],
            build=None,
            fallback_invoked=False,
            message=(
                f"update recovery for PR #{config.pr_number} was already "
                f"published at {marker['current_local_head']}"
            ),
        )
    if phase == UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED:
        _validate_resumed_local_head(config, marker)
        raise RecoveryError(
            f"update fallback is already prepared at {marker_path}; invoke the "
            "plain refine command so its fresh checkpoint can resume"
        )
    if phase == UPDATE_RECOVERY_PHASE_PUSHING:
        return _finish_pushing_phase(
            config,
            marker_path,
            marker,
            build=None,
        )
    if phase == UPDATE_RECOVERY_PHASE_STARTING:
        marker, pr, target = _resume_starting_phase(
            config,
            marker_path,
            marker,
        )
    elif phase == UPDATE_RECOVERY_PHASE_MERGED:
        marker, pr, target = _resume_merged_phase(
            config,
            marker_path,
            marker,
        )
    else:  # pragma: no cover - marker loader restricts the phase vocabulary
        raise AssertionError(f"unhandled update recovery phase {phase}")

    updated_head = marker["current_local_head"]
    if marker.get("had_conflicts"):
        return _run_prepared_fallback(
            config,
            marker_path=marker_path,
            marker=marker,
            pr=pr,
            target=target,
            local_head=updated_head,
            reason="base update produced merge conflicts",
            build=None,
        )

    targets = plan_retry_build_targets(
        repo_dir,
        base_sha=marker["base_sha"],
        head_sha=updated_head,
        target_path=target,
    )
    if targets:
        try:
            build = run_lake_build(
                repo_dir,
                config.build_timeout,
                targets=targets,
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise RecoveryError(f"lake build runner crashed: {exc}") from exc
    else:
        build = BuildResult(
            skipped_reason="no explicit Lean target derived from PR or chapter"
        )

    if build.ok is not True or build.timed_out or build.skipped_reason:
        return _run_prepared_fallback(
            config,
            marker_path=marker_path,
            marker=marker,
            pr=pr,
            target=target,
            local_head=updated_head,
            reason=_failure_description(build, config.build_timeout),
            build=build,
        )

    # The build may create ignored artifacts, but it must not rewrite tracked
    # source underneath the SHA we are about to attest and publish.
    dirty = _git(
        repo_dir,
        "status",
        "--porcelain",
        "--untracked-files=no",
    ).stdout.strip()
    if dirty or _head_sha(repo_dir) != updated_head:
        raise RecoveryError(
            "worktree changed during the green build; refusing to publish a "
            "SHA-bound result"
        )

    _read_marker_pr(
        config,
        marker,
        allowed_heads={marker["expected_remote_head"]},
    )
    marker = _transition_update_recovery_marker(
        marker_path,
        marker,
        UPDATE_RECOVERY_PHASE_PUSHING,
        build_duration_seconds=build.duration_seconds,
    )
    return _finish_pushing_phase(
        config,
        marker_path,
        marker,
        build=build,
    )


def update_continue(config: UpdateContinueConfig) -> UpdateContinueResult:
    """Crash-safe base update that follows a fast-forwarding live base.

    A long build can overlap commits landing on the PR's base branch.  In that
    case the durable marker rolls forward, merges the new exact base, and
    rebuilds before publication.  The bounded loop prevents a continuously
    moving base from creating unbounded in-process work; its latest marker
    remains safe to resume on the next conductor attempt.
    """
    for _attempt in range(MAX_UPDATE_BASE_ROLLFORWARDS + 1):
        result = _update_continue_once(config)
        if result is not None:
            return result
    raise RecoveryError(
        f"remote base branch kept advancing during "
        f"{MAX_UPDATE_BASE_ROLLFORWARDS} consecutive rebuilds; retry the "
        "preserved recovery checkpoint"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m marathon.recovery",
        description="Run non-destructive Marathon recovery actions.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    retry = sub.add_parser(
        "retry-build",
        help="re-run lake build for an unchanged PR head (no Aristotle)",
    )
    retry.add_argument("--repo-dir", type=Path, required=True)
    retry.add_argument("--repo", dest="github_repo", required=True)
    retry.add_argument("--pr", dest="pr_number", type=int, required=True)
    retry.add_argument("--expected-head", dest="expected_head_sha", required=True)
    retry.add_argument("--expected-branch")
    retry.add_argument("--expected-base")
    retry.add_argument(
        "--target",
        dest="target_path",
        type=Path,
        required=True,
        help="repo-relative chapter/target directory",
    )
    retry.add_argument("--build-timeout", type=int, default=600)

    update = sub.add_parser(
        "update-continue",
        help=(
            "merge the current PR base, publish if green, otherwise run a "
            "fresh fallback refine"
        ),
    )
    update.add_argument("--repo-dir", type=Path, required=True)
    update.add_argument("--repo", dest="github_repo", required=True)
    update.add_argument("--pr", dest="pr_number", type=int, required=True)
    update.add_argument(
        "--expected-head",
        dest="expected_head_sha",
        required=True,
    )
    update.add_argument("--expected-branch")
    update.add_argument("--expected-base")
    update.add_argument(
        "--target",
        dest="target_path",
        type=Path,
        required=True,
        help="repo-relative chapter/target directory",
    )
    update.add_argument("--workdir", type=Path, required=True)
    update.add_argument(
        "--operation-id",
        help="optional durable conductor recovery-operation identity",
    )
    update.add_argument("--build-timeout", type=int, default=600)
    update.add_argument(
        "fallback_command",
        nargs=argparse.REMAINDER,
        help="fallback command and arguments, preceded by --",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "retry-build":
        config: RetryBuildConfig | UpdateContinueConfig = RetryBuildConfig(
            repo_dir=args.repo_dir,
            github_repo=args.github_repo,
            pr_number=args.pr_number,
            expected_head_sha=args.expected_head_sha,
            expected_branch=args.expected_branch,
            expected_base=args.expected_base,
            target_path=args.target_path,
            build_timeout=args.build_timeout,
        )
    elif args.action == "update-continue":
        config = UpdateContinueConfig(
            repo_dir=args.repo_dir,
            github_repo=args.github_repo,
            pr_number=args.pr_number,
            expected_head_sha=args.expected_head_sha,
            expected_branch=args.expected_branch,
            expected_base=args.expected_base,
            target_path=args.target_path,
            workdir=args.workdir,
            fallback_command=tuple(args.fallback_command),
            build_timeout=args.build_timeout,
            operation_id=args.operation_id,
        )
    else:  # pragma: no cover - argparse enforces this
        raise AssertionError(f"unhandled recovery action {args.action}")
    try:
        if isinstance(config, RetryBuildConfig):
            result: RetryBuildResult | UpdateContinueResult = retry_build(config)
        else:
            result = update_continue(config)
    except RecoveryError as exc:
        print(f"recovery {args.action} aborted: {exc}", file=sys.stderr)
        return EXIT_RECOVERY_ABORTED
    stream = sys.stdout if result.ok else sys.stderr
    print(result.message, file=stream)
    if result.build and result.build.log_tail:
        print(result.build.log_tail, file=stream)
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())

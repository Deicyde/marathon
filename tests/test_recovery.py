"""Tests for the non-Aristotle recovery worker."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import marathon.recovery as recovery_mod
from marathon.post_pipeline import BuildResult
from marathon.recovery import (
    EXIT_RECOVERY_ABORTED,
    RecoveryError,
    RetryBuildConfig,
    UPDATE_RECOVERY_MARKER_FILENAME,
    UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED,
    UPDATE_RECOVERY_PHASE_MERGED,
    UPDATE_RECOVERY_PHASE_PUBLISHED,
    UPDATE_RECOVERY_PHASE_PUSHING,
    UPDATE_RECOVERY_PHASE_STARTING,
    UpdateContinueConfig,
    retry_build,
    update_continue,
)
from marathon.review.automerge import TRUSTED_BUILD_CONTEXT
from marathon.state import RefineState, load_refine_state, save_refine_state


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "lakefile.toml").write_text(
        'name = "Fixture"\nversion = "0.1.0"\n\n'
        '[[lean_lib]]\nname = "Demo"\n'
    )
    chapter = repo / "Demo" / "Chapter15"
    chapter.mkdir(parents=True)
    lean = chapter / "A.lean"
    lean.write_text("theorem old : True := by trivial\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    branch = "marathon/refine-c15-i171"
    _git(repo, "checkout", "-b", branch)
    lean.write_text("theorem old : True := by trivial\ntheorem new : True := by trivial\n")
    _git(repo, "add", str(lean.relative_to(repo)))
    _git(repo, "commit", "-m", "refine")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head, branch


def _pr_payload(
    *,
    base: str,
    head: str,
    branch: str,
    number: int = 277,
) -> dict:
    return {
        "number": number,
        "state": "open",
        "html_url": f"https://github.test/o/r/pull/{number}",
        "head": {"ref": branch, "sha": head},
        "base": {"ref": "main", "sha": base},
    }


class _FakeGitHub:
    def __init__(self, pr_reads: list[dict | Callable[[], dict]]):
        self.pr_reads = list(pr_reads)
        self.statuses: list[dict[str, str]] = []

    def __call__(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if args[:1] == ("api",) and len(args) == 2:
            assert self.pr_reads, "unexpected extra PR read"
            payload_or_factory = self.pr_reads.pop(0)
            payload = (
                payload_or_factory()
                if callable(payload_or_factory)
                else payload_or_factory
            )
            return subprocess.CompletedProcess(
                ["gh", *args], 0, stdout=json.dumps(payload), stderr=""
            )
        assert args[:3] == ("api", "--method", "POST")
        fields: dict[str, str] = {"endpoint": args[3]}
        for index, arg in enumerate(args):
            if arg == "-f":
                key, value = args[index + 1].split("=", 1)
                fields[key] = value
        self.statuses.append(fields)
        return subprocess.CompletedProcess(
            ["gh", *args], 0, stdout="{}", stderr=""
        )


def _config(repo: Path, head: str, branch: str) -> RetryBuildConfig:
    return RetryBuildConfig(
        repo_dir=repo,
        github_repo="o/r",
        pr_number=277,
        expected_head_sha=head,
        expected_branch=branch,
        expected_base="main",
        target_path=Path("Demo/Chapter15"),
        build_timeout=37,
    )


def _update_config(
    repo: Path,
    head: str,
    branch: str,
    workdir: Path,
    fallback: tuple[str, ...] = ("fallback-refine",),
    operation_id: str | None = None,
) -> UpdateContinueConfig:
    return UpdateContinueConfig(
        repo_dir=repo,
        github_repo="o/r",
        pr_number=277,
        expected_head_sha=head,
        expected_branch=branch,
        expected_base="main",
        target_path=Path("Demo/Chapter15"),
        workdir=workdir,
        fallback_command=fallback,
        build_timeout=37,
        operation_id=operation_id,
    )


def _advance_main_cleanly(repo: Path, branch: str) -> str:
    _git(repo, "checkout", "main")
    (repo / "Demo" / "Chapter15" / "FromMain.lean").write_text(
        "theorem fromMain : True := by trivial\n"
    )
    _git(repo, "add", "Demo/Chapter15/FromMain.lean")
    _git(repo, "commit", "-m", "advance main")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", branch)
    return base


def _install_origin(repo: Path, tmp_path: Path, branch: str) -> Path:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "origin", "main", branch)
    return bare


def _advance_remote_main_cleanly(bare: Path, tmp_path: Path) -> str:
    upstream = tmp_path / "upstream"
    cloned = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--branch",
            "main",
            str(bare),
            str(upstream),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cloned.returncode == 0, cloned.stderr
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test User")
    advanced = upstream / "Demo" / "Chapter15" / "RemoteAdvance.lean"
    advanced.write_text("theorem remoteAdvance : True := by trivial\n")
    _git(upstream, "add", "Demo/Chapter15/RemoteAdvance.lean")
    _git(upstream, "commit", "-m", "advance remote main during recovery")
    base = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "push", "origin", "main")
    return base


def _advance_remote_pr_branch(
    bare: Path,
    tmp_path: Path,
    branch: str,
) -> str:
    intruder = tmp_path / "intruder"
    cloned = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--branch",
            branch,
            str(bare),
            str(intruder),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cloned.returncode == 0, cloned.stderr
    _git(intruder, "config", "user.email", "test@example.com")
    _git(intruder, "config", "user.name", "Test User")
    changed = intruder / "Demo" / "Chapter15" / "CompetingWriter.lean"
    changed.write_text("theorem competingWriter : True := by trivial\n")
    _git(intruder, "add", "Demo/Chapter15/CompetingWriter.lean")
    _git(intruder, "commit", "-m", "competing PR branch writer")
    head = _git(intruder, "rev-parse", "HEAD")
    _git(intruder, "push", "origin", branch)
    return head


def test_retry_build_attests_exact_sha_and_reuses_target_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head, branch = _fixture_repo(tmp_path)
    github = _FakeGitHub(
        [
            _pr_payload(base=base, head=head, branch=branch),
            _pr_payload(base=base, head=head, branch=branch),
        ]
    )
    built: list[tuple[Path, int, list[str] | None]] = []

    def fake_build(
        repo_dir: Path, timeout: int, targets: list[str] | None = None
    ) -> BuildResult:
        built.append((repo_dir, timeout, targets))
        return BuildResult(ok=True, duration_seconds=1.25)

    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr("marathon.recovery.run_lake_build", fake_build)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)

    result = retry_build(_config(repo, head, branch))

    assert result.ok is True
    assert result.exit_code == 0
    assert result.targets == ("Demo.Chapter15.A",)
    assert built == [(repo, 37, ["Demo.Chapter15.A"])]
    assert [status["state"] for status in github.statuses] == [
        "pending",
        "success",
    ]
    assert {status["context"] for status in github.statuses} == {
        TRUSTED_BUILD_CONTEXT
    }
    assert {status["endpoint"] for status in github.statuses} == {
        f"repos/o/r/statuses/{head}"
    }


def test_retry_build_allows_detached_clean_checkout_at_exact_pr_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head, branch = _fixture_repo(tmp_path)
    _git(repo, "checkout", "--detach", head)
    github = _FakeGitHub(
        [
            _pr_payload(base=base, head=head, branch=branch),
            _pr_payload(base=base, head=head, branch=branch),
        ]
    )
    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr(
        "marathon.recovery.run_lake_build",
        lambda *args, **kwargs: BuildResult(ok=True),
    )

    result = retry_build(_config(repo, head, branch))

    assert result.ok is True
    assert result.branch == branch


def test_retry_build_falls_back_to_chapter_targets_when_diff_has_no_lean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, _old_head, _old_branch = _fixture_repo(tmp_path)
    branch = "marathon/refine-c15-i240"
    _git(repo, "checkout", "-b", branch, base)
    (repo / "README.md").write_text("metadata-only PR\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "metadata")
    head = _git(repo, "rev-parse", "HEAD")
    github = _FakeGitHub(
        [
            _pr_payload(base=base, head=head, branch=branch),
            _pr_payload(base=base, head=head, branch=branch),
        ]
    )
    built: list[list[str] | None] = []
    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr(
        "marathon.recovery.run_lake_build",
        lambda _repo, _timeout, targets=None: (
            built.append(targets) or BuildResult(ok=True)
        ),
    )

    result = retry_build(_config(repo, head, branch))

    assert result.targets == ("Demo.Chapter15.A",)
    assert built == [["Demo.Chapter15.A"]]


def test_retry_build_discards_result_if_remote_head_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head, branch = _fixture_repo(tmp_path)
    github = _FakeGitHub(
        [
            _pr_payload(base=base, head=head, branch=branch),
            _pr_payload(base=base, head="b" * 40, branch=branch),
        ]
    )
    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr(
        "marathon.recovery.run_lake_build",
        lambda *args, **kwargs: BuildResult(ok=True, duration_seconds=1),
    )

    result = retry_build(_config(repo, head, branch))

    assert result.ok is False
    assert result.exit_code == EXIT_RECOVERY_ABORTED
    assert "head changed" in result.message
    assert [status["state"] for status in github.statuses] == [
        "pending",
        "failure",
    ]
    assert {status["endpoint"] for status in github.statuses} == {
        f"repos/o/r/statuses/{head}"
    }


def test_retry_build_discards_result_if_base_moves_during_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head, branch = _fixture_repo(tmp_path)
    github = _FakeGitHub(
        [
            _pr_payload(base=base, head=head, branch=branch),
            _pr_payload(base="b" * 40, head=head, branch=branch),
        ]
    )
    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr(
        "marathon.recovery.run_lake_build",
        lambda *args, **kwargs: BuildResult(ok=True, duration_seconds=1),
    )

    result = retry_build(_config(repo, head, branch))

    assert result.ok is False
    assert result.exit_code == EXIT_RECOVERY_ABORTED
    assert "base moved" in result.message
    assert [status["state"] for status in github.statuses] == [
        "pending",
        "failure",
    ]


def test_retry_build_refuses_mismatched_local_head_before_status_or_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head, branch = _fixture_repo(tmp_path)
    previous = _git(repo, "rev-parse", "HEAD~1")
    github = _FakeGitHub([_pr_payload(base=base, head=previous, branch=branch)])
    called = False

    def fake_build(*args: object, **kwargs: object) -> BuildResult:
        nonlocal called
        called = True
        return BuildResult(ok=True)

    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr("marathon.recovery.run_lake_build", fake_build)

    with pytest.raises(RecoveryError, match="local HEAD"):
        retry_build(_config(repo, previous, branch))

    assert head != previous
    assert called is False
    assert github.statuses == []


def test_retry_build_refuses_tracked_dirt_because_status_is_sha_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head, branch = _fixture_repo(tmp_path)
    (repo / "Demo" / "Chapter15" / "A.lean").write_text("unpublished\n")
    github = _FakeGitHub([_pr_payload(base=base, head=head, branch=branch)])
    monkeypatch.setattr("marathon.recovery.gh", github)

    with pytest.raises(RecoveryError, match="SHA-bound build dishonest"):
        retry_build(_config(repo, head, branch))

    assert github.statuses == []


def test_retry_build_posts_failure_for_red_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head, branch = _fixture_repo(tmp_path)
    github = _FakeGitHub(
        [
            _pr_payload(base=base, head=head, branch=branch),
            _pr_payload(base=base, head=head, branch=branch),
        ]
    )
    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr(
        "marathon.recovery.run_lake_build",
        lambda *args, **kwargs: BuildResult(ok=False, log_tail="type mismatch"),
    )

    result = retry_build(_config(repo, head, branch))

    assert result.ok is False
    assert result.exit_code == 1
    assert [status["state"] for status in github.statuses] == [
        "pending",
        "failure",
    ]
    assert github.statuses[-1]["description"] == "lake build failed"


def test_update_continue_green_fast_path_merges_builds_and_pushes_exact_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, stale_rest_base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    bare = _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"

    github = _FakeGitHub(
        [
            _pr_payload(base=stale_rest_base, head=original_head, branch=branch),
            _pr_payload(base=stale_rest_base, head=original_head, branch=branch),
            _pr_payload(base=stale_rest_base, head=original_head, branch=branch),
            _pr_payload(base=stale_rest_base, head=original_head, branch=branch),
            # GitHub can briefly expose the old head after a successful push.
            _pr_payload(base=stale_rest_base, head=original_head, branch=branch),
            lambda: _pr_payload(
                base=stale_rest_base,
                head=_git(repo, "rev-parse", "HEAD"),
                branch=branch,
            ),
        ]
    )
    built: list[list[str] | None] = []
    sleeps: list[float] = []
    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr(
        "marathon.recovery.time.sleep",
        lambda delay: sleeps.append(delay),
    )
    monkeypatch.setattr(
        "marathon.recovery.run_lake_build",
        lambda _repo, _timeout, targets=None: (
            built.append(targets)
            or BuildResult(ok=True, duration_seconds=2.5)
        ),
    )
    monkeypatch.setattr(
        "marathon.recovery._run_fallback",
        lambda *args, **kwargs: pytest.fail("green update must not call fallback"),
    )
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)

    result = update_continue(
        _update_config(repo, original_head, branch, workdir)
    )

    updated_head = _git(repo, "rev-parse", "HEAD")
    assert result.ok is True
    assert result.fast_path is True
    assert result.fallback_invoked is False
    assert result.head_sha == updated_head
    assert updated_head != original_head
    assert built == [["Demo.Chapter15.A"]]
    assert _git(
        repo, "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"
    ) == updated_head
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", original_head, updated_head],
        cwd=repo,
        check=False,
    ).returncode == 0
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", current_base, updated_head],
        cwd=repo,
        check=False,
    ).returncode == 0
    assert [status["state"] for status in github.statuses] == ["success"]
    assert github.statuses[0]["endpoint"] == f"repos/o/r/statuses/{updated_head}"
    assert github.statuses[0]["context"] == TRUSTED_BUILD_CONTEXT
    marker = json.loads((workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text())
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_PUBLISHED
    assert marker["base_sha"] == current_base
    assert marker["current_local_head"] == updated_head
    assert sleeps == [0.25]


def test_update_continue_rolls_forward_when_base_moves_during_green_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    initial_base = _advance_main_cleanly(repo, branch)
    bare = _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"
    moved: dict[str, str] = {}

    def live_pr() -> dict:
        return _pr_payload(
            # GitHub's pull payload may retain an old base SHA. The branch ref
            # itself is the publication boundary.
            base=initial_base,
            head=_git(
                bare,
                "rev-parse",
                f"refs/heads/{branch}",
            ),
            branch=branch,
        )

    github = _FakeGitHub([live_pr for _ in range(16)])
    builds: list[list[str] | None] = []

    def build(
        _repo: Path, _timeout: int, targets: list[str] | None = None
    ) -> BuildResult:
        builds.append(targets)
        if len(builds) == 1:
            moved["base"] = _advance_remote_main_cleanly(bare, tmp_path)
        return BuildResult(ok=True, duration_seconds=float(len(builds)))

    monkeypatch.setattr(recovery_mod, "gh", github)
    monkeypatch.setattr(recovery_mod, "run_lake_build", build)
    monkeypatch.setattr(
        recovery_mod,
        "_run_fallback",
        lambda *a, **k: pytest.fail("a moving base must rebuild, not use Aristotle"),
    )

    result = update_continue(
        _update_config(repo, original_head, branch, workdir)
    )

    marker = json.loads((workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text())
    assert result.ok is True
    assert result.fast_path is True
    assert builds == [["Demo.Chapter15.A"], ["Demo.Chapter15.A"]]
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_PUBLISHED
    assert marker["base_sha"] == moved["base"]
    assert marker["build_duration_seconds"] == 2.0
    assert subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            moved["base"],
            result.head_sha,
        ],
        cwd=repo,
        check=False,
    ).returncode == 0
    assert [status["state"] for status in github.statuses] == ["success"]


def test_update_continue_rechecks_and_rolls_base_moved_during_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    initial_base = _advance_main_cleanly(repo, branch)
    bare = _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"

    def live_pr() -> dict:
        return _pr_payload(
            base=initial_base,
            head=_git(bare, "rev-parse", f"refs/heads/{branch}"),
            branch=branch,
        )

    github = _FakeGitHub([live_pr for _ in range(16)])
    real_push = recovery_mod._push_updated_head
    moved: dict[str, str] = {}

    def push_then_move_base(*args: object, **kwargs: object) -> None:
        real_push(*args, **kwargs)
        if not moved:
            moved["base"] = _advance_remote_main_cleanly(bare, tmp_path)

    builds = 0

    def build(*_args: object, **_kwargs: object) -> BuildResult:
        nonlocal builds
        builds += 1
        return BuildResult(ok=True, duration_seconds=float(builds))

    monkeypatch.setattr(recovery_mod, "gh", github)
    monkeypatch.setattr(recovery_mod, "_push_updated_head", push_then_move_base)
    monkeypatch.setattr(recovery_mod, "run_lake_build", build)
    monkeypatch.setattr(
        recovery_mod,
        "_run_fallback",
        lambda *a, **k: pytest.fail("publication race must rebuild, not refine"),
    )

    result = update_continue(
        _update_config(repo, original_head, branch, workdir)
    )

    marker = json.loads((workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text())
    assert result.ok is True
    assert builds == 2
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_PUBLISHED
    assert marker["base_sha"] == moved["base"]
    assert subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            moved["base"],
            result.head_sha,
        ],
        cwd=repo,
        check=False,
    ).returncode == 0
    assert [status["state"] for status in github.statuses] == ["success"]


def test_update_continue_aborts_if_pr_head_moves_during_publication_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    initial_base = _advance_main_cleanly(repo, branch)
    bare = _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"

    def live_pr() -> dict:
        return _pr_payload(
            base=initial_base,
            head=_git(bare, "rev-parse", f"refs/heads/{branch}"),
            branch=branch,
        )

    github = _FakeGitHub([live_pr for _ in range(10)])
    real_await = recovery_mod._await_published_revision
    competing: dict[str, str] = {}

    def await_then_move_head(
        *args: object,
        **kwargs: object,
    ) -> recovery_mod.PullRequestRevision:
        published = real_await(*args, **kwargs)
        competing["head"] = _advance_remote_pr_branch(
            bare, tmp_path, branch
        )
        return published

    monkeypatch.setattr(recovery_mod, "gh", github)
    monkeypatch.setattr(
        recovery_mod,
        "_await_published_revision",
        await_then_move_head,
    )
    monkeypatch.setattr(
        recovery_mod,
        "run_lake_build",
        lambda *a, **k: BuildResult(ok=True, duration_seconds=1.0),
    )

    with pytest.raises(
        RecoveryError,
        match="moved to unexpected head .* after publication",
    ):
        update_continue(
            _update_config(repo, original_head, branch, workdir)
        )

    marker = json.loads((workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text())
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_PUSHING
    assert _git(bare, "rev-parse", f"refs/heads/{branch}") == competing["head"]
    assert github.statuses == []


def test_update_continue_rolls_forward_before_red_build_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    initial_base = _advance_main_cleanly(repo, branch)
    bare = _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"
    moved: dict[str, str] = {}

    def live_pr() -> dict:
        return _pr_payload(
            base=initial_base,
            head=_git(bare, "rev-parse", f"refs/heads/{branch}"),
            branch=branch,
        )

    github = _FakeGitHub([live_pr for _ in range(16)])
    builds = 0

    def build(*_args: object, **_kwargs: object) -> BuildResult:
        nonlocal builds
        builds += 1
        if builds == 1:
            moved["base"] = _advance_remote_main_cleanly(bare, tmp_path)
            return BuildResult(ok=False, log_tail="stale-base failure")
        return BuildResult(ok=True, duration_seconds=2.0)

    monkeypatch.setattr(recovery_mod, "gh", github)
    monkeypatch.setattr(recovery_mod, "run_lake_build", build)
    monkeypatch.setattr(
        recovery_mod,
        "_run_fallback",
        lambda *a, **k: pytest.fail(
            "Aristotle must not run until the live base is rebuilt"
        ),
    )

    result = update_continue(
        _update_config(repo, original_head, branch, workdir)
    )

    marker = json.loads((workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text())
    assert result.ok is True
    assert result.fast_path is True
    assert result.fallback_invoked is False
    assert builds == 2
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_PUBLISHED
    assert marker["base_sha"] == moved["base"]
    assert [status["state"] for status in github.statuses] == ["success"]


def test_update_continue_joins_diverged_dirty_checkpoint_without_aristotle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, initial_base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    bare = _install_origin(repo, tmp_path, branch)

    # Simulate a preserved worker checkpoint whose local branch diverged from
    # the selected PR head, then accumulated a further uncommitted Lean file.
    # Recovery must join both histories; resetting this branch to origin would
    # throw away exactly the work the recovery ladder promises to preserve.
    _git(repo, "reset", "--hard", initial_base)
    committed_local = repo / "Demo" / "Chapter15" / "CommittedLocal.lean"
    committed_local.write_text(
        "theorem committedLocal : True := by trivial\n"
    )
    _git(repo, "add", "Demo/Chapter15/CommittedLocal.lean")
    _git(repo, "commit", "-m", "unpublished local checkpoint")
    diverged_head = _git(repo, "rev-parse", "HEAD")
    dirty_local = repo / "Demo" / "Chapter15" / "DirtyLocal.lean"
    dirty_local.write_text("theorem dirtyLocal : True := by trivial\n")
    runtime_marker = repo / ".marathon" / "conductor" / "refine.pid"
    runtime_marker.parent.mkdir(parents=True)
    runtime_marker.write_text("live runtime identity\n")

    workdir = tmp_path / "work"
    workdir.mkdir()
    save_refine_state(
        workdir / "marathon-refine-state.json",
        RefineState(
            target_folder=str(repo / "Demo" / "Chapter15"),
            project_id="preserved-project",
            agent_task_id="preserved-task",
        ),
    )
    github = _FakeGitHub(
        [
            _pr_payload(base=current_base, head=original_head, branch=branch),
            _pr_payload(base=current_base, head=original_head, branch=branch),
            _pr_payload(base=current_base, head=original_head, branch=branch),
            _pr_payload(base=current_base, head=original_head, branch=branch),
            lambda: _pr_payload(
                base=current_base,
                head=_git(repo, "rev-parse", "HEAD"),
                branch=branch,
            ),
        ]
    )
    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr(
        "marathon.recovery.run_lake_build",
        lambda *args, **kwargs: BuildResult(ok=True, duration_seconds=1),
    )
    monkeypatch.setattr(
        "marathon.recovery._run_fallback",
        lambda *args, **kwargs: pytest.fail(
            "a clean joined checkpoint must not spend an Aristotle call"
        ),
    )

    result = update_continue(
        _update_config(repo, original_head, branch, workdir)
    )

    published = _git(repo, "rev-parse", "HEAD")
    marker = json.loads((workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text())
    saved = load_refine_state(workdir / "marathon-refine-state.json")
    assert result.ok is True and result.fast_path is True
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_PUBLISHED
    assert marker["adopted_local_head"] not in {original_head, diverged_head}
    for required in (
        original_head,
        diverged_head,
        marker["adopted_local_head"],
        current_base,
    ):
        assert subprocess.run(
            ["git", "merge-base", "--is-ancestor", required, published],
            cwd=repo,
            check=False,
        ).returncode == 0
    assert _git(
        repo, "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"
    ) == published
    assert "committedLocal" in _git(
        repo, "show", "HEAD:Demo/Chapter15/CommittedLocal.lean"
    )
    assert "dirtyLocal" in _git(
        repo, "show", "HEAD:Demo/Chapter15/DirtyLocal.lean"
    )
    assert runtime_marker.read_text() == "live runtime identity\n"
    assert _git(repo, "ls-tree", "-r", "--name-only", "HEAD").find(
        ".marathon/conductor/refine.pid"
    ) == -1
    assert saved is not None
    assert saved.project_id == "preserved-project"
    assert saved.agent_task_id == "preserved-task"


def test_update_continue_resumes_if_killed_after_joining_diverged_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, initial_base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    _install_origin(repo, tmp_path, branch)
    _git(repo, "reset", "--hard", initial_base)
    local = repo / "Demo" / "Chapter15" / "InterruptedLocal.lean"
    local.write_text("theorem interruptedLocal : True := by trivial\n")
    workdir = tmp_path / "work"
    workdir.mkdir()
    save_refine_state(
        workdir / "marathon-refine-state.json",
        RefineState(
            target_folder=str(repo / "Demo" / "Chapter15"),
            project_id="crash-safe-project",
            agent_task_id="crash-safe-task",
        ),
    )
    config = _update_config(
        repo,
        original_head,
        branch,
        workdir,
        operation_id="adoption-crash-operation",
    )
    old_pr = _pr_payload(base=current_base, head=original_head, branch=branch)
    real_merge = recovery_mod._merge_base_checkpoint

    with monkeypatch.context() as crash:
        crash.setattr(recovery_mod, "gh", _FakeGitHub([old_pr, old_pr]))

        def join_then_die(*args: object, **kwargs: object) -> tuple[str, bool]:
            result = real_merge(*args, **kwargs)
            if kwargs.get("base_sha") == original_head:
                raise RuntimeError("simulated death after PR-head join")
            return result

        crash.setattr(recovery_mod, "_merge_base_checkpoint", join_then_die)
        with pytest.raises(RuntimeError, match="after PR-head join"):
            update_continue(config)

    marker_path = workdir / UPDATE_RECOVERY_MARKER_FILENAME
    interrupted_marker = json.loads(marker_path.read_text())
    joined_head = _git(repo, "rev-parse", "HEAD")
    assert interrupted_marker["phase"] == UPDATE_RECOVERY_PHASE_STARTING
    assert interrupted_marker["adopted_local_head"] != original_head
    assert subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            interrupted_marker["adopted_local_head"],
            joined_head,
        ],
        cwd=repo,
        check=False,
    ).returncode == 0
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", original_head, joined_head],
        cwd=repo,
        check=False,
    ).returncode == 0

    with monkeypatch.context() as resume:
        resume.setattr(recovery_mod, "gh", _FakeGitHub([old_pr, old_pr]))
        resume.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *args, **kwargs: BuildResult(ok=False, log_tail="repair me"),
        )
        resume.setattr(recovery_mod, "_run_fallback", lambda *a, **k: 0)
        result = update_continue(config)

    final_head = _git(repo, "rev-parse", "HEAD")
    final_marker = json.loads(marker_path.read_text())
    saved = load_refine_state(workdir / "marathon-refine-state.json")
    assert result.ok is True and result.fallback_invoked is True
    assert final_marker["phase"] == UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED
    for required in (
        original_head,
        interrupted_marker["adopted_local_head"],
        current_base,
    ):
        assert subprocess.run(
            ["git", "merge-base", "--is-ancestor", required, final_head],
            cwd=repo,
            check=False,
        ).returncode == 0
    assert "interruptedLocal" in _git(
        repo, "show", "HEAD:Demo/Chapter15/InterruptedLocal.lean"
    )
    assert saved is not None
    assert saved.project_id == "crash-safe-project"
    assert saved.agent_task_id == "crash-safe-task"
    assert saved.force_continuation is True


def test_update_continue_conflict_preserves_dirty_checkpoint_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, _head, branch = _fixture_repo(tmp_path)
    lean = repo / "Demo" / "Chapter15" / "A.lean"
    lean.write_text(
        "theorem branchVersion : True := by trivial\n"
        "theorem new : True := by trivial\n"
    )
    _git(repo, "add", "Demo/Chapter15/A.lean")
    _git(repo, "commit", "-m", "edit on PR")
    original_head = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "main")
    lean.write_text("theorem mainVersion : True := by trivial\n")
    _git(repo, "add", "Demo/Chapter15/A.lean")
    _git(repo, "commit", "-m", "conflicting main edit")
    current_base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", branch)
    _install_origin(repo, tmp_path, branch)
    local_file = repo / "Demo" / "Chapter15" / "LocalCheckpoint.lean"
    local_file.write_text("theorem localCheckpoint : True := by trivial\n")

    workdir = tmp_path / "work"
    workdir.mkdir()
    save_refine_state(
        workdir / "marathon-refine-state.json",
        RefineState(
            target_folder=str(repo / "Demo" / "Chapter15"),
            project_id="old-project",
            agent_task_id="old-task",
            force_continuation=True,
        ),
    )
    github = _FakeGitHub(
        [
            _pr_payload(base=current_base, head=original_head, branch=branch),
            _pr_payload(base=current_base, head=original_head, branch=branch),
            _pr_payload(base=current_base, head=original_head, branch=branch),
        ]
    )
    observed: dict[str, object] = {}

    def fake_fallback(command: object, *, cwd: Path) -> int:
        state = load_refine_state(workdir / "marathon-refine-state.json")
        assert state is not None
        observed["command"] = command
        observed["cwd"] = cwd
        observed["state"] = state
        observed["marker"] = json.loads(
            (workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text()
        )
        return 7

    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr("marathon.recovery._run_fallback", fake_fallback)
    monkeypatch.setattr(
        "marathon.recovery.run_lake_build",
        lambda *args, **kwargs: pytest.fail("conflicted update must not build"),
    )

    result = update_continue(
        _update_config(
            repo,
            original_head,
            branch,
            workdir,
            fallback=("--", "fresh-refine", "--flag"),
        )
    )

    checkpoint = _git(repo, "rev-parse", "HEAD")
    state = observed["state"]
    marker = observed["marker"]
    assert result.exit_code == 7
    assert result.fast_path is False
    assert result.fallback_invoked is True
    assert observed["command"] == ("fresh-refine", "--flag")
    assert observed["cwd"] == repo
    assert isinstance(state, RefineState)
    assert state.project_id == "old-project"
    assert state.agent_task_id == "old-task"
    assert state.force_continuation is True
    assert state.preserve_local_branch is True
    assert state.continuation_commit_sha == checkpoint
    assert marker["branch"] == branch
    assert marker["original_expected_head"] == original_head
    assert marker["adopted_local_head"] != original_head
    assert marker["current_local_head"] == checkpoint
    assert marker["base_sha"] == current_base
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED
    assert len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    assert "<<<<<<<" in _git(repo, "show", "HEAD:Demo/Chapter15/A.lean")
    assert "localCheckpoint" in _git(
        repo, "show", "HEAD:Demo/Chapter15/LocalCheckpoint.lean"
    )
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert github.statuses == []


def test_update_continue_red_build_preserves_old_session_for_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"
    workdir.mkdir()
    save_refine_state(
        workdir / "marathon-refine-state.json",
        RefineState(
            target_folder=str(repo / "Demo" / "Chapter15"),
            project_id="must-not-survive",
            agent_task_id="must-not-survive",
        ),
    )
    github = _FakeGitHub(
        [
            _pr_payload(base=current_base, head=original_head, branch=branch),
            _pr_payload(base=current_base, head=original_head, branch=branch),
            _pr_payload(base=current_base, head=original_head, branch=branch),
        ]
    )

    def fake_fallback(command: object, *, cwd: Path) -> int:
        del command, cwd
        state = load_refine_state(workdir / "marathon-refine-state.json")
        assert state is not None
        assert state.project_id == "must-not-survive"
        assert state.agent_task_id == "must-not-survive"
        assert state.force_continuation is True
        assert state.preserve_local_branch is True
        assert state.continuation_commit_sha == _git(repo, "rev-parse", "HEAD")
        return 0

    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr("marathon.recovery._run_fallback", fake_fallback)
    monkeypatch.setattr(
        "marathon.recovery.run_lake_build",
        lambda *args, **kwargs: BuildResult(
            ok=False,
            log_tail="type mismatch",
        ),
    )

    result = update_continue(
        _update_config(repo, original_head, branch, workdir)
    )

    assert result.ok is True
    assert result.exit_code == 0
    assert result.fast_path is False
    assert result.build is not None and result.build.ok is False
    assert (workdir / UPDATE_RECOVERY_MARKER_FILENAME).is_file()
    assert json.loads(
        (workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text()
    )["phase"] == UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED
    assert github.statuses == []


def test_update_continue_aborts_if_pr_head_moves_before_exact_lease_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    _install_origin(repo, tmp_path, branch)
    github = _FakeGitHub(
        [
            _pr_payload(base=current_base, head=original_head, branch=branch),
            _pr_payload(base=current_base, head=original_head, branch=branch),
            _pr_payload(base=current_base, head="b" * 40, branch=branch),
        ]
    )
    pushed = False

    def fake_push(*args: object, **kwargs: object) -> None:
        nonlocal pushed
        pushed = True

    monkeypatch.setattr("marathon.recovery.gh", github)
    monkeypatch.setattr("marathon.recovery._push_updated_head", fake_push)
    monkeypatch.setattr(
        "marathon.recovery.run_lake_build",
        lambda *args, **kwargs: BuildResult(ok=True),
    )

    with pytest.raises(RecoveryError, match="head changed"):
        update_continue(
            _update_config(repo, original_head, branch, tmp_path / "work")
        )

    assert pushed is False
    assert github.statuses == []


def test_update_continue_aborts_if_remote_base_is_force_rewritten_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    bare = _install_origin(repo, tmp_path, branch)

    def live_pr() -> dict:
        return _pr_payload(
            base=current_base,
            head=_git(bare, "rev-parse", f"refs/heads/{branch}"),
            branch=branch,
        )

    github = _FakeGitHub([live_pr for _ in range(8)])
    pushed = False

    def fake_push(*args: object, **kwargs: object) -> None:
        nonlocal pushed
        pushed = True

    def green_build(*_args: object, **_kwargs: object) -> BuildResult:
        upstream = tmp_path / "rewriter"
        cloned = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--branch",
                "main",
                str(bare),
                str(upstream),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert cloned.returncode == 0, cloned.stderr
        _git(upstream, "config", "user.email", "test@example.com")
        _git(upstream, "config", "user.name", "Test User")
        tree = _git(upstream, "rev-parse", "HEAD^{tree}")
        rewritten = _git(upstream, "commit-tree", tree, "-m", "rewrite main")
        _git(upstream, "push", "--force", "origin", f"{rewritten}:main")
        return BuildResult(ok=True)

    monkeypatch.setattr(recovery_mod, "gh", github)
    monkeypatch.setattr(recovery_mod, "run_lake_build", green_build)
    monkeypatch.setattr(recovery_mod, "_push_updated_head", fake_push)

    with pytest.raises(RecoveryError, match="remote base branch main was rewritten"):
        update_continue(
            _update_config(repo, original_head, branch, tmp_path / "work")
        )

    assert pushed is False
    marker = json.loads(
        (tmp_path / "work" / UPDATE_RECOVERY_MARKER_FILENAME).read_text()
    )
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_PUSHING


def test_update_continue_rejects_legacy_unphased_marker_before_second_base_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    marker = workdir / UPDATE_RECOVERY_MARKER_FILENAME
    marker.write_text("{}\n")
    github = _FakeGitHub([])
    monkeypatch.setattr("marathon.recovery.gh", github)

    with pytest.raises(RecoveryError, match="unsupported update recovery marker"):
        update_continue(
            _update_config(repo, original_head, branch, workdir)
        )

    assert _git(repo, "rev-parse", "HEAD") == original_head
    assert github.statuses == []
    assert github.pr_reads == []


def test_update_continue_journals_starting_before_merge_and_resumes_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"
    config = _update_config(
        repo,
        original_head,
        branch,
        workdir,
        operation_id="operation-starting",
    )
    old_pr = _pr_payload(base=current_base, head=original_head, branch=branch)

    with monkeypatch.context() as crash:
        github = _FakeGitHub([old_pr, old_pr])
        crash.setattr(recovery_mod, "gh", github)

        def fail_after_marker(*args: object, **kwargs: object) -> tuple[str, bool]:
            del args, kwargs
            marker = json.loads(
                (workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text()
            )
            assert marker["phase"] == UPDATE_RECOVERY_PHASE_STARTING
            raise RuntimeError("simulated death before merge")

        crash.setattr(recovery_mod, "_merge_base_checkpoint", fail_after_marker)
        with pytest.raises(RuntimeError, match="simulated death"):
            update_continue(config)

    marker = json.loads((workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text())
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_STARTING
    assert marker["operation_id"] == "operation-starting"
    assert marker["pr"] == 277
    assert marker["branch"] == branch
    assert marker["original_expected_head"] == original_head
    assert marker["base_sha"] == current_base
    assert marker["current_local_head"] == original_head
    assert _git(repo, "rev-parse", "HEAD") == original_head
    assert not list(workdir.glob(f".{UPDATE_RECOVERY_MARKER_FILENAME}.*.tmp"))

    # A different conductor operation cannot adopt the marker, and the check is
    # local: GitHub is not consulted before the mismatch is rejected.
    with monkeypatch.context() as mismatch:
        mismatch.setattr(
            recovery_mod,
            "gh",
            lambda *a, **k: pytest.fail("identity mismatch must precede GitHub"),
        )
        with pytest.raises(RecoveryError, match="operation ID mismatch"):
            update_continue(
                _update_config(
                    repo,
                    original_head,
                    branch,
                    workdir,
                    operation_id="different-operation",
                )
            )

    with monkeypatch.context() as resume:
        github = _FakeGitHub(
            [
                old_pr,
                old_pr,
                old_pr,
                lambda: _pr_payload(
                    base=current_base,
                    head=_git(repo, "rev-parse", "HEAD"),
                    branch=branch,
                ),
            ]
        )
        resume.setattr(recovery_mod, "gh", github)
        resume.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *a, **k: BuildResult(ok=True),
        )
        result = update_continue(config)

    assert result.ok is True
    assert json.loads(
        (workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text()
    )["phase"] == UPDATE_RECOVERY_PHASE_PUBLISHED


def test_update_continue_recovers_merge_committed_before_phase_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"
    config = _update_config(repo, original_head, branch, workdir)
    old_pr = _pr_payload(base=current_base, head=original_head, branch=branch)
    real_transition = recovery_mod._transition_update_recovery_marker

    with monkeypatch.context() as crash:
        github = _FakeGitHub([old_pr, old_pr])
        crash.setattr(recovery_mod, "gh", github)

        def die_before_merged_phase(
            marker_path: Path,
            marker: dict,
            phase: str,
            **updates: object,
        ) -> dict:
            if phase == UPDATE_RECOVERY_PHASE_MERGED:
                assert _git(repo, "rev-parse", "HEAD") != original_head
                raise RuntimeError("simulated death after merge commit")
            return real_transition(marker_path, marker, phase, **updates)

        crash.setattr(
            recovery_mod,
            "_transition_update_recovery_marker",
            die_before_merged_phase,
        )
        with pytest.raises(RuntimeError, match="after merge commit"):
            update_continue(config)

    merged_head = _git(repo, "rev-parse", "HEAD")
    assert merged_head != original_head
    assert json.loads(
        (workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text()
    )["phase"] == UPDATE_RECOVERY_PHASE_STARTING

    with monkeypatch.context() as resume:
        github = _FakeGitHub(
            [
                old_pr,
                old_pr,
                old_pr,
                _pr_payload(base=current_base, head=merged_head, branch=branch),
            ]
        )
        resume.setattr(recovery_mod, "gh", github)
        resume.setattr(
            recovery_mod,
            "_merge_base_checkpoint",
            lambda *a, **k: pytest.fail("completed merge must not run twice"),
        )
        resume.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *a, **k: BuildResult(ok=True),
        )
        result = update_continue(config)

    assert result.ok is True
    assert result.head_sha == merged_head
    assert _git(
        repo, "rev-list", "--first-parent", "--count", f"{original_head}..HEAD"
    ) == "1"


def test_update_continue_staged_conflicts_resume_as_mandatory_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, _head, branch = _fixture_repo(tmp_path)
    lean = repo / "Demo" / "Chapter15" / "A.lean"
    lean.write_text(
        "theorem branchVersion : True := by trivial\n"
        "theorem new : True := by trivial\n"
    )
    _git(repo, "add", "Demo/Chapter15/A.lean")
    _git(repo, "commit", "-m", "edit on PR")
    original_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    lean.write_text("theorem mainVersion : True := by trivial\n")
    _git(repo, "add", "Demo/Chapter15/A.lean")
    _git(repo, "commit", "-m", "conflicting main edit")
    current_base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", branch)
    _install_origin(repo, tmp_path, branch)

    workdir = tmp_path / "work"
    config = _update_config(repo, original_head, branch, workdir)
    old_pr = _pr_payload(base=current_base, head=original_head, branch=branch)

    with monkeypatch.context() as crash:
        crash.setattr(recovery_mod, "gh", _FakeGitHub([old_pr, old_pr]))

        def stage_conflicts_then_die(*args: object, **kwargs: object) -> tuple[str, bool]:
            del args, kwargs
            merge = subprocess.run(
                ["git", "merge", "--no-edit", current_base],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            assert merge.returncode != 0
            subprocess.run(["git", "add", "-u", "--"], cwd=repo, check=True)
            assert _git(repo, "diff", "--name-only", "--diff-filter=U") == ""
            assert _git(repo, "rev-parse", "--verify", "MERGE_HEAD") == current_base
            raise RuntimeError("simulated death after staging conflicts")

        crash.setattr(
            recovery_mod,
            "_merge_base_checkpoint",
            stage_conflicts_then_die,
        )
        with pytest.raises(RuntimeError, match="after staging conflicts"):
            update_continue(config)

    assert json.loads(
        (workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text()
    )["phase"] == UPDATE_RECOVERY_PHASE_STARTING

    observed: dict[str, RefineState] = {}

    def fallback(command: object, *, cwd: Path) -> int:
        del command, cwd
        state = load_refine_state(workdir / "marathon-refine-state.json")
        assert state is not None
        observed["state"] = state
        return 0

    with monkeypatch.context() as resume:
        resume.setattr(recovery_mod, "gh", _FakeGitHub([old_pr, old_pr]))
        resume.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *a, **k: pytest.fail("staged conflict must not use green fast path"),
        )
        resume.setattr(recovery_mod, "_run_fallback", fallback)
        result = update_continue(config)

    marker = json.loads(
        (workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text()
    )
    assert result.ok is True
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED
    assert marker["had_conflicts"] is True
    assert observed["state"].project_id is None
    assert observed["state"].agent_task_id is None
    assert "<<<<<<<" in _git(repo, "show", "HEAD:Demo/Chapter15/A.lean")


def test_update_continue_resumes_merged_phase_without_merging_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"
    config = _update_config(repo, original_head, branch, workdir)
    old_pr = _pr_payload(base=current_base, head=original_head, branch=branch)

    with monkeypatch.context() as crash:
        crash.setattr(recovery_mod, "gh", _FakeGitHub([old_pr, old_pr]))
        crash.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("simulated death during build")
            ),
        )
        with pytest.raises(RuntimeError, match="during build"):
            update_continue(config)

    merged_head = _git(repo, "rev-parse", "HEAD")
    assert json.loads(
        (workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text()
    )["phase"] == UPDATE_RECOVERY_PHASE_MERGED

    builds: list[str] = []
    with monkeypatch.context() as resume:
        resume.setattr(
            recovery_mod,
            "gh",
            _FakeGitHub(
                [
                    old_pr,
                    old_pr,
                    old_pr,
                    _pr_payload(base=current_base, head=merged_head, branch=branch),
                ]
            ),
        )
        resume.setattr(
            recovery_mod,
            "_merge_base_checkpoint",
            lambda *a, **k: pytest.fail("merged phase must not merge again"),
        )
        resume.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *a, **k: builds.append("build") or BuildResult(ok=True),
        )
        result = update_continue(config)

    assert result.ok is True
    assert builds == ["build"]


def test_update_continue_recovers_fallback_commit_before_phase_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"
    config = _update_config(repo, original_head, branch, workdir)
    old_pr = _pr_payload(base=current_base, head=original_head, branch=branch)
    real_commit = recovery_mod._commit_fallback_checkpoint

    with monkeypatch.context() as crash:
        crash.setattr(recovery_mod, "gh", _FakeGitHub([old_pr, old_pr]))

        def red_build(*args: object, **kwargs: object) -> BuildResult:
            del args, kwargs
            (repo / "Demo" / "Chapter15" / "A.lean").write_text(
                "theorem interruptedFallback : False := by\n"
            )
            return BuildResult(ok=False, log_tail="type mismatch")

        def commit_then_die(*args: object, **kwargs: object) -> str:
            real_commit(*args, **kwargs)
            raise RuntimeError("simulated death after fallback commit")

        crash.setattr(recovery_mod, "run_lake_build", red_build)
        crash.setattr(
            recovery_mod,
            "_commit_fallback_checkpoint",
            commit_then_die,
        )
        with pytest.raises(RuntimeError, match="after fallback commit"):
            update_continue(config)

    marker_path = workdir / UPDATE_RECOVERY_MARKER_FILENAME
    assert json.loads(marker_path.read_text())["phase"] == UPDATE_RECOVERY_PHASE_MERGED
    fallback_head = _git(repo, "rev-parse", "HEAD")
    assert "preserve update fallback" in _git(repo, "show", "-s", "--format=%s")

    observed: dict[str, RefineState] = {}

    def prepared_fallback(command: object, *, cwd: Path) -> int:
        del command, cwd
        state = load_refine_state(workdir / "marathon-refine-state.json")
        assert state is not None
        observed["state"] = state
        return 0

    with monkeypatch.context() as resume:
        resume.setattr(recovery_mod, "gh", _FakeGitHub([old_pr, old_pr]))
        resume.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *a, **k: BuildResult(ok=False, log_tail="still red"),
        )
        resume.setattr(recovery_mod, "_run_fallback", prepared_fallback)
        result = update_continue(config)

    assert result.ok is True
    assert result.head_sha == fallback_head
    assert observed["state"].project_id is None
    assert observed["state"].agent_task_id is None
    marker = json.loads(marker_path.read_text())
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED
    assert marker["current_local_head"] == fallback_head


def test_update_continue_resumes_after_push_without_pushing_twice_and_published_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    current_base = _advance_main_cleanly(repo, branch)
    bare = _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"
    config = _update_config(
        repo,
        original_head,
        branch,
        workdir,
        operation_id="operation-push",
    )
    old_pr = _pr_payload(base=current_base, head=original_head, branch=branch)

    with monkeypatch.context() as crash:
        github = _FakeGitHub([old_pr, old_pr, old_pr, old_pr])
        crash.setattr(recovery_mod, "gh", github)
        crash.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *a, **k: BuildResult(ok=True, duration_seconds=3.0),
        )
        crash.setattr(
            recovery_mod,
            "_await_published_revision",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("simulated death after push")
            ),
        )
        with pytest.raises(RuntimeError, match="after push"):
            update_continue(config)

    updated_head = _git(repo, "rev-parse", "HEAD")
    assert _git(
        repo, "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"
    ) == updated_head
    marker_path = workdir / UPDATE_RECOVERY_MARKER_FILENAME
    assert json.loads(marker_path.read_text())["phase"] == UPDATE_RECOVERY_PHASE_PUSHING

    with monkeypatch.context() as resume:
        github = _FakeGitHub(
            [
                _pr_payload(base=current_base, head=updated_head, branch=branch),
                _pr_payload(base=current_base, head=updated_head, branch=branch),
            ]
        )
        resume.setattr(recovery_mod, "gh", github)
        resume.setattr(
            recovery_mod,
            "_push_updated_head",
            lambda *a, **k: pytest.fail("published remote head must not push twice"),
        )
        resume.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *a, **k: pytest.fail("pushing phase must not rebuild"),
        )
        result = update_continue(config)

    assert result.ok is True
    assert [status["state"] for status in github.statuses] == ["success"]
    assert json.loads(marker_path.read_text())["phase"] == UPDATE_RECOVERY_PHASE_PUBLISHED

    # Published is a terminal journal state: no GitHub, build, or push call is
    # repeated merely because the conductor reaps/relaunches the worker again.
    with monkeypatch.context() as again:
        again.setattr(
            recovery_mod,
            "gh",
            lambda *a, **k: pytest.fail("published recovery must not call GitHub"),
        )
        again.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *a, **k: pytest.fail("published recovery must not rebuild"),
        )
        repeated = update_continue(config)
    assert repeated.ok is True
    assert repeated.head_sha == updated_head
    assert "already published" in repeated.message


def test_update_continue_rolls_forward_if_base_moves_after_push_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, original_head, branch = _fixture_repo(tmp_path)
    initial_base = _advance_main_cleanly(repo, branch)
    bare = _install_origin(repo, tmp_path, branch)
    workdir = tmp_path / "work"
    config = _update_config(
        repo,
        original_head,
        branch,
        workdir,
        operation_id="operation-post-push-base-move",
    )
    old_pr = _pr_payload(base=initial_base, head=original_head, branch=branch)

    with monkeypatch.context() as crash:
        crash.setattr(
            recovery_mod,
            "gh",
            _FakeGitHub([old_pr, old_pr, old_pr, old_pr]),
        )
        crash.setattr(
            recovery_mod,
            "run_lake_build",
            lambda *a, **k: BuildResult(ok=True, duration_seconds=1.0),
        )
        crash.setattr(
            recovery_mod,
            "_await_published_revision",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("simulated death after push")
            ),
        )
        with pytest.raises(RuntimeError, match="after push"):
            update_continue(config)

    first_published = _git(
        bare, "rev-parse", f"refs/heads/{branch}"
    )
    advanced_base = _advance_remote_main_cleanly(bare, tmp_path)

    def live_pr() -> dict:
        return _pr_payload(
            base=initial_base,
            head=_git(bare, "rev-parse", f"refs/heads/{branch}"),
            branch=branch,
        )

    github = _FakeGitHub([live_pr for _ in range(10)])
    builds = 0

    def rebuilt(*_args: object, **_kwargs: object) -> BuildResult:
        nonlocal builds
        builds += 1
        return BuildResult(ok=True, duration_seconds=2.0)

    with monkeypatch.context() as resume:
        resume.setattr(recovery_mod, "gh", github)
        resume.setattr(recovery_mod, "run_lake_build", rebuilt)
        resume.setattr(
            recovery_mod,
            "_run_fallback",
            lambda *a, **k: pytest.fail("fast-forward base must not use Aristotle"),
        )
        result = update_continue(config)

    marker = json.loads(
        (workdir / UPDATE_RECOVERY_MARKER_FILENAME).read_text()
    )
    assert result.ok is True
    assert result.head_sha != first_published
    assert builds == 1
    assert marker["phase"] == UPDATE_RECOVERY_PHASE_PUBLISHED
    assert marker["expected_remote_head"] == first_published
    assert marker["base_sha"] == advanced_base
    assert subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            advanced_base,
            result.head_sha,
        ],
        cwd=repo,
        check=False,
    ).returncode == 0
    assert [status["state"] for status in github.statuses] == ["success"]


def test_update_continue_cli_accepts_operation_id() -> None:
    args = recovery_mod._parser().parse_args(
        [
            "update-continue",
            "--repo-dir", "/tmp/repo",
            "--repo", "o/r",
            "--pr", "277",
            "--expected-head", "a" * 40,
            "--target", "Demo/Chapter15",
            "--workdir", "/tmp/work",
            "--operation-id", "operation-277",
            "--",
            "refine",
        ]
    )
    assert args.operation_id == "operation-277"

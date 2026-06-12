"""Tests for Phase-3 metadata decoupling + reject routing.

Contract under test (docs/marathon-v2-plan.md §3 Phase 3 row: "Metadata
files move to Conductor-side regeneration — never committed by workers
(generalizes the wall-time fix)"):

* ``PipelineConfig.commit_metadata=False`` excludes ``formalization.yaml``
  from the per-iteration git staging AND skips its per-iteration refresh,
  while the merge-friendly metadata (project-id-keyed wall-time sidecar,
  append-only PromptLog) stays committed;
* ``formalization.regenerate_metadata`` is the conductor's central
  regeneration path: True/False reflects a *meaningful* yaml change
  (the ``_auto: last updated`` stamp alone is churn, not change), and
  the commit path refuses to commit over unrelated dirt;
* the conductor's completion hook fires only on successful jobs, only
  when the primary checkout is on the base branch and clean (deferral
  note otherwise), with push following the jobs' auto-push setting;
* ``marathon review reject`` routes to a LIVE conductor (no per-chapter
  daemon launch) and falls back to the daemon exactly as before when no
  conductor is running.

Every subprocess / git boundary is monkeypatched: no test invokes
Aristotle, Claude, gh, or real git commands.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import marathon.conductor as conductor
import marathon.formalization as formalization
import marathon.post_pipeline as pp
import marathon.review.daemon as daemon
import marathon.review.review as review
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels
from marathon.review.state import load_state, state_path


# --- shared fixtures ------------------------------------------------------------


def make_repo(tmp_path: Path) -> Path:
    """A repo dir holding all three metadata surfaces plus a target."""
    repo = tmp_path / "repo"
    (repo / ".marathon").mkdir(parents=True)
    (repo / ".marathon" / "PromptLog.md").write_text("# Prompt log\n")
    (repo / ".marathon" / "wall-time.json").write_text(
        json.dumps({"version": 2, "projects": {}, "build_only_seconds": 0})
    )
    (repo / "formalization.yaml").write_text("version: v0.2\n")
    (repo / "Demo" / "Chapter14").mkdir(parents=True)
    return repo


def make_cfg(tmp_path: Path) -> ReviewConfig:
    repo = make_repo(tmp_path)
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo="example/Demo",
        parent_issue=1,
        referee_path=repo / ".marathon/referee.md",
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={14: ChapterRegistry(chapter=14, entries=[(22, "Lemma A")])},
    )


@pytest.fixture
def git_calls(monkeypatch):
    """Replace post_pipeline's git boundary: record every command and
    script the answers run_git_commit needs (staged diff non-empty so
    the commit proceeds, a HEAD sha to slice)."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "diff", "--cached"]:
            return subprocess.CompletedProcess(cmd, 1)  # something staged
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="abcdef12345\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(pp.subprocess, "run", fake_run)
    return calls


def staged_paths(calls: list[list[str]]) -> list[str]:
    [add] = [c for c in calls if c[:2] == ["git", "add"]]
    return add[add.index("--") + 1:]


# --- run_git_commit staging ------------------------------------------------------


def test_no_metadata_commit_excludes_yaml_keeps_sidecar_and_promptlog(
    tmp_path, git_calls
):
    """The decoupling itself: under commit_metadata=False the yaml never
    enters the index, but the two merge-friendly surfaces (write-once-
    keyed sidecar, append-only PromptLog) are staged as always."""
    repo = make_repo(tmp_path)
    result = pp.run_git_commit(
        repo, repo / "Demo" / "Chapter14", "msg", commit_metadata=False
    )
    assert result.sha == "abcdef12"
    staged = staged_paths(git_calls)
    assert "Demo/Chapter14" in staged
    assert ".marathon/PromptLog.md" in staged
    assert ".marathon/wall-time.json" in staged
    assert "formalization.yaml" not in staged


def test_default_commit_still_stages_yaml(tmp_path, git_calls):
    """Parity: single-flight runs (no conductor) keep today's behavior —
    the yaml is bundled into the iteration commit."""
    repo = make_repo(tmp_path)
    pp.run_git_commit(repo, repo / "Demo" / "Chapter14", "msg")
    staged = staged_paths(git_calls)
    assert "formalization.yaml" in staged
    assert ".marathon/wall-time.json" in staged


# --- run_post_pipeline: refresh skipped, flag threaded ----------------------------


def _commit_spy(monkeypatch):
    seen: dict = {}

    def fake_commit(repo_dir, target_path, message, project_id=None,
                    claude_in_loop=False, extra_paths=None,
                    commit_metadata=True):
        seen["commit_metadata"] = commit_metadata
        return pp.CommitResult(sha="abc1234")

    monkeypatch.setattr(pp, "run_git_commit", fake_commit)
    return seen


def test_pipeline_skips_yaml_refresh_when_metadata_uncommitted(
    tmp_path, monkeypatch, capsys
):
    """commit_metadata=False must also skip the per-iteration yaml
    refresh — updating a file we won't commit would leave the worker's
    checkout dirty at the repo root and block its next branch switch."""
    repo = make_repo(tmp_path)
    seen = _commit_spy(monkeypatch)
    monkeypatch.setattr(
        formalization, "update_formalization",
        lambda *a, **kw: pytest.fail(
            "update_formalization must not run under --no-metadata-commit"
        ),
    )
    config = pp.PipelineConfig(auto_commit=True, commit_metadata=False, gate="off")
    pp.run_post_pipeline(
        config, repo, repo / "Demo" / "Chapter14", "Chapter14",
        iteration=1, project_id="proj-1",
    )
    assert seen["commit_metadata"] is False  # threaded into the staging
    assert "--no-metadata-commit" in capsys.readouterr().out  # deferral note


def test_pipeline_refreshes_yaml_by_default(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    _commit_spy(monkeypatch)
    refreshed: list = []
    monkeypatch.setattr(
        formalization, "update_formalization",
        lambda repo_dir, **kw: refreshed.append(repo_dir) or None,
    )
    config = pp.PipelineConfig(auto_commit=True, gate="off")
    pp.run_post_pipeline(
        config, repo, repo / "Demo" / "Chapter14", "Chapter14",
        iteration=1, project_id="proj-1",
    )
    assert refreshed == [repo]


# --- formalization.regenerate_metadata ---------------------------------------------


@pytest.fixture
def formalization_git(monkeypatch):
    """formalization's git boundary: scripted `git ls-files` payload
    (count_sorries' input, bytes — it splits NUL-separated output) and
    `git status --porcelain` text; add/commit/push recorded + succeed."""
    state = SimpleNamespace(calls=[], ls_files=b"", porcelain="")

    def fake_run(cmd, **kwargs):
        state.calls.append(list(cmd))
        if cmd[:2] == ["git", "ls-files"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=state.ls_files, stderr=b"")
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=state.porcelain, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(formalization.subprocess, "run", fake_run)
    return state


def seeded_yaml_repo(tmp_path: Path, formalization_git) -> Path:
    """Repo with a freshly-written yaml whose sorry_count is 0 (the
    update path itself writes it, so later comparisons are apples to
    apples — same dumper, same stamp shape)."""
    repo = make_repo(tmp_path)
    formalization_git.ls_files = b""
    formalization.update_formalization(repo, framework="Marathon")
    formalization_git.calls.clear()
    return repo


def test_regenerate_metadata_false_when_yaml_missing(tmp_path, formalization_git):
    repo = tmp_path / "bare"
    repo.mkdir()
    assert formalization.regenerate_metadata(repo) is False
    assert formalization_git.calls == []  # opt-in respected: no work at all


def test_regenerate_metadata_commits_real_change(tmp_path, formalization_git):
    """A sorry-count change is a meaningful change: True, plus the
    mechanical commit (yaml only) and the push the caller asked for."""
    repo = seeded_yaml_repo(tmp_path, formalization_git)
    (repo / "Demo" / "Chapter14" / "A.lean").write_text(
        "theorem foo : True := by sorry\n"
    )
    formalization_git.ls_files = b"Demo/Chapter14/A.lean\x00"
    formalization_git.porcelain = " M formalization.yaml\n"  # only the yaml dirty

    assert formalization.regenerate_metadata(repo, commit=True, push=True) is True

    assert "sorry_count: 1" in (repo / "formalization.yaml").read_text()
    git = [c for c in formalization_git.calls if c[0] == "git"]
    assert ["git", "add", "--", "formalization.yaml"] in git
    assert ["git", "commit", "-m", formalization.REGENERATE_COMMIT_MESSAGE] in git
    assert ["git", "push"] in git


def test_regenerate_metadata_timestamp_only_is_not_a_change(
    tmp_path, formalization_git
):
    """Re-running with identical inputs must return False, restore the
    on-disk bytes (no checkout dirt), and never commit — the `_auto:
    last updated` stamp alone is churn, not change."""
    repo = seeded_yaml_repo(tmp_path, formalization_git)
    before = (repo / "formalization.yaml").read_text()

    assert formalization.regenerate_metadata(repo, commit=True, push=True) is False

    assert (repo / "formalization.yaml").read_text() == before
    assert [c for c in formalization_git.calls if c[:2] == ["git", "commit"]] == []


def test_regenerate_metadata_refuses_commit_over_unrelated_dirt(
    tmp_path, formalization_git, capsys
):
    """Real change + a dirty checkout: the yaml is still updated (True)
    but the commit is refused with a note — machine commits never
    smuggle operator work-in-progress."""
    repo = seeded_yaml_repo(tmp_path, formalization_git)
    (repo / "Demo" / "Chapter14" / "A.lean").write_text(
        "theorem foo : True := by sorry\n"
    )
    formalization_git.ls_files = b"Demo/Chapter14/A.lean\x00"
    formalization_git.porcelain = " M Other.lean\n M formalization.yaml\n"

    assert formalization.regenerate_metadata(repo, commit=True, push=True) is True

    assert "NOT committed" in capsys.readouterr().out
    assert [c for c in formalization_git.calls if c[:2] == ["git", "commit"]] == []
    assert [c for c in formalization_git.calls if c[:2] == ["git", "push"]] == []


def test_regenerate_metadata_commits_over_bookkeeping_dirt_stages_only_yaml(
    tmp_path, formalization_git
):
    """Unstaged .marathon/ bookkeeping is marathon's own expected dirt
    (record_iteration writes it on every success; consumer repos may
    track it): it must not block the mechanical commit — and must not
    ride into it. Only the yaml is ever staged."""
    repo = seeded_yaml_repo(tmp_path, formalization_git)
    (repo / "Demo" / "Chapter14" / "A.lean").write_text(
        "theorem foo : True := by sorry\n"
    )
    formalization_git.ls_files = b"Demo/Chapter14/A.lean\x00"
    formalization_git.porcelain = (
        " M .marathon/review/state.json\n"
        "?? .marathon/conductor/jobs.json\n"
        " M formalization.yaml\n"
    )

    assert formalization.regenerate_metadata(repo, commit=True) is True

    git = [c for c in formalization_git.calls if c[0] == "git"]
    assert ["git", "commit", "-m", formalization.REGENERATE_COMMIT_MESSAGE] in git
    # The carve-out never widens the commit: the only `git add` is the yaml.
    adds = [c for c in git if c[:2] == ["git", "add"]]
    assert adds == [["git", "add", "--", "formalization.yaml"]]


def test_regenerate_metadata_staged_bookkeeping_still_blocks_commit(
    tmp_path, formalization_git, capsys
):
    """A STAGED bookkeeping change is real dirt: `git commit` sweeps in
    the whole index, so carving it out would smuggle the file into the
    machine commit. Yaml updated (True), commit refused."""
    repo = seeded_yaml_repo(tmp_path, formalization_git)
    (repo / "Demo" / "Chapter14" / "A.lean").write_text(
        "theorem foo : True := by sorry\n"
    )
    formalization_git.ls_files = b"Demo/Chapter14/A.lean\x00"
    formalization_git.porcelain = (
        "M  .marathon/review/state.json\n M formalization.yaml\n"
    )

    assert formalization.regenerate_metadata(repo, commit=True) is True

    assert "NOT committed" in capsys.readouterr().out
    assert [c for c in formalization_git.calls if c[:2] == ["git", "commit"]] == []


# --- conductor: dispatch flag + completion hook -------------------------------------


def test_dispatch_adds_no_metadata_commit_keeps_daemon_args_verbatim(
    tmp_path, monkeypatch
):
    """The conductor's one dispatch-site addition: --no-metadata-commit,
    with DEFAULT_REFINE_ARGS still the imported daemon set, verbatim."""
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(
        conductor.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        conductor.subprocess, "Popen",
        lambda cmd, **kw: spawned.append(list(cmd)) or SimpleNamespace(pid=4001),
    )

    job = conductor._dispatch_job(cfg, 22, 14, tmp_path / "runs", {})

    assert job is not None
    [cmd] = spawned
    assert "--no-metadata-commit" in cmd
    assert cmd[-len(daemon.DEFAULT_REFINE_ARGS):] == daemon.DEFAULT_REFINE_ARGS


@pytest.fixture
def conductor_git(monkeypatch):
    """Conductor's git boundary for the hook tests: scripted HEAD branch
    + porcelain output; everything recorded."""
    state = SimpleNamespace(calls=[], branch="main", porcelain="")

    def fake_run(cmd, **kwargs):
        state.calls.append(list(cmd))
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{state.branch}\n", stderr="")
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=state.porcelain, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(conductor.subprocess, "run", fake_run)
    return state


def test_hook_regenerates_on_clean_base_branch(tmp_path, conductor_git, monkeypatch):
    """Clean primary checkout on the base branch: regenerate, commit,
    push following the jobs' auto-push setting (DEFAULT_REFINE_ARGS)."""
    cfg = make_cfg(tmp_path)
    seen: dict = {}

    def spy(repo_dir, *, commit, push):
        seen.update(repo_dir=repo_dir, commit=commit, push=push)
        return True

    monkeypatch.setattr(formalization, "regenerate_metadata", spy)

    conductor._regenerate_metadata_after_success(cfg)

    assert seen == {
        "repo_dir": cfg.repo_dir,
        "commit": True,
        "push": "--auto-push" in daemon.DEFAULT_REFINE_ARGS,
    }


def test_hook_defers_on_non_base_branch(tmp_path, conductor_git, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    conductor_git.branch = "docs-wip"
    monkeypatch.setattr(
        formalization, "regenerate_metadata",
        lambda *a, **kw: pytest.fail("must not regenerate off the base branch"),
    )
    conductor._regenerate_metadata_after_success(cfg)
    out = capsys.readouterr().out
    assert "deferred" in out and "docs-wip" in out


def test_hook_defers_on_dirty_checkout(tmp_path, conductor_git, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    conductor_git.porcelain = " M Demo/Chapter14/A.lean\n"
    monkeypatch.setattr(
        formalization, "regenerate_metadata",
        lambda *a, **kw: pytest.fail("must not regenerate over a dirty checkout"),
    )
    conductor._regenerate_metadata_after_success(cfg)
    assert "deferred" in capsys.readouterr().out


def test_hook_proceeds_over_marathon_bookkeeping_dirt(
    tmp_path, conductor_git, monkeypatch, capsys
):
    """The deployment-blocking case: record_iteration has just dirtied
    git-tracked .marathon/review/state.json in the primary checkout, so
    a literal clean-tree check would defer after EVERY success and the
    yaml would never regenerate. Marathon's own bookkeeping is expected
    dirt — the hook must proceed."""
    cfg = make_cfg(tmp_path)
    conductor_git.porcelain = (
        " M .marathon/review/state.json\n?? .marathon/conductor/jobs.json\n"
    )
    seen: dict = {}
    monkeypatch.setattr(
        formalization, "regenerate_metadata",
        lambda repo_dir, *, commit, push: seen.update(commit=commit) or True,
    )

    conductor._regenerate_metadata_after_success(cfg)

    assert seen == {"commit": True}
    assert "deferred" not in capsys.readouterr().out


def test_hook_defers_on_bookkeeping_plus_user_dirt(
    tmp_path, conductor_git, monkeypatch, capsys
):
    """The carve-out is exactly .marathon/: any other dirty path is
    (potential) operator work in flight, so its presence defers even
    when bookkeeping dirt sits alongside it."""
    cfg = make_cfg(tmp_path)
    conductor_git.porcelain = (
        " M .marathon/review/state.json\n M Demo/Chapter14/A.lean\n"
    )
    monkeypatch.setattr(
        formalization, "regenerate_metadata",
        lambda *a, **kw: pytest.fail("must not regenerate over user dirt"),
    )
    conductor._regenerate_metadata_after_success(cfg)
    assert "deferred" in capsys.readouterr().out


def test_reap_fires_hook_on_success_not_on_failure(tmp_path, monkeypatch):
    """The hook is a *completion* hook: clean exits only. Failed exits
    go to the Phase-0 state machine and must not touch metadata."""
    cfg = make_cfg(tmp_path)
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "issues": {"22": {"status": "rejected",
                          "verdict_ts": "2026-06-10T12:00:00-04:00",
                          "notes": "fix"}},
    }))
    monkeypatch.setattr(
        conductor.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    fired: list = []
    monkeypatch.setattr(
        conductor, "_regenerate_metadata_after_success",
        lambda cfg_: fired.append(cfg_.repo_dir),
    )

    def job(rc):
        return conductor.ConductorJob(
            issue_num=22, chapter=14, target="Demo/Chapter14",
            worktree=str(tmp_path / "runs" / "wt-i22-x"),
            workdir=str(tmp_path / "runs" / "wd-i22-x"),
            branch="marathon/refine-c14-i22", pid=4001, status="running",
            proc=SimpleNamespace(poll=lambda: rc),
        )

    assert conductor._reap_finished(cfg, [job(0)], {}, {}, max_attempts=3) == 1
    assert fired == [cfg.repo_dir]

    assert conductor._reap_finished(cfg, [job(1)], {}, {}, max_attempts=3) == 1
    assert fired == [cfg.repo_dir]  # failure: no second firing
    assert load_state(cfg).issues[22].attempts == 1  # state machine still ran


# --- review reject routing ------------------------------------------------------------


def reject_args(num=22):
    return SimpleNamespace(issue_num=num, notes="bad proof", comment=None,
                           no_refine=False)


@pytest.fixture
def reject_env(tmp_path, monkeypatch):
    """cmd_reject with its config/GitHub boundaries faked: real
    record_rejection into tmp state.json, recorded gh calls."""
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review, "load_config", lambda repo_dir=None: cfg)
    gh_calls: list = []
    monkeypatch.setattr(
        review, "gh", lambda *a, **kw: gh_calls.append(a) or
        subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    return SimpleNamespace(cfg=cfg, gh=gh_calls)


def test_reject_routes_to_live_conductor(reject_env, monkeypatch, capsys):
    """A live conductor lock means the rejection is already in the
    conductor's cross-chapter queue: no per-chapter daemon launch."""
    lock = conductor.conductor_lock_path(reject_env.cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))  # provably-alive pid: our own
    monkeypatch.setattr(
        review.subprocess, "Popen",
        lambda *a, **kw: pytest.fail(
            "per-chapter daemon must not launch under a live conductor"
        ),
    )

    review.cmd_reject(reject_args())

    out = capsys.readouterr().out
    assert "conductor" in out
    assert "no per-chapter daemon launched" in out
    assert load_state(reject_env.cfg).issues[22].status == "rejected"  # still queued


def test_reject_launches_daemon_without_conductor(reject_env, monkeypatch, capsys):
    """No conductor lock: per-chapter daemon behavior unchanged."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        review.subprocess, "Popen",
        lambda cmd, **kw: spawned.append(list(cmd)) or SimpleNamespace(pid=4001),
    )

    review.cmd_reject(reject_args())

    assert spawned == [
        [sys.executable, "-m", "marathon.review.daemon", "--chapter", "14"]
    ]
    assert "refine daemon launched" in capsys.readouterr().out


def test_reject_ignores_stale_conductor_lock(reject_env, monkeypatch, capsys):
    """A dead PID in conductor.lock reads as 'no conductor' — the
    fallback must be exactly the pre-Phase-3 daemon launch."""
    lock = conductor.conductor_lock_path(reject_env.cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("99999999")  # provably dead
    spawned: list = []
    monkeypatch.setattr(
        review.subprocess, "Popen",
        lambda cmd, **kw: spawned.append(list(cmd)) or SimpleNamespace(pid=4001),
    )

    review.cmd_reject(reject_args())

    assert len(spawned) == 1
    assert "refine daemon launched" in capsys.readouterr().out

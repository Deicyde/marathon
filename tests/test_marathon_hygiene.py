"""Harness-hygiene tests for the metrics redesign (plan §4).

* ``prepare_auto_pr_branch``'s marathon-bookkeeping auto-commit stages the
  EXACT dirty ``.marathon/`` paths — never a wholesale ``git add --
  .marathon/`` that could sweep in an un-gitignored ``marathon.db`` or
  unrelated bookkeeping.
* ``ensure_marathon_gitignore`` writes ``.marathon/.gitignore`` ignoring
  ``marathon.db*`` (and ``*.bak``) for every consumer, idempotently.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import marathon.formalization as formalization
import marathon.post_pipeline as pp
from marathon.formalization import is_ignorable_bookkeeping_dirt
from marathon.ledger import Ledger, ensure_marathon_gitignore


# --- narrowed git-add in prepare_auto_pr_branch ----------------------------


def test_auto_pr_branch_stages_exact_dirty_paths_not_whole_dir(monkeypatch, tmp_path):
    """The branch-prep marathon-bookkeeping auto-commit must stage the
    specific dirty paths, NOT ``.marathon/`` wholesale (which could capture
    marathon.db or other untracked bookkeeping)."""
    adds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "fetch", "origin"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "status"]:
            # Two dirty marathon bookkeeping paths (modified + untracked).
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout=(
                    " M .marathon/review/state.json\n"
                    "?? .marathon/metrics/Chapter14-22-proj.json\n"
                ),
                stderr="",
            )
        if cmd[:2] == ["git", "add"]:
            adds.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(pp.subprocess, "run", fake_run)

    ok, branch, msg = pp.prepare_auto_pr_branch(tmp_path, "Chapter14", 22, base="main")
    assert ok is True
    # Exactly one add, of the two named paths — never the bare directory.
    assert len(adds) == 1
    staged = adds[0][adds[0].index("--") + 1:]
    assert staged == [
        ".marathon/review/state.json",
        ".marathon/metrics/Chapter14-22-proj.json",
    ]
    assert ".marathon/" not in staged  # not the wholesale directory


def test_auto_pr_branch_refuses_non_marathon_dirt(monkeypatch, tmp_path):
    """Unchanged guard: any non-.marathon dirt still refuses the switch."""
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "fetch", "origin"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=" M Demo/Chapter14/A.lean\n", stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(pp.subprocess, "run", fake_run)
    ok, branch, msg = pp.prepare_auto_pr_branch(tmp_path, "Chapter14", 22, base="main")
    assert ok is False
    assert "non-marathon changes" in msg


# --- ensure_marathon_gitignore ---------------------------------------------


def test_ensure_marathon_gitignore_creates_with_db_patterns(tmp_path):
    created = ensure_marathon_gitignore(tmp_path)
    assert created is True
    text = (tmp_path / ".marathon" / ".gitignore").read_text()
    assert "marathon.db" in text
    assert "marathon.db-wal" in text
    assert "marathon.db-shm" in text
    assert "*.bak" in text
    # Never the whole-directory ignore (that would untrack the shards).
    assert text.strip().splitlines() != ["*"]
    assert "*\n" not in text


def test_ensure_marathon_gitignore_is_idempotent(tmp_path):
    assert ensure_marathon_gitignore(tmp_path) is True
    first = (tmp_path / ".marathon" / ".gitignore").read_text()
    # Second run: nothing missing → no change, no duplication.
    assert ensure_marathon_gitignore(tmp_path) is False
    assert (tmp_path / ".marathon" / ".gitignore").read_text() == first


def test_ensure_marathon_gitignore_preserves_operator_lines(tmp_path):
    (tmp_path / ".marathon").mkdir()
    (tmp_path / ".marathon" / ".gitignore").write_text("# mine\nscratch/\n")
    assert ensure_marathon_gitignore(tmp_path) is True
    text = (tmp_path / ".marathon" / ".gitignore").read_text()
    assert "# mine" in text and "scratch/" in text  # preserved
    assert "marathon.db" in text                     # added


def test_ledger_init_establishes_gitignore(tmp_path):
    Ledger.for_repo(tmp_path).init()
    gi = tmp_path / ".marathon" / ".gitignore"
    assert gi.is_file()
    assert "marathon.db" in gi.read_text()


# --- un-strand clears EVERY committed path (the wedge regression) ----------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


@pytest.fixture
def real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    (repo / ".marathon" / "metrics").mkdir(parents=True)
    (repo / "formalization.yaml").write_text("version: v0.2\n")
    (repo / ".marathon" / "wall-time.json").write_text(
        '{"version":2,"projects":{},"build_only_seconds":0}\n'
    )
    (repo / ".marathon" / "metrics" / "old.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_unstrand_clears_all_committed_paths_not_just_yaml(real_repo):
    """The wedge regression: when the central regen commit carries the
    rollups + a NEW shard alongside the yaml, the un-strand must leave the
    index CLEAN. Restoring only the yaml (the old behavior) left the
    rollup/shard paths STAGED — which the conductor's clean-tree gate
    counts as dirt, permanently deferring every future metadata regen
    after the first PR-routed success."""
    repo = real_repo
    # Simulate the central regen commit: yaml change + rollup change + a NEW
    # shard (status A after reset --soft — the case `git checkout` can't fix).
    (repo / "formalization.yaml").write_text("version: v0.2 CHANGED\n")
    (repo / ".marathon" / "wall-time.json").write_text(
        '{"version":2,"projects":{"p1":{"seconds":100}},"build_only_seconds":0}\n'
    )
    (repo / ".marathon" / "metrics" / "new.json").write_text('{"new":1}\n')
    _git(repo, "add", "--", "formalization.yaml", ".marathon/wall-time.json",
         ".marathon/metrics")
    _git(repo, "commit", "-qm", "regen")

    formalization._unstrand_primary_after_regen_push(
        repo, "main",
        committed_paths=["formalization.yaml", ".marathon/wall-time.json",
                         ".marathon/metrics"],
    )

    # The regen commit is dropped from the local branch (origin holds it).
    head_subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_subject == "base"

    # The index is CLEAN: no STAGED entry (index column M/A/D) remains.
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout
    staged = [ln for ln in porcelain.splitlines() if ln and ln[0] in "MAD"]
    assert staged == [], f"un-strand left staged paths: {staged!r}"

    # The yaml is restored to HEAD exactly; the .marathon/ dirt that remains
    # is unstaged bookkeeping the conductor gate explicitly exempts.
    assert (repo / "formalization.yaml").read_text() == "version: v0.2\n"
    dirty = [ln for ln in porcelain.splitlines()
             if ln.strip() and not is_ignorable_bookkeeping_dirt(ln)]
    assert dirty == [], f"conductor gate would DEFER on: {dirty!r}"


def test_unstrand_legacy_single_yaml_still_restores(real_repo):
    """Back-compat: with no committed_paths (legacy caller), the un-strand
    still restores the yaml alone (index + worktree to HEAD)."""
    repo = real_repo
    (repo / "formalization.yaml").write_text("version: v0.2 CHANGED\n")
    _git(repo, "add", "--", "formalization.yaml")
    _git(repo, "commit", "-qm", "regen")

    formalization._unstrand_primary_after_regen_push(repo, "main")

    assert (repo / "formalization.yaml").read_text() == "version: v0.2\n"
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout
    assert [ln for ln in porcelain.splitlines() if ln and ln[0] in "MAD"] == []

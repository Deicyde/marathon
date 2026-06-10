"""Tests for ``marathon review sync`` (marathon.review.sync).

Phase-1 contract under test (docs/marathon-v2-plan.md §2 ruling 1
"two-way projection"; §3 Phase 1 "two-way GitHub sync"):

* drift is detected in BOTH directions — GitHub verdict labels missing
  locally propose inbound pulls; local verdicts missing on GitHub
  propose outbound label pushes;
* GitHub labels are authoritative for HUMAN verdicts, but local-only
  operational state (``stalled``, ``attempts``, ``last_iteration_ts``,
  rejection notes) is NEVER overwritten — a stalled entry whose GitHub
  label still says rejected is reported as ``local-only state (kept)``
  and left byte-identical on apply;
* the default invocation is a pure read: no state.json write, no
  verdicts.jsonl, no ledger db, no ``gh issue edit``;
* ``--apply`` is idempotent — a second ``compute_drift`` immediately
  after reports no actionable rows;
* inbound verdicts applied by sync are provenance-tagged
  ``source='sync'`` in both the tracked verdicts.jsonl and the ledger's
  ``verdict_events`` history (never mislabeled as operator 'cli'
  keystrokes), and a pulled rejection is parked (``needs_iteration`` is
  False) so the refine daemon can never feed its placeholder notes to
  Aristotle verbatim.

All GitHub access is monkeypatched at the module-under-test boundary
(``fetch_issues_bulk`` / ``issue_labels`` / ``gh``) — no subprocesses,
no network. State files live under tmp_path.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import marathon.review.state as state_mod
import marathon.review.sync as sync_mod
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels
from marathon.review.state import load_state
from marathon.review.sync import (
    ACTION_KEEP,
    ACTION_PULL_REJECT,
    ACTION_PULL_VERIFY,
    ACTION_PUSH_REJECTED,
    ACTION_PUSH_VERIFIED,
    ACTION_SKIP,
    apply_drift,
    cmd_sync,
    compute_drift,
    propose_action,
)


# --- helpers -----------------------------------------------------------------


def make_cfg(tmp_path: Path) -> ReviewConfig:
    """Realistic ReviewConfig rooted at tmp_path (same fixture shape as
    test_review_plumbing / test_ledger)."""
    return ReviewConfig(
        repo_dir=tmp_path,
        config_path=tmp_path / ".marathon" / "review" / "config.toml",
        github_repo="someone/SomeProject",
        parent_issue=1,
        referee_path=tmp_path / ".marathon" / "referee.md",
        target_path_template="SomeProject/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(
                chapter=14,
                entries=[(14, "Lemma 14.7"), (15, "Proposition 14.8")],
            ),
        },
    )


def write_state(tmp_path: Path, issues: dict[int, dict]) -> Path:
    path = tmp_path / ".marathon" / "review" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": 1, "issues": {str(k): v for k, v in issues.items()}},
            indent=2,
        )
        + "\n"
    )
    return path


def patch_labels(monkeypatch, labels_by_issue: dict[int, set[str]]):
    """All requested issues resolve via ONE bulk call; the per-issue
    path must stay cold (asserted by raising)."""

    def fake_bulk(nums, repo):
        return {
            n: {
                "title": f"t{n}",
                "state": "OPEN",
                "body": "",
                "labels": set(labels_by_issue.get(n, set())),
            }
            for n in nums
        }

    monkeypatch.setattr(sync_mod, "fetch_issues_bulk", fake_bulk)

    def no_per_issue(*a, **kw):
        raise AssertionError("per-issue gh call made despite bulk success")

    monkeypatch.setattr(sync_mod, "issue_labels", no_per_issue)


def completed(args=("gh",), returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=list(args), returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeGh:
    """Records gh(...) calls and MUTATES a shared label store on
    ``issue edit --add-label/--remove-label`` — so apply-then-recompute
    tests exercise real idempotence, not a frozen fixture."""

    def __init__(self, labels_by_issue: dict[int, set[str]]):
        self.labels = labels_by_issue
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args, check=True, capture=True):
        self.calls.append(args)
        if args[:2] == ("issue", "edit"):
            num = int(args[2])
            store = self.labels.setdefault(num, set())
            for i, a in enumerate(args):
                if a == "--add-label":
                    store.add(args[i + 1])
                elif a == "--remove-label":
                    store.discard(args[i + 1])
        return completed(args=("gh", *args))

    def edits(self):
        return [c for c in self.calls if c[:2] == ("issue", "edit")]


@pytest.fixture(autouse=True)
def _reset_ledger_warn_flag(monkeypatch):
    """Same per-test pinning as test_ledger: the dual-write shim's
    warn-once flag is process-wide and tests share a process."""
    monkeypatch.setattr(state_mod, "_ledger_warn_emitted", False)


# --- the policy decision table -------------------------------------------------


def test_propose_action_full_matrix():
    """The whole binding policy in one place. GitHub is authoritative
    for human verdicts; local operational state is never clobbered;
    local verdicts missing on GitHub go outbound."""
    # Inbound: GitHub human verdict absent/stale locally.
    assert propose_action("verified", None) == ACTION_PULL_VERIFY
    assert propose_action("verified", "rejected") == ACTION_PULL_VERIFY
    # A GitHub verify is a NEW human verdict — it supersedes the stall.
    assert propose_action("verified", "stalled") == ACTION_PULL_VERIFY
    assert propose_action("rejected", None) == ACTION_PULL_REJECT
    assert propose_action("rejected", "verified") == ACTION_PULL_REJECT
    # Local-only operational state kept: still-rejected label is NOT a
    # new verdict, so the stall stands.
    assert propose_action("rejected", "stalled") == ACTION_KEEP
    # Outbound: local verdicts GitHub never heard about.
    assert propose_action(None, "verified") == ACTION_PUSH_VERIFIED
    assert propose_action(None, "rejected") == ACTION_PUSH_REJECTED
    # A stall's underlying human verdict is the rejection.
    assert propose_action(None, "stalled") == ACTION_PUSH_REJECTED
    # In sync / nothing to do.
    assert propose_action("verified", "verified") is None
    assert propose_action("rejected", "rejected") is None
    assert propose_action(None, None) is None
    # in-flight-fix is GitHub-side bookkeeping, not a verdict.
    assert propose_action("inflight", "rejected") is None
    assert propose_action("inflight", "stalled") is None
    assert propose_action("inflight", None) is None
    assert propose_action("inflight", "verified") == ACTION_PUSH_VERIFIED


# --- compute_drift: both directions, one bulk call ------------------------------


def test_compute_drift_detects_both_directions(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    # GitHub: #14 verified; #15 unlabeled. Local: #14 absent; #15 rejected.
    patch_labels(monkeypatch, {14: {"review:verified"}, 15: set()})
    write_state(
        tmp_path,
        {15: {"status": "rejected", "verdict_ts": "2026-06-01T10:00:00-04:00",
              "notes": "fix the hypothesis"}},
    )

    report = compute_drift(cfg, chapter=14)

    assert report.issues_checked == 2
    by_issue = {r.issue: r for r in report.rows}
    assert by_issue[14].action == ACTION_PULL_VERIFY
    assert by_issue[14].github_status == "verified"
    assert by_issue[14].local_status is None
    assert by_issue[14].chapter == 14
    assert by_issue[15].action == ACTION_PUSH_REJECTED
    assert by_issue[15].github_status is None
    assert by_issue[15].local_status == "rejected"
    # Both rows are actionable; none are keep/skip.
    assert len(report.actionable) == 2


def test_compute_drift_in_sync_is_empty(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    patch_labels(monkeypatch, {14: {"review:verified"}, 15: {"review:rejected"}})
    write_state(
        tmp_path,
        {
            14: {"status": "verified", "verdict_ts": "2026-06-01T10:00:00-04:00"},
            15: {"status": "rejected", "verdict_ts": "2026-06-01T11:00:00-04:00",
                 "notes": "real notes"},
        },
    )
    report = compute_drift(cfg, chapter=14)
    assert report.rows == []
    assert report.issues_checked == 2


def test_compute_drift_per_issue_fallback_and_skip(tmp_path, monkeypatch, capsys):
    """Bulk failure → printed warning + per-issue fallback; an issue
    whose labels are unfetchable on BOTH paths becomes a skip row (with
    local state) or no row at all (without)."""
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(sync_mod, "fetch_issues_bulk", lambda nums, repo: None)
    monkeypatch.setattr(
        sync_mod,
        "issue_labels",
        lambda num, repo: {"review:verified"} if num == 14 else None,
    )
    write_state(
        tmp_path,
        {15: {"status": "rejected", "verdict_ts": "2026-06-01T10:00:00-04:00",
              "notes": "n"}},
    )

    report = compute_drift(cfg, chapter=14)

    assert "bulk GraphQL issue fetch failed" in capsys.readouterr().err
    by_issue = {r.issue: r for r in report.rows}
    assert by_issue[14].action == ACTION_PULL_VERIFY  # per-issue fallback worked
    assert by_issue[15].action == ACTION_SKIP         # unknown labels: never act
    assert report.actionable == [by_issue[14]]
    # Apply must not touch the skip row.
    apply_drift(cfg, [by_issue[15]], push_labels=True)
    assert load_state(cfg).get(15).status == "rejected"


def test_project_wide_includes_unregistered_state_entries(tmp_path, monkeypatch):
    """chapter=None sweeps state.json orphans (issues in no registry) —
    exactly the entries a reconciliation job exists to surface."""
    cfg = make_cfg(tmp_path)
    patch_labels(monkeypatch, {14: set(), 15: set(), 99: set()})
    write_state(
        tmp_path,
        {99: {"status": "verified", "verdict_ts": "2026-06-01T10:00:00-04:00"}},
    )

    report = compute_drift(cfg, chapter=None)

    assert report.issues_checked == 3
    (row,) = report.rows
    assert (row.issue, row.action, row.chapter) == (99, ACTION_PUSH_VERIFIED, None)


# --- stalled preservation --------------------------------------------------------


def test_stalled_entry_is_kept_verbatim(tmp_path, monkeypatch):
    """A stalled local entry whose GitHub label still says rejected is
    reported as 'local-only state (kept)' and left byte-identical by
    apply — attempts / notes / last_iteration_ts survive."""
    cfg = make_cfg(tmp_path)
    patch_labels(monkeypatch, {14: {"review:rejected"}, 15: set()})
    state_path = write_state(
        tmp_path,
        {14: {"status": "stalled", "verdict_ts": "2026-06-01T10:00:00-04:00",
              "notes": "the human's real notes",
              "last_iteration_ts": "2026-06-02T09:00:00-04:00",
              "attempts": 3}},
    )
    before = state_path.read_text()

    report = compute_drift(cfg, chapter=14)
    (row,) = report.rows
    assert row.action == ACTION_KEEP
    assert row.detail == "local-only state (kept)"
    assert (row.github_status, row.local_status) == ("rejected", "stalled")
    assert report.actionable == []  # kept rows are informational, not drift

    summary = apply_drift(cfg, report.rows, push_labels=True)
    assert (summary.pulled, summary.pushed, summary.deferred) == (0, 0, 0)
    assert state_path.read_text() == before


# --- dry-run makes no writes ------------------------------------------------------


def test_dry_run_makes_no_writes(tmp_path, monkeypatch, capsys):
    """The default `marathon review sync` is a pure read: drift in both
    directions present, yet state.json is untouched, no verdicts.jsonl,
    no ledger db, and zero gh `issue edit` calls."""
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(sync_mod, "load_config", lambda: cfg)
    fake_gh = FakeGh({})
    monkeypatch.setattr(sync_mod, "gh", fake_gh)
    patch_labels(monkeypatch, {14: {"review:verified"}, 15: set()})
    state_path = write_state(
        tmp_path,
        {15: {"status": "rejected", "verdict_ts": "2026-06-01T10:00:00-04:00",
              "notes": "n"}},
    )
    before = state_path.read_text()

    cmd_sync(SimpleNamespace(chapter=14, apply=False, push_labels=False))

    out = capsys.readouterr().out
    assert "drift" in out and "dry-run: nothing changed" in out
    # Both directions rendered in the table.
    assert "pull: record verification" in out
    assert "push: gh label" in out
    assert state_path.read_text() == before
    assert not (tmp_path / ".marathon" / "review" / "verdicts.jsonl").exists()
    assert not (tmp_path / ".marathon" / "marathon.db").exists()
    assert fake_gh.edits() == []


def test_push_labels_without_apply_is_refused(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(sync_mod, "load_config", lambda: cfg)
    with pytest.raises(SystemExit):
        cmd_sync(SimpleNamespace(chapter=None, apply=False, push_labels=True))


# --- apply: inbound provenance + parking ------------------------------------------


def test_apply_inbound_tags_source_sync(tmp_path, monkeypatch):
    """Applied verdicts go through the real record_* path (state.json +
    ledger dual-write + tracked JSONL) but carry source='sync', and a
    pulled rejection is parked so the daemon never prompts Aristotle
    with the placeholder notes."""
    cfg = make_cfg(tmp_path)
    # GitHub: #14 verified, #15 rejected. Local: empty.
    patch_labels(monkeypatch, {14: {"review:verified"}, 15: {"review:rejected"}})

    report = compute_drift(cfg, chapter=14)
    summary = apply_drift(cfg, report.rows, push_labels=False)
    assert (summary.pulled, summary.pushed, summary.deferred) == (2, 0, 0)

    # Legacy read-side truth got both verdicts.
    state = load_state(cfg)
    assert state.get(14).status == "verified"
    rejected = state.get(15)
    assert rejected.status == "rejected"
    assert "synced from GitHub" in rejected.notes
    # Parked: placeholder notes must never become an Aristotle prompt.
    assert rejected.needs_iteration() is False

    # Tracked JSONL: one line per verdict, source='sync' on every line.
    jsonl = (tmp_path / ".marathon" / "review" / "verdicts.jsonl").read_text()
    records = [json.loads(line) for line in jsonl.splitlines()]
    assert {(r["issue"], r["verdict"]) for r in records} == {
        (14, "verified"), (15, "rejected"),
    }
    assert all(r["source"] == "sync" for r in records)

    # Ledger history rows are sync-tagged too (never 'cli').
    db = tmp_path / ".marathon" / "marathon.db"
    with sqlite3.connect(db) as conn:
        events = conn.execute(
            "SELECT issue_num, verdict, source FROM verdict_events ORDER BY issue_num"
        ).fetchall()
        issues = dict(
            conn.execute("SELECT issue_num, status FROM issues").fetchall()
        )
    assert events == [(14, "verified", "sync"), (15, "rejected", "sync")]
    assert issues == {14: "verified", 15: "rejected"}


def test_sync_source_swap_is_restored_after_apply(tmp_path, monkeypatch):
    """The source-tagging context manager must not leak: a normal CLI
    verdict recorded right after an apply is tagged 'cli' again."""
    cfg = make_cfg(tmp_path)
    patch_labels(monkeypatch, {14: {"review:verified"}, 15: set()})
    apply_drift(cfg, compute_drift(cfg, chapter=14).rows)

    state_mod.record_rejection(cfg, 15, "real human notes")

    jsonl = (tmp_path / ".marathon" / "review" / "verdicts.jsonl").read_text()
    records = [json.loads(line) for line in jsonl.splitlines()]
    assert [r["source"] for r in records] == ["sync", "cli"]


# --- apply then re-run: clean ------------------------------------------------------


def test_apply_then_second_run_reports_no_drift(tmp_path, monkeypatch):
    """Idempotence in both directions: after --apply --push-labels, an
    immediate second compute_drift yields no actionable rows (the fake
    gh mutates the label store, so the outbound push is really visible
    to the second run)."""
    cfg = make_cfg(tmp_path)
    labels = {14: {"review:verified"}, 15: set()}  # 14: inbound; 15: outbound
    patch_labels(monkeypatch, labels)
    fake_gh = FakeGh(labels)
    monkeypatch.setattr(sync_mod, "gh", fake_gh)
    write_state(
        tmp_path,
        {15: {"status": "verified", "verdict_ts": "2026-06-01T10:00:00-04:00"}},
    )

    first = compute_drift(cfg, chapter=14)
    assert {r.action for r in first.rows} == {ACTION_PULL_VERIFY, ACTION_PUSH_VERIFIED}

    summary = apply_drift(cfg, first.rows, push_labels=True)
    assert (summary.pulled, summary.pushed, summary.deferred) == (1, 1, 0)
    # Outbound edit used cmd_verify's label shape: add verified, drop rejected.
    (edit,) = fake_gh.edits()
    assert edit[2] == "15"
    assert edit[5:9] == ("--add-label", "review:verified",
                         "--remove-label", "review:rejected")
    assert labels[15] == {"review:verified"}

    second = compute_drift(cfg, chapter=14)
    assert second.rows == []
    assert second.actionable == []


def test_outbound_pushes_deferred_without_flag(tmp_path, monkeypatch):
    """--apply without --push-labels performs inbound updates only;
    outbound rows are counted as deferred and GitHub is untouched."""
    cfg = make_cfg(tmp_path)
    labels = {14: {"review:verified"}, 15: set()}
    patch_labels(monkeypatch, labels)
    fake_gh = FakeGh(labels)
    monkeypatch.setattr(sync_mod, "gh", fake_gh)
    write_state(
        tmp_path,
        {15: {"status": "rejected", "verdict_ts": "2026-06-01T10:00:00-04:00",
              "notes": "real notes"}},
    )

    report = compute_drift(cfg, chapter=14)
    summary = apply_drift(cfg, report.rows, push_labels=False)

    assert (summary.pulled, summary.pushed, summary.deferred) == (1, 0, 1)
    assert fake_gh.edits() == []
    assert labels[15] == set()
    # The deferred outbound row reappears on the next run, still pushable.
    again = compute_drift(cfg, chapter=14)
    assert [r.action for r in again.rows] == [ACTION_PUSH_REJECTED]

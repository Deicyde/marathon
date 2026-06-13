"""Offline tests for the Phase-5b invalidation engine + amnesty review
flow (marathon.audit.invalidate, the batched tracker helper, and the
tier-aware review commands).

Binding rulings under test (docs/marathon-v2-plan.md §2 ruling 4;
crit-feas-verification-surface-first §3 + §5):

* each invalidation class is surfaced — the decl's own fingerprint
  changed, a pinned cone member changed (NAMED in the report), the decl
  went absent — while cross-toolchain wholesale staleness is a SEPARATE
  list (amnesty's job, not a per-decl alarm);
* a dry run writes nothing (no gh call, no breaker file);
* apply does EXACTLY ONE tracker body rewrite for N flips (the
  write-storm ruling: assert the gh-body-edit call count == 1) and the
  comment circuit breaker dedups a repeated notice;
* re-pin APPENDS (event count grows, old events still present) and
  REFUSES decls absent/unknown in the current snapshot;
* `review list --tiers` degrades to '-' with one note when no snapshot
  exists, and `review next --min-tier` skips below-floor issues with a
  printed reason — both ZERO behavior change without the new flags.

No subprocesses, no network, no Lean toolchain: every gh boundary is
monkeypatched and snapshots are built in memory.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

import marathon.audit.invalidate as invalidate
import marathon.review.review as review
import marathon.review.tracker as tracker
from marathon.audit.engine import save_snapshot
from marathon.audit.invalidate import (
    CAUSE_ABSENT,
    CAUSE_CONE,
    CAUSE_TYPE,
    INVALIDATION_COMMENT_DAILY_CAP,
    compute_invalidations,
    notify_invalidations,
)
from marathon.audit.lean_template import DEFAULT_TRUSTED_PREFIXES
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.audit.trust import record_revocation, record_spec_verdict
from marathon.ledger import Ledger
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels


# --- builders (shared style with test_trust) ---------------------------------


TOOLCHAIN = "leanprover/lean4:v4.28.0"
BUMPED = "leanprover/lean4:v4.29.0"


def mk_decl(
    name="Foo.bar", kind="theorem", module="Foo", status="ok",
    type_pp="Nat", value_pp=None, cone=(), axioms=(), has_sorry=False,
    tags=(), reason=None,
) -> DeclAudit:
    return DeclAudit(
        name=name, kind=kind, module=module, status=status, type_pp=type_pp,
        value_pp=value_pp, cone=list(cone), axioms=list(axioms),
        has_sorry=has_sorry, tags=list(tags), reason=reason,
    )


def mk_snapshot(decls, *, repo_dir="/r", failures=(), **kw) -> AuditSnapshot:
    defaults = dict(
        repo_dir=repo_dir, modules=["Foo"], toolchain=TOOLCHAIN,
        lean_version="4.28.0", package_revs={},
        trusted_prefixes=list(DEFAULT_TRUSTED_PREFIXES),
        created_at="2026-06-12T00:00:00+00:00", decls=list(decls),
        failures=list(failures),
    )
    defaults.update(kw)
    return AuditSnapshot(**defaults)


def cone_pair():
    helper = mk_decl(name="Foo.helper", kind="def", type_pp="Nat → Nat",
                     value_pp="fun n => n")
    main = mk_decl(name="Foo.main", type_pp="Foo.helper 1 = 1",
                   cone=["Foo.helper"])
    return helper, main


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger.for_repo(tmp_path)


def make_cfg(tmp_path: Path) -> ReviewConfig:
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
                entries=[(101, "main thm"), (102, "helper def")],
            ),
        },
    )


# A parent tracker body with one chapter section, two decl lines, both
# already verified (🟡) so an invalidation can flip them back to 🟠.
PARENT_BODY = (
    "# LeeSM Tracker\n\n"
    "### Chapter 14: Stuff\n"
    "- 🟡 #101 main thm\n"
    "- 🟡 #102 helper def\n"
)


@pytest.fixture
def gh_spy(monkeypatch):
    """Capture every tracker ``gh`` call and serve a fixed parent body.

    Returns a list of the gh argv tuples — the test asserts how many
    ``issue edit`` body rewrites happened (must be exactly one per
    batched apply)."""
    calls: list[tuple] = []

    def fake_gh(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=list(args), returncode=0,
                                           stdout="", stderr="")

    monkeypatch.setattr(tracker, "gh", fake_gh)
    monkeypatch.setattr(tracker, "issue_body", lambda num, repo: PARENT_BODY)
    return calls


@pytest.fixture
def comment_spy(monkeypatch):
    """Capture invalidation marker-comment gh subprocess calls."""
    calls: list[list] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="",
                                           stderr="")

    monkeypatch.setattr(invalidate.subprocess, "run", fake_run)
    return calls


# --- compute_invalidations: each class ---------------------------------------


def test_own_fingerprint_change_is_type_changed(ledger):
    old = mk_snapshot([mk_decl(type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.bar", old, issue_num=101)
    new = mk_snapshot([mk_decl(type_pp="Int")])
    report = compute_invalidations(old, new, ledger)
    assert [i.decl_name for i in report.invalidations] == ["Foo.bar"]
    inv = report.invalidations[0]
    assert inv.cause == CAUSE_TYPE
    assert inv.tier_claimed == "T2"
    assert inv.tier_now == "T1"
    assert inv.issue_num == 101
    assert report.stale_toolchain == []


def test_cone_member_change_names_the_member(ledger):
    helper, main = cone_pair()
    old = mk_snapshot([helper, main])
    record_spec_verdict(ledger, "Foo.main", old, issue_num=101)
    changed_helper = mk_decl(name="Foo.helper", kind="def",
                             type_pp="Int → Int", value_pp="fun n => n")
    new = mk_snapshot([changed_helper, main])
    report = compute_invalidations(old, new, ledger)
    (inv,) = report.invalidations
    assert inv.cause == CAUSE_CONE
    assert inv.cone_member == "Foo.helper"  # the NAMED upstream card
    assert "Foo.helper" in inv.detail


def test_cone_member_vanishing_is_named_cone_change(ledger):
    helper, main = cone_pair()
    record_spec_verdict(ledger, "Foo.main", mk_snapshot([helper, main]),
                        issue_num=101)
    report = compute_invalidations(
        mk_snapshot([helper, main]), mk_snapshot([main]), ledger
    )
    (inv,) = report.invalidations
    assert inv.cause == CAUSE_CONE
    assert inv.cone_member == "Foo.helper"


def test_absent_decl_is_absent_class(ledger):
    decl = mk_decl(name="Foo.gone")
    record_spec_verdict(ledger, "Foo.gone", mk_snapshot([decl]), issue_num=101)
    other = mk_decl(name="Foo.present")
    report = compute_invalidations(
        mk_snapshot([decl]), mk_snapshot([other]), ledger
    )
    (inv,) = report.invalidations
    assert inv.cause == CAUSE_ABSENT
    assert inv.tier_now == "UNKNOWN"


def test_stale_toolchain_is_separated_not_a_per_decl_invalidation(ledger):
    """A clean cross-toolchain bump (pins still match) is wholesale
    staleness for amnesty — it must NOT appear as a per-decl meaning
    alarm."""
    snap = mk_snapshot([mk_decl()])
    record_spec_verdict(ledger, "Foo.bar", snap, issue_num=101)
    bumped = mk_snapshot([mk_decl()], toolchain=BUMPED)
    report = compute_invalidations(snap, bumped, ledger)
    assert report.invalidations == []
    assert report.stale_toolchain == ["Foo.bar"]


def test_cross_toolchain_real_change_still_separated(ledger):
    """Even a pp mismatch across toolchains is unverifiable, not a
    detected change — so it stays in stale_toolchain, never a type alarm
    (the binding 'never silent invalidation under a change claim'
    ruling)."""
    snap = mk_snapshot([mk_decl(type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.bar", snap, issue_num=101)
    bumped = mk_snapshot([mk_decl(type_pp="Nat ")], toolchain=BUMPED)
    report = compute_invalidations(snap, bumped, ledger)
    assert report.invalidations == []
    assert report.stale_toolchain == ["Foo.bar"]


def test_revoked_verdict_is_not_invalidated(ledger):
    """A verdict the human already walked back has nothing to
    invalidate."""
    old = mk_snapshot([mk_decl(type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.bar", old, issue_num=101)
    record_revocation(ledger, "Foo.bar")
    new = mk_snapshot([mk_decl(type_pp="Int")])
    report = compute_invalidations(old, new, ledger)
    assert report.invalidations == []
    assert report.stale_toolchain == []


def test_still_matching_verdict_is_not_invalidated(ledger):
    snap = mk_snapshot([mk_decl()])
    record_spec_verdict(ledger, "Foo.bar", snap, issue_num=101)
    report = compute_invalidations(snap, mk_snapshot([mk_decl()]), ledger)
    assert not report  # __bool__ False — nothing to surface


# --- notify_invalidations: dry-run writes nothing ----------------------------


def test_dry_run_writes_nothing(tmp_path, ledger, gh_spy, comment_spy):
    cfg = make_cfg(tmp_path)
    old = mk_snapshot([mk_decl(type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.bar", old, issue_num=101)
    new = mk_snapshot([mk_decl(type_pp="Int")])
    report = compute_invalidations(old, new, ledger)

    out = notify_invalidations(cfg, report, apply=False)
    assert "dry run" in out
    assert gh_spy == []  # no tracker edit
    assert comment_spy == []  # no comment
    assert not invalidate._breaker_path(tmp_path).exists()  # no breaker file


# --- apply: exactly ONE tracker rewrite for N flips --------------------------


def test_apply_does_exactly_one_tracker_rewrite_for_n_flips(
    tmp_path, ledger, gh_spy, comment_spy
):
    cfg = make_cfg(tmp_path)
    # Two decls, two distinct issues, both verified then both invalidated.
    old = mk_snapshot([mk_decl(name="Foo.a", type_pp="Nat"),
                       mk_decl(name="Foo.b", type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.a", old, issue_num=101)
    record_spec_verdict(ledger, "Foo.b", old, issue_num=102)
    new = mk_snapshot([mk_decl(name="Foo.a", type_pp="Int"),
                       mk_decl(name="Foo.b", type_pp="Int")])
    report = compute_invalidations(old, new, ledger)
    assert len(report.invalidations) == 2

    notify_invalidations(cfg, report, apply=True)
    # The write-storm ruling: ONE `gh issue edit` body rewrite for both
    # flips, not one per decl.
    edits = [c for c in gh_spy if len(c) >= 2 and c[:2] == ("issue", "edit")]
    assert len(edits) == 1
    # Two marker comments (one per affected issue).
    comments = [c for c in comment_spy if c[:3] == ["gh", "issue", "comment"]]
    assert len(comments) == 2


def test_two_decls_one_issue_flip_that_issue_once(
    tmp_path, ledger, gh_spy, comment_spy
):
    """Two invalidated decls sharing one sub-issue still flip that one
    tracker line — the body rewrite is single and the issue is deduped."""
    cfg = make_cfg(tmp_path)
    old = mk_snapshot([mk_decl(name="Foo.a", type_pp="Nat"),
                       mk_decl(name="Foo.b", type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.a", old, issue_num=101)
    record_spec_verdict(ledger, "Foo.b", old, issue_num=101)
    new = mk_snapshot([mk_decl(name="Foo.a", type_pp="Int"),
                       mk_decl(name="Foo.b", type_pp="Int")])
    report = compute_invalidations(old, new, ledger)
    notify_invalidations(cfg, report, apply=True)
    edits = [c for c in gh_spy if len(c) >= 2 and c[:2] == ("issue", "edit")]
    assert len(edits) == 1


# --- circuit breaker dedups a repeated comment -------------------------------


def test_comment_breaker_dedups_repeated_notice(
    tmp_path, ledger, gh_spy, comment_spy
):
    cfg = make_cfg(tmp_path)
    old = mk_snapshot([mk_decl(type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.bar", old, issue_num=101)
    new = mk_snapshot([mk_decl(type_pp="Int")])
    report = compute_invalidations(old, new, ledger)

    notify_invalidations(cfg, report, apply=True)
    assert len([c for c in comment_spy if c[1] == "issue"]) == 1
    assert invalidate._breaker_path(tmp_path).exists()

    # Re-running with the identical report posts NO second comment — the
    # content-hash signature is already in the breaker.
    comment_spy.clear()
    notify_invalidations(cfg, report, apply=True)
    assert comment_spy == []


def test_comment_daily_cap_caps_distinct_signatures(
    tmp_path, ledger, gh_spy, monkeypatch, capsys
):
    """Distinct signatures for one issue stop posting once the per-issue
    daily cap is reached (mirrors landing's bounce cap). Distinct decls
    (the detail names each decl) give distinct signatures that the
    content-hash dedup will not collapse — only the daily cap stops
    them."""
    cfg = make_cfg(tmp_path)
    calls: list[list] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="",
                                           stderr="")

    monkeypatch.setattr(invalidate.subprocess, "run", fake_run)
    n = INVALIDATION_COMMENT_DAILY_CAP + 2
    # n distinct decls, all pinned to the SAME issue #101, all
    # invalidated at once → n distinct signatures, one issue.
    old = mk_snapshot([mk_decl(name=f"Foo.d{i}", type_pp="Nat")
                       for i in range(n)])
    for i in range(n):
        record_spec_verdict(ledger, f"Foo.d{i}", old, issue_num=101)
    new = mk_snapshot([mk_decl(name=f"Foo.d{i}", type_pp="Int")
                       for i in range(n)])
    report = compute_invalidations(old, new, ledger)
    assert len(report.invalidations) == n
    notify_invalidations(cfg, report, apply=True)
    posted = [c for c in calls if c[1] == "issue"]
    assert len(posted) == INVALIDATION_COMMENT_DAILY_CAP


def test_failed_gh_post_not_counted_against_breaker(
    tmp_path, ledger, gh_spy, monkeypatch
):
    """A failed gh comment is best-effort: it is NOT recorded in the
    breaker, so the next run retries the notice."""
    cfg = make_cfg(tmp_path)
    old = mk_snapshot([mk_decl(type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.bar", old, issue_num=101)
    new = mk_snapshot([mk_decl(type_pp="Int")])
    report = compute_invalidations(old, new, ledger)

    fail = {"n": 0}

    def flaky_run(cmd, **kwargs):
        fail["n"] += 1
        rc = 1 if fail["n"] == 1 else 0
        return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout="",
                                           stderr="boom")

    monkeypatch.setattr(invalidate.subprocess, "run", flaky_run)
    notify_invalidations(cfg, report, apply=True)
    # First post failed → no breaker file persisted (nothing posted).
    assert not invalidate._breaker_path(tmp_path).exists()
    # Retry succeeds — the notice was not suppressed by the breaker.
    notify_invalidations(cfg, report, apply=True)
    assert invalidate._breaker_path(tmp_path).exists()


# --- repin: appends, refuses unknowns ----------------------------------------


def _repin_args(tmp_path, *, decl=None, yes=False):
    return argparse.Namespace(
        repo_dir=tmp_path, decl=decl, yes=yes,
    )


def test_repin_appends_new_event_and_preserves_old(tmp_path, ledger, capsys):
    from marathon.__main__ import _run_audit_repin

    repo = tmp_path
    old = mk_snapshot([mk_decl(type_pp="Nat")], repo_dir=str(repo))
    record_spec_verdict(ledger, "Foo.bar", old, issue_num=101)
    # The current snapshot has the changed type — the verdict is stale.
    new = mk_snapshot([mk_decl(type_pp="Int")], repo_dir=str(repo))
    save_snapshot(old, repo)   # becomes previous.json on next save
    save_snapshot(new, repo)   # latest.json; old rotates to previous

    before = ledger.decl_verdict_events("Foo.bar")
    assert len(before) == 1

    _run_audit_repin(_repin_args(repo, yes=True))
    after = ledger.decl_verdict_events("Foo.bar")
    # APPEND, not mutate: count grew, the original event survives.
    assert len(after) == 2
    assert after[-1].id == before[0].id  # oldest unchanged
    assert after[0].source == "repin"
    # Re-pinned to the CURRENT (Int) fingerprint, so the verdict is live
    # again.
    assert after[0].fingerprint_type == new.by_name()["Foo.bar"].fingerprint_type
    report = compute_invalidations(old, new, ledger)
    assert report.invalidations == []


def test_repin_dry_run_writes_nothing(tmp_path, ledger, capsys):
    from marathon.__main__ import _run_audit_repin

    repo = tmp_path
    old = mk_snapshot([mk_decl(type_pp="Nat")], repo_dir=str(repo))
    record_spec_verdict(ledger, "Foo.bar", old, issue_num=101)
    new = mk_snapshot([mk_decl(type_pp="Int")], repo_dir=str(repo))
    save_snapshot(old, repo)
    save_snapshot(new, repo)

    _run_audit_repin(_repin_args(repo, yes=False))
    out = capsys.readouterr().out
    assert "dry run" in out
    assert len(ledger.decl_verdict_events("Foo.bar")) == 1  # nothing written


def test_repin_refuses_absent_decl(tmp_path, ledger, capsys):
    from marathon.__main__ import _run_audit_repin

    repo = tmp_path
    old = mk_snapshot([mk_decl(name="Foo.gone", type_pp="Nat")],
                      repo_dir=str(repo))
    record_spec_verdict(ledger, "Foo.gone", old, issue_num=101)
    # Current snapshot no longer contains Foo.gone.
    new = mk_snapshot([mk_decl(name="Foo.present")], repo_dir=str(repo))
    save_snapshot(old, repo)
    save_snapshot(new, repo)

    _run_audit_repin(_repin_args(repo, yes=True))
    out = capsys.readouterr().out
    assert "refused" in out.lower()
    assert "Foo.gone" in out
    # Refused — no repin event appended.
    events = ledger.decl_verdict_events("Foo.gone")
    assert [e.source for e in events] == ["cli"]


def test_repin_refuses_vanished_cone_member_without_crashing(
    tmp_path, ledger, capsys
):
    """A re-pin of a decl whose pinned cone member VANISHED (a refactor
    deleted the helper lemma — the exact thing cone-pinning detects) must
    refuse that decl per the 'refuse absent/unknown' ruling, NAMING the
    missing member, instead of crashing with an uncaught TrustError after
    the table prints. The decl's own type is unchanged, so the bug was
    routing it to `pinnable`; the fix pre-checks the cone."""
    from marathon.__main__ import _run_audit_repin

    repo = tmp_path
    helper, main = cone_pair()
    old = mk_snapshot([helper, main], repo_dir=str(repo))
    record_spec_verdict(ledger, "Foo.main", old, issue_num=101)
    # Foo.helper vanishes; Foo.main's own type is unchanged.
    new = mk_snapshot([main], repo_dir=str(repo))
    save_snapshot(old, repo)
    save_snapshot(new, repo)

    _run_audit_repin(_repin_args(repo, yes=True))  # must NOT raise
    out = capsys.readouterr().out
    # Refused, naming the missing cone member — not the misleading
    # old-fp==new-fp 'cone' row, and not a stack trace.
    assert "refused" in out.lower()
    assert "Foo.helper" in out
    assert "cannot re-pin" in out
    # Nothing pinnable, nothing written: the cli verdict is untouched.
    assert [e.source for e in ledger.decl_verdict_events("Foo.main")] == ["cli"]


def test_repin_batch_no_partial_write_on_vanished_cone(tmp_path, ledger, capsys):
    """In a multi-decl batch, a vanished-cone decl must not abort the loop
    after earlier decls were already re-pinned (the partial-amnesty bug):
    the clean own-type-change decl is re-pinned, the vanished-cone decl is
    refused, and the command does not crash."""
    from marathon.__main__ import _run_audit_repin

    repo = tmp_path
    helper = mk_decl(name="Foo.helper", kind="def", type_pp="Nat → Nat",
                     value_pp="fun n => n")
    aaa = mk_decl(name="Foo.aaa", type_pp="Nat")
    zzz = mk_decl(name="Foo.zzz", type_pp="Foo.helper 1 = 1",
                  cone=["Foo.helper"])
    old = mk_snapshot([helper, aaa, zzz], repo_dir=str(repo))
    record_spec_verdict(ledger, "Foo.aaa", old, issue_num=101)
    record_spec_verdict(ledger, "Foo.zzz", old, issue_num=102)
    # Foo.aaa's own type changes (re-pinnable); Foo.helper vanishes, so
    # Foo.zzz's cone is unpinnable.
    new = mk_snapshot([mk_decl(name="Foo.aaa", type_pp="Int"), zzz],
                      repo_dir=str(repo))
    save_snapshot(old, repo)
    save_snapshot(new, repo)

    _run_audit_repin(_repin_args(repo, yes=True))  # must NOT raise
    out = capsys.readouterr().out
    assert "Foo.zzz" in out and "refused" in out.lower()
    # Foo.aaa re-pinned (the legitimate own-change), Foo.zzz untouched.
    assert [e.source for e in ledger.decl_verdict_events("Foo.aaa")] \
        == ["repin", "cli"]
    assert [e.source for e in ledger.decl_verdict_events("Foo.zzz")] == ["cli"]


def test_repin_unresolvable_decl_filter_errors(tmp_path, ledger, capsys):
    from marathon.__main__ import _run_audit_repin

    repo = tmp_path
    old = mk_snapshot([mk_decl(type_pp="Nat")], repo_dir=str(repo))
    record_spec_verdict(ledger, "Foo.bar", old, issue_num=101)
    new = mk_snapshot([mk_decl(type_pp="Int")], repo_dir=str(repo))
    save_snapshot(old, repo)
    save_snapshot(new, repo)

    with pytest.raises(SystemExit):
        _run_audit_repin(_repin_args(repo, decl=["nonexistent"], yes=True))


def test_repin_toolchain_only_kind(tmp_path, ledger, capsys):
    """A clean toolchain bump is re-pinnable as 'toolchain-only' (not a
    meaning change), pinning the verdict onto the new toolchain."""
    from marathon.__main__ import _run_audit_repin

    repo = tmp_path
    old = mk_snapshot([mk_decl()], repo_dir=str(repo))
    record_spec_verdict(ledger, "Foo.bar", old, issue_num=101)
    new = mk_snapshot([mk_decl()], repo_dir=str(repo), toolchain=BUMPED)
    save_snapshot(old, repo)
    save_snapshot(new, repo)

    _run_audit_repin(_repin_args(repo, yes=True))
    out = capsys.readouterr().out
    assert "toolchain-only" in out
    after = ledger.decl_verdict_events("Foo.bar")
    assert after[0].source == "repin"
    assert after[0].toolchain == BUMPED  # re-pinned onto the new toolchain


# --- review list --tiers degrades to '-' without a snapshot ------------------


def _list_args(chapter=14, tiers=False):
    return argparse.Namespace(chapter=chapter, tiers=tiers)


def _stub_review_cfg(monkeypatch, cfg):
    monkeypatch.setattr(review, "load_config", lambda *a, **k: cfg)


def test_list_tiers_degrades_to_dash_without_snapshot(
    tmp_path, monkeypatch, capsys
):
    cfg = make_cfg(tmp_path)
    _stub_review_cfg(monkeypatch, cfg)
    # No audit snapshot exists under tmp_path/.marathon/audit/.
    monkeypatch.setattr(
        review, "_bulk_registry_meta",
        lambda c, r: {
            101: {"labels": set(), "title": "main thm", "body": ""},
            102: {"labels": set(), "title": "helper def", "body": ""},
        },
    )
    review.cmd_list(_list_args(tiers=True))
    out = capsys.readouterr().out
    assert "no audit snapshot" in out  # the ONE degrade note
    assert "tier" in out  # header present
    # Both issues still listed (command works unchanged) and every row's
    # tier column renders '-'.
    assert "main thm" in out and "helper def" in out
    assert out.count(" -        ") == 2  # the '-' tier column on both rows


def test_list_without_tiers_flag_is_unchanged(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    _stub_review_cfg(monkeypatch, cfg)
    monkeypatch.setattr(
        review, "_bulk_registry_meta",
        lambda c, r: {
            101: {"labels": set(), "title": "main thm", "body": ""},
        },
    )
    review.cmd_list(_list_args(tiers=False))
    out = capsys.readouterr().out
    # No tier column, no snapshot note — byte-for-byte the old behavior.
    assert "tier" not in out
    assert "no audit snapshot" not in out


# --- review next --min-tier skips with a reason ------------------------------


def _next_args(chapter=14, min_tier=None):
    return argparse.Namespace(chapter=chapter, min_tier=min_tier)


def test_next_min_tier_skips_below_floor_with_reason(
    tmp_path, monkeypatch, capsys
):
    cfg = make_cfg(tmp_path)
    _stub_review_cfg(monkeypatch, cfg)
    repo = tmp_path
    # #101 cites Foo.lowthm (only T1); #102 cites Foo.hithm (T2).
    low = mk_decl(name="SomeProject.lowthm", module="SomeProject")
    hi = mk_decl(name="SomeProject.hithm", module="SomeProject")
    snap = mk_snapshot([low, hi], repo_dir=str(repo))
    save_snapshot(snap, repo)
    led = Ledger.for_repo(repo)
    record_spec_verdict(led, "SomeProject.hithm", snap, issue_num=102)

    bodies = {
        101: "```lean\ntheorem lowthm : Nat := by sorry\n```",
        102: "```lean\ntheorem hithm : Nat := by sorry\n```",
    }
    monkeypatch.setattr(
        review, "_bulk_registry_meta",
        lambda c, r: {
            n: {"labels": set(), "title": f"t{n}", "body": bodies[n]}
            for n in (101, 102)
        },
    )
    monkeypatch.setattr(review, "_show_issue", lambda c, n: print(f"SHOW {n}"))

    review.cmd_next(_next_args(min_tier="T2"))
    out = capsys.readouterr().out
    # #101 (T1) is skipped with a reason naming its decl; #102 (T2) shown.
    assert "skipping #101" in out
    assert "SomeProject.lowthm" in out
    assert "SHOW 102" in out


def test_next_without_min_tier_offers_first_unreviewed(
    tmp_path, monkeypatch, capsys
):
    cfg = make_cfg(tmp_path)
    _stub_review_cfg(monkeypatch, cfg)
    monkeypatch.setattr(
        review, "_bulk_registry_meta",
        lambda c, r: {
            101: {"labels": set(), "title": "t101", "body": ""},
            102: {"labels": set(), "title": "t102", "body": ""},
        },
    )
    monkeypatch.setattr(review, "_show_issue", lambda c, n: print(f"SHOW {n}"))
    review.cmd_next(_next_args(min_tier=None))
    out = capsys.readouterr().out
    # Unchanged: first unreviewed offered, no tier machinery touched.
    assert "SHOW 101" in out
    assert "skipping" not in out


def test_next_min_tier_without_snapshot_notes_and_offers(
    tmp_path, monkeypatch, capsys
):
    """A --min-tier with no snapshot must not stall Ch.12-style first
    reviews behind tooling that never ran: print the note, gate on
    nothing (every issue's tier is unknown), offer the first."""
    cfg = make_cfg(tmp_path)
    _stub_review_cfg(monkeypatch, cfg)
    monkeypatch.setattr(
        review, "_bulk_registry_meta",
        lambda c, r: {
            101: {"labels": set(), "title": "t101", "body": "```lean\ndef x\n```"},
        },
    )
    monkeypatch.setattr(review, "_show_issue", lambda c, n: print(f"SHOW {n}"))
    review.cmd_next(_next_args(min_tier="T2"))
    out = capsys.readouterr().out
    assert "no audit snapshot" in out
    assert "SHOW 101" in out  # offered, not stalled

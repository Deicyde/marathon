"""The LIVE verify flow promotes its cited declarations T1 → T2.

Before this wiring, ``decl_verdicts`` events were written ONLY by the
one-time ``--attest`` backfill; the deck-ship → ``cmd_verify`` path never
touched the trust ladder, so verified issues stayed stuck at T1.
``marathon.review.review._grant_t2_on_verify`` (called from ``cmd_verify``
right after ``record_verification``) closes that gap.

Contract under test (all best-effort — the verify already landed):

* a verify whose cited decls are present+elaborated in the snapshot →
  each computes ``compute_tier == 'T2'`` afterward, source='deck-verify';
* a cited decl absent / status=unknown in the snapshot → skipped, the
  in-snapshot decls still reach T2 (one bad decl never blocks the rest);
* no snapshot → no events written, no crash;
* re-verify is idempotent: a second pass appends NO duplicate event for
  an already-pinned decl.

The two gh/disk boundaries ``_grant_t2_on_verify`` touches —
``review.github.issue_body`` (the issue body) and
``audit.engine.load_snapshot`` (the on-disk snapshot) — are
monkeypatched; no network, no Lean toolchain, no subprocess. The ledger
is the real on-disk one (``Ledger.for_review_config`` derives
``<repo>/.marathon/marathon.db``), so the assertions read exactly what
the wiring wrote.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marathon.audit.lean_template import DEFAULT_TRUSTED_PREFIXES
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.audit.trust import compute_tier
from marathon.ledger import Ledger
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels
from marathon.review.review import _grant_t2_on_verify


TOOLCHAIN = "leanprover/lean4:v4.28.0"


# --- builders (same style as test_trust / test_audit_engine) -----------------


def mk_decl(
    name: str,
    *,
    kind="theorem",
    module="SomeProject",
    status="ok",
    type_pp="1 + 1 = 2",
    value_pp=None,
    cone=(),
    has_sorry=False,
    reason=None,
) -> DeclAudit:
    return DeclAudit(
        name=name, kind=kind, module=module, status=status,
        type_pp=type_pp, value_pp=value_pp, cone=list(cone),
        axioms=[], has_sorry=has_sorry, tags=[], reason=reason,
    )


def mk_snapshot(decls, *, repo_dir="/r", **kw) -> AuditSnapshot:
    defaults = dict(
        repo_dir=repo_dir,
        modules=["SomeProject"],
        toolchain=TOOLCHAIN,
        lean_version="4.28.0",
        package_revs={},
        trusted_prefixes=list(DEFAULT_TRUSTED_PREFIXES),
        created_at="2026-06-12T00:00:00+00:00",
        decls=list(decls),
        failures=[],
    )
    defaults.update(kw)
    return AuditSnapshot(**defaults)


def make_cfg(tmp_path: Path) -> ReviewConfig:
    return ReviewConfig(
        repo_dir=tmp_path,
        config_path=tmp_path / ".marathon" / "review" / "config.toml",
        github_repo="someone/SomeProject",
        parent_issue=1,
        target_path_template="SomeProject/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(
                chapter=14,
                entries=[(42, "Lemma 14.7")],
            ),
        },
    )


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    """The SAME db ``_grant_t2_on_verify`` opens via for_review_config."""
    cfg = make_cfg(tmp_path)
    return Ledger.for_review_config(cfg)


def install_boundaries(monkeypatch, *, body, snapshot):
    """Stub the two boundaries the wiring touches: the issue body (gh)
    and the on-disk snapshot loader."""
    monkeypatch.setattr(
        "marathon.review.github.issue_body",
        lambda issue_num, repo: body,
    )
    monkeypatch.setattr(
        "marathon.audit.engine.load_snapshot",
        lambda repo_dir, *a, **k: snapshot,
    )


# The cited decls are written namespace-unqualified in the issue body
# (as real review issues do) — the wiring's _resolve_decl resolves them
# to the unique snapshot suffix match.
BODY_TWO = (
    "### Lean signatures\n"
    "```lean\n"
    "theorem foo_thm : 1 + 1 = 2 := by simp\n"
    "def bar_def : Nat := 0\n"
    "```\n"
)


def snap_two() -> AuditSnapshot:
    return mk_snapshot([
        mk_decl("SomeProject.foo_thm", type_pp="1 + 1 = 2"),
        mk_decl("SomeProject.bar_def", kind="def", type_pp="Nat",
                value_pp="0"),
    ])


# --- tests -------------------------------------------------------------------


def test_verify_promotes_cited_decls_to_t2(tmp_path, ledger, monkeypatch):
    cfg = make_cfg(tmp_path)
    snap = snap_two()
    install_boundaries(monkeypatch, body=BODY_TWO, snapshot=snap)

    _grant_t2_on_verify(cfg, 42)

    for name in ("SomeProject.foo_thm", "SomeProject.bar_def"):
        events = ledger.decl_verdict_events(name)
        assert len(events) == 1, name
        ev = events[0]
        assert ev.tier_claimed == "T2"
        assert ev.verdict == "verified"
        assert ev.source == "deck-verify"
        assert ev.issue_num == 42
        # The whole point: the live tier is now T2, not T1.
        assert compute_tier(name, snap, ledger).tier == "T2"


def test_absent_or_unknown_decl_skipped_but_others_reach_t2(
    tmp_path, ledger, monkeypatch
):
    cfg = make_cfg(tmp_path)
    # foo_thm elaborated; gone_def is absent from the snapshot entirely;
    # flaky_def is present but status=unknown (did not elaborate).
    snap = mk_snapshot([
        mk_decl("SomeProject.foo_thm"),
        mk_decl("SomeProject.flaky_def", kind="def", status="unknown",
                type_pp=None, has_sorry=None, reason="elaboration failed"),
    ])
    body = (
        "```lean\n"
        "theorem foo_thm : 1 + 1 = 2 := rfl\n"
        "def gone_def : Nat := 0\n"
        "def flaky_def : Nat := 0\n"
        "```\n"
    )
    install_boundaries(monkeypatch, body=body, snapshot=snap)

    # Must not raise — a skipped decl never aborts the step.
    _grant_t2_on_verify(cfg, 42)

    # The in-snapshot, elaborated decl reaches T2 ...
    (ev,) = ledger.decl_verdict_events("SomeProject.foo_thm")
    assert ev.source == "deck-verify" and ev.tier_claimed == "T2"
    assert compute_tier("SomeProject.foo_thm", snap, ledger).tier == "T2"
    # ... while the absent and the unknown decls got no events.
    assert ledger.decl_verdict_events("SomeProject.gone_def") == []
    assert ledger.decl_verdict_events("SomeProject.flaky_def") == []


def test_no_snapshot_writes_nothing_and_does_not_crash(
    tmp_path, ledger, monkeypatch
):
    cfg = make_cfg(tmp_path)
    install_boundaries(monkeypatch, body=BODY_TWO, snapshot=None)

    # No snapshot → best-effort skip, never a crash, never an event.
    _grant_t2_on_verify(cfg, 42)

    assert ledger.all_decl_verdict_events() == {}


def test_re_verify_is_idempotent(tmp_path, ledger, monkeypatch):
    cfg = make_cfg(tmp_path)
    snap = snap_two()
    install_boundaries(monkeypatch, body=BODY_TWO, snapshot=snap)

    _grant_t2_on_verify(cfg, 42)
    # A second verify against the SAME snapshot fingerprints must append
    # no duplicate event (append-only stays clean).
    _grant_t2_on_verify(cfg, 42)

    for name in ("SomeProject.foo_thm", "SomeProject.bar_def"):
        assert len(ledger.decl_verdict_events(name)) == 1, name
        assert compute_tier(name, snap, ledger).tier == "T2"

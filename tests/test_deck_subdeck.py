"""Tests for the Phase-8 SUBDECK backend (marathon.deck.subdeck).

Contract under test (the subdeck task spec + the shared API contract):

* the per-(issue,decl) verdict store — records verify/reject/defer LOCALLY
  (no Aristotle / GitHub / ledger); a reject REQUIRES a non-empty note;
  ``clear_issue`` drops a shipped issue's store;
* ``build_subdeck`` — reuses cards.build_card_detail for the per-decl views,
  overlays the local verdict/note, orders UNREVIEWED-first then worst-tier
  then name, and computes progress (defer does NOT count reviewed),
  completeness, and the {kept, rejected, deferred} summary;
* ``compose_ship_note`` — the exact KEEP/FIX composite (pure);
* ``ship_issue`` — refuses an incomplete subdeck (NO cmd_*); all-verified
  routes cmd_verify (NOT cmd_reject); any-rejected routes cmd_reject with
  the composed note VERBATIM (NOT cmd_verify); clears the store on success;
* the server — GET /api/subdeck never fires a verdict; POST /api/decl-verdict
  records locally (never cmd_*) and needs the token; POST /api/ship is the
  only new irreversible route and needs the token.

Fully offline: gh (fetch_issues_bulk) and the committed verdict handlers
(cmd_verify / cmd_reject) are monkeypatched; no network, no lake, no
Aristotle, no real browser. The HTTP layer is driven through a threaded
server on an ephemeral 127.0.0.1 port via urllib.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, Thread

import pytest

import marathon.deck.cards as cards
import marathon.deck.server as deck_server
import marathon.deck.subdeck as subdeck
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels

REPO_SLUG = "example/Demo"


# ---------------------------------------------------------------------------
# Fixtures / builders (mirroring tests/test_deck_backend.py)
# ---------------------------------------------------------------------------


def make_cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo=REPO_SLUG,
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(
                chapter=14,
                entries=[(22, "Def Foo"), (23, "Lemma Bar"), (24, "Def Baz")],
            ),
        },
    )


def _decl(name, *, kind="theorem", type_pp="Type", cone=(), axioms=("propext",),
          has_sorry=False, value_pp=None, tags=()) -> DeclAudit:
    return DeclAudit(
        name=name,
        kind=kind,
        module="Demo.Chapter14",
        status="ok",
        type_pp=type_pp,
        value_pp=value_pp,
        cone=list(cone),
        axioms=list(axioms),
        has_sorry=has_sorry,
        tags=list(tags),
        reason=None,
    )


def make_snapshot(decls) -> AuditSnapshot:
    return AuditSnapshot(
        repo_dir="/repo",
        modules=["Demo.Chapter14"],
        toolchain="leanprover/lean4:v4.0.0",
        lean_version="4.0.0",
        package_revs={},
        trusted_prefixes=["Mathlib", "Init"],
        created_at="2026-06-14T00:00:00+00:00",
        decls=list(decls),
        failures=[],
    )


def install_snapshot(cfg, snapshot) -> None:
    from marathon.audit.engine import save_snapshot

    save_snapshot(snapshot, cfg.repo_dir)


def patch_bulk(monkeypatch, meta: dict) -> None:
    def fake_bulk(nums, repo):
        return {n: meta[n] for n in nums if n in meta}

    monkeypatch.setattr(cards, "fetch_issues_bulk", fake_bulk, raising=False)
    import marathon.review.github as gh_mod

    monkeypatch.setattr(gh_mod, "fetch_issues_bulk", fake_bulk)
    monkeypatch.setattr(gh_mod, "fetch_issues_rest_bulk", lambda nums, repo: {})


def patch_no_verdict_handlers(monkeypatch) -> dict:
    """Monkeypatch the committed verdict handlers to record (not perform)
    their calls. Returns a ``calls`` dict the test asserts against."""
    calls: dict = {}
    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: calls.setdefault("verify", args),
    )
    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda args: calls.setdefault("reject", args),
    )
    return calls


def _bundle_body(*decls: str) -> str:
    """An issue body citing several decls in one ```lean block (the parser
    the deck reuses), one short ``def`` per name."""
    lines = "\n".join(f"def {d} : T := ?" for d in decls)
    return f"### Lean signatures\n\n```lean\n{lines}\n```"


def make_bundle_cfg(tmp_path, monkeypatch, *, issue=22):
    """A cfg + installed snapshot where issue ``issue`` (registered in
    chapter 14) bundles three decls: Foo (T1), Bar (T0 — a non-whitelist
    axiom caps it), Baz (T1). Returns the cfg."""
    cfg = make_cfg(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", type_pp="Nat", value_pp="0"),
        _decl("Demo.Chapter14.Bar", kind="def", type_pp="Int", value_pp="1",
              axioms=["propext", "Demo.cheat"]),
        _decl("Demo.Chapter14.Baz", kind="theorem", type_pp="Foo = Foo"),
    ]))
    patch_bulk(monkeypatch, {
        issue: {
            "title": f"#{issue} bundle", "state": "OPEN",
            "body": _bundle_body("Foo", "Bar", "Baz"), "labels": set(),
        },
    })
    return cfg


def install_inspectable_bundle(cfg):
    """Replace the deliberately mixed-tier bundle with an all-T1 surface."""
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", type_pp="Nat", value_pp="0"),
        _decl("Demo.Chapter14.Bar", kind="def", type_pp="Int", value_pp="1"),
        _decl("Demo.Chapter14.Baz", kind="theorem", type_pp="Foo = Foo"),
    ]))


# ---------------------------------------------------------------------------
# Per-(issue,decl) verdict store
# ---------------------------------------------------------------------------


def test_store_records_and_reads_back(tmp_path):
    cfg = make_cfg(tmp_path)
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(
        cfg, 30, "Demo.Chapter14.Bar", "reject", note="needs ≥, not >"
    )
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Baz", "defer")

    recs = subdeck.issue_verdicts(cfg, 30)
    assert recs["Demo.Chapter14.Foo"]["verdict"] == "verify"
    assert recs["Demo.Chapter14.Bar"]["verdict"] == "reject"
    assert recs["Demo.Chapter14.Bar"]["note"] == "needs ≥, not >"
    assert recs["Demo.Chapter14.Baz"]["verdict"] == "defer"
    # Each record carries a timestamp.
    assert recs["Demo.Chapter14.Foo"]["ts"]
    # The sidecar lives in the .marathon/review dir (additive — not state.json).
    assert (cfg.repo_dir / subdeck.SUBDECK_RELPATH).is_file()


def test_store_reject_requires_note(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(ValueError):
        subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Bar", "reject")
    with pytest.raises(ValueError):
        subdeck.record_decl_verdict(
            cfg, 30, "Demo.Chapter14.Bar", "reject", note="   "
        )
    # Nothing was written for the refused decl.
    assert "Demo.Chapter14.Bar" not in subdeck.issue_verdicts(cfg, 30)


def test_store_unknown_verdict_refused(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(ValueError):
        subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "maybe")


def test_store_verdict_supersedes(tmp_path):
    cfg = make_cfg(tmp_path)
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "defer")
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "verify")
    recs = subdeck.issue_verdicts(cfg, 30)
    assert recs["Demo.Chapter14.Foo"]["verdict"] == "verify"


def test_clear_issue_drops_store(tmp_path):
    cfg = make_cfg(tmp_path)
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(cfg, 31, "Demo.Chapter14.Other", "verify")
    subdeck.clear_issue(cfg, 30)
    assert subdeck.issue_verdicts(cfg, 30) == {}
    # A sibling issue's store is untouched.
    assert subdeck.issue_verdicts(cfg, 31)["Demo.Chapter14.Other"]["verdict"] \
        == "verify"
    # Clearing an absent issue is a no-op.
    subdeck.clear_issue(cfg, 999)


def test_exact_rename_moves_ownership_but_requires_fresh_review(tmp_path):
    cfg = make_cfg(tmp_path)
    old = "Demo.Chapter14.OldFoo"
    new = "Demo.Chapter14.NewFoo"
    subdeck.record_decl_verdict(cfg, 22, old, "verify")
    subdeck.set_decl_lock(cfg, 22, old, True)

    result = subdeck.reconcile_decl_verdict_ownership(
        cfg, 22,
        renames=[{"old": old, "new": new}], splits=[], additions=[],
    )

    records = subdeck.issue_verdicts(cfg, 22)
    assert result["migrated"] == [{"old": old, "new": new}]
    assert old not in records
    assert records[new]["requires_review"] is True
    assert records[new]["renamed_from"] == old
    assert "verdict" not in records[new] and "locked" not in records[new]
    assert records[new]["previous_record"]["verdict"] == "verify"
    assert records[new]["previous_record"]["locked"] is True


def test_migrated_rename_is_unreviewed_in_the_built_subdeck(
    tmp_path, monkeypatch,
):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    old = "Demo.Chapter14.LegacyFoo"
    new = "Demo.Chapter14.Foo"
    subdeck.record_decl_verdict(cfg, 22, old, "verify")
    subdeck.set_decl_lock(cfg, 22, old, True)

    subdeck.reconcile_decl_verdict_ownership(
        cfg, 22,
        renames=[{"old": old, "new": new}], splits=[], additions=[],
    )

    built = subdeck.build_subdeck(cfg, 22)
    card = next(item for item in built["cards"] if item["name"] == new)
    assert card["verdict"] is None
    assert card["locked"] is False
    assert card["changed_since_review"] is True
    assert new not in built["summary"]["kept"]
    assert built["complete"] is False


def test_rename_collision_preserves_target_and_split_never_copies_parent(tmp_path):
    cfg = make_cfg(tmp_path)
    old = "Demo.Chapter14.Old"
    existing = "Demo.Chapter14.Existing"
    split_old = "Demo.Chapter14.Parent"
    children = ["Demo.Chapter14.Left", "Demo.Chapter14.Right"]
    subdeck.record_decl_verdict(cfg, 22, old, "reject", note="old note")
    subdeck.record_decl_verdict(cfg, 22, existing, "verify")
    subdeck.record_decl_verdict(cfg, 22, split_old, "verify")
    subdeck.set_decl_lock(cfg, 22, split_old, True)

    result = subdeck.reconcile_decl_verdict_ownership(
        cfg, 22,
        renames=[{"old": old, "new": existing}],
        splits=[{"old": split_old, "targets": children}],
        additions=[],
    )

    records = subdeck.issue_verdicts(cfg, 22)
    assert old not in records and split_old not in records
    assert records[existing]["verdict"] == "verify"
    assert result["collisions"] == [{"old": old, "new": existing}]
    assert result["split_archived"] == [
        {"old": split_old, "targets": children}
    ]
    assert all(child not in records for child in children)
    events = records[subdeck.RECONCILIATION_HISTORY_KEY]["events"]
    assert {event["kind"] for event in events} == {"rename", "split"}


def test_reconcile_addition_preserves_existing_surfaced_verdict_and_is_idempotent(
    tmp_path,
):
    cfg = make_cfg(tmp_path)
    name = "Demo.Chapter14.Surfaced"
    subdeck.record_decl_verdict(cfg, 22, name, "reject", note="still wrong")
    before = subdeck.issue_verdicts(cfg, 22)[name].copy()

    first = subdeck.reconcile_decl_verdict_ownership(
        cfg, 22, renames=[], splits=[], additions=[name],
    )
    second = subdeck.reconcile_decl_verdict_ownership(
        cfg, 22, renames=[], splits=[], additions=[name],
    )

    assert first["additions_preserved"] == [name]
    assert second["additions_preserved"] == [name]
    assert subdeck.issue_verdicts(cfg, 22)[name] == before


def test_concurrent_store_mutations_are_one_locked_read_modify_write(
    tmp_path, monkeypatch
):
    """Two ThreadingHTTPServer-style writers cannot both save stale snapshots.

    The first atomic save is held open while the second worker attempts its
    write. Under the transaction lock the second cannot even reach save; after
    release it reloads the first update and both declarations survive.
    """
    cfg = make_cfg(tmp_path)
    real_save = subdeck._save_store_unlocked
    first_at_save = Event()
    release_first = Event()
    second_at_save = Event()
    count_lock = Lock()
    save_count = 0

    def controlled_save(actual_cfg, store):
        nonlocal save_count
        with count_lock:
            save_count += 1
            ordinal = save_count
        if ordinal == 1:
            first_at_save.set()
            assert release_first.wait(5), "test did not release first writer"
        else:
            second_at_save.set()
        real_save(actual_cfg, store)

    monkeypatch.setattr(subdeck, "_save_store_unlocked", controlled_save)
    start = Barrier(3)

    def write(issue_num, decl):
        start.wait()
        return subdeck.record_decl_verdict(cfg, issue_num, decl, "verify")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(write, 30, "N.Left"),
            pool.submit(write, 31, "N.Right"),
        ]
        start.wait()
        assert first_at_save.wait(5)
        try:
            assert not second_at_save.wait(0.15), (
                "a second writer reached save before the first transaction ended"
            )
        finally:
            release_first.set()
        for future in futures:
            future.result(timeout=5)

    assert second_at_save.is_set()
    assert set(subdeck.issue_verdicts(cfg, 30)) == {"N.Left"}
    assert set(subdeck.issue_verdicts(cfg, 31)) == {"N.Right"}


def test_receipt_invalidation_failure_cannot_commit_new_verdict(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)

    def fail_clear(_cfg, _issue):
        raise OSError("receipt is not writable")

    monkeypatch.setattr(subdeck, "_clear_ship_receipt", fail_clear)
    with pytest.raises(OSError, match="receipt is not writable"):
        subdeck.record_decl_verdict(cfg, 30, "N.A", "verify")
    assert subdeck.issue_verdicts(cfg, 30) == {}


def test_ship_operation_id_is_stable_for_retry_and_changes_with_review(
    tmp_path,
):
    cfg = make_cfg(tmp_path)
    subdeck.record_decl_verdict(cfg, 30, "N.A", "reject", note="fix it")

    first = subdeck._ship_operation_id(cfg, 30, "reject", "composite note")
    assert first == subdeck._ship_operation_id(
        cfg, 30, "reject", "composite note"
    )

    subdeck.record_decl_verdict(cfg, 30, "N.A", "reject", note="fix it again")
    assert first != subdeck._ship_operation_id(
        cfg, 30, "reject", "composite note"
    )


# ---------------------------------------------------------------------------
# build_subdeck — overlay, ordering, progress / complete / summary
# ---------------------------------------------------------------------------


def test_build_subdeck_overlays_and_orders(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)

    sd = subdeck.build_subdeck(cfg, 22)
    # One card per cited decl, card_tier = worst over them (Bar is T0).
    assert {c["name"] for c in sd["cards"]} == {
        "Demo.Chapter14.Foo", "Demo.Chapter14.Bar", "Demo.Chapter14.Baz",
    }
    assert sd["card_tier"] == "T0"
    assert sd["issue"] == 22
    assert sd["chapter"] == 14
    # Navigation/dependency metadata survives CardDetail -> subdeck adaptation;
    # the frontend's open/deps controls must not receive an empty shape merely
    # because the UI switched from issue cards to per-declaration cards.
    assert sd["permalink"] == f"https://github.com/{REPO_SLUG}/issues/22"
    assert sd["deps"] == []
    # No verdicts yet → all cards unreviewed, ordered worst-tier first.
    assert all(c["verdict"] is None for c in sd["cards"])
    assert sd["cards"][0]["name"] == "Demo.Chapter14.Bar"  # T0 leads
    assert sd["progress"] == {"reviewed": 0, "total": 3}
    assert sd["complete"] is False

    # Review one decl (verify Foo): it moves AFTER the unreviewed cards.
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    sd = subdeck.build_subdeck(cfg, 22)
    by_name = {c["name"]: c for c in sd["cards"]}
    # The SERIALIZED card carries the PAST-TENSE wire verdict the frontend
    # renders/styles (dv-verified) — the store stays present-tense ('verify').
    assert by_name["Demo.Chapter14.Foo"]["verdict"] == "verified"
    # Unreviewed-first ordering: the two unreviewed (Bar, Baz) precede Foo.
    order = [c["name"] for c in sd["cards"]]
    assert order.index("Demo.Chapter14.Foo") == 2  # reviewed → last
    assert sd["progress"] == {"reviewed": 1, "total": 3}
    assert sd["complete"] is False


def test_build_subdeck_defer_not_counted_reviewed(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Bar", "verify")
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Baz", "defer")

    sd = subdeck.build_subdeck(cfg, 22)
    # Two verify + one defer → reviewed = 2 (defer does NOT count), incomplete.
    assert sd["progress"] == {"reviewed": 2, "total": 3}
    assert sd["complete"] is False
    assert sd["summary"]["kept"] == ["Demo.Chapter14.Bar", "Demo.Chapter14.Foo"]
    assert sd["summary"]["deferred"] == ["Demo.Chapter14.Baz"]
    assert sd["summary"]["rejected"] == []


def test_build_subdeck_complete_and_summary(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Bar", "reject", note="wrong bound"
    )
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Baz", "verify")

    sd = subdeck.build_subdeck(cfg, 22)
    assert sd["progress"] == {"reviewed": 3, "total": 3}
    assert sd["complete"] is True
    assert sd["summary"]["kept"] == ["Demo.Chapter14.Baz", "Demo.Chapter14.Foo"]
    assert sd["summary"]["rejected"] == [
        {"name": "Demo.Chapter14.Bar", "note": "wrong bound"},
    ]
    assert sd["summary"]["deferred"] == []
    # next_unreviewed_card is None when every decl is reviewed.
    assert subdeck.next_unreviewed_card(sd) is None


def test_next_unreviewed_card_points_at_first_open(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Bar", "verify")
    sd = subdeck.build_subdeck(cfg, 22)
    nxt = subdeck.next_unreviewed_card(sd)
    assert nxt is not None
    assert nxt["verdict"] is None
    assert nxt["name"] != "Demo.Chapter14.Bar"  # the reviewed one is skipped


# ---------------------------------------------------------------------------
# compose_ship_note — the exact KEEP / FIX format
# ---------------------------------------------------------------------------


def test_compose_ship_note_exact_format():
    summary = {
        "kept": ["A.Foo", "A.Baz"],
        "rejected": [
            {"name": "A.Bar", "note": "hypothesis h : x > 0 should be x ≥ 0"},
            {"name": "A.Qux", "note": "missing finiteness assumption"},
        ],
        "deferred": [],
    }
    note = subdeck.compose_ship_note(7, summary)
    expected = (
        "Per-declaration review of issue #7.\n"
        "\n"
        "KEEP these declarations exactly as written (human-verified — do NOT "
        "change their statements or signatures):\n"
        "- A.Foo\n"
        "- A.Baz\n"
        "\n"
        "FIX these declarations (rejected):\n"
        "- A.Bar: hypothesis h : x > 0 should be x ≥ 0\n"
        "- A.Qux: missing finiteness assumption\n"
        "\n"
        "Leave all other code untouched."
    )
    assert note == expected


# ---------------------------------------------------------------------------
# ship_issue — incomplete refusal; verify path; reject path (verbatim note)
# ---------------------------------------------------------------------------


def test_ship_refuses_incomplete_no_side_effect(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    # Only one of three decls reviewed → incomplete.
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is False
    assert "reason" in out
    # NO committed verdict handler ran.
    assert calls == {}
    # The store is intact (the refusal is non-destructive).
    assert subdeck.issue_verdicts(cfg, 22)["Demo.Chapter14.Foo"]["verdict"] \
        == "verify"


def test_ship_refuses_when_a_decl_only_deferred(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Bar", "verify")
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Baz", "defer")

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is False
    assert calls == {}  # a deferred decl keeps the issue from shipping


def test_ship_refuses_a_changed_review_surface(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    opened = subdeck.build_subdeck(cfg, 22)
    for decl in ("Foo", "Bar", "Baz"):
        subdeck.record_decl_verdict(
            cfg, 22, f"Demo.Chapter14.{decl}", "verify"
        )
    reviewed = subdeck.build_subdeck(cfg, 22)
    assert len(reviewed["review_token"]) == 64
    # The irreversible token binds the visible decisions as well as the source:
    # a stale tab must not ship choices made later in another tab.
    assert reviewed["review_token"] != opened["review_token"]
    changed = json.loads(json.dumps(reviewed))
    changed["cards"][0]["source_block"] = "-- Aristotle changed this"
    assert subdeck.subdeck_review_token(changed) != reviewed["review_token"]

    out = subdeck.ship_issue(
        cfg, 22, expected_review_token="0" * 64
    )

    assert out["ok"] is False
    assert "changed" in out["reason"].lower()
    assert calls == {}


def test_stale_tab_cannot_ship_another_tabs_changed_decisions(
    tmp_path, monkeypatch,
):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    for decl in ("Foo", "Bar", "Baz"):
        subdeck.record_decl_verdict(
            cfg, 22, f"Demo.Chapter14.{decl}", "verify"
        )
    tab_a = subdeck.build_subdeck(cfg, 22)
    assert tab_a["complete"] is True
    assert not tab_a["summary"]["rejected"]

    # Another tab changes the exact summary after tab A rendered its confirm UI.
    subdeck.record_decl_verdict(
        cfg,
        22,
        "Demo.Chapter14.Bar",
        "reject",
        note="the statement is too weak",
    )
    tab_b = subdeck.build_subdeck(cfg, 22)
    assert tab_b["review_token"] != tab_a["review_token"]
    assert tab_b["summary"]["rejected"] == [{
        "name": "Demo.Chapter14.Bar",
        "note": "the statement is too weak",
    }]

    out = subdeck.ship_issue(
        cfg, 22, expected_review_token=tab_a["review_token"]
    )

    assert out["ok"] is False
    assert "changed" in out["reason"].lower()
    assert calls == {}


def test_ship_receipt_only_suppresses_its_bound_review_generation(
    tmp_path, monkeypatch,
):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    old_token = "a" * 64
    new_token = "b" * 64
    subdeck._save_ship_receipt(
        cfg,
        22,
        {"ok": True, "outcome": "shipped", "message": "old round"},
        review_token=old_token,
    )
    calls = []

    def fresh_attempt(
        _cfg, issue_num, *, skip_api=False, expected_review_token=None
    ):
        calls.append((issue_num, skip_api, expected_review_token))
        return {"ok": False, "reason": "fresh generation checked"}

    monkeypatch.setattr(subdeck, "_ship_issue_once", fresh_attempt)

    same = subdeck.ship_issue(
        cfg, 22, expected_review_token=old_token
    )
    assert same["ok"] is True
    assert same["duplicate"] is True
    assert calls == []

    newer = subdeck.ship_issue(
        cfg, 22, expected_review_token=new_token
    )
    assert newer == {"ok": False, "reason": "fresh generation checked"}
    assert calls == [(22, False, new_token)]


def test_ship_partial_reject_stops_before_later_declarations(tmp_path, monkeypatch):
    """Once an upstream declaration is rejected, the fix can ship without
    forcing decisions on later declarations whose meaning may change."""
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Bar", "reject", note="wrong upstream definition"
    )
    # Baz is intentionally untouched.

    out = subdeck.ship_issue(cfg, 22)

    assert out["ok"] is True
    assert out["outcome"] == "shipped"
    assert out["partial"] is True
    assert "stopped at first rejection" in out["message"]
    assert "reject" in calls and "verify" not in calls
    assert len(calls["reject"].operation_id) == 64
    note = calls["reject"].notes
    assert "- Demo.Chapter14.Foo" in note
    assert "- Demo.Chapter14.Bar: wrong upstream definition" in note
    assert "Demo.Chapter14.Baz" not in note
    assert "Leave all other code untouched." in note
    # Only the explicitly kept predecessor carries into the next round. The
    # rejected and never-reviewed declarations both return for review.
    assert out["carried_over"] == ["Demo.Chapter14.Foo"]
    assert set(subdeck.issue_verdicts(cfg, 22)) == {"Demo.Chapter14.Foo"}


def test_rejected_upstream_invalidates_verified_dependent_before_ship(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", value_pp="0"),
        _decl("Demo.Chapter14.Bar", kind="def", value_pp="1"),
        _decl(
            "Demo.Chapter14.Baz", type_pp="Bar = Bar",
            cone=["Demo.Chapter14.Bar"],
        ),
    ]))
    patch_bulk(monkeypatch, {
        22: {
            "title": "#22 dependent bundle", "state": "OPEN",
            "body": _bundle_body("Foo", "Bar", "Baz"), "labels": set(),
        },
    })
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Baz", "verify")
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Bar", "reject", note="wrong definition"
    )

    sd = subdeck.build_subdeck(cfg, 22)
    baz = next(c for c in sd["cards"] if c["name"] == "Demo.Chapter14.Baz")
    assert baz["verdict"] is None
    assert baz["dependency_invalidated"] is True
    assert sd["progress"] == {"reviewed": 2, "total": 3}
    assert sd["complete"] is False
    assert sd["summary"]["invalidated"] == ["Demo.Chapter14.Baz"]
    assert "Demo.Chapter14.Baz" not in sd["summary"]["kept"]

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is True and out["partial"] is True
    assert out["waiting"] == ["Demo.Chapter14.Baz"]
    assert "Demo.Chapter14.Baz" not in calls["reject"].notes
    assert out["carried_over"] == ["Demo.Chapter14.Foo"]


def test_ship_all_verified_routes_cmd_verify(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    for d in ("Foo", "Bar", "Baz"):
        subdeck.record_decl_verdict(cfg, 22, f"Demo.Chapter14.{d}", "verify")

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is True
    assert out["outcome"] == "verified"
    # cmd_verify routed; cmd_reject NOT.
    assert "verify" in calls and "reject" not in calls
    assert calls["verify"].issue_num == 22
    assert calls["verify"].close is False  # deck keeps the issue OPEN
    assert len(calls["verify"].operation_id) == 64
    # The store is cleared on a successful ship.
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_concurrent_ship_is_single_flight_and_second_response_is_duplicate(
    tmp_path, monkeypatch
):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    for name in ("Foo", "Bar", "Baz"):
        subdeck.record_decl_verdict(
            cfg, 22, f"Demo.Chapter14.{name}", "verify"
        )

    from marathon.review import review as review_mod

    call_lock = Lock()
    call_count = 0
    first_apply = Event()
    release_apply = Event()

    def verify_once(_args):
        nonlocal call_count
        with call_lock:
            call_count += 1
        first_apply.set()
        assert release_apply.wait(5), "test did not release ship side effect"

    monkeypatch.setattr(review_mod, "cmd_verify", verify_once)
    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda _args: pytest.fail("all-verified ship must not reject"),
    )
    start = Barrier(3)

    def ship():
        start.wait()
        return subdeck.ship_issue(cfg, 22)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ship), pool.submit(ship)]
        start.wait()
        assert first_apply.wait(5)
        release_apply.set()
        results = [future.result(timeout=10) for future in futures]

    assert call_count == 1
    assert {result.get("duplicate") for result in results} == {False, True}
    assert all(result["ok"] and result["outcome"] == "verified" for result in results)


def test_ship_any_rejected_routes_cmd_reject_verbatim(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Bar", "reject",
        note="hypothesis h : x > 0 should be x ≥ 0",
    )
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Baz", "verify")

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is True
    assert out["outcome"] == "shipped"
    # cmd_reject routed; cmd_verify NOT.
    assert "reject" in calls and "verify" not in calls
    assert calls["reject"].issue_num == 22
    assert calls["reject"].no_refine is False

    # The note reaching cmd_reject is the composed KEEP/FIX note, VERBATIM:
    note = calls["reject"].notes
    assert "Per-declaration review of issue #22." in note
    assert "KEEP these declarations exactly as written" in note
    assert "- Demo.Chapter14.Baz" in note  # kept
    assert "- Demo.Chapter14.Foo" in note  # kept
    assert "FIX these declarations (rejected):" in note
    assert (
        "- Demo.Chapter14.Bar: hypothesis h : x > 0 should be x ≥ 0" in note
    )
    assert "Leave all other code untouched." in note
    # The exact composite is what ship sent (no Claude in the loop) — the
    # whole note equals compose_ship_note over the reviewed summary.
    expected_note = subdeck.compose_ship_note(22, {
        "kept": ["Demo.Chapter14.Baz", "Demo.Chapter14.Foo"],
        "rejected": [{
            "name": "Demo.Chapter14.Bar",
            "note": "hypothesis h : x > 0 should be x ≥ 0",
        }],
        "deferred": [],
    })
    assert note == expected_note
    # A REJECT ship PERSISTS the KEPT (verified) decls — pinned to their current
    # source — so the human does not re-review unchanged 'kept' decls when the
    # refined issue returns; the REJECTED decl (Aristotle will fix it) is dropped.
    assert out["carried_over"] == ["Demo.Chapter14.Baz", "Demo.Chapter14.Foo"]
    persisted = subdeck.issue_verdicts(cfg, 22)
    assert set(persisted) == {"Demo.Chapter14.Foo", "Demo.Chapter14.Baz"}
    assert "Demo.Chapter14.Bar" not in persisted          # rejected → dropped
    for name in ("Demo.Chapter14.Foo", "Demo.Chapter14.Baz"):
        assert persisted[name]["verdict"] == "verify"
        assert persisted[name].get("fingerprint")          # pinned to source


def test_ship_skips_redundant_next_queue_rebuild(tmp_path, monkeypatch):
    """Shipping leaves queue advancement to the client's post-ship refresh."""
    cfg = make_cfg(tmp_path)
    # Each issue's decl lives in its OWN namespace (FooNs/BarNs/BazNs) so the
    # reconciliation's namespace surfacing never pulls another issue's decl into
    # issue 22's subdeck, leaving a complete single-card subdeck.
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.FooNs.Foo", kind="def", value_pp="x"),
        _decl("Demo.BarNs.Bar", kind="def", value_pp="y"),
        _decl("Demo.BazNs.Baz", kind="def", value_pp="0"),
    ]))
    patch_bulk(monkeypatch, {
        22: {"title": "#22", "state": "OPEN",
             "body": _bundle_body("Foo"), "labels": set()},
        23: {"title": "#23", "state": "OPEN",
             "body": _bundle_body("Bar"), "labels": set()},
        24: {"title": "#24", "state": "OPEN",
             "body": _bundle_body("Baz"), "labels": set()},
    })
    patch_no_verdict_handlers(monkeypatch)
    from marathon.deck import verdicts as verdicts_mod

    monkeypatch.setattr(
        verdicts_mod,
        "_next_ready_card",
        lambda *args, **kwargs: pytest.fail(
            "ship must not synchronously rebuild the queue"
        ),
    )
    subdeck.record_decl_verdict(cfg, 22, "Demo.FooNs.Foo", "verify")

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is True
    assert out["advanced_to_issue"] is None


# ---------------------------------------------------------------------------
# Server — the three endpoints (binding, GET-is-pure, token gate)
# ---------------------------------------------------------------------------


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    """A DeckServer on an ephemeral 127.0.0.1 port over the bundle cfg (issue
    22 bundles Foo/Bar/Baz). Yields (server, base_url, token, cfg)."""
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    server = deck_server.make_server(cfg, port=0, default_chapter=14)
    host, port = server.server_address[:2]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://{host}:{port}", server.token, cfg
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url, body: dict, headers=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _await_ship(base: str, ack_body: str) -> dict:
    ack = json.loads(ack_body)
    assert ack["status"] == "running"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status, body = _get(base + ack["status_url"])
        assert status == 200
        job = json.loads(body)
        if job["status"] in {"succeeded", "failed", "awaiting_retry"}:
            return job.get("result") or job
        time.sleep(0.01)
    raise AssertionError("background ship did not finish")


def test_get_subdeck_is_pure_no_verdict(running_server, monkeypatch):
    server, base, _token, _cfg = running_server
    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: pytest.fail("GET subdeck must not verify"),
    )
    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda args: pytest.fail("GET subdeck must not reject"),
    )
    status, body = _get(base + "/api/subdeck/22")
    assert status == 200
    payload = json.loads(body)
    assert payload["issue"] == 22
    assert payload["card_tier"] == "T0"
    assert {c["name"] for c in payload["cards"]} == {
        "Demo.Chapter14.Foo", "Demo.Chapter14.Bar", "Demo.Chapter14.Baz",
    }
    assert payload["progress"] == {"reviewed": 0, "total": 3}
    assert payload["complete"] is False


def test_decl_verdict_endpoint_records_locally(running_server, monkeypatch):
    server, base, token, cfg = running_server
    install_inspectable_bundle(cfg)
    # A decl-verdict must NEVER route to the committed handlers.
    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: pytest.fail("decl-verdict must not verify"),
    )
    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda args: pytest.fail("decl-verdict must not reject"),
    )
    status, opened = _get(base + "/api/subdeck/22")
    assert status == 200
    foo = next(
        card for card in json.loads(opened)["cards"]
        if card["name"] == "Demo.Chapter14.Foo"
    )
    status, body = _post(
        base + "/api/decl-verdict",
        {
            "issue": 22,
            "decl": "Demo.Chapter14.Foo",
            "verdict": "verify",
            "decl_fingerprint": foo["context_fingerprint"],
        },
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["progress"] == {"reviewed": 1, "total": 3}
    assert payload["complete"] is False
    assert payload["next_decl"] is not None
    assert payload["next_decl"]["verdict"] is None
    # Recorded to the LOCAL sidecar.
    stored = subdeck.issue_verdicts(cfg, 22)["Demo.Chapter14.Foo"]
    assert stored["verdict"] == "verify"
    assert len(stored["fingerprint"]) == 64
    assert stored["fingerprint_version"] == 2


def test_decl_verdict_reuses_one_authoritative_source_surface(
    running_server, monkeypatch
):
    _server, base, token, cfg = running_server
    install_inspectable_bundle(cfg)
    status, raw = _get(base + "/api/subdeck/22")
    assert status == 200
    foo = next(
        card for card in json.loads(raw)["cards"]
        if card["name"] == "Demo.Chapter14.Foo"
    )

    real_load = subdeck._load_subdeck_inputs
    calls = 0

    def counted_load(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(subdeck, "_load_subdeck_inputs", counted_load)
    status, _body = _post(
        base + "/api/decl-verdict",
        {
            "issue": 22,
            "decl": foo["name"],
            "verdict": "verify",
            "decl_fingerprint": foo["context_fingerprint"],
        },
        headers={deck_server.TOKEN_HEADER: token},
    )

    assert status == 200
    assert calls == 1


def test_decl_verdict_endpoint_rejects_stale_card_fingerprint(
    running_server,
):
    _server, base, token, cfg = running_server
    status, raw = _get(base + "/api/subdeck/22")
    assert status == 200
    current = json.loads(raw)
    foo = next(
        card for card in current["cards"]
        if card["name"] == "Demo.Chapter14.Foo"
    )
    assert len(foo["context_fingerprint"]) == 64

    status, body = _post(
        base + "/api/decl-verdict",
        {
            "issue": 22,
            "decl": foo["name"],
            "verdict": "verify",
            "decl_fingerprint": "0" * 64,
        },
        headers={deck_server.TOKEN_HEADER: token},
    )

    assert status == 409
    assert "changed" in json.loads(body)["error"].lower()
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_decl_verdict_endpoint_requires_shown_context_and_inspectable_issue(
    running_server,
):
    _server, base, token, cfg = running_server
    status, raw = _get(base + "/api/subdeck/22")
    assert status == 200
    payload = json.loads(raw)
    assert payload["inspectable"] is False
    foo = next(
        card for card in payload["cards"]
        if card["name"] == "Demo.Chapter14.Foo"
    )

    status, body = _post(
        base + "/api/decl-verdict",
        {"issue": 22, "decl": foo["name"], "verdict": "verify"},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 409
    assert "context" in json.loads(body)["error"].lower()

    status, body = _post(
        base + "/api/decl-verdict",
        {
            "issue": 22,
            "decl": foo["name"],
            "verdict": "verify",
            "decl_fingerprint": foo["context_fingerprint"],
        },
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 409
    assert "tier t0" in json.loads(body)["error"].lower()
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_informal_verdict_remains_a_non_declaration_local_slot(
    running_server, monkeypatch,
):
    _server, base, token, cfg = running_server
    patch_bulk(monkeypatch, {
        22: {
            "title": "#22 bundle",
            "state": "OPEN",
            "labels": set(),
            "body": _bundle_body("Foo", "Bar", "Baz") + """

### Informal Statement

*⚠️ LLM-rendered from common knowledge; verification pending.*

> The bundled result has the stated three parts.
""",
        },
    })
    status, raw = _get(base + "/api/subdeck/22")
    assert status == 200
    informal = json.loads(raw)["informal"]
    status, body = _post(
        base + "/api/decl-verdict",
        {
            "issue": 22,
            "decl": subdeck.INFORMAL_KEY,
            "verdict": "verify",
            "decl_fingerprint": informal["context_fingerprint"],
        },
        headers={deck_server.TOKEN_HEADER: token},
    )

    assert status == 200
    assert json.loads(body)["ok"] is True
    stored = subdeck.issue_verdicts(cfg, 22)[subdeck.INFORMAL_KEY]
    assert stored["verdict"] == "verify"
    assert stored["fingerprint"] == informal["context_fingerprint"]


def test_decl_lock_endpoint_binds_and_checks_context_fingerprint(
    running_server,
):
    _server, base, token, cfg = running_server
    install_inspectable_bundle(cfg)
    status, raw = _get(base + "/api/subdeck/22")
    assert status == 200
    foo = next(
        card for card in json.loads(raw)["cards"]
        if card["name"] == "Demo.Chapter14.Foo"
    )

    status, _body = _post(
        base + "/api/decl-lock",
        {
            "issue": 22,
            "decl": foo["name"],
            "locked": True,
            "decl_fingerprint": "f" * 64,
        },
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 409
    assert subdeck.issue_verdicts(cfg, 22) == {}

    status, body = _post(
        base + "/api/decl-lock",
        {
            "issue": 22,
            "decl": foo["name"],
            "locked": True,
            "decl_fingerprint": foo["context_fingerprint"],
        },
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 200
    assert json.loads(body)["locked"] is True
    stored = subdeck.issue_verdicts(cfg, 22)[foo["name"]]
    assert stored["locked"] is True
    assert stored["fingerprint"] == foo["context_fingerprint"]


def test_run_detail_is_a_pure_get(running_server):
    _server, base, _token, _cfg = running_server
    # Unknown run → 200 {found:false} (pure read, no token needed).
    status, body = _get(base + "/api/run/999")
    assert status == 200
    payload = json.loads(body)
    assert payload["found"] is False and payload["issue_num"] == 999
    # malformed id → 400
    assert _get(base + "/api/run/notanint")[0] == 400


def test_missing_suggestions_is_pure_get(running_server, monkeypatch):
    server, base, _token, _cfg = running_server
    from marathon.review import review as review_mod
    monkeypatch.setattr(review_mod, "cmd_verify",
                        lambda args: pytest.fail("suggestions GET must not verify"))
    monkeypatch.setattr(review_mod, "cmd_reject",
                        lambda args: pytest.fail("suggestions GET must not reject"))
    # Pure read: 200 with the {issue, candidates:[...]} shape, NO token needed.
    status, body = _get(base + "/api/missing-suggestions/22")
    assert status == 200
    payload = json.loads(body)
    assert payload["issue"] == 22
    assert isinstance(payload["candidates"], list)
    # Not a side-effecting POST route.
    assert "/api/missing-suggestions/22" not in deck_server.DeckHandler._POST_ROUTES
    # Malformed id → 400.
    assert _get(base + "/api/missing-suggestions/notanint")[0] == 400


def test_missing_endpoint_records_locally_and_needs_token(running_server, monkeypatch):
    server, base, token, cfg = running_server
    from marathon.review import review as review_mod
    monkeypatch.setattr(review_mod, "cmd_verify",
                        lambda args: pytest.fail("missing-note must not verify"))
    monkeypatch.setattr(review_mod, "cmd_reject",
                        lambda args: pytest.fail("missing-note must not reject"))

    # no token → 403, nothing written
    status, _ = _post(base + "/api/missing",
                      {"issue": 22, "note": "function-multiplication lemma"})
    assert status == 403
    assert subdeck.issue_verdicts(cfg, 22) == {}

    # with token → recorded LOCALLY, surfaced in the response
    status, body = _post(
        base + "/api/missing",
        {"issue": 22, "note": "function-multiplication lemma"},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 200
    assert json.loads(body)["missing"] == "function-multiplication lemma"
    assert subdeck.build_subdeck(cfg, 22)["missing"] == "function-multiplication lemma"


def test_decl_verdict_reject_without_note_is_400(running_server):
    _server, base, token, _cfg = running_server
    status, raw = _get(base + "/api/subdeck/22")
    assert status == 200
    bar = next(
        card for card in json.loads(raw)["cards"]
        if card["name"] == "Demo.Chapter14.Bar"
    )
    status, _ = _post(
        base + "/api/decl-verdict",
        {
            "issue": 22,
            "decl": "Demo.Chapter14.Bar",
            "verdict": "reject",
            "decl_fingerprint": bar["context_fingerprint"],
        },
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 400


def test_decl_verdict_without_token_is_403(running_server):
    _server, base, _token, cfg = running_server
    status, body = _post(
        base + "/api/decl-verdict",
        {"issue": 22, "decl": "Demo.Chapter14.Foo", "verdict": "verify"},
    )
    assert status == 403
    assert json.loads(body)["ok"] is False
    # Nothing recorded — the token gate fired before any write.
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_ship_endpoint_requires_token(running_server):
    _server, base, _token, _cfg = running_server
    status, _ = _post(base + "/api/ship", {"issue": 22})
    assert status == 403


def test_ship_endpoint_requires_exact_review_context(running_server):
    _server, base, token, _cfg = running_server
    status, body = _post(
        base + "/api/ship",
        {"issue": 22},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 409
    assert "review context" in json.loads(body)["error"].lower()


def test_ship_endpoint_routes_through_verdict_path(running_server, monkeypatch):
    server, base, token, cfg = running_server
    calls = patch_no_verdict_handlers(monkeypatch)
    # This fixture deliberately contains a T0 declaration to exercise subdeck
    # rendering. This test targets verdict routing, so isolate it from the
    # independently-tested authoritative machine gate.
    server.ship_jobs._readiness_checker = lambda _cfg, _issue: {
        "shippable": True
    }
    for d in ("Foo", "Bar", "Baz"):
        subdeck.record_decl_verdict(cfg, 22, f"Demo.Chapter14.{d}", "verify")
    review_token = subdeck.build_subdeck(cfg, 22)["review_token"]

    status, body = _post(
        base + "/api/ship", {"issue": 22, "review_token": review_token},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 202
    payload = _await_ship(base, body)
    assert payload["ok"] is True
    assert payload["outcome"] == "verified"
    # Routed through the committed cmd_verify; cmd_reject NOT.
    assert "verify" in calls and "reject" not in calls
    assert calls["verify"].issue_num == 22
    # The store is cleared on a successful ship.
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_ship_endpoint_accepts_partial_reject(running_server, monkeypatch):
    server, base, token, cfg = running_server
    calls = patch_no_verdict_handlers(monkeypatch)
    server.ship_jobs._readiness_checker = lambda _cfg, _issue: {
        "shippable": True
    }
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Bar", "reject", note="repair the upstream def"
    )
    review_token = subdeck.build_subdeck(cfg, 22)["review_token"]

    status, body = _post(
        base + "/api/ship", {"issue": 22, "review_token": review_token},
        headers={deck_server.TOKEN_HEADER: token},
    )

    assert status == 202
    payload = _await_ship(base, body)
    assert payload["ok"] is True and payload["partial"] is True
    assert "reject" in calls and "verify" not in calls
    assert "Demo.Chapter14.Baz" not in calls["reject"].notes


def test_ship_endpoint_incomplete_returns_ok_false(running_server, monkeypatch):
    server, base, token, cfg = running_server
    calls = patch_no_verdict_handlers(monkeypatch)
    server.ship_jobs._readiness_checker = lambda _cfg, _issue: {
        "shippable": True
    }
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    review_token = subdeck.build_subdeck(cfg, 22)["review_token"]

    status, body = _post(
        base + "/api/ship", {"issue": 22, "review_token": review_token},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 202  # durably accepted; worker then reports refusal
    payload = _await_ship(base, body)
    assert payload["ok"] is False
    assert "reason" in payload
    assert calls == {}  # NO irreversible action fired


# --- carried-over verdicts (reject -> refine -> re-review remembers approvals) -

def test_decl_fingerprint_tracks_source():
    a = {"name": "N.A", "source_block": "def A := 0"}
    a2 = {"name": "N.A", "source_block": "def A := 0"}      # identical source
    a3 = {"name": "N.A", "source_block": "def A := 1"}      # changed source
    assert subdeck._decl_fingerprint(a) == subdeck._decl_fingerprint(a2)
    assert subdeck._decl_fingerprint(a) != subdeck._decl_fingerprint(a3)


def test_decl_fingerprint_tracks_elaborated_kernel_dependencies():
    base = {
        "name": "N.Result",
        "source_block": "theorem Result : P := proof",
        "statement_pp": "P",
        "kernel": [{
            "name": "N.Upstream", "kind": "def",
            "type_pp": "Nat", "value_pp": "0",
        }],
    }
    changed = {
        **base,
        "kernel": [{
            "name": "N.Upstream", "kind": "def",
            "type_pp": "Nat", "value_pp": "1",
        }],
    }
    assert subdeck._decl_fingerprint(base) != subdeck._decl_fingerprint(changed)


def test_carried_verdict_reopens_when_only_upstream_kernel_value_changes(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)

    def snapshot(upstream_value):
        return make_snapshot([
            _decl(
                "Demo.Chapter14.Upstream", kind="def", type_pp="Nat",
                value_pp=upstream_value,
            ),
            _decl(
                "Demo.Chapter14.Result", type_pp="Upstream = Upstream",
                cone=["Demo.Chapter14.Upstream"],
            ),
        ])

    patch_bulk(monkeypatch, {
        22: {
            "title": "#22 kernel carry", "state": "OPEN",
            "body": _bundle_body("Upstream", "Result"), "labels": set(),
        },
    })
    install_snapshot(cfg, snapshot("0"))
    before = subdeck.build_subdeck(cfg, 22)
    result = next(c for c in before["cards"] if c["name"].endswith(".Result"))
    subdeck._save_store(cfg, {"22": {result["name"]: {
        "verdict": "verify", "note": None, "ts": "t",
        "fingerprint": subdeck._decl_fingerprint(result),
        "fingerprint_version": 2,
    }}})

    # Result's own source/type is unchanged; only an upstream definition's
    # elaborated value in its transitive kernel changes.
    install_snapshot(cfg, snapshot("1"))
    after = subdeck.build_subdeck(cfg, 22)
    reopened = next(c for c in after["cards"] if c["name"].endswith(".Result"))
    assert reopened["verdict"] is None
    assert reopened["changed_since_review"] is True
    assert reopened["carried_over"] is False


def test_current_session_verdict_reopens_when_dependency_context_changes(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)

    def snapshot(upstream_value):
        return make_snapshot([
            _decl(
                "Demo.Chapter14.Upstream", kind="def", type_pp="Nat",
                value_pp=upstream_value,
            ),
            _decl(
                "Demo.Chapter14.Result", type_pp="Upstream = Upstream",
                cone=["Demo.Chapter14.Upstream"],
            ),
        ])

    patch_bulk(monkeypatch, {
        22: {
            "title": "#22 live context", "state": "OPEN",
            "body": _bundle_body("Upstream", "Result"), "labels": set(),
        },
    })
    install_snapshot(cfg, snapshot("0"))
    stored = subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Result", "verify"
    )
    assert len(stored["fingerprint"]) == 64
    assert stored["fingerprint_version"] == 2

    install_snapshot(cfg, snapshot("1"))
    refreshed = subdeck.build_subdeck(cfg, 22)
    result = next(
        card for card in refreshed["cards"]
        if card["name"] == "Demo.Chapter14.Result"
    )
    assert result["verdict"] is None
    assert result["changed_since_review"] is True
    assert "Demo.Chapter14.Result" not in refreshed["summary"]["kept"]


def test_legacy_unbound_declaration_verdict_is_invalidated(
    tmp_path, monkeypatch
):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    subdeck._save_store(cfg, {"22": {
        "Demo.Chapter14.Foo": {
            "verdict": "verify", "note": None, "ts": "legacy",
        },
    }})

    current = subdeck.build_subdeck(cfg, 22)
    foo = next(
        card for card in current["cards"]
        if card["name"] == "Demo.Chapter14.Foo"
    )
    assert foo["verdict"] is None
    assert foo["changed_since_review"] is True


def test_persist_kept_verdicts_keeps_verified_drops_rejected(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    informal_statement = "The exact informal statement."
    informal_fingerprint = subdeck._informal_fingerprint(
        informal_statement, True
    )
    subdeck._save_store(cfg, {"22": {
        "Demo.Chapter14.Foo": {
            "verdict": "verify",
            "note": None,
            "ts": "t",
            "locked": True,
        },
        "Demo.Chapter14.Bar": {"verdict": "reject", "note": "fix", "ts": "t"},
        subdeck.INFORMAL_KEY: {
            "verdict": "verify",
            "note": None,
            "ts": "t",
            "fingerprint": informal_fingerprint,
        },
    }})
    sd = {
        "cards": [
            {"name": "Demo.Chapter14.Foo", "source_block": "def Foo := 0"},
            {"name": "Demo.Chapter14.Bar", "source_block": "def Bar := 1"},
        ],
        "informal": {
            "statement": informal_statement,
            "is_llm": True,
            "verdict": "verified",
            "context_fingerprint": informal_fingerprint,
        },
    }
    carried = subdeck._persist_kept_verdicts(cfg, 22, sd, ["Demo.Chapter14.Foo"])

    assert carried == ["Demo.Chapter14.Foo"]
    store = subdeck.issue_verdicts(cfg, 22)
    assert "Demo.Chapter14.Bar" not in store               # rejected dropped
    assert store["Demo.Chapter14.Foo"]["verdict"] == "verify"
    assert store["Demo.Chapter14.Foo"]["fingerprint"] == subdeck._decl_fingerprint(
        sd["cards"][0]
    )
    assert store["Demo.Chapter14.Foo"]["locked"] is True
    assert subdeck.INFORMAL_KEY in store                   # exact prose kept


def test_carried_over_verdict_stands_when_source_unchanged(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    sd = subdeck.build_subdeck(cfg, 22)
    foo = next(c for c in sd["cards"] if c["name"] == "Demo.Chapter14.Foo")
    # craft a persisted verdict pinned to Foo's CURRENT source → unchanged
    subdeck._save_store(cfg, {"22": {"Demo.Chapter14.Foo": {
            "verdict": "verify", "note": None, "ts": "t",
            "fingerprint": subdeck._decl_fingerprint(foo),
            "fingerprint_version": 2,
            "carried_over": True,
        }}})

    sd2 = subdeck.build_subdeck(cfg, 22)
    foo2 = next(c for c in sd2["cards"] if c["name"] == "Demo.Chapter14.Foo")
    assert foo2["verdict"] == "verified" and foo2["carried_over"] is True
    assert foo2["changed_since_review"] is False
    # it counts as REVIEWED (no re-review needed)
    assert any(c["name"] == "Demo.Chapter14.Foo" for c in sd2["cards"])
    assert "Demo.Chapter14.Foo" in sd2["summary"]["kept"]


def test_carried_over_verdict_reopens_when_source_changed(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    # a persisted verdict pinned to a STALE fingerprint (Aristotle altered it)
    subdeck._save_store(cfg, {"22": {"Demo.Chapter14.Foo": {
        "verdict": "verify", "note": None, "ts": "t",
        "fingerprint": "STALE_DIFFERENT_HASH",
    }}})

    sd = subdeck.build_subdeck(cfg, 22)
    foo = next(c for c in sd["cards"] if c["name"] == "Demo.Chapter14.Foo")
    assert foo["verdict"] is None and foo["changed_since_review"] is True
    assert foo["carried_over"] is False
    # it is NOT counted kept (must be re-reviewed)
    assert "Demo.Chapter14.Foo" not in sd["summary"]["kept"]


# --- "something is MISSING": ADD-to-Aristotle for an incomplete issue --------

def test_record_missing_round_trip_and_clear(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    assert subdeck.build_subdeck(cfg, 22)["missing"] is None
    subdeck.record_missing(cfg, 22, "  function-multiplication lemma  ")
    assert subdeck.build_subdeck(cfg, 22)["missing"] == "function-multiplication lemma"
    # the missing note is NOT a card / not in progress / not in summary
    sd = subdeck.build_subdeck(cfg, 22)
    assert subdeck.MISSING_KEY not in {c["name"] for c in sd["cards"]}
    # clearing drops it
    subdeck.record_missing(cfg, 22, "")
    assert subdeck.build_subdeck(cfg, 22)["missing"] is None


def test_compose_ship_note_add_section():
    summary = {"kept": ["N.A"], "rejected": [], "deferred": []}
    note = subdeck.compose_ship_note(22, summary, "function multiplication case")
    assert "ADD the missing declaration(s)" in note
    assert "- function multiplication case" in note
    assert "KEEP these declarations" in note
    # no missing → no ADD section at all
    assert "ADD" not in subdeck.compose_ship_note(22, summary)


def test_ship_with_missing_only_routes_to_aristotle(tmp_path, monkeypatch):
    """Every decl verified BUT a missing-declaration flagged → ships to Aristotle
    (cmd_reject) with an ADD section, NOT marked verified."""
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    for d in ("Foo", "Bar", "Baz"):
        subdeck.record_decl_verdict(cfg, 22, f"Demo.Chapter14.{d}", "verify")
    subdeck.record_missing(cfg, 22, "function-multiplication case: f • A is smooth")

    out = subdeck.ship_issue(cfg, 22)

    assert out["ok"] is True and out["outcome"] == "shipped"   # NOT "verified"
    assert "reject" in calls and "verify" not in calls
    note = calls["reject"].notes
    assert "KEEP these declarations" in note
    assert "ADD the missing declaration(s)" in note
    assert "function-multiplication case: f • A is smooth" in note
    # the missing note is consumed at ship (the kept verdicts persist; missing not)
    assert subdeck.build_subdeck(cfg, 22)["missing"] is None


# --- opt-in hard-freeze lock -------------------------------------------------


def test_set_decl_lock_sets_and_clears(tmp_path):
    cfg = make_cfg(tmp_path)
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "verify")

    subdeck.set_decl_lock(cfg, 30, "Demo.Chapter14.Foo", True)
    assert subdeck.issue_verdicts(cfg, 30)["Demo.Chapter14.Foo"]["locked"] is True

    subdeck.set_decl_lock(cfg, 30, "Demo.Chapter14.Foo", False)
    assert "locked" not in subdeck.issue_verdicts(cfg, 30)["Demo.Chapter14.Foo"]


def test_set_decl_lock_on_unreviewed_implies_verify(tmp_path):
    cfg = make_cfg(tmp_path)
    subdeck.set_decl_lock(cfg, 30, "Demo.Chapter14.New", True)
    rec = subdeck.issue_verdicts(cfg, 30)["Demo.Chapter14.New"]
    assert rec["verdict"] == "verify" and rec["locked"] is True


def test_lock_carries_over_a_reverify_but_drops_on_reject(tmp_path):
    cfg = make_cfg(tmp_path)
    subdeck.set_decl_lock(cfg, 30, "Demo.Chapter14.Foo", True)
    # re-verify → lock preserved
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "verify")
    assert subdeck.issue_verdicts(cfg, 30)["Demo.Chapter14.Foo"].get("locked") is True
    # reject (human now wants it changed) → lock dropped
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "reject", note="fix")
    assert "locked" not in subdeck.issue_verdicts(cfg, 30)["Demo.Chapter14.Foo"]


def test_build_subdeck_orders_dependencies_first(tmp_path, monkeypatch):
    """Within an issue, a declaration is never dealt before one its kernel
    leans on — alphabetical order must not override dependency order."""
    cfg = make_cfg(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", type_pp="Nat", value_pp="0"),
        _decl("Demo.Chapter14.Bar", kind="def", type_pp="Int", value_pp="1",
              cone=("Demo.Chapter14.Foo",)),
        _decl("Demo.Chapter14.Baz", kind="theorem", type_pp="Foo = Foo",
              cone=("Demo.Chapter14.Bar",)),
    ]))
    patch_bulk(monkeypatch, {
        22: {
            "title": "#22 bundle", "state": "OPEN",
            "body": _bundle_body("Foo", "Bar", "Baz"), "labels": set(),
        },
    })

    sd = subdeck.build_subdeck(cfg, 22)
    order = [c["name"] for c in sd["cards"]]
    assert order == [
        "Demo.Chapter14.Foo",
        "Demo.Chapter14.Bar",
        "Demo.Chapter14.Baz",
    ]


def test_persist_kept_verdicts_carries_defer_notes(tmp_path, monkeypatch):
    """A defer (and its note) survives a reject ship, re-flagged as carried
    over — stored reviewer context is never silently discarded."""
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    subdeck._save_store(cfg, {"22": {
        "Demo.Chapter14.Foo": {"verdict": "verify", "note": None, "ts": "t"},
        "Demo.Chapter14.Bar": {"verdict": "reject", "note": "fix", "ts": "t"},
        "Demo.Chapter14.Baz": {
            "verdict": "defer",
            "note": "skipped: waiting on the Bar refactor",
            "ts": "t",
        },
    }})
    sd = {
        "cards": [
            {"name": "Demo.Chapter14.Foo", "source_block": "def Foo := 0"},
            {"name": "Demo.Chapter14.Bar", "source_block": "def Bar := 1"},
            {"name": "Demo.Chapter14.Baz", "source_block": "def Baz := 2"},
        ],
        "informal": {},
    }

    carried = subdeck._persist_kept_verdicts(cfg, 22, sd, ["Demo.Chapter14.Foo"])

    assert carried == ["Demo.Chapter14.Foo"]
    store = subdeck.issue_verdicts(cfg, 22)
    assert "Demo.Chapter14.Bar" not in store                 # rejected dropped
    baz = store["Demo.Chapter14.Baz"]
    assert baz["verdict"] == "defer"
    assert baz["note"] == "skipped: waiting on the Bar refactor"
    assert baz["carried_over"] is True
    assert baz["fingerprint"] == subdeck._decl_fingerprint(sd["cards"][2])


def test_swipe_records_reviewed_at_basis(tmp_path, monkeypatch):
    """A live code-backed verdict stores the reviewed-at source/statement
    beside its fingerprint so a later invalidation can show a diff."""
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    install_inspectable_bundle(cfg)
    sd = subdeck.build_subdeck(cfg, 22)
    foo = next(c for c in sd["cards"] if c["name"] == "Demo.Chapter14.Foo")

    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Foo", "verify",
        expected_fingerprint=foo["context_fingerprint"],
        require_current=True,
    )

    rec = subdeck.issue_verdicts(cfg, 22)["Demo.Chapter14.Foo"]
    assert rec["basis"]["source"] == foo["source_block"]
    assert rec["basis"]["statement"] == foo["statement_pp"]


def test_changed_card_exposes_previous_review_basis(tmp_path, monkeypatch):
    """A fingerprint-invalidated verdict surfaces its old decision and
    reviewed-at text on the card so the client can diff and re-affirm."""
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    install_inspectable_bundle(cfg)
    subdeck._save_store(cfg, {"22": {"Demo.Chapter14.Foo": {
        "verdict": "verify",
        "note": "checked against Lee p. 12",
        "ts": "t",
        "fingerprint": "0" * 64,          # stale pin → masked as changed
        "fingerprint_version": subdeck.DECL_FINGERPRINT_VERSION,
        "basis": {"source": "def Foo := old", "statement": "OldType"},
    }}})

    sd = subdeck.build_subdeck(cfg, 22)
    foo = next(c for c in sd["cards"] if c["name"] == "Demo.Chapter14.Foo")
    assert foo["changed_since_review"] is True
    assert foo["verdict"] is None
    prev = foo["previous_review"]
    assert prev["verdict"] == "verified"
    assert prev["note"] == "checked against Lee p. 12"
    assert prev["source_block"] == "def Foo := old"
    assert prev["statement_pp"] == "OldType"

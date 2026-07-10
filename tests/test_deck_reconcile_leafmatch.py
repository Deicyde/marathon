"""``auto_reconcile_issue`` — the namespace-rename leaf-match fallback (#98).

When a refactor renames ``MixedTensorField.foo`` → ``CovariantTensorField.foo``
(SAME leaf, different namespace), ``git log -S foo`` finds a multi-decl commit
and :func:`_proven_rename` returns None — so the old reconciler kept the stale
citation and the deck blocked the card. The fix: a UNIQUE leaf match across the
chapter's live decls (:func:`_leaf_match_rename` over
:func:`live_chapter_decls`).

Coverage here:

* ``_leaf_match_rename`` — unique→match, zero→None, two-sharing-leaf→None,
  leaf-already-cited→None (the conservative unique-only rule);
* ``live_chapter_decls`` — prefix filtering + the Chapter15-vs-Chapter150 /
  Chapter12 anti-collision (== prefix OR startswith(prefix + '.'));
* ``auto_reconcile_issue`` catching a namespace rename in a ``### Lean
  signatures`` body (the new name appears, applied via leaf-match);
* a VERIFIED issue → ``flagged_renames`` populated, ``updated=False``, NO write;
* a ``### Lean declarations`` body → the stale name token is swapped in place,
  every other section byte-identical.

Fully offline: head_sha / issue_body / the snapshot are monkeypatched or written
under a tmp repo; no network / git / lake.
"""

from __future__ import annotations

import pytest

from marathon.deck import refresh as refresh_mod
from marathon.deck.refresh import (
    auto_reconcile_issue,
    live_chapter_decls,
    _leaf_match_rename,
    SIGNATURES_HEADER,
)
from marathon.review.config import ChapterRegistry

from tests.test_deck_source_fallback import (
    make_cfg, make_snapshot, install_snapshot, patch_bulk, _decl,
)


# ---------------------------------------------------------------------------
# _leaf_match_rename — the conservative unique-leaf rule
# ---------------------------------------------------------------------------


def test_leaf_match_unique():
    live = ["A.CovariantTensorField.exists_of_tensorial", "A.Other.helper"]
    got = _leaf_match_rename(
        "A.MixedTensorField.exists_of_tensorial", live, cited_names=set()
    )
    assert got == "A.CovariantTensorField.exists_of_tensorial"


def test_leaf_match_zero_is_none():
    live = ["A.Foo.something_else", "A.Bar.helper"]
    assert _leaf_match_rename("A.Mixed.gone", live, cited_names=set()) is None


def test_leaf_match_two_sharing_leaf_is_none():
    # Two live decls share the stale leaf — ambiguous, so no rename.
    live = ["A.Cov.exists_of_tensorial", "A.Other.exists_of_tensorial"]
    assert (
        _leaf_match_rename("A.Mixed.exists_of_tensorial", live, cited_names=set())
        is None
    )


def test_leaf_match_excludes_already_cited():
    # The only leaf-sharing live decl is ALREADY cited by this issue → not a
    # rename target (its own block stays); no other candidate → None.
    live = ["A.Cov.exists_of_tensorial"]
    cited = {"A.Cov.exists_of_tensorial"}
    assert (
        _leaf_match_rename("A.Mixed.exists_of_tensorial", live, cited_names=cited)
        is None
    )


# ---------------------------------------------------------------------------
# live_chapter_decls — prefix filtering + anti-collision
# ---------------------------------------------------------------------------


def _cfg_ch15(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.chapters[15] = ChapterRegistry(chapter=15, entries=[(98, "Lemma 15.1")])
    return cfg


def test_live_chapter_decls_filters_by_prefix(tmp_path):
    cfg = _cfg_ch15(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter15.A.foo", module="Demo.Chapter15.A"),
        _decl("Demo.Chapter15.bar", module="Demo.Chapter15"),
        _decl("Demo.Chapter14.other", module="Demo.Chapter14"),
    ], cfg.repo_dir))
    got = set(live_chapter_decls(cfg, 15))
    assert got == {"Demo.Chapter15.A.foo", "Demo.Chapter15.bar"}


def test_live_chapter_decls_anticollision(tmp_path):
    """``Chapter15`` must NOT swallow ``Chapter150`` / ``Chapter12`` decls — the
    module test is ``== prefix`` OR ``startswith(prefix + '.')``, never a bare
    ``startswith``."""
    cfg = _cfg_ch15(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter15.keep", module="Demo.Chapter15"),
        _decl("Demo.Chapter15.sub.keep", module="Demo.Chapter15.sub"),
        _decl("Demo.Chapter150.drop", module="Demo.Chapter150"),
        _decl("Demo.Chapter12.drop", module="Demo.Chapter12"),
    ], cfg.repo_dir))
    got = set(live_chapter_decls(cfg, 15))
    assert got == {"Demo.Chapter15.keep", "Demo.Chapter15.sub.keep"}


def test_live_chapter_decls_no_snapshot_is_empty(tmp_path):
    cfg = _cfg_ch15(tmp_path)  # no snapshot installed
    assert live_chapter_decls(cfg, 15) == []


# ---------------------------------------------------------------------------
# auto_reconcile_issue — namespace rename via leaf-match (### Lean signatures)
# ---------------------------------------------------------------------------

# Source after the refactor: the leaf `exists_of_tensorial` now lives under the
# CovariantTensorField namespace (the issue body still cites MixedTensorField).
_SIG_SRC = """\
import Mathlib

namespace Demo.Chapter15.Tf

/-- Existence from tensoriality. -/
theorem CovariantTensorField.exists_of_tensorial : True := trivial

end Demo.Chapter15.Tf
"""


def _sig_body(name: str) -> str:
    return (
        f"{SIGNATURES_HEADER}\n\n"
        f"```lean\ntheorem {name} : True\n```\n"
        f"[`{name}` at Demo/Chapter15/Tf.lean:L1-L2]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter15/Tf.lean#L1-L2)"
        f"\n\n### Informal Statement\n\ntext\n"
    )


def _setup_sig(tmp_path, monkeypatch, *, cited, labels=None, body=None):
    cfg = make_cfg(tmp_path)
    cfg.chapters[15] = ChapterRegistry(chapter=15, entries=[(98, "Lemma 15.1")])
    (cfg.repo_dir / "Demo" / "Chapter15").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter15" / "Tf.lean").write_text(_SIG_SRC)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial",
              module="Demo.Chapter15.Tf"),
    ], cfg.repo_dir))
    body = body if body is not None else _sig_body(cited)
    patch_bulk(monkeypatch, {98: {"title": "Lemma 15.1", "state": "OPEN",
                                  "labels": labels or set(), "body": body}})
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEWSHA")
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: body)
    return cfg, body


def test_signatures_namespace_rename_requires_confirmation(tmp_path, monkeypatch):
    """The #98 case: the body cites ``…Tf.MixedTensorField.exists_of_tensorial``
    (gone); the live decl is ``…Tf.CovariantTensorField.exists_of_tensorial``
    (same leaf). A unique orphan is useful evidence, but does not prove identity;
    the stale citation is retained and the mapping is returned as a proposal."""
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    cfg, _body = _setup_sig(tmp_path, monkeypatch, cited=cited)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    mapping = {
        "old": "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial",
        "new": "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial",
    }
    assert r["renamed"] == []
    assert r["proposed_renames"] == [mapping]
    assert mapping["old"] in r["kept_stale"]
    assert "new_body" not in r
    assert len(r["proposal_token"]) == 64


def test_signatures_confirmed_current_proposal_is_applied(tmp_path, monkeypatch):
    """One explicit confirmation promotes only the exact proposal the human
    saw.  The second pass re-derives the candidate and compare-and-swaps on its
    proposal token before performing the single body edit."""
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    mapping = {"old": cited, "new": new}
    cfg, body = _setup_sig(tmp_path, monkeypatch, cited=cited)
    from marathon.deck.subdeck import (
        issue_verdicts, record_decl_verdict, set_decl_lock,
    )

    record_decl_verdict(cfg, 98, cited, "verify")
    set_decl_lock(cfg, 98, cited, True)
    preview = auto_reconcile_issue(cfg, 98, dry_run=True)
    written = {}
    monkeypatch.setattr(
        refresh_mod, "_gh_edit_body",
        lambda repo, issue, new_body: written.update(
            repo=repo, issue=issue, body=new_body
        ),
    )

    result = auto_reconcile_issue(
        cfg,
        98,
        confirmation={
            "token": preview["proposal_token"],
            "renames": [mapping],
            "splits": [],
        },
    )

    assert result["ok"] is True
    assert result["updated"] is True
    assert result["renamed"] == [mapping]
    assert written["issue"] == 98
    assert new in written["body"]
    assert cited not in written["body"]
    assert body != written["body"]
    records = issue_verdicts(cfg, 98)
    assert cited not in records
    assert records[new]["requires_review"] is True
    assert "verdict" not in records[new] and "locked" not in records[new]
    assert result["ownership_reconciliation"]["migrated"] == [mapping]


def test_confirmation_token_conflict_fails_without_write(tmp_path, monkeypatch):
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    cfg, _body = _setup_sig(tmp_path, monkeypatch, cited=cited)
    from marathon.deck.subdeck import issue_verdicts, record_decl_verdict

    record_decl_verdict(cfg, 98, cited, "verify")
    before = issue_verdicts(cfg, 98)
    monkeypatch.setattr(
        refresh_mod, "_gh_edit_body",
        lambda *a, **k: pytest.fail("a stale confirmation must not edit GitHub"),
    )

    result = auto_reconcile_issue(
        cfg,
        98,
        confirmation={
            "token": "0" * 64,
            "renames": [{"old": cited, "new": new}],
            "splits": [],
        },
    )

    assert result["ok"] is False
    assert result["updated"] is False
    assert result["confirmation_conflict"] is True
    assert result["proposed_renames"] == [{"old": cited, "new": new}]
    assert "reload" in result["message"]
    assert issue_verdicts(cfg, 98) == before


def test_human_body_edit_during_confirmation_is_not_overwritten(
    tmp_path, monkeypatch
):
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    mapping = {"old": cited, "new": new}
    cfg, body = _setup_sig(tmp_path, monkeypatch, cited=cited)
    preview = auto_reconcile_issue(cfg, 98, dry_run=True)

    import marathon.review.github as gh_mod

    reads = iter([body, body + "\nHuman note written concurrently.\n"])
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: next(reads))
    monkeypatch.setattr(
        refresh_mod, "_gh_edit_body",
        lambda *a, **k: pytest.fail("concurrent human prose must not be overwritten"),
    )

    result = auto_reconcile_issue(
        cfg,
        98,
        confirmation={
            "token": preview["proposal_token"],
            "renames": [mapping],
            "splits": [],
        },
    )

    assert result["ok"] is False
    assert result["updated"] is False
    assert result["body_conflict"] is True
    assert result["confirmation_conflict"] is True
    assert "no edit was made" in result["message"]


def test_false_proposal_can_be_dismissed_once_without_stalling_cursor(
    tmp_path, monkeypatch
):
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    mapping = {"old": cited, "new": new}
    cfg, body = _setup_sig(tmp_path, monkeypatch, cited=cited)
    remote = {"body": body}
    import marathon.review.github as gh_mod

    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: remote["body"])
    preview = auto_reconcile_issue(cfg, 98, dry_run=True)
    monkeypatch.setattr(
        refresh_mod, "_gh_edit_body",
        lambda *a, **k: pytest.fail("dismissing identity does not edit the body"),
    )

    dismissed = auto_reconcile_issue(
        cfg,
        98,
        confirmation={
            "token": preview["proposal_token"],
            "renames": [],
            "splits": [],
            "dismiss_renames": [mapping],
        },
    )
    # Unrelated prose and HEAD movement do not change citation identity or the
    # proposed target, so the one human dismissal remains durable.
    remote["body"] = body.replace("\ntext\n", "\nunrelated prose edit\n")
    again = auto_reconcile_issue(cfg, 98, dry_run=True)
    # An unrelated later commit must not resurrect the same false leaf match.
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEXTSHA")
    after_unrelated_head = auto_reconcile_issue(cfg, 98, dry_run=True)

    assert dismissed["ok"] is True
    assert dismissed["updated"] is False
    assert dismissed["dismissed_renames"] == [mapping]
    assert again["proposed_renames"] == []
    assert after_unrelated_head["proposed_renames"] == []
    assert "already reconciled" in again["message"]
    decision_dir = (
        cfg.repo_dir / ".marathon" / "conductor" / "reconcile-decisions"
    )
    assert (decision_dir / "98.json").is_file()
    assert (decision_dir / ".gitignore").read_text() == "*\n"


def test_dismissed_mapping_resurfaces_when_target_source_changes(
    tmp_path, monkeypatch
):
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    mapping = {"old": cited, "new": new}
    cfg, body = _setup_sig(tmp_path, monkeypatch, cited=cited)
    preview = auto_reconcile_issue(cfg, 98, dry_run=True)
    auto_reconcile_issue(
        cfg, 98,
        confirmation={
            "token": preview["proposal_token"],
            "renames": [], "splits": [], "dismiss_renames": [mapping],
        },
    )

    path = cfg.repo_dir / "Demo" / "Chapter15" / "Tf.lean"
    path.write_text(_SIG_SRC.replace(": True := trivial", ": True := by\n  trivial"))
    from marathon.deck.definitions import clear_definition_index_cache

    clear_definition_index_cache(cfg.repo_dir)
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: body)

    result = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert result["proposed_renames"] == [mapping]


def test_dismissed_mapping_resurfaces_when_citation_anchor_changes(
    tmp_path, monkeypatch
):
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    mapping = {"old": cited, "new": new}
    cfg, body = _setup_sig(tmp_path, monkeypatch, cited=cited)
    preview = auto_reconcile_issue(cfg, 98, dry_run=True)
    auto_reconcile_issue(
        cfg, 98,
        confirmation={
            "token": preview["proposal_token"],
            "renames": [], "splits": [], "dismiss_renames": [mapping],
        },
    )
    changed = body.replace(f"theorem {cited} : True", f"theorem {cited} : True := by trivial")
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: changed)

    result = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert result["proposed_renames"] == [mapping]


def test_many_dismissals_are_independent_not_a_prunable_chain(tmp_path):
    cfg = make_cfg(tmp_path)
    mappings = [
        {"old": f"A.Old{i}", "new": f"A.New{i}"}
        for i in range(101)
    ]
    body = "### Lean declarations\n" + "".join(
        f"- `A.Old{i}`\n" for i in range(101)
    )
    source = "namespace A\n" + "".join(
        f"def New{i} : Nat := {i}\n" for i in range(101)
    ) + "end A\n"
    (cfg.repo_dir / "Targets.lean").write_text(source)
    refresh_mod._record_reconciliation_dismissal(
        cfg, 22, body, mappings, []
    )

    remaining, splits = refresh_mod._apply_persisted_dismissals(
        cfg, 22, body, mappings, []
    )

    assert remaining == [] and splits == []
    payload = (
        cfg.repo_dir / ".marathon" / "conductor" /
        "reconcile-decisions" / "22.json"
    ).read_text()
    assert '"A.Old0"' in payload and '"A.Old100"' in payload


def test_verified_issue_flags_rename_no_write(tmp_path, monkeypatch):
    """A VERIFIED issue is never silently rewritten: the proposed rename lands in
    ``flagged_renames`` with ``updated=False`` and NO ``gh issue edit`` (we
    monkeypatch the edit to raise — it must never be called)."""
    from marathon.review.state import ReviewState, IssueState, save_state

    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    cfg, _body = _setup_sig(tmp_path, monkeypatch, cited=cited)
    # Mark the issue verified in state.json.
    save_state(cfg, ReviewState(issues={
        98: IssueState(status="verified", verdict_ts="2026-06-20T00:00:00-04:00"),
    }))

    def _boom(*a, **k):
        raise AssertionError("a verified issue body must NEVER be edited")

    monkeypatch.setattr(refresh_mod, "_gh_edit_body", _boom)

    # NOT dry_run — the guard, not dry_run, is what blocks the write.
    r = auto_reconcile_issue(cfg, 98)
    assert r["updated"] is False
    assert r["flagged_renames"] == [{
        "old": "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial",
        "new": "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial",
    }]
    assert "verified/frozen" in r["message"]


# ---------------------------------------------------------------------------
# auto_reconcile_issue — ### Lean declarations (intake) token swap
# ---------------------------------------------------------------------------

_OTHER_SECTIONS = (
    "## Context\n\nSome prose with an unrelated `backtick` token that must not "
    "change.\n\n"
)


def _decl_body(cited: str) -> str:
    return (
        f"{_OTHER_SECTIONS}"
        f"### Lean declarations\n\n"
        f"- [Tf.lean:1-2](https://github.com/x/y/blob/OLDSHA/Demo/Chapter15/Tf.lean#L1-L2)"
        f" — `{cited}`\n\n"
        f"### Informal Statement\n\ntext\n"
    )


def test_declarations_format_leaf_match_is_proposal_only(tmp_path, monkeypatch):
    """A ``### Lean declarations`` body (no signatures section): the stale name
    token is never silently replaced from leaf equality alone."""
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    body = _decl_body(cited)
    cfg, _b = _setup_sig(tmp_path, monkeypatch, cited="unused", body=body)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == []
    assert r["proposed_renames"] == [{"old": cited, "new": new}]
    assert cited in r["kept_stale"]
    assert "new_body" not in r


def test_declarations_format_confirmed_proposal_is_applied(tmp_path, monkeypatch):
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    mapping = {"old": cited, "new": new}
    body = _decl_body(cited)
    cfg, _b = _setup_sig(tmp_path, monkeypatch, cited="unused", body=body)
    preview = auto_reconcile_issue(cfg, 98, dry_run=True)
    written = {}
    monkeypatch.setattr(
        refresh_mod, "_gh_edit_body",
        lambda repo, issue, new_body: written.update(body=new_body),
    )

    result = auto_reconcile_issue(
        cfg,
        98,
        confirmation={
            "token": preview["proposal_token"],
            "renames": [mapping],
            "splits": [],
        },
    )

    assert result["updated"] is True
    assert result["renamed"] == [mapping]
    assert new in written["body"]
    assert cited not in written["body"]


def test_declarations_keyword_shape_is_proposal_only(tmp_path, monkeypatch):
    """Keyword-shaped citations receive the same fail-closed rename policy."""
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    body = (
        "### Lean declarations\n\n"
        f"- src — `theorem {cited}`\n\n"
        "### Informal Statement\n\ntext\n"
    )
    cfg, _b = _setup_sig(tmp_path, monkeypatch, cited="unused", body=body)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == []
    assert r["proposed_renames"] == [{"old": cited, "new": new}]
    assert "new_body" not in r


@pytest.mark.parametrize("prefix", ["@[simp] theorem", "noncomputable def"])
def test_declarations_attr_modifier_shape_is_proposal_only(
    tmp_path, monkeypatch, prefix
):
    """MAJOR: the extractor's ``_DECL_RE`` accepts a leading ``@[…]`` attribute
    cluster and ``noncomputable/private/protected`` modifiers, so the intake can
    emit `` `@[simp] theorem Foo` `` / `` `noncomputable def Foo` ``. The swap
    parser must still extract these shapes, but leaf equality cannot silently
    rewrite either form."""
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    body = (
        "### Lean declarations\n\n"
        f"- src — `{prefix} {cited}`\n\n"
        "### Informal Statement\n\ntext\n"
    )
    cfg, _b = _setup_sig(tmp_path, monkeypatch, cited="unused", body=body)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == []
    assert r["proposed_renames"] == [{"old": cited, "new": new}]
    assert cited in r["kept_stale"]
    assert "new_body" not in r


# ---------------------------------------------------------------------------
# CRITICAL 1 — orphaned-scoped pool: a deleted decl with a generic leaf is
# NOT falsely renamed onto an unrelated live decl owned by another issue.
# ---------------------------------------------------------------------------

def _setup_symm(tmp_path, monkeypatch, *, issue98_body, sibling97_body,
                snapshot_decls, source, state=None):
    """A chapter-15 registry with TWO issues (98 under reconcile, 97 a sibling).
    Both bodies are served by the bulk fetch + the single-issue issue_body, so
    the orphaned computation (which loads the whole chapter) sees 97's cites.
    ``source`` is the `Demo/Chapter15.lean` text (the live-code pool)."""
    cfg = make_cfg(tmp_path)
    cfg.chapters[15] = ChapterRegistry(
        chapter=15, entries=[(97, "Lemma 15.0"), (98, "Lemma 15.1")]
    )
    (cfg.repo_dir / "Demo" / "Chapter15").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter15.lean").write_text(source)
    install_snapshot(cfg, make_snapshot(snapshot_decls, cfg.repo_dir))
    patch_bulk(monkeypatch, {
        97: {"title": "Lemma 15.0", "state": "OPEN", "labels": set(),
             "body": sibling97_body},
        98: {"title": "Lemma 15.1", "state": "OPEN", "labels": set(),
             "body": issue98_body},
    })
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEWSHA")
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: (
        issue98_body if n == 98 else sibling97_body))
    if state is not None:
        from marathon.review.state import save_state
        save_state(cfg, state)
    return cfg


_GRP_ONLY_SRC = """\
import Mathlib

namespace Demo.Chapter15

theorem Grp.symm : True := trivial

end Demo.Chapter15
"""


def test_false_rename_blocked_sibling_owns_leaf(tmp_path, monkeypatch):
    """CRITICAL 1: a stale `Mixed.symm` (deleted) must NOT be renamed onto the
    live `Grp.symm` just because it is the only chapter-wide `symm` — `Grp.symm`
    is cited by SIBLING issue #97, so it is NOT orphaned and is excluded from the
    candidate pool. The stale citation is KEPT, no rename applied."""
    stale = "Demo.Chapter15.Mixed.symm"
    issue98 = _sig_body(stale)
    # #97 owns Grp.symm (qualified citation) — the ONLY live `symm` in the code.
    sibling97 = _sig_body("Demo.Chapter15.Grp.symm")
    cfg = _setup_symm(
        tmp_path, monkeypatch,
        issue98_body=issue98, sibling97_body=sibling97,
        source=_GRP_ONLY_SRC,
        snapshot_decls=[
            _decl("Demo.Chapter15.Grp.symm", module="Demo.Chapter15"),
        ],
    )
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == []
    assert r["kept_stale"] == [stale]
    assert "new_body" not in r  # nothing to write


def test_orphaned_excludes_sibling_cited(tmp_path, monkeypatch):
    """``orphaned_chapter_decls`` excludes a decl cited by ANY chapter issue
    (here the sibling #97 owns `Grp.symm`), and keeps a genuinely-uncited decl
    (`Tf.helper`, cited by no one)."""
    from marathon.deck.refresh import orphaned_chapter_decls, live_chapter_decls
    sibling97 = _sig_body("Demo.Chapter15.Grp.symm")
    issue98 = _sig_body("Demo.Chapter15.Tf.symm")
    cfg = _setup_symm(
        tmp_path, monkeypatch,
        issue98_body=issue98, sibling97_body=sibling97,
        source=_GRP_ONLY_SRC,
        snapshot_decls=[
            _decl("Demo.Chapter15.Grp.symm", module="Demo.Chapter15"),
            _decl("Demo.Chapter15.Tf.symm", module="Demo.Chapter15"),
            _decl("Demo.Chapter15.Tf.helper", module="Demo.Chapter15"),
        ],
    )
    live = live_chapter_decls(cfg, 15)
    orphaned = set(orphaned_chapter_decls(cfg, 15, live))
    # Grp.symm (cited by 97) and Tf.symm (cited by 98) are NOT orphaned;
    # Tf.helper (cited by no one) IS.
    assert "Demo.Chapter15.Grp.symm" not in orphaned
    assert "Demo.Chapter15.Tf.symm" not in orphaned
    assert "Demo.Chapter15.Tf.helper" in orphaned


_TF_SYMM_SRC = """\
import Mathlib

namespace Demo.Chapter15

theorem Tf.symm : True := trivial

end Demo.Chapter15
"""


def test_critical2_bare_already_cited_no_rename(tmp_path, monkeypatch):
    """CRITICAL 2: the issue already cites `symm` BARE (uniquely resolving to the
    live `…Tf.symm`); a stale `Mixed.symm` also appears. Because the orphaned set
    is computed on RESOLVED snapshot names, `Tf.symm` is in the cited union → not
    orphaned → never selected as the rename target. No rename, no duplicate
    block — the stale citation is kept verbatim."""
    stale = "Demo.Chapter15.Mixed.symm"
    # The issue body cites BOTH the bare `symm` (unique live → …Tf.symm) and the
    # stale `Mixed.symm`. Two signatures blocks.
    issue98 = (
        f"{SIGNATURES_HEADER}\n\n"
        f"```lean\ntheorem symm : True\n```\n"
        f"[`symm` at Demo/Chapter15.lean:L5-L5]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter15.lean#L5-L5)\n\n"
        f"```lean\ntheorem {stale} : True\n```\n"
        f"[`{stale}` at Demo/Chapter15.lean:L9-L9]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter15.lean#L9-L9)\n\n"
        f"### Informal Statement\n\ntext\n"
    )
    # Single-issue chapter; the ONLY live `symm` is the orphan-candidate Tf.symm,
    # which the issue itself cites (bare) so it is NOT orphaned.
    cfg = make_cfg(tmp_path)
    cfg.chapters[15] = ChapterRegistry(chapter=15, entries=[(98, "Lemma 15.1")])
    (cfg.repo_dir / "Demo").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter15.lean").write_text(_TF_SYMM_SRC)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter15.Tf.symm", module="Demo.Chapter15"),
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, {98: {"title": "Lemma 15.1", "state": "OPEN",
                                  "labels": set(), "body": issue98}})
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEWSHA")
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: issue98)

    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == []
    assert r["kept_stale"] == [stale]
    assert "new_body" not in r


def test_critical2_declarations_bare_already_cited_no_dup(tmp_path, monkeypatch):
    """CRITICAL 2 (declarations format): a `### Lean declarations` body cites the
    bare `symm` (unique live → …Tf.symm) AND a stale `Mixed.symm`. `Tf.symm` is
    in the resolved cited union → not orphaned → never the rename target. No
    rename, no duplicate block — the body is left as-is."""
    stale = "Demo.Chapter15.Mixed.symm"
    body = (
        "### Lean declarations\n\n"
        "- s1 — `symm`\n"
        f"- s2 — `{stale}`\n\n"
        "### Informal Statement\n\ntext\n"
    )
    cfg = make_cfg(tmp_path)
    cfg.chapters[15] = ChapterRegistry(chapter=15, entries=[(98, "Lemma 15.1")])
    (cfg.repo_dir / "Demo").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter15.lean").write_text(_TF_SYMM_SRC)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter15.Tf.symm", module="Demo.Chapter15"),
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, {98: {"title": "Lemma 15.1", "state": "OPEN",
                                  "labels": set(), "body": body}})
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEWSHA")
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: body)

    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == []
    assert r["kept_stale"] == [stale]
    assert "new_body" not in r


# ---------------------------------------------------------------------------
# CRITICAL 3 — corrupt state.json must FREEZE (fail-safe), not silently rewrite
# ---------------------------------------------------------------------------


def test_corrupt_state_json_freezes_no_write(tmp_path, monkeypatch):
    """CRITICAL 3: a CORRUPT state.json (load_state swallows the JSONDecodeError
    and returns an empty state, so the verified flag is invisible) must still
    FREEZE the issue — the empty-manifest `### Lean declarations` intake case
    would otherwise be silently rewritten. With a corrupt state.json + empty
    manifest, the leaf-match rename is flagged, `updated` is False, and
    `_gh_edit_body` is never called."""
    from marathon.review.state import state_path

    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    # A `### Lean declarations` intake body (empty approved-statements manifest).
    body = _decl_body(cited)
    cfg, _b = _setup_sig(tmp_path, monkeypatch, cited="unused", body=body)
    # Corrupt the state file.
    sp = state_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("{ not json")

    def _boom(*a, **k):
        raise AssertionError("a frozen issue body must NEVER be edited")

    monkeypatch.setattr(refresh_mod, "_gh_edit_body", _boom)

    # NOT dry_run — the freeze, not dry_run, blocks the write.
    r = auto_reconcile_issue(cfg, 98)
    assert r["updated"] is False
    assert r["flagged_renames"] == [{"old": cited, "new": new}]
    assert "verified/frozen" in r["message"]


# ---------------------------------------------------------------------------
# MINOR — many-to-one target dedup
# ---------------------------------------------------------------------------


def test_many_to_one_dedup_declarations(tmp_path, monkeypatch):
    """MINOR: two distinct stale citations sharing a leaf both leaf-match the
    SAME single orphaned target. The first claims it; the second has no remaining
    candidate → kept_stale (no collapse, no duplicate)."""
    stale_a = "Demo.Chapter15.A.foo"
    stale_b = "Demo.Chapter15.B.foo"
    target = "Demo.Chapter15.C.foo"
    body = (
        "### Lean declarations\n\n"
        f"- s1 — `{stale_a}`\n"
        f"- s2 — `{stale_b}`\n\n"
        "### Informal Statement\n\ntext\n"
    )
    # The only live `foo` is the orphaned C.foo (cited by no issue).
    cfg = make_cfg(tmp_path)
    cfg.chapters[15] = ChapterRegistry(chapter=15, entries=[(98, "Lemma 15.1")])
    (cfg.repo_dir / "Demo").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter15.lean").write_text(
        "import Mathlib\nnamespace Demo.Chapter15\ntheorem C.foo : True := "
        "trivial\nend Demo.Chapter15\n"
    )
    install_snapshot(cfg, make_snapshot([
        _decl(target, module="Demo.Chapter15"),
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, {98: {"title": "Lemma 15.1", "state": "OPEN",
                                  "labels": set(), "body": body}})
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEWSHA")
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: body)

    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    # Exactly ONE proposal (the first stale), both citations remain until a
    # human confirms identity.
    assert r["renamed"] == []
    assert r["proposed_renames"] == [{"old": stale_a, "new": target}]
    assert stale_a in r["kept_stale"]
    assert stale_b in r["kept_stale"]
    assert "new_body" not in r


# ---------------------------------------------------------------------------
# MINOR — multi-section `### Lean declarations` (all sections spliced)
# ---------------------------------------------------------------------------


def test_multi_section_declarations_proposal_does_not_splice(tmp_path, monkeypatch):
    """MINOR: the extractor reads ALL `### Lean declarations` sections; a stale
    token in a SECOND such section is detected but not silently rewritten."""
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    body = (
        "### Lean declarations\n\n"
        "- first — `Demo.Chapter15.Tf.unrelated`\n\n"
        "### Notes\n\nprose\n\n"
        "### Lean declarations\n\n"
        f"- second — `{cited}`\n\n"
        "### Informal Statement\n\ntext\n"
    )
    cfg, _b = _setup_sig(tmp_path, monkeypatch, cited="unused", body=body)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == []
    assert r["proposed_renames"] == [{"old": cited, "new": new}]
    assert "new_body" not in r


# ---------------------------------------------------------------------------
# ### Lean declarations — the ADD path (back-fill uncited in-code decls)
# ---------------------------------------------------------------------------
#
# The real Chapter-15 coverage gap: a refine ADDED `ManifoldOrientation.
# toPointwiseOrientation` (docstring references the issue's result-key 15.5)
# but it ended up cited by NO issue — so it never became a review card. The
# `### Lean declarations` reconcile path must back-fill it as an intake bullet,
# mirroring the signatures ADD path.
#
# `missing_suggestions_for_issue` surfaces an uncited candidate when: the title
# is numbered (15.5), the issue cites a decl whose namespace the candidate is a
# DIRECT member of, the candidate's OWN docstring references the key, and it is
# cited by no chapter issue. The cited decl must resolve in the snapshot (so the
# namespace is computed); the candidate lives ONLY in the code.

# `Demo.Chapter15.Orient` namespace, current code:
#   ofManifold            — Prop 15.5 (part 1), CITED by issue 98 (in snapshot)
#   toPointwiseOrientation — Prop 15.5 (part 2), uncited refine-add (code-only)
#   unrelated             — Prop 15.9, a different result (must NOT be added)
#   helper                — no textbook result (must NOT be added)
_ADD_SRC = """\
import Mathlib

namespace Demo.Chapter15.Orient

/-- Proposition 15.5 (part 1): a manifold orientation. -/
theorem ofManifold : True := trivial

/-- Proposition 15.5 (part 2): the induced pointwise orientation. -/
theorem toPointwiseOrientation : True := trivial

/-- Proposition 15.9: an unrelated result in the same namespace. -/
theorem unrelated : True := trivial

/-- A plumbing helper — no textbook result. -/
theorem helper : True := trivial

end Demo.Chapter15.Orient
"""

_CITED_OFMANIFOLD = "Demo.Chapter15.Orient.ofManifold"
_ADD_CANDIDATE = "Demo.Chapter15.Orient.toPointwiseOrientation"


def _decl_add_body(cited: str = _CITED_OFMANIFOLD) -> str:
    """A `### Lean declarations` (intake) body citing `cited`, wrapped in other
    sections so the byte-identical-outside check has something to guard."""
    return (
        f"{_OTHER_SECTIONS}"
        f"### Lean declarations\n\n"
        f"- [Demo/Chapter15/Orient.lean:L6-L7]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter15/Orient.lean#L6-L7)"
        f" — `{cited}`\n\n"
        f"### Informal Statement\n\ntext\n"
    )


def _setup_add(tmp_path, monkeypatch, *, body, title="Proposition 15.5 — orient",
               extra_snapshot=(), sibling_body=None, state=None):
    """A chapter-15 registry whose issue 98 body is `body`; the `_ADD_SRC`
    namespace lives in the code; the cited `ofManifold` is in the snapshot so its
    namespace resolves. An optional sibling issue 97 can CITE the ADD candidate
    (to exercise the cited_by!=[] exclusion)."""
    cfg = make_cfg(tmp_path)
    entries = [(98, "Prop 15.5")]
    bulk = {98: {"title": title, "state": "OPEN", "labels": set(), "body": body}}
    if sibling_body is not None:
        entries.insert(0, (97, "Prop 15.5 sibling"))
        bulk[97] = {"title": title, "state": "OPEN", "labels": set(),
                    "body": sibling_body}
    cfg.chapters[15] = ChapterRegistry(chapter=15, entries=entries)
    (cfg.repo_dir / "Demo" / "Chapter15").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter15" / "Orient.lean").write_text(_ADD_SRC)
    install_snapshot(cfg, make_snapshot([
        _decl(_CITED_OFMANIFOLD, module="Demo.Chapter15.Orient"),
        *extra_snapshot,
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, bulk)
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEWSHA")
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: bulk[n]["body"])
    if state is not None:
        from marathon.review.state import save_state
        save_state(cfg, state)
    return cfg


def test_declarations_format_add_uncited_in_code_decl(tmp_path, monkeypatch):
    """ADD (a): a `### Lean declarations` body with an uncited in-code candidate
    matching the issue result-key (15.5) → it is APPENDED as an intake bullet at
    the end of the declarations section. The bullet carries a real permalink at
    the NEW sha; every other section is byte-identical."""
    body = _decl_add_body()
    cfg = _setup_add(tmp_path, monkeypatch, body=body)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)

    assert r["added"] == [_ADD_CANDIDATE]
    assert r["renamed"] == []
    nb = r["new_body"]
    # The new bullet is present, with a permalink pinned at the NEW sha, and the
    # backtick'd decl name in the intake shape.
    assert f"— `{_ADD_CANDIDATE}`" in nb
    assert "blob/NEWSHA/Demo/Chapter15/Orient.lean#L" in nb
    # It is appended INSIDE the declarations section (before Informal Statement),
    # right after the existing bullet.
    decl_sec = nb.split("### Lean declarations\n\n", 1)[1].split("### Informal")[0]
    assert f"`{_CITED_OFMANIFOLD}`" in decl_sec
    assert f"`{_ADD_CANDIDATE}`" in decl_sec
    # The unrelated (15.9) and the un-numbered helper are NOT added.
    assert "unrelated`" not in nb
    assert "Orient.helper`" not in nb
    # Everything before the declarations section is byte-identical, and the
    # trailing Informal Statement section is untouched.
    assert nb.startswith(_OTHER_SECTIONS + "### Lean declarations")
    assert nb.endswith("### Informal Statement\n\ntext\n")
    assert "unrelated `backtick` token" in nb


def test_declarations_format_add_frozen_suppressed_no_write(tmp_path, monkeypatch):
    """ADD (b): a FROZEN (verified) issue with an ADD candidate → the ADD is
    suppressed and NOTHING is written. `_gh_edit_body` is monkeypatched to raise:
    it must never be called. `updated` is False and `added` is empty."""
    from marathon.review.state import ReviewState, IssueState

    body = _decl_add_body()
    cfg = _setup_add(
        tmp_path, monkeypatch, body=body,
        state=ReviewState(issues={
            98: IssueState(status="verified",
                           verdict_ts="2026-06-20T00:00:00-04:00"),
        }),
    )

    def _boom(*a, **k):
        raise AssertionError("a frozen issue body must NEVER be edited")

    monkeypatch.setattr(refresh_mod, "_gh_edit_body", _boom)

    # NOT dry_run — the freeze, not dry_run, blocks the write.
    r = auto_reconcile_issue(cfg, 98)
    assert r["updated"] is False
    assert r["added"] == []
    assert r["flagged_renames"] == []   # no rename here, only a suppressed ADD
    assert "verified/frozen" in r["message"]
    assert "add(s) suppressed" in r["message"]


def test_declarations_format_add_skipped_when_cited_by_another_issue(
    tmp_path, monkeypatch
):
    """ADD (c): the candidate is ALREADY cited by a SIBLING issue (97) → its
    `cited_by` is non-empty, so it is NOT added (back-fill targets only decls
    cited by NO issue). With no rename either, NOTHING is written."""
    body = _decl_add_body()
    # Sibling 97 cites the ADD candidate in ITS declarations body → cited_by!=[].
    sibling = (
        "### Lean declarations\n\n"
        f"- src — `{_ADD_CANDIDATE}`\n\n"
        "### Informal Statement\n\ntext\n"
    )
    cfg = _setup_add(
        tmp_path, monkeypatch, body=body, sibling_body=sibling,
        # The candidate must resolve in the snapshot for the sibling's citation
        # to register against it in `decl_to_issues`.
        extra_snapshot=[_decl(_ADD_CANDIDATE, module="Demo.Chapter15.Orient")],
    )

    def _boom(*a, **k):
        raise AssertionError("nothing to add or rename ⇒ no edit")

    monkeypatch.setattr(refresh_mod, "_gh_edit_body", _boom)

    r = auto_reconcile_issue(cfg, 98)
    assert r["added"] == []
    assert r["renamed"] == []
    assert r["updated"] is False
    assert "already reconciled" in r["message"]


def test_declarations_format_add_when_section_is_last_eof(tmp_path, monkeypatch):
    """ADD edge: the `### Lean declarations` section is the LAST in the body (no
    trailing `### ` header). The bullet is appended before EOF, the body's
    trailing newline run is preserved, and the prior section is byte-identical."""
    head = "## Context\n\nprose\n\n"
    body = (
        f"{head}### Lean declarations\n\n"
        f"- src — `{_CITED_OFMANIFOLD}`\n"
    )
    cfg = _setup_add(tmp_path, monkeypatch, body=body)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["added"] == [_ADD_CANDIDATE]
    nb = r["new_body"]
    # The prefix (everything up to & including the existing bullet) is intact…
    assert nb.startswith(head + "### Lean declarations\n\n- src — "
                         f"`{_CITED_OFMANIFOLD}`\n")
    # …and the new bullet trails it, with the result-key candidate.
    assert f"— `{_ADD_CANDIDATE}`" in nb
    assert nb.endswith("\n")


def test_declarations_format_rename_proposal_suppresses_duplicate_add(tmp_path, monkeypatch):
    """RENAME + ADD together: the body cites a STALE
    `Mixed.toPointwiseOrientation` (a namespace-rename of the orphaned
    `Orient.toPointwiseOrientation` — SAME leaf) which ALSO is the ADD candidate
    surfaced by the result-key. The rename CLAIMS it, so it is NOT also appended
    as a duplicate ADD bullet — renames first, then ADD (mirrors the signatures
    `renamed_targets` dedup)."""
    stale = "Demo.Chapter15.Mixed.toPointwiseOrientation"  # same leaf, diff ns
    body = (
        f"{_OTHER_SECTIONS}### Lean declarations\n\n"
        f"- [src](https://github.com/x/y/blob/OLDSHA/f.lean#L1-L1) — "
        f"`{_CITED_OFMANIFOLD}`\n"
        f"- [src](https://github.com/x/y/blob/OLDSHA/f.lean#L1-L1) — `{stale}`\n\n"
        "### Informal Statement\n\ntext\n"
    )
    # The issue cites `Orient.ofManifold` (resolves → Orient namespace, so the ADD
    # path surfaces the same-namespace `toPointwiseOrientation`) AND a stale
    # `Mixed.toPointwiseOrientation` (same leaf). The orphaned live
    # `Orient.toPointwiseOrientation` is BOTH the leaf-match target AND the ADD
    # candidate; it must be in the snapshot as an orphan (cited by no issue).
    cfg = _setup_add(
        tmp_path, monkeypatch, body=body,
        extra_snapshot=[_decl(_ADD_CANDIDATE, module="Demo.Chapter15.Orient")],
    )
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    # The stale mapping is proposed, not applied; its target is not also
    # appended as an ADD while identity awaits confirmation.
    assert r["renamed"] == []
    assert r["proposed_renames"] == [
        {"old": stale, "new": _ADD_CANDIDATE}
    ]
    assert r["added"] == []
    assert stale in r["kept_stale"]
    assert "new_body" not in r


# ---------------------------------------------------------------------------
# ### Scaffolding folded — the ADD path must leave a folded decl folded
# ---------------------------------------------------------------------------
#
# A session can DELIBERATELY fold a mechanical accessor under `### Scaffolding
# folded` (Ch15 lists e.g. `ManifoldOrientation.ext`, `…isContinuous`). The ADD
# path derives candidates from `missing_suggestions_for_issue` filtered to empty
# `cited_by` — that filter only sees `### Lean declarations` / ```lean cards, NOT
# the folded section, so a folded decl would otherwise be wrongly re-ADDED as a
# card, undoing the fold. `_folded_decls(body)` must exclude it in BOTH the
# signatures and declarations ADD paths.


def test_folded_decls_extracts_all_shapes():
    """`_folded_decls` reads ONLY the `### Scaffolding folded` section and handles
    the bare shape, the `def Name` keyword shape, a comma-separated list inside
    ONE backtick span, and a list across multiple spans/bullets — returning the
    bare names. Decls in OTHER sections / prose are never read as folded."""
    from marathon.deck.refresh import _folded_decls

    body = (
        "### Lean declarations\n\n"
        "- src — `Demo.Chapter15.Orient.ofManifold`\n\n"
        "### Scaffolding folded\n\n"
        # comma-separated list across two backtick spans on one bullet
        "- `instNegOrient`, `Demo.Chapter15.Orient.neg_apply` — the Neg instance.\n"
        # keyword shape in a backtick span
        "- `def Demo.Chapter15.Orient.helper` — plumbing.\n"
        # comma-separated list INSIDE one backtick span
        "- `Demo.Chapter15.Orient.ext, Demo.Chapter15.Orient.isContinuous` — accessors.\n\n"
        "### Informal Statement\n\n"
        # a prose backtick AFTER the folded section must NOT be read as folded
        "uses `Demo.Chapter15.Orient.notFolded` here\n"
    )
    got = _folded_decls(body)
    assert got == {
        "instNegOrient",
        "Demo.Chapter15.Orient.neg_apply",
        "Demo.Chapter15.Orient.helper",
        "Demo.Chapter15.Orient.ext",
        "Demo.Chapter15.Orient.isContinuous",
    }
    assert "Demo.Chapter15.Orient.ofManifold" not in got  # ### Lean declarations
    assert "Demo.Chapter15.Orient.notFolded" not in got   # prose elsewhere


def test_folded_decls_no_section_is_empty():
    from marathon.deck.refresh import _folded_decls
    assert _folded_decls(_decl_add_body()) == set()  # no folded section at all


def test_declarations_format_add_skips_folded_decl(tmp_path, monkeypatch):
    """ADD (folded): a `### Lean declarations` body that ALSO folds the otherwise-
    uncited in-code ADD candidate (`toPointwiseOrientation`) under `###
    Scaffolding folded` → the candidate is NOT re-added (it is acknowledged as
    folded). With no rename either, NOTHING is written."""
    body = (
        f"{_OTHER_SECTIONS}"
        f"### Lean declarations\n\n"
        f"- [Demo/Chapter15/Orient.lean:L6-L7]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter15/Orient.lean#L6-L7)"
        f" — `{_CITED_OFMANIFOLD}`\n\n"
        f"### Scaffolding folded\n\n"
        f"- `{_ADD_CANDIDATE}` — folded as a mechanical accessor.\n\n"
        f"### Informal Statement\n\ntext\n"
    )
    cfg = _setup_add(tmp_path, monkeypatch, body=body)

    def _boom(*a, **k):
        raise AssertionError("a folded decl must NOT be re-added ⇒ no edit")

    monkeypatch.setattr(refresh_mod, "_gh_edit_body", _boom)

    r = auto_reconcile_issue(cfg, 98)
    assert r["added"] == []        # the folded candidate is NOT re-added
    assert r["renamed"] == []
    assert r["updated"] is False
    assert "already reconciled" in r["message"]


def test_signatures_format_add_skips_folded_decl(tmp_path, monkeypatch):
    """ADD (folded, signatures format): a `### Lean signatures` body that ALSO
    folds the uncited in-code ADD candidate under `### Scaffolding folded` → the
    signatures ADD path (`add_only`) excludes it, so nothing is added/written."""
    body = (
        f"{SIGNATURES_HEADER}\n\n"
        f"```lean\ntheorem {_CITED_OFMANIFOLD} : True\n```\n"
        f"[`{_CITED_OFMANIFOLD}` at Demo/Chapter15/Orient.lean:L6-L6]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter15/Orient.lean#L6-L6)\n\n"
        f"### Scaffolding folded\n\n"
        f"- `{_ADD_CANDIDATE}` — folded as a mechanical accessor.\n\n"
        f"### Informal Statement\n\ntext\n"
    )
    cfg = _setup_add(tmp_path, monkeypatch, body=body)

    def _boom(*a, **k):
        raise AssertionError("a folded decl must NOT be re-added ⇒ no edit")

    monkeypatch.setattr(refresh_mod, "_gh_edit_body", _boom)

    r = auto_reconcile_issue(cfg, 98)
    assert r["added"] == []        # the folded candidate is NOT re-added
    assert r["updated"] is False


# ---------------------------------------------------------------------------
# ### Scaffolding folded — HARDENING the guard against four adversarial defects
# ---------------------------------------------------------------------------
#
# The folded note is free-form LLM prose and runs automatically in the
# conductor, so any extraction defect silently undoes a fold or hides a real
# gap. Each test below pins one defect and its fix; all use the SAME real
# Chapter-15 ADD setup (`_setup_add` / `_decl_add_body`) and assert the folded
# candidate is NOT re-added (added == []) — or, for the section-bleed test, that
# a GENUINE downstream candidate IS still added (not hidden by a section that
# bled past a shallower/deeper header).


def test_folded_umbrella_colon_first_decl_extracted():
    """DEFECT 2 (unit): the documented `<umbrella>: <decl₁>, <decl₂>, …` shape
    inside ONE backtick span. The leading `<umbrella>: ` label is stripped, so
    the FIRST decl is extracted (not silently dropped with the umbrella prefix
    still attached). The umbrella itself is NOT a folded decl.

    The FIRST decl is BARE (`extLeaf`, no dot) on purpose: the plain-prose
    dotted-name harvest cannot reach a bare token, so this isolates the
    umbrella-strip + comma-split + `_BARE_NAME_RE` path — without the strip the
    first piece is `ManifoldOrientation: extLeaf`, matching neither token, and
    `extLeaf` is silently dropped."""
    from marathon.deck.refresh import _folded_decls

    body = (
        "### Scaffolding folded\n\n"
        "- `ManifoldOrientation: extLeaf, "
        "Demo.Chapter15.Orient.isContinuous` — accessors.\n\n"
        "### Informal Statement\n\ntext\n"
    )
    got = _folded_decls(body)
    # BOTH decls in the umbrella span are folded — including the bare FIRST one
    # (only the umbrella-strip + comma-split path can recover `extLeaf`).
    assert "extLeaf" in got
    assert "Demo.Chapter15.Orient.isContinuous" in got
    # The umbrella grouping-parent is not itself a folded decl.
    assert "ManifoldOrientation" not in got


def test_folded_umbrella_colon_first_decl_not_readded(tmp_path, monkeypatch):
    """DEFECT 2 (consumer): the ADD candidate is the FIRST decl of an
    umbrella-colon span. Pre-fix the umbrella prefix kept it from matching, so it
    leaked through and was re-ADDed. With the strip it is folded → NOT re-added.
    The `### Lean declarations` body cites `ofManifold`; the candidate
    `toPointwiseOrientation` is folded as the FIRST decl under an umbrella."""
    body = (
        f"{_OTHER_SECTIONS}"
        f"### Lean declarations\n\n"
        f"- [Demo/Chapter15/Orient.lean:L6-L7]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter15/Orient.lean#L6-L7)"
        f" — `{_CITED_OFMANIFOLD}`\n\n"
        f"### Scaffolding folded\n\n"
        f"- `ManifoldOrientation: {_ADD_CANDIDATE}, Demo.Chapter15.Orient.ext`"
        f" — folded accessors.\n\n"
        f"### Informal Statement\n\ntext\n"
    )
    cfg = _setup_add(tmp_path, monkeypatch, body=body)

    def _boom(*a, **k):
        raise AssertionError("the FIRST folded decl must NOT be re-added")

    monkeypatch.setattr(refresh_mod, "_gh_edit_body", _boom)

    r = auto_reconcile_issue(cfg, 98)
    assert r["added"] == []
    assert r["updated"] is False
    assert "already reconciled" in r["message"]


def test_folded_bare_leaf_vs_qualified_candidate_not_readded(
    tmp_path, monkeypatch
):
    """DEFECT 4: the folded note names the BARE LEAF (`toPointwiseOrientation`)
    while the ADD candidate is fully-qualified
    (`Demo.Chapter15.Orient.toPointwiseOrientation`). The consumer excludes a
    candidate whose LEAF is in the folded leaf-set, so the qualified candidate is
    NOT re-added."""
    leaf = _ADD_CANDIDATE.rsplit(".", 1)[-1]  # toPointwiseOrientation
    body = (
        f"{_OTHER_SECTIONS}"
        f"### Lean declarations\n\n"
        f"- [Demo/Chapter15/Orient.lean:L6-L7]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter15/Orient.lean#L6-L7)"
        f" — `{_CITED_OFMANIFOLD}`\n\n"
        f"### Scaffolding folded\n\n"
        f"- `{leaf}` — folded by bare leaf name.\n\n"
        f"### Informal Statement\n\ntext\n"
    )
    cfg = _setup_add(tmp_path, monkeypatch, body=body)

    def _boom(*a, **k):
        raise AssertionError("a bare-leaf folded decl must exclude the "
                             "qualified candidate ⇒ no edit")

    monkeypatch.setattr(refresh_mod, "_gh_edit_body", _boom)

    r = auto_reconcile_issue(cfg, 98)
    assert r["added"] == []
    assert r["updated"] is False
    assert "already reconciled" in r["message"]


def test_folded_section_does_not_bleed_past_h2_h4_headers(
    tmp_path, monkeypatch
):
    """DEFECT 1: a `### Scaffolding folded` section followed by a SHALLOWER `## `
    (h2) and a DEEPER `#### ` (h4) header whose prose names the GENUINE uncited
    ADD candidate. Pre-fix the section bled past those non-h3 headers and read
    the candidate's dotted name as "folded", hiding it. With the any-header
    terminator the section stops at the `## ` boundary, so the genuine candidate
    IS still added.

    The folded note itself folds an UNRELATED decl (`Orient.helper`); the real
    ADD candidate `toPointwiseOrientation` is named only in the downstream h2/h4
    prose and must survive as an ADD."""
    body = (
        f"### Lean declarations\n\n"
        f"- [Demo/Chapter15/Orient.lean:L6-L7]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter15/Orient.lean#L6-L7)"
        f" — `{_CITED_OFMANIFOLD}`\n\n"
        f"### Scaffolding folded\n\n"
        f"- `Demo.Chapter15.Orient.helper` — a folded plumbing helper.\n\n"
        f"## Downstream notes\n\n"
        f"This h2 mentions `{_ADD_CANDIDATE}` in prose.\n\n"
        f"#### A deeper note\n\n"
        f"and the h4 references {_ADD_CANDIDATE} again.\n\n"
        f"### Informal Statement\n\ntext\n"
    )
    cfg = _setup_add(tmp_path, monkeypatch, body=body)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    # The downstream candidate is NOT hidden by section-bleed — it IS added.
    assert r["added"] == [_ADD_CANDIDATE]
    nb = r["new_body"]
    assert f"— `{_ADD_CANDIDATE}`" in nb


def test_folded_plain_prose_no_backtick_dotted_not_readded(
    tmp_path, monkeypatch
):
    """DEFECT 3: a PLAIN-PROSE folded note — no backticks at all — naming the
    dotted ADD candidate. The dotted-identifier harvest reads it from the raw
    section text, so the candidate is folded → NOT re-added even without
    backticks."""
    body = (
        f"{_OTHER_SECTIONS}"
        f"### Lean declarations\n\n"
        f"- [Demo/Chapter15/Orient.lean:L6-L7]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter15/Orient.lean#L6-L7)"
        f" — `{_CITED_OFMANIFOLD}`\n\n"
        f"### Scaffolding folded\n\n"
        f"We folded {_ADD_CANDIDATE} as a mechanical accessor under the "
        f"orientation umbrella; no backticks here.\n\n"
        f"### Informal Statement\n\ntext\n"
    )
    cfg = _setup_add(tmp_path, monkeypatch, body=body)

    def _boom(*a, **k):
        raise AssertionError("a plain-prose folded dotted decl must NOT be "
                             "re-added ⇒ no edit")

    monkeypatch.setattr(refresh_mod, "_gh_edit_body", _boom)

    r = auto_reconcile_issue(cfg, 98)
    assert r["added"] == []
    assert r["updated"] is False
    assert "already reconciled" in r["message"]

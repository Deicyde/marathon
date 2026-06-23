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


def test_signatures_namespace_rename_caught(tmp_path, monkeypatch):
    """The #98 case: the body cites ``…Tf.MixedTensorField.exists_of_tensorial``
    (gone); the live decl is ``…Tf.CovariantTensorField.exists_of_tensorial``
    (same leaf). _proven_rename has no git proof here, so the leaf-match fallback
    resolves it and the new name is written into the signatures."""
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    cfg, _body = _setup_sig(tmp_path, monkeypatch, cited=cited)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == [{
        "old": "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial",
        "new": "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial",
    }]
    assert r["kept_stale"] == []
    nb = r["new_body"]
    assert "CovariantTensorField.exists_of_tensorial` at" in nb
    # the stale MixedTensorField citation is gone from the rewritten block
    assert "MixedTensorField.exists_of_tensorial` at" not in nb
    # other sections preserved
    assert "### Informal Statement\n\ntext\n" in nb


def test_verified_issue_flags_rename_no_write(tmp_path, monkeypatch):
    """A VERIFIED issue is never silently rewritten: the proposed rename lands in
    ``flagged_renames`` with ``updated=False`` and NO ``gh issue edit`` (we
    monkeypatch the edit to raise — it must never be called)."""
    from marathon.review import state as state_mod
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


def test_declarations_format_token_swapped(tmp_path, monkeypatch):
    """A ``### Lean declarations`` body (no signatures section): the stale name
    token is replaced by the leaf-matched new name WITHIN that section; every
    other section is byte-identical."""
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    body = _decl_body(cited)
    cfg, _b = _setup_sig(tmp_path, monkeypatch, cited="unused", body=body)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == [{"old": cited, "new": new}]
    nb = r["new_body"]
    assert f"`{new}`" in nb
    assert f"`{cited}`" not in nb
    # Everything before the declarations section is byte-identical…
    assert nb.startswith(_OTHER_SECTIONS + "### Lean declarations")
    # …and the trailing Informal Statement section is untouched.
    assert nb.endswith("### Informal Statement\n\ntext\n")
    # the unrelated prose backtick survived
    assert "unrelated `backtick` token" in nb


def test_declarations_format_keyword_shape_swapped(tmp_path, monkeypatch):
    """The intake also emits the keyword shape `` `def Name` `` — only the name
    part is swapped, the keyword preserved."""
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    body = (
        "### Lean declarations\n\n"
        f"- src — `theorem {cited}`\n\n"
        "### Informal Statement\n\ntext\n"
    )
    cfg, _b = _setup_sig(tmp_path, monkeypatch, cited="unused", body=body)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == [{"old": cited, "new": new}]
    assert f"`theorem {new}`" in r["new_body"]
    assert cited not in r["new_body"]


@pytest.mark.parametrize("prefix", ["@[simp] theorem", "noncomputable def"])
def test_declarations_format_attr_modifier_shape_swapped(
    tmp_path, monkeypatch, prefix
):
    """MAJOR: the extractor's ``_DECL_RE`` accepts a leading ``@[…]`` attribute
    cluster and ``noncomputable/private/protected`` modifiers, so the intake can
    emit `` `@[simp] theorem Foo` `` / `` `noncomputable def Foo` ``. The swap
    regex must match the SAME shapes — else the token is extracted+leaf-matched
    but the in-place swap no-ops and the rename is silently dropped to
    kept_stale. Only the NAME is rewritten; the attr+modifier+keyword run is
    preserved verbatim."""
    cited = "Demo.Chapter15.Tf.MixedTensorField.exists_of_tensorial"
    new = "Demo.Chapter15.Tf.CovariantTensorField.exists_of_tensorial"
    body = (
        "### Lean declarations\n\n"
        f"- src — `{prefix} {cited}`\n\n"
        "### Informal Statement\n\ntext\n"
    )
    cfg, _b = _setup_sig(tmp_path, monkeypatch, cited="unused", body=body)
    r = auto_reconcile_issue(cfg, 98, dry_run=True)
    assert r["renamed"] == [{"old": cited, "new": new}]
    assert r["kept_stale"] == []
    assert f"`{prefix} {new}`" in r["new_body"]
    assert cited not in r["new_body"]


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
    # Exactly ONE rename (the first stale), the other kept.
    assert r["renamed"] == [{"old": stale_a, "new": target}]
    assert stale_b in r["kept_stale"]
    nb = r["new_body"]
    # The target appears exactly once (no duplicate-collapse).
    assert nb.count(f"`{target}`") == 1


# ---------------------------------------------------------------------------
# MINOR — multi-section `### Lean declarations` (all sections spliced)
# ---------------------------------------------------------------------------


def test_multi_section_declarations_all_spliced(tmp_path, monkeypatch):
    """MINOR: the extractor reads ALL `### Lean declarations` sections; a stale
    token in a SECOND such section must be swapped too (not just the first)."""
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
    assert r["renamed"] == [{"old": cited, "new": new}]
    nb = r["new_body"]
    assert f"`{new}`" in nb
    assert f"`{cited}`" not in nb
    # The first section's unrelated token and the Notes prose survive verbatim.
    assert "`Demo.Chapter15.Tf.unrelated`" in nb
    assert "### Notes\n\nprose\n\n" in nb

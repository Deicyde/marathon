"""``drop_stale_citation`` — the HUMAN-TRIGGERED "drop a dead citation" escape.

When an issue cites a decl that was deliberately REMOVED from the code
(folded/duplicated away — not renamed, not genuinely-missing work), the deck
shows a stale-citation warning and the auto-reconciler conservatively KEEPS it
(it can't distinguish a removal from missing work). :func:`drop_stale_citation`
is the one-click human escape that DROPS such a confirmed-dead citation so the
issue reviews cleanly. It is gated: only a citation that is

* CITED by the issue body, AND
* NOT in the current code (a stale citation, ``in_code=False``), AND
* has NO leaf-match rename target

is droppable. A live decl / a rename target / an uncited name is REFUSED
(``ok=False``) and NOTHING is written.

Coverage here:

* drops a ``### Lean declarations`` bullet — other sections byte-identical, a
  prose note added to ``### Informal Statement``, and the dropped name is NOT
  re-extracted as a citation afterward;
* drops a ``### Lean signatures`` ```` ```lean ```` block;
* REFUSES a live (in-code) decl;
* REFUSES a decl with a leaf-match rename ("reconcile, don't drop");
* REFUSES a decl the issue does not cite.

Fully offline: head_sha / issue_body / _gh_edit_body / the snapshot are
monkeypatched or written under a tmp repo; no network / git / lake. Reuses the
``test_deck_source_fallback`` builders + the ``test_deck_reconcile_leafmatch``
signature-body fixtures.
"""

from __future__ import annotations

from marathon.deck import refresh as refresh_mod
from marathon.deck.refresh import (
    auto_reconcile_issue,
    drop_stale_citation,
    live_chapter_decls,
    SIGNATURES_HEADER,
)
from marathon.review.config import ChapterRegistry
from marathon.review.verified_decls import extract_declarations_from_issue_body

from tests.test_deck_source_fallback import (
    make_cfg, make_snapshot, install_snapshot, patch_bulk, _decl,
)


# The chapter-15 source carries exactly ONE live decl (the kept one); the
# dropped citations are present in NEITHER the source NOR the snapshot, so their
# card views report in_code=False (a genuine stale citation).
_SRC = """\
import Mathlib

namespace Demo.Chapter15.Tf

/-- A live, in-code result the issue also cites. -/
theorem KeptResult.holds : True := trivial

end Demo.Chapter15.Tf
"""


def _setup(tmp_path, monkeypatch, *, body, live_extra=()):
    """A chapter-15 cfg with the source written, a snapshot carrying the live
    decl(s), and the issue body mocked. ``_gh_edit_body`` is replaced by a
    recorder so a write is observable (and asserted-against when none is
    expected). ``live_extra`` adds further live decls to the snapshot (used to
    plant a leaf-match rename target)."""
    cfg = make_cfg(tmp_path)
    cfg.chapters[15] = ChapterRegistry(chapter=15, entries=[(98, "Lemma 15.1")])
    (cfg.repo_dir / "Demo" / "Chapter15").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter15" / "Tf.lean").write_text(_SRC)
    decls = [_decl("Demo.Chapter15.Tf.KeptResult.holds", module="Demo.Chapter15.Tf")]
    for name in live_extra:
        decls.append(_decl(name, module="Demo.Chapter15.Tf"))
    install_snapshot(cfg, make_snapshot(decls, cfg.repo_dir))
    patch_bulk(monkeypatch, {98: {"title": "Lemma 15.1", "state": "OPEN",
                                  "labels": set(), "body": body}})
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: body)
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEWSHA")

    edits: list[tuple] = []
    monkeypatch.setattr(
        refresh_mod, "_gh_edit_body",
        lambda repo, num, b: edits.append((repo, num, b)),
    )
    return cfg, edits


# ---------------------------------------------------------------------------
# DROP — ### Lean declarations bullet
# ---------------------------------------------------------------------------

# The dead citation's decl is in NEITHER the source NOR the snapshot, and shares
# its leaf with NO live decl (so no leaf-match rename).
_DEAD = "Demo.Chapter15.Tf.RemovedDup.gone"

_OTHER = (
    "## Context\n\nUnrelated prose with a `backtick` token that must not "
    "change.\n\n"
)


def _decl_body(*cited):
    bullets = "\n".join(
        f"- [Tf.lean:1-2](https://github.com/x/y/blob/OLD/Demo/Chapter15/Tf.lean#L1-L2)"
        f" — `{c}`"
        for c in cited
    )
    return (
        f"{_OTHER}"
        f"### Lean declarations\n\n{bullets}\n\n"
        f"### Informal Statement\n\nThe statement.\n"
    )


def test_drops_declaration_bullet(tmp_path, monkeypatch):
    """A ``### Lean declarations`` body: the dead citation's bullet is removed,
    the live bullet stays, every OTHER section is byte-identical, a prose note
    lands in ``### Informal Statement``, and the dropped name is NO LONGER
    extracted as a citation."""
    body = _decl_body("Demo.Chapter15.Tf.KeptResult.holds", _DEAD)
    cfg, edits = _setup(tmp_path, monkeypatch, body=body)
    r = drop_stale_citation(cfg, 98, _DEAD, dry_run=True)
    assert r["ok"] is True and r["dropped"] is True
    nb = r["new_body"]
    # The dead BULLET (the source-link line) is gone; the live one survives.
    # (The name still appears once — in backticks — inside the prose drop-note,
    # but NOT as a `### Lean declarations` bullet; see the extraction check.)
    assert "RemovedDup.gone`\n" not in nb  # no trailing-`name`-bullet line
    assert _DEAD not in extract_declarations_from_issue_body(nb)
    assert "`Demo.Chapter15.Tf.KeptResult.holds`" in nb
    # Context section byte-identical (prose backtick untouched).
    assert nb.startswith(_OTHER + "### Lean declarations")
    assert "Unrelated prose with a `backtick` token" in nb
    # The note landed in the Informal Statement section (prose, not declarations).
    assert "♻️ Dropped stale citation `Demo.Chapter15.Tf.RemovedDup.gone`" in nb
    assert "removed from the code (not a rename)." in nb
    # CRITICAL: the dropped name is no longer a CITATION (the note is prose).
    assert _DEAD not in extract_declarations_from_issue_body(nb)
    # dry_run: nothing written.
    assert edits == []


def test_drop_declaration_bullet_writes(tmp_path, monkeypatch):
    """Not dry_run: the new body is written via a single ``gh issue edit``."""
    body = _decl_body("Demo.Chapter15.Tf.KeptResult.holds", _DEAD)
    cfg, edits = _setup(tmp_path, monkeypatch, body=body)
    r = drop_stale_citation(cfg, 98, _DEAD)
    assert r["dropped"] is True
    assert len(edits) == 1
    _repo, num, written = edits[0]
    assert num == 98
    # The dead citation is no longer EXTRACTED as a citation from the new body.
    assert _DEAD not in extract_declarations_from_issue_body(written)


# ---------------------------------------------------------------------------
# DROP — ### Lean signatures block
# ---------------------------------------------------------------------------


def _sig_body(*cited):
    blocks = "\n\n".join(
        f"```lean\ntheorem {c} : True\n```\n"
        f"[`{c}` at Demo/Chapter15/Tf.lean:L1-L2]"
        f"(https://github.com/x/y/blob/OLD/Demo/Chapter15/Tf.lean#L1-L2)"
        for c in cited
    )
    return (
        f"{SIGNATURES_HEADER}\n\n{blocks}\n\n"
        f"### Informal Statement\n\nThe statement.\n"
    )


def test_drops_signature_block(tmp_path, monkeypatch):
    """A ``### Lean signatures`` body: the dead citation's whole ```` ```lean ````
    block-run is removed, the live block stays, the Informal Statement section
    gains the note, and the dropped name is not re-extracted."""
    kept = "Demo.Chapter15.Tf.KeptResult.holds"
    body = _sig_body(kept, _DEAD)
    cfg, edits = _setup(tmp_path, monkeypatch, body=body)
    r = drop_stale_citation(cfg, 98, _DEAD, dry_run=True)
    assert r["ok"] is True and r["dropped"] is True
    nb = r["new_body"]
    # The dead block (fence + permalink) is gone; the live block survives.
    assert "RemovedDup.gone" not in nb.replace(
        "♻️ Dropped stale citation `Demo.Chapter15.Tf.RemovedDup.gone`", ""
    )
    assert f"`{kept}` at Demo/Chapter15/Tf.lean" in nb
    assert "theorem Demo.Chapter15.Tf.KeptResult.holds : True" in nb
    # Note in the Informal Statement section; dropped name not a citation.
    assert "♻️ Dropped stale citation `Demo.Chapter15.Tf.RemovedDup.gone`" in nb
    assert _DEAD not in extract_declarations_from_issue_body(nb)
    assert edits == []


# ---------------------------------------------------------------------------
# REFUSALS — the safety gate
# ---------------------------------------------------------------------------


def test_refuses_live_decl(tmp_path, monkeypatch):
    """A decl still IN the code is not a stale citation — refused, no write."""
    live = "Demo.Chapter15.Tf.KeptResult.holds"
    body = _decl_body(live, _DEAD)
    cfg, edits = _setup(tmp_path, monkeypatch, body=body)
    r = drop_stale_citation(cfg, 98, live)
    assert r["ok"] is False and r["dropped"] is False
    assert "still in the code" in r["message"]
    assert edits == []


def test_refuses_rename_available(tmp_path, monkeypatch):
    """A stale citation whose leaf matches a UNIQUE orphaned live decl has a
    rename target — RECONCILE, don't drop. Refused, no write."""
    # Cited (stale) decl + a live ORPHANED decl sharing its leaf `relocated`.
    stale = "Demo.Chapter15.Tf.OldHome.relocated"
    rename_target = "Demo.Chapter15.Tf.NewHome.relocated"
    body = _decl_body("Demo.Chapter15.Tf.KeptResult.holds", stale)
    cfg, edits = _setup(
        tmp_path, monkeypatch, body=body, live_extra=[rename_target]
    )
    r = drop_stale_citation(cfg, 98, stale)
    assert r["ok"] is False and r["dropped"] is False
    assert "rename is available" in r["message"]
    assert rename_target in r["message"]
    assert "reconcile, don't drop" in r["message"]
    assert edits == []


def test_refuses_uncited_decl(tmp_path, monkeypatch):
    """A name the issue does not cite is refused (nothing to drop), no write."""
    body = _decl_body("Demo.Chapter15.Tf.KeptResult.holds", _DEAD)
    cfg, edits = _setup(tmp_path, monkeypatch, body=body)
    r = drop_stale_citation(cfg, 98, "Demo.Chapter15.Tf.NeverCited.absent")
    assert r["ok"] is False and r["dropped"] is False
    assert "does not cite" in r["message"]
    assert edits == []


# ---------------------------------------------------------------------------
# AUTOMATIC RECONCILE NEVER DROPS — disappearance still needs human intent
# ---------------------------------------------------------------------------


def _confirm_delete_diff(monkeypatch):
    import marathon.review.verified_decls as vd

    monkeypatch.setattr(
        vd, "declarations_added_removed_in_diff",
        lambda repo, old, new, paths: (set(), {_DEAD.rsplit(".", 1)[-1]}),
    )


def test_diff_confirmed_delete_stays_stale_in_declarations(
    tmp_path, monkeypatch,
):
    """Git proves disappearance, not intentional redundancy.  Even alongside
    another live card, automatic reconcile must preserve the citation."""
    kept = "Demo.Chapter15.Tf.KeptResult.holds"
    body = _decl_body(kept, _DEAD)
    cfg, edits = _setup(tmp_path, monkeypatch, body=body)
    _confirm_delete_diff(monkeypatch)

    r = auto_reconcile_issue(cfg, 98, dry_run=True, diff_window=("OLD", "NEW"))

    assert r["updated"] is False
    assert _DEAD in r["kept_stale"]
    assert _DEAD in extract_declarations_from_issue_body(body)
    assert kept in extract_declarations_from_issue_body(body)
    assert edits == []


def test_diff_confirmed_delete_stays_stale_in_signatures_and_keeps_body(
    tmp_path, monkeypatch,
):
    """Ch14-style signature blocks get the same conservative treatment."""
    kept = "Demo.Chapter15.Tf.KeptResult.holds"
    body = _sig_body(_DEAD, kept)  # dead FIRST exercises the old header-loss bug
    cfg, edits = _setup(tmp_path, monkeypatch, body=body)
    _confirm_delete_diff(monkeypatch)

    r = auto_reconcile_issue(cfg, 98, dry_run=True, diff_window=("OLD", "NEW"))

    assert r["updated"] is False
    assert _DEAD in r["kept_stale"]
    assert body.startswith(SIGNATURES_HEADER)
    assert _DEAD in extract_declarations_from_issue_body(body)
    assert kept in extract_declarations_from_issue_body(body)
    assert edits == []


def test_diff_confirmed_delete_of_sole_card_stays_stale(tmp_path, monkeypatch):
    """A sole declaration disappearing is missing work, not housekeeping.  Keep
    the citation loud so the issue cannot silently become a zero-card ghost."""
    body = _decl_body(_DEAD)
    cfg, edits = _setup(tmp_path, monkeypatch, body=body)
    _confirm_delete_diff(monkeypatch)
    import marathon.deck.cards as cards_mod
    monkeypatch.setattr(cards_mod, "missing_suggestions_for_issue", lambda c, n: [])

    r = auto_reconcile_issue(cfg, 98, dry_run=True, diff_window=("OLD", "NEW"))

    assert r["updated"] is False
    assert _DEAD in r["kept_stale"]
    assert _DEAD in extract_declarations_from_issue_body(body)
    assert edits == []


def test_signature_sole_pair_different_leaf_is_only_a_human_proposal(
    tmp_path, monkeypatch,
):
    """One removal + one addition in a commit does not prove a rename."""
    new = "Demo.Chapter15.Tf.KeptResult.holds"
    body = _sig_body(_DEAD)
    cfg, edits = _setup(tmp_path, monkeypatch, body=body)
    import marathon.review.verified_decls as vd
    monkeypatch.setattr(
        vd, "declarations_added_removed_in_diff",
        lambda repo, old, head, paths: ({new.rsplit(".", 1)[-1]}, {"gone"}),
    )

    r = auto_reconcile_issue(cfg, 98, dry_run=True, diff_window=("OLD", "NEW"))

    assert r["updated"] is False
    assert r["proposed_renames"] == [{"old": _DEAD, "new": new}]
    assert "human confirmation" in r["message"]
    assert "new_body" not in r
    assert edits == []


def test_frozen_confirmed_delete_is_flagged_without_dropping(
    tmp_path, monkeypatch,
):
    from marathon.review.state import IssueState, ReviewState, save_state

    body = _decl_body(_DEAD)
    cfg, edits = _setup(tmp_path, monkeypatch, body=body)
    save_state(cfg, ReviewState(issues={
        98: IssueState(status="verified", verdict_ts="2026-07-01T00:00:00Z"),
    }))
    _confirm_delete_diff(monkeypatch)
    import marathon.deck.cards as cards_mod
    monkeypatch.setattr(cards_mod, "missing_suggestions_for_issue", lambda c, n: [])

    r = auto_reconcile_issue(cfg, 98, diff_window=("OLD", "NEW"))

    assert r["updated"] is False
    assert r["flagged_deletes"] == [_DEAD]
    assert _DEAD in r["kept_stale"]
    assert edits == []


def test_live_decl_discovery_unions_source_ahead_of_stale_audit(tmp_path):
    """A just-merged rename is visible before the async audit snapshot refresh;
    otherwise reconcile records the new HEAD while still blind to the target."""
    cfg = make_cfg(tmp_path)
    cfg.chapters[15] = ChapterRegistry(chapter=15, entries=[(98, "Lemma 15.1")])
    target = cfg.repo_dir / "Demo" / "Chapter15"
    target.mkdir(parents=True, exist_ok=True)
    (target / "Tf.lean").write_text(_SRC)
    install_snapshot(cfg, make_snapshot([], cfg.repo_dir))  # deliberately stale

    assert "Demo.Chapter15.Tf.KeptResult.holds" in live_chapter_decls(cfg, 15)

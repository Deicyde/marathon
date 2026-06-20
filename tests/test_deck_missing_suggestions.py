"""``missing_suggestions_for_issue`` — the deck's "this may already be
formalized" warning shown when the reviewer opens *flag a missing declaration*.

The headline case is the REAL #94 incident reconstructed faithfully: the
function-multiplication decl the reviewer was about to flag missing already
exists in the code, but under a name introduced by a rename (#107:
``smul_smoothFunction`` → ``smul_of_contMDiff``) that POST-DATES the audit
snapshot — so neither the snapshot nor any issue body knows the new name. The
suggestion must be recovered from a scan of the CURRENT code.

Reuses the offline fixtures from :mod:`tests.test_deck_source_fallback` (a
tmp_path repo + a fabricated snapshot + a monkeypatched bulk issue fetch). No
network / lake / Aristotle.
"""

from __future__ import annotations

import json

from marathon.review.config import ChapterRegistry
from marathon.deck.cards import missing_suggestions_for_issue

from tests.test_deck_source_fallback import (
    make_cfg,
    make_snapshot,
    install_snapshot,
    patch_bulk,
    _bundle_body,
    _decl,
)


# A Tf namespace holding, in the CURRENT code:
#   tprod              — Prop 12.22 (part 2), cited by issue 22
#   smul_of_contMDiff  — Prop 12.22 (part 1), the RENAME of the snapshot's
#                        smul_smoothFunction; present ONLY in the code
#   smulFun            — Prop 12.22 (part 3), cited by the SIBLING issue 23
#   other              — Prop 12.30, an UNRELATED same-namespace result
#   helper             — no textbook result
_SRC = """\
import Mathlib

namespace Demo.Chapter14.Tf

/-- Proposition 12.22 (part 2): the tensor product preserves smoothness. -/
theorem tprod : True := trivial

/-- Proposition 12.22 (part 1): multiplication by a smooth function preserves
smoothness of mixed tensor fields. -/
theorem smul_of_contMDiff : True := trivial

/-- Proposition 12.22 (part 3): a sibling-owned smoothness closure lemma. -/
theorem smulFun : True := trivial

/-- Proposition 12.30: an unrelated result in the same namespace. -/
theorem other : True := trivial

/-- A private algebra helper — no textbook result. -/
theorem helper : True := trivial

end Demo.Chapter14.Tf
"""


def _cfg_22_23(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.chapters[14] = ChapterRegistry(
        chapter=14, entries=[(22, "Prop 12.22"), (23, "closure family")]
    )
    return cfg


def _install(cfg, monkeypatch, *, issue22_body, issue23_body, title22):
    (cfg.repo_dir / "Demo" / "Chapter14").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter14" / "Tf.lean").write_text(_SRC)
    # Snapshot PRE-rename: it has smul_smoothFunction (the OLD name, gone from
    # the code), NOT smul_of_contMDiff. tprod/smulFun/other/helper are current.
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Tf.tprod", module="Demo.Chapter14.Tf"),
        _decl("Demo.Chapter14.Tf.smul_smoothFunction", module="Demo.Chapter14.Tf"),
        _decl("Demo.Chapter14.Tf.smulFun", module="Demo.Chapter14.Tf"),
        _decl("Demo.Chapter14.Tf.other", module="Demo.Chapter14.Tf"),
        _decl("Demo.Chapter14.Tf.helper", module="Demo.Chapter14.Tf"),
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {"title": title22, "state": "OPEN", "labels": set(),
             "body": issue22_body},
        23: {"title": "Ch14 def — smoothness closure family", "state": "OPEN",
             "labels": set(), "body": issue23_body},
    })


def test_rename_incident_surfaces_uncited_code_decl(tmp_path, monkeypatch):
    """The faithful #94: issue 22 cites the OLD name smul_smoothFunction (now a
    stale citation, renamed in the code to smul_of_contMDiff). The suggestion
    recovers the renamed decl from the CODE — not the snapshot, not a body —
    with cited_by==[] (no body references the new name)."""
    cfg = _cfg_22_23(tmp_path)
    _install(
        cfg, monkeypatch,
        title22="Proposition 12.22 — tensor product & function multiplication",
        issue22_body=_bundle_body(
            "Demo.Chapter14.Tf.tprod",
            "Demo.Chapter14.Tf.smul_smoothFunction",  # OLD name (stale citation)
        ),
        issue23_body=_bundle_body("Demo.Chapter14.Tf.smulFun"),
    )
    out = missing_suggestions_for_issue(cfg, 22)
    by_decl = {c["decl"]: c for c in out}

    # The rename is recovered FROM THE CODE (absent from snapshot + bodies).
    assert "Demo.Chapter14.Tf.smul_of_contMDiff" in by_decl
    sug = by_decl["Demo.Chapter14.Tf.smul_of_contMDiff"]
    assert sug["matched_key"] == "12.22"
    assert sug["cited_by"] == []          # no body cites the new name
    assert sug["in_code"] is True
    assert sug["source_file"].endswith("Tf.lean")
    assert "smooth function" in (sug["snippet"] or "").lower()


def test_sibling_owned_decl_surfaces_with_citation(tmp_path, monkeypatch):
    """A same-result decl OWNED BY A SIBLING issue (smulFun, cited by #23) is
    surfaced for #22 with cited_by==[23] — and ranked before the uncited one."""
    cfg = _cfg_22_23(tmp_path)
    _install(
        cfg, monkeypatch,
        title22="Proposition 12.22 — tensor product & function multiplication",
        issue22_body=_bundle_body("Demo.Chapter14.Tf.tprod"),
        issue23_body=_bundle_body("Demo.Chapter14.Tf.smulFun"),
    )
    out = missing_suggestions_for_issue(cfg, 22)
    by_decl = {c["decl"]: c for c in out}
    assert by_decl["Demo.Chapter14.Tf.smulFun"]["cited_by"] == [23]
    # sibling-OWNED ranks before the uncited code-only duplicate.
    decls = [c["decl"] for c in out]
    assert decls.index("Demo.Chapter14.Tf.smulFun") < decls.index(
        "Demo.Chapter14.Tf.smul_of_contMDiff"
    )


def test_result_key_gate_and_cited_self_excluded(tmp_path, monkeypatch):
    """The result-key docstring gate excludes a same-namespace decl for a
    DIFFERENT result (other, 12.30) and unrelated plumbing (helper); a decl the
    issue itself cites (tprod) is never suggested."""
    cfg = _cfg_22_23(tmp_path)
    _install(
        cfg, monkeypatch,
        title22="Proposition 12.22 — tensor product & function multiplication",
        issue22_body=_bundle_body("Demo.Chapter14.Tf.tprod"),
        issue23_body=_bundle_body("Demo.Chapter14.Tf.smulFun"),
    )
    decls = {c["decl"] for c in missing_suggestions_for_issue(cfg, 22)}
    assert "Demo.Chapter14.Tf.other" not in decls     # 12.30 ≠ 12.22
    assert "Demo.Chapter14.Tf.helper" not in decls     # no result key
    assert "Demo.Chapter14.Tf.tprod" not in decls      # cited by #22 itself


def test_unnumbered_title_returns_empty(tmp_path, monkeypatch):
    """An un-numbered ('def') issue has no result to be missing a part of —
    its body citations are authoritative, so no suggestions."""
    cfg = _cfg_22_23(tmp_path)
    _install(
        cfg, monkeypatch,
        title22="Ch14 def — tensor field smoothness subtype",  # no '12.xx'
        issue22_body=_bundle_body("Demo.Chapter14.Tf.tprod"),
        issue23_body=_bundle_body("Demo.Chapter14.Tf.smulFun"),
    )
    assert missing_suggestions_for_issue(cfg, 22) == []


def test_no_snapshot_and_no_repo_returns_empty(tmp_path, monkeypatch):
    """With neither a snapshot nor a scannable Lean repo there is no source of
    decl text — degrade to []."""
    cfg = _cfg_22_23(tmp_path)
    # No snapshot installed, no .lean written. Bodies still resolve namespaces?
    # No — without a snapshot the cited names don't resolve, but the gate fires
    # first on "no snapshot AND no scannable repo".
    patch_bulk(monkeypatch, {
        22: {"title": "Proposition 12.22 — foo", "state": "OPEN", "labels": set(),
             "body": _bundle_body("Demo.Chapter14.Tf.tprod")},
        23: {"title": "Ch14 def", "state": "OPEN", "labels": set(),
             "body": _bundle_body("Demo.Chapter14.Tf.smulFun")},
    })
    assert missing_suggestions_for_issue(cfg, 22) == []


def test_pure_read_records_no_verdict(tmp_path, monkeypatch):
    """The suggestion read records NO verdict and NO review state. (It opens the
    ledger read-cache `marathon.db`, exactly as the surfacing read does — that
    benign cache is excluded; nothing else under .marathon may change, and the
    deck verdict store must never appear.)"""
    cfg = _cfg_22_23(tmp_path)
    _install(
        cfg, monkeypatch,
        title22="Proposition 12.22 — tensor product & function multiplication",
        issue22_body=_bundle_body("Demo.Chapter14.Tf.tprod"),
        issue23_body=_bundle_body("Demo.Chapter14.Tf.smulFun"),
    )

    def _snapshot_marathon():
        root = cfg.repo_dir / ".marathon"
        out = {}
        for p in sorted(root.rglob("*")):
            # Exclude the ledger read-cache (created on first access; a pure
            # READ side-effect shared with the surfacing path).
            if p.is_file() and not p.name.startswith("marathon.db"):
                out[str(p.relative_to(root))] = p.read_bytes()
        return out

    # Warm the ledger cache, snapshot, then assert a subsequent read mutates
    # nothing else AND records no verdict.
    missing_suggestions_for_issue(cfg, 22)
    before = _snapshot_marathon()
    missing_suggestions_for_issue(cfg, 22)
    assert _snapshot_marathon() == before
    assert not (cfg.repo_dir / ".marathon/review/deck-subdecks.json").exists()


def test_result_key_prefix_collision_excluded(tmp_path, monkeypatch):
    """A short result key (12.2) must NOT match a longer number (12.22): the
    match is boundary-anchored, so a 12.2 issue never claims a 12.22 decl is
    'already formalized'."""
    cfg = _cfg_22_23(tmp_path)
    (cfg.repo_dir / "Demo" / "Chapter14").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter14" / "Tf.lean").write_text(
        "import Mathlib\n\nnamespace Demo.Chapter14.Tf\n\n"
        "/-- Lemma 12.2: the exactly-12.2 result. -/\n"
        "theorem exactlyTwoTwo : True := trivial\n\n"
        "/-- Proposition 12.22: a different, longer-numbered result. -/\n"
        "theorem longerTwentyTwo : True := trivial\n\n"
        "/-- The issue's own cited decl. -/\n"
        "theorem cited : True := trivial\n\n"
        "end Demo.Chapter14.Tf\n"
    )
    install_snapshot(cfg, make_snapshot(
        [_decl("Demo.Chapter14.Tf.cited", module="Demo.Chapter14.Tf")], cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {"title": "Lemma 12.2 — the short-key result", "state": "OPEN",
             "labels": set(), "body": _bundle_body("Demo.Chapter14.Tf.cited")},
        23: {"title": "Ch14 def", "state": "OPEN", "labels": set(),
             "body": _bundle_body("Demo.Chapter14.Tf.cited")},
    })
    by_decl = {c["decl"]: c for c in missing_suggestions_for_issue(cfg, 22)}
    assert "Demo.Chapter14.Tf.exactlyTwoTwo" in by_decl
    assert by_decl["Demo.Chapter14.Tf.exactlyTwoTwo"]["matched_key"] == "12.2"
    # the 12.22 decl is NOT surfaced for a 12.2 issue (no prefix collision)
    assert "Demo.Chapter14.Tf.longerTwentyTwo" not in by_decl


def test_body_only_mention_not_matched(tmp_path, monkeypatch):
    """A result number appearing only in the PROOF BODY (not the leading
    docstring) does not count — the gate is the decl's own docstring."""
    cfg = _cfg_22_23(tmp_path)
    (cfg.repo_dir / "Demo" / "Chapter14").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter14" / "Tf.lean").write_text(
        "import Mathlib\n\nnamespace Demo.Chapter14.Tf\n\n"
        "/-- A helper with no textbook number in its docstring. -/\n"
        "theorem helper : True := by\n  -- follows from Proposition 12.22\n  trivial\n\n"
        "/-- The issue's own cited decl. -/\n"
        "theorem cited : True := trivial\n\n"
        "end Demo.Chapter14.Tf\n"
    )
    install_snapshot(cfg, make_snapshot(
        [_decl("Demo.Chapter14.Tf.cited", module="Demo.Chapter14.Tf")], cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {"title": "Proposition 12.22 — foo", "state": "OPEN", "labels": set(),
             "body": _bundle_body("Demo.Chapter14.Tf.cited")},
        23: {"title": "Ch14 def", "state": "OPEN", "labels": set(),
             "body": _bundle_body("Demo.Chapter14.Tf.cited")},
    })
    decls = {c["decl"] for c in missing_suggestions_for_issue(cfg, 22)}
    assert "Demo.Chapter14.Tf.helper" not in decls  # body mention doesn't count


def test_demo_missing_suggestions_matches_real_shape(tmp_path, monkeypatch):
    """The demo candidate dict carries EXACTLY the same keys as the real
    cards.missing_suggestions_for_issue dict (drift guard for the frontend)."""
    cfg = _cfg_22_23(tmp_path)
    _install(
        cfg, monkeypatch,
        title22="Proposition 12.22 — tensor product & function multiplication",
        issue22_body=_bundle_body("Demo.Chapter14.Tf.tprod"),
        issue23_body=_bundle_body("Demo.Chapter14.Tf.smulFun"),
    )
    real = missing_suggestions_for_issue(cfg, 22)
    assert real, "fixture should produce at least one real candidate"
    real_keys = set(real[0].keys())

    from marathon.deck import demo as demo_mod
    monkeypatch.setattr(
        demo_mod, "_BY_ID",
        {1: {"title": "Proposition 12.22 — function multiplication"}},
    )
    demo = demo_mod.demo_missing_suggestions(1)
    assert demo and set(demo[0].keys()) == real_keys

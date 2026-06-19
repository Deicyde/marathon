"""The leading INFORMAL-APPROVAL card: the human approves/rejects the issue's
LLM-rendered informal text ONCE, up front. Approving clears the per-card
"LLM-rendered — verify" warning and, at ship, flips the issue body's
"⚠️ LLM-rendered … verification pending" marker to a verified marker.

Contract under test:
* ``_informal_slot`` — collects the LLM-rendered renderings + overlays the one
  approval verdict (reserved ``INFORMAL_KEY``); ``None`` when nothing is
  LLM-rendered;
* the approval verdict is ISOLATED — keyed by ``INFORMAL_KEY`` (never a real
  decl), it never appears as a card and never enters progress/summary;
* ``refresh._flip_informal_marker`` — flips the marker(s) in the Informal
  Statement section ONLY (both textual variants), idempotently, preserving
  every other section verbatim;
* ``ship_issue`` — flips the body marker (best-effort) ONLY when the informal
  text was approved, independent of the KEEP/FIX outcome.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import marathon.deck.cards as deck_cards
import marathon.deck.refresh as refresh
import marathon.deck.subdeck as subdeck
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels

DATE = "2026-06-18"


def _cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo="o/r",
        parent_issue=1,
        referee_path=repo / ".marathon/referee.md",
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={14: ChapterRegistry(chapter=14, entries=[(30, "x")])},
    )


def _view(name, *, is_llm, text):
    return SimpleNamespace(name=name, informal_is_llm=is_llm, informal_rendering=text)


def _detail(issue_informal, issue_informal_is_llm, decls=()):
    return SimpleNamespace(
        issue_informal=issue_informal,
        issue_informal_is_llm=issue_informal_is_llm,
        decls=list(decls),
    )


# --- _issue_informal_statement (parse the body's ### Informal Statement) ----

_BODY = """**Parent**: #1

### Lean signatures

```lean
def Foo : T
```

### Informal Statement

*⚠️ LLM-rendered from common knowledge; verification pending.*

> **Proposition 12.19 (Lee p. 317).** Let $A$ be a tensor field.
> (a) it is smooth.

### Mechanical accuracy

- a note mentioning LLM-rendered, verification pending in ANOTHER section
"""


def test_issue_informal_statement_extracts_and_strips():
    text, pending = deck_cards._issue_informal_statement(_BODY)
    assert pending is True
    # marker line stripped, blockquote markers stripped, prose kept
    assert "verification pending" not in text
    assert text.startswith("**Proposition 12.19")
    assert "> " not in text
    assert "(a) it is smooth." in text


def test_issue_informal_statement_absent_section():
    text, pending = deck_cards._issue_informal_statement(
        "### Lean signatures\n\n```lean\ndef Foo : T\n```\n"
    )
    assert text is None and pending is False


def test_issue_informal_statement_strips_blockquoted_marker():
    # Regression: a BLOCKQUOTED marker line ("> *⚠️ …*") must still be dropped
    # (the blockquote prefix is stripped BEFORE the marker check).
    body = (
        "### Informal Statement\n\n"
        "> *⚠️ LLM-rendered from common knowledge; verification pending.*\n"
        "> **Prop.** Foo is foo.\n\n### Verdict\n"
    )
    text, pending = deck_cards._issue_informal_statement(body)
    assert pending is True
    assert "LLM-rendered" not in text           # blockquoted marker dropped
    assert text == "**Prop.** Foo is foo."


def test_issue_informal_statement_not_pending_when_verified():
    body = (
        "### Informal Statement\n\n"
        "*✅ Verified against the source (human-approved 2026-06-18)*\n\n"
        "> The result says foo.\n\n### Verdict\n"
    )
    text, pending = deck_cards._issue_informal_statement(body)
    assert pending is False
    assert text == "The result says foo."


# --- _informal_slot (pure) — ONE top-level statement, not per-decl items ----

def test_informal_slot_none_when_no_top_level_text():
    # no body informal statement AND no llm-rendered headline → nothing.
    detail = _detail(None, False, decls=[_view("N.A", is_llm=False, text=None)])
    assert subdeck._informal_slot(detail, {}) is None


def test_informal_slot_uses_the_issue_top_level_statement():
    detail = _detail("Prop 12.19: the result says foo.", True,
                     decls=[_view("N.A", is_llm=True, text="A says foo.")])
    slot = subdeck._informal_slot(detail, {})
    assert slot["is_llm"] is True
    assert slot["verdict"] is None
    # the ONE statement is the issue top-level text, NOT the per-decl rendering
    assert slot["statement"] == "Prop 12.19: the result says foo."
    assert "items" not in slot
    # verdict overlaid + mapped to the past-tense wire form the frontend reads
    approved = subdeck._informal_slot(
        detail, {subdeck.INFORMAL_KEY: {"verdict": "verify", "note": None}}
    )
    assert approved["verdict"] == "verified"
    rejected = subdeck._informal_slot(
        detail, {subdeck.INFORMAL_KEY: {"verdict": "reject", "note": "wrong"}}
    )
    assert rejected["verdict"] == "rejected" and rejected["note"] == "wrong"


def test_informal_slot_falls_back_to_headline_when_no_body_section():
    # no body informal statement → fall back to the headline decl's rendering.
    detail = _detail(None, False, decls=[
        _view("N.Head", is_llm=True, text="Head rendering."),
        _view("N.Other", is_llm=True, text="Other rendering."),
    ])
    slot = subdeck._informal_slot(detail, {})
    assert slot["statement"] == "Head rendering."   # headline only, not all decls
    assert slot["is_llm"] is True


def test_informal_slot_not_pending_when_body_already_verified():
    # body statement present but already verified (is_llm False) → still a slot,
    # but is_llm False so the frontend shows no approval card and suppresses the
    # per-card warning.
    detail = _detail("Prop 12.19 (verified).", False)
    slot = subdeck._informal_slot(detail, {})
    assert slot is not None and slot["is_llm"] is False


# --- approval verdict isolation --------------------------------------------

def test_informal_verdict_is_isolated_from_the_decl_store(tmp_path):
    """The reserved INFORMAL_KEY verdict is stored like any other but, because
    it is never a real decl name, it never matches a card view — it stays out
    of the per-decl review entirely. ``issue_verdicts`` round-trips it; the
    decl-keyed reads ignore it."""
    cfg = _cfg(tmp_path)
    subdeck.record_decl_verdict(cfg, 30, "N.RealDecl", "verify")
    subdeck.record_decl_verdict(cfg, 30, subdeck.INFORMAL_KEY, "verify")

    recs = subdeck.issue_verdicts(cfg, 30)
    assert recs[subdeck.INFORMAL_KEY]["verdict"] == "verify"
    assert recs["N.RealDecl"]["verdict"] == "verify"
    # _is_reviewed over the reserved record is irrelevant — it is never asked of
    # a card; but assert the helper does not crash on it.
    assert subdeck._is_reviewed(recs[subdeck.INFORMAL_KEY]) is True


def test_informal_reject_requires_a_note_like_any_reject(tmp_path):
    cfg = _cfg(tmp_path)
    import pytest

    with pytest.raises(ValueError):
        subdeck.record_decl_verdict(cfg, 30, subdeck.INFORMAL_KEY, "reject")
    # verify needs no note
    rec = subdeck.record_decl_verdict(cfg, 30, subdeck.INFORMAL_KEY, "verify")
    assert rec["verdict"] == "verify"


# --- _flip_informal_marker (pure, section-scoped, idempotent) --------------

_COREVIEWER_BODY = """**Parent**: #1

### Lean signatures

```lean
def Foo : T
```

### Informal Statement

*⚠️ LLM-rendered from common knowledge; verification pending.*

> **Prop.** Foo is foo.

### Mechanical accuracy

- the informal rendering was LLM-rendered, verification pending elsewhere — DO NOT FLIP (wrong section)

### Verdict

_(VERIFIED / REJECTED)_
"""


def test_flip_informal_marker_coreviewer_variant_section_scoped():
    new_body, flipped = refresh._flip_informal_marker(_COREVIEWER_BODY, DATE)
    assert flipped is True
    # the Informal Statement marker is flipped, asterisks preserved
    assert f"*✅ Verified against the source (human-approved {DATE})*" in new_body
    assert "LLM-rendered from common knowledge" not in new_body
    # the LLM mention in the Mechanical accuracy section is UNTOUCHED
    assert "LLM-rendered, verification pending elsewhere — DO NOT FLIP" in new_body
    # every other section is preserved verbatim
    assert "### Lean signatures" in new_body and "def Foo : T" in new_body
    assert "### Verdict" in new_body


def test_flip_informal_marker_spec_auditor_variant():
    body = "### Informal Statement\n\n*⚠️ LLM-rendered, verification pending*\n\nfoo\n"
    new_body, flipped = refresh._flip_informal_marker(body, DATE)
    assert flipped is True
    assert f"*✅ Verified against the source (human-approved {DATE})*" in new_body


def test_flip_informal_marker_idempotent():
    once, f1 = refresh._flip_informal_marker(_COREVIEWER_BODY, DATE)
    assert f1 is True
    twice, f2 = refresh._flip_informal_marker(once, DATE)
    assert f2 is False            # nothing left to flip
    assert twice == once          # unchanged


def test_flip_matches_what_display_treats_as_pending():
    # Regression: the flip regex must match EXACTLY what the display side treats
    # as a pending marker — otherwise an emoji-stripped marker shows the approval
    # card but the flip silently no-ops (orphaned approval). The ⚠️ is optional.
    for marker in (
        "*⚠️ LLM-rendered, verification pending*",
        "*LLM-rendered, verification pending*",                 # emoji stripped
        "*⚠️ LLM-rendered from common knowledge; verification pending.*",
        "*LLM-rendered from common knowledge; verification pending.*",
    ):
        body = f"### Informal Statement\n\n{marker}\n\nfoo\n"
        _t, pending = deck_cards._issue_informal_statement(body)
        new_body, flipped = refresh._flip_informal_marker(body, DATE)
        assert pending == flipped is True, f"display/flip disagree on {marker!r}"
        assert "LLM-rendered" not in new_body
        assert f"✅ Verified against the source (human-approved {DATE})" in new_body


def test_flip_informal_marker_absent_section_is_noop():
    body = "### Lean signatures\n\n```lean\ndef Foo : T\n```\n"
    new_body, flipped = refresh._flip_informal_marker(body, DATE)
    assert flipped is False
    assert new_body == body


def test_flip_informal_marker_flips_all_in_section():
    body = (
        "### Informal Statement\n\n"
        "*⚠️ LLM-rendered, verification pending*\n\n"
        "> (a) foo\n\n"
        "*⚠️ LLM-rendered from common knowledge; verification pending.*\n\n"
        "> (b) bar\n"
    )
    new_body, flipped = refresh._flip_informal_marker(body, DATE)
    assert flipped is True
    assert new_body.count("✅ Verified against the source") == 2
    assert "LLM-rendered" not in new_body


# --- mark_informal_verified (the ship write) -------------------------------

def test_mark_informal_verified_writes_when_marker_present(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    writes: list = []
    monkeypatch.setattr(
        "marathon.review.github.issue_body", lambda num, repo: _COREVIEWER_BODY
    )
    monkeypatch.setattr(
        refresh, "_gh_edit_body",
        lambda repo, num, body: writes.append((repo, num, body)),
    )
    res = refresh.mark_informal_verified(cfg, 30)
    assert res["updated"] is True
    assert len(writes) == 1
    assert "✅ Verified against the source" in writes[0][2]


def test_mark_informal_verified_noop_without_marker(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    writes: list = []
    monkeypatch.setattr(
        "marathon.review.github.issue_body",
        lambda num, repo: "### Informal Statement\n\nalready verified\n",
    )
    monkeypatch.setattr(
        refresh, "_gh_edit_body",
        lambda repo, num, body: writes.append(body),
    )
    res = refresh.mark_informal_verified(cfg, 30)
    assert res["updated"] is False
    assert writes == []  # writes NOTHING when there is no marker


# --- ship_issue: marker flip gated on approval -----------------------------

def _complete_subdeck() -> dict:
    return {
        "issue": 30, "complete": True,
        "progress": {"reviewed": 1, "total": 1},
        "cards": [{"name": "N.Foo", "surfaced": False}],
        "auto_approved": [],
        "summary": {"kept": ["N.Foo"], "rejected": [], "deferred": []},
    }


def _patch_ship_common(monkeypatch):
    import marathon.deck.verdicts as verdicts_mod

    monkeypatch.setattr(
        verdicts_mod, "apply_verdict",
        lambda cfg, issue, verdict, note=None: None,
    )
    monkeypatch.setattr(subdeck, "build_subdeck", lambda c, i, **k: _complete_subdeck())
    monkeypatch.setattr(subdeck, "clear_issue", lambda cfg, issue: None)
    monkeypatch.setattr(subdeck, "_next_ready_issue", lambda cfg, issue: None)


def test_ship_flips_marker_when_informal_approved(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_ship_common(monkeypatch)
    monkeypatch.setattr(
        subdeck, "issue_verdicts",
        lambda c, i: {subdeck.INFORMAL_KEY: {"verdict": "verify"}},
    )
    marks: list = []
    monkeypatch.setattr(
        refresh, "mark_informal_verified",
        lambda c, i: (marks.append(i) or {"ok": True, "updated": True}),
    )

    res = subdeck.ship_issue(cfg, 30)

    assert marks == [30]
    assert res["informal_verified"] is True
    assert "marked the issue's informal statement verified" in res["message"]


def test_ship_does_not_flip_marker_when_not_approved(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_ship_common(monkeypatch)
    monkeypatch.setattr(subdeck, "issue_verdicts", lambda c, i: {})  # no informal
    marks: list = []
    monkeypatch.setattr(
        refresh, "mark_informal_verified",
        lambda c, i: (marks.append(i) or {"ok": True, "updated": True}),
    )

    res = subdeck.ship_issue(cfg, 30)

    assert marks == []                       # never called
    assert res["informal_verified"] is False

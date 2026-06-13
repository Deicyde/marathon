"""Offline tests for marathon.audit.spec_card — the spec card (machine
half) and the structural semantic-delta signal.

Binding rulings under test:

* the card's "Definitions you must read" section contains ONLY the trust
  kernel's project-local defs — never the whole file/snapshot;
* the evidence block carries the COMPUTED tier (trust.compute_tier),
  axioms beyond whitelist, sorry status, deception tags;
* ``semantic_delta`` emits one of the CLOSED structural classes from
  fingerprints — strengthened/weakened are NOT here; cone members that
  change/appear/vanish are NAMED (the load-bearing #50 case: a def's
  value changes while the downstream theorem's own type is byte-identical
  -> ``cone-changed`` naming the def).

No subprocesses, no network, no Lean toolchain.
"""

from __future__ import annotations

from marathon.audit.lean_template import DEFAULT_TRUSTED_PREFIXES
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.audit.spec_card import (
    DELTA_CLASSES,
    SemanticDelta,
    SpecCard,
    semantic_delta,
)
from marathon.audit.trust import record_spec_verdict
from marathon.ledger import Ledger


# --- builders ----------------------------------------------------------------


def mk_decl(
    name="Foo.bar",
    kind="theorem",
    module="Foo",
    status="ok",
    type_pp="Nat",
    value_pp=None,
    cone=(),
    axioms=("propext",),
    has_sorry=False,
    tags=(),
    reason=None,
) -> DeclAudit:
    return DeclAudit(
        name=name, kind=kind, module=module, status=status,
        type_pp=type_pp, value_pp=value_pp, cone=list(cone),
        axioms=list(axioms), has_sorry=has_sorry, tags=list(tags),
        reason=reason,
    )


def mk_snapshot(decls, **kw) -> AuditSnapshot:
    defaults = dict(
        repo_dir="/r",
        modules=["Foo"],
        toolchain="leanprover/lean4:v4.28.0",
        lean_version="4.28.0",
        package_revs={},
        trusted_prefixes=list(DEFAULT_TRUSTED_PREFIXES),
        created_at="2026-06-13T00:00:00+00:00",
        decls=list(decls),
        failures=[],
    )
    defaults.update(kw)
    return AuditSnapshot(**defaults)


def cone_snapshot():
    """thm references local def `energy`; plus an UNRELATED local def
    `noise` that must NOT show up in thm's card."""
    energy = mk_decl(name="Foo.energy", kind="def", type_pp="Nat",
                     value_pp="0", cone=[])
    noise = mk_decl(name="Foo.noise", kind="def", type_pp="Nat",
                    value_pp="99", cone=[])
    thm = mk_decl(name="Foo.thm", kind="theorem",
                  type_pp="Foo.energy = 0", cone=["Foo.energy"])
    return mk_snapshot([thm, energy, noise])


def ledger_for(tmp_path) -> Ledger:
    return Ledger.for_repo(tmp_path)


# --- from_snapshot + render_markdown -----------------------------------------


def test_card_lists_only_kernel_defs_not_whole_file(tmp_path):
    led = ledger_for(tmp_path)
    card = SpecCard.from_snapshot("Foo.thm", cone_snapshot(), led)
    md = card.render_markdown()
    # The kernel def appears...
    assert "Foo.energy" in md
    # ...the unrelated local def does NOT (only the kernel is shown).
    assert "Foo.noise" not in md
    assert card.kernel.size_decls == 1


def test_card_statement_and_evidence_table(tmp_path):
    led = ledger_for(tmp_path)
    thm = mk_decl(name="Foo.t", type_pp="1 + 1 = 2", cone=[],
                  axioms=["propext", "MyAxiom"], has_sorry=False,
                  tags=["vacuous_body"])
    card = SpecCard.from_snapshot("Foo.t", mk_snapshot([thm]), led)
    md = card.render_markdown()
    assert "## Statement" in md
    assert "1 + 1 = 2" in md
    assert "## Evidence" in md
    assert "| facet | value |" in md
    # axioms beyond whitelist surfaced; sorryAx/propext not listed.
    assert "MyAxiom" in card.evidence.axioms_beyond_whitelist
    assert "propext" not in card.evidence.axioms_beyond_whitelist
    assert card.evidence.deception_tags == ["vacuous_body"]
    assert card.evidence.sorry is False


def test_card_tier_is_computed_t1_when_clean(tmp_path):
    led = ledger_for(tmp_path)
    card = SpecCard.from_snapshot("Foo.thm", cone_snapshot(), led)
    # axiom-clean, no tags, no human verdict -> T1.
    assert card.evidence.tier == "T1"
    assert "trust tier: **T1**" in card.render_markdown()


def test_card_tier_reflects_human_verdict(tmp_path):
    led = ledger_for(tmp_path)
    snap = cone_snapshot()
    record_spec_verdict(led, "Foo.thm", snap)  # pins T2
    card = SpecCard.from_snapshot("Foo.thm", snap, led)
    assert card.evidence.tier == "T2"


def test_card_dirty_axioms_block_t1(tmp_path):
    led = ledger_for(tmp_path)
    thm = mk_decl(name="Foo.t", axioms=["propext", "sketchyAxiom"], cone=[])
    card = SpecCard.from_snapshot("Foo.t", mk_snapshot([thm]), led)
    assert card.evidence.tier == "T0"
    assert "sketchyAxiom" in card.evidence.axioms_beyond_whitelist


def test_card_empty_kernel_says_trusted_vocabulary(tmp_path):
    led = ledger_for(tmp_path)
    thm = mk_decl(name="Foo.t", type_pp="ContMDiff IR IR top f", cone=[])
    card = SpecCard.from_snapshot("Foo.t", mk_snapshot([thm]), led)
    md = card.render_markdown()
    assert card.kernel.size_decls == 0
    assert "Zero new definitions to read" in md
    # Footer always reminds Mathlib vocabulary is trusted.
    assert "trusted" in md.lower()


def test_card_footer_notes_mathlib_trusted_and_no_proofs(tmp_path):
    led = ledger_for(tmp_path)
    card = SpecCard.from_snapshot("Foo.thm", cone_snapshot(), led)
    md = card.render_markdown()
    assert "Mathlib and core constants are trusted vocabulary" in md
    assert "Proof bodies are never in the kernel" in md


def test_card_def_member_shows_value(tmp_path):
    led = ledger_for(tmp_path)
    card = SpecCard.from_snapshot("Foo.thm", cone_snapshot(), led)
    md = card.render_markdown()
    # The def's value (the meaning that matters) is rendered.
    assert "value (the meaning that matters)" in md


def test_card_unknown_target_is_renderable(tmp_path):
    led = ledger_for(tmp_path)
    bad = mk_decl(name="Foo.bad", status="unknown", type_pp=None,
                  has_sorry=None, reason="boom", axioms=[])
    card = SpecCard.from_snapshot("Foo.bad", mk_snapshot([bad]), led)
    md = card.render_markdown()
    assert card.evidence.tier == "UNKNOWN"
    assert "did not elaborate" in md or "absent" in md


def test_card_claude_slots_default_empty(tmp_path):
    led = ledger_for(tmp_path)
    card = SpecCard.from_snapshot("Foo.thm", cone_snapshot(), led)
    assert card.informal_rendering is None
    assert card.kernel_shrink_suggestions == []
    assert card.semantic_delta_prose is None
    # ...and when filled, they render.
    card.informal_rendering = "energy is zero"
    card.kernel_shrink_suggestions = ["energy is really (0 : Nat)"]
    md = card.render_markdown()
    assert "## Informal rendering" in md
    assert "energy is zero" in md
    assert "Kernel-shrink suggestions" in md


# --- semantic_delta — closed structural vocabulary ---------------------------


def test_delta_unchanged():
    snap = cone_snapshot()
    delta = semantic_delta(snap, snap, "Foo.thm")
    assert delta.classes == ["unchanged"]
    assert delta.is_unchanged
    assert set(delta.classes) <= set(DELTA_CLASSES)


def test_delta_type_changed():
    old = mk_snapshot([mk_decl(name="Foo.t", type_pp="1 = 1", cone=[])])
    new = mk_snapshot([mk_decl(name="Foo.t", type_pp="1 = 2", cone=[])])
    delta = semantic_delta(old, new, "Foo.t")
    assert "type-changed" in delta.classes
    assert delta.type_changed is True


def test_delta_cone_changed_names_added_and_removed_members():
    old = mk_snapshot([
        mk_decl(name="Foo.t", type_pp="P", cone=["Foo.a"]),
        mk_decl(name="Foo.a", kind="def", type_pp="Nat", value_pp="0"),
    ])
    new = mk_snapshot([
        mk_decl(name="Foo.t", type_pp="P", cone=["Foo.b"]),
        mk_decl(name="Foo.b", kind="def", type_pp="Nat", value_pp="0"),
    ])
    delta = semantic_delta(old, new, "Foo.t")
    assert "cone-changed" in delta.classes
    assert delta.cone_added == ["Foo.b"]
    assert delta.cone_removed == ["Foo.a"]


def test_delta_cone_member_meaning_changed_is_the_50_case():
    # The #50 failure: downstream theorem's OWN type is byte-identical,
    # but a def in its cone was reshaped (its value changed). A
    # type-only signal would miss this; cone-changed must catch it and
    # NAME the def.
    old = mk_snapshot([
        mk_decl(name="Foo.t", type_pp="Foo.energy = 0", cone=["Foo.energy"]),
        mk_decl(name="Foo.energy", kind="def", type_pp="Nat", value_pp="0"),
    ])
    new = mk_snapshot([
        mk_decl(name="Foo.t", type_pp="Foo.energy = 0", cone=["Foo.energy"]),
        mk_decl(name="Foo.energy", kind="def", type_pp="Nat",
                value_pp="the real integrand"),
    ])
    delta = semantic_delta(old, new, "Foo.t")
    assert delta.classes == ["cone-changed"]
    assert delta.cone_meaning_changed == ["Foo.energy"]
    assert not delta.type_changed  # the theorem's own type didn't move


def test_delta_axioms_changed_names_added_removed():
    old = mk_snapshot([mk_decl(name="Foo.t", axioms=["propext"], cone=[])])
    new = mk_snapshot([
        mk_decl(name="Foo.t", axioms=["propext", "Classical.choice"], cone=[]),
    ])
    delta = semantic_delta(old, new, "Foo.t")
    assert "axioms-changed" in delta.classes
    assert delta.axioms_added == ["Classical.choice"]
    assert delta.axioms_removed == []


def test_delta_sorry_status_changed():
    old = mk_snapshot([mk_decl(name="Foo.t", has_sorry=True,
                               axioms=["propext", "sorryAx"], cone=[])])
    new = mk_snapshot([mk_decl(name="Foo.t", has_sorry=False,
                               axioms=["propext"], cone=[])])
    delta = semantic_delta(old, new, "Foo.t")
    assert "sorry-status-changed" in delta.classes
    assert delta.sorry_before is True
    assert delta.sorry_after is False


def test_delta_unknown_side_withholds_meaning_change():
    # One side did not elaborate: no phantom type/meaning change is
    # asserted (absence of evidence is not evidence of change).
    old = mk_snapshot([mk_decl(name="Foo.t", type_pp="1 = 1", cone=[])])
    bad = mk_decl(name="Foo.t", status="unknown", type_pp=None,
                  has_sorry=None, reason="boom", axioms=[])
    new = mk_snapshot([bad])
    delta = semantic_delta(old, new, "Foo.t")
    assert not delta.type_changed
    assert "type-changed" not in delta.classes
    assert any("evidence" in n or "status changed" in n for n in delta.notes)


def test_delta_removed_and_added_decl():
    old = mk_snapshot([mk_decl(name="Foo.t", cone=[])])
    new = mk_snapshot([])
    removed = semantic_delta(old, new, "Foo.t")
    assert "cone-changed" in removed.classes
    added = semantic_delta(new, old, "Foo.t")
    assert "cone-changed" in added.classes


def test_delta_classes_are_in_closed_vocabulary():
    # Several facets move at once -> multiple classes, all from the set.
    old = mk_snapshot([
        mk_decl(name="Foo.t", type_pp="1=1", axioms=["propext"],
                has_sorry=True, cone=["Foo.a"]),
        mk_decl(name="Foo.a", kind="def", type_pp="Nat", value_pp="0"),
    ])
    new = mk_snapshot([
        mk_decl(name="Foo.t", type_pp="1=2", axioms=["propext", "X"],
                has_sorry=False, cone=["Foo.a"]),
        mk_decl(name="Foo.a", kind="def", type_pp="Nat", value_pp="9"),
    ])
    delta = semantic_delta(old, new, "Foo.t")
    assert set(delta.classes) <= set(DELTA_CLASSES)
    assert "type-changed" in delta.classes
    assert "cone-changed" in delta.classes
    assert "axioms-changed" in delta.classes
    assert "sorry-status-changed" in delta.classes

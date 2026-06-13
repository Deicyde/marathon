"""marathon.audit.spec_card — the spec card: diff-of-meaning, not diff-of-code.

Plan §2 ruling 5 + ``design-verification-surface-first`` ("Spec cards:
diff-of-meaning, not diff-of-code"). The human-facing unit of goal 2: a
single card that shows the human EXACTLY what they must read to trust a
theorem — the statement, the trust-kernel definitions (and only those),
and the machine evidence — plus, on change, a structural semantic delta.

This module builds the **machine half** of the card, purely over an audit
snapshot + ledger. The **Claude half** — a fresh informal rendering of
the statement, kernel-shrink suggestions, and an advisory prose
strengthened/weakened guess — is filled in later by the spec-auditor role
(out of scope here; the slots are present and empty). Keeping the split
explicit is the design's load-bearing firewall point: the card's machine
facts never depend on a model's say-so.

``semantic_delta`` is the **machine signal** that classifies what changed
between two snapshots, in a CLOSED vocabulary derived purely from
fingerprints (:data:`DELTA_CLASSES`). Strengthened/weakened are NOT
decidable structurally — the spec-auditor adds that prose guess as
advisory later; here we only report which structural facet moved (type,
cone, axioms, sorry-status) and name the cone members that changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from marathon.audit.kernel import Kernel, KernelMember, compute_kernel
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.gate import AXIOM_WHITELIST, SORRY_AXIOM

#: The closed vocabulary of structural semantic deltas. These are the
#: MACHINE classes — purely fingerprint-derived, never a meaning guess.
DELTA_CLASSES = (
    "unchanged",
    "type-changed",
    "cone-changed",
    "axioms-changed",
    "sorry-status-changed",
)


@dataclass(frozen=True)
class Evidence:
    """The machine-evidence block of a spec card.

    ``axioms_beyond_whitelist`` are the axioms that block T1 (sorryAx is
    surfaced separately via ``sorry`` — it is *accounted*, not failed).
    ``tier`` is the computed trust tier (never stored); ``tier_qualifiers``
    carry any staleness/degradation markers from the tier computation."""

    tier: str
    tier_qualifiers: list[str] = field(default_factory=list)
    axioms_beyond_whitelist: list[str] = field(default_factory=list)
    sorry: bool | None = None
    deception_tags: list[str] = field(default_factory=list)


@dataclass
class SpecCard:
    """One declaration's spec card.

    The machine half is built by :meth:`from_snapshot`; the three
    Claude-filled slots (``informal_rendering``, ``kernel_shrink_suggestions``,
    ``semantic_delta_prose``) default empty and are attached later by the
    spec-auditor. ``render_markdown`` produces the human-facing card."""

    # --- target identity ---
    target: str
    kind: str
    type_pp: str | None
    fingerprint_type: str | None
    fingerprint_value: str | None
    # --- the minimized human-read surface ---
    kernel: Kernel
    # --- machine evidence ---
    evidence: Evidence
    # --- slots the spec-auditor fills later (Claude half) ---
    informal_rendering: str | None = None
    kernel_shrink_suggestions: list[str] = field(default_factory=list)
    semantic_delta_prose: str | None = None

    @classmethod
    def from_snapshot(
        cls, decl_name: str, snapshot: AuditSnapshot, ledger
    ) -> "SpecCard":
        """Build the machine half of the card for ``decl_name``.

        Pure over (snapshot, ledger): computes the trust kernel, the
        tier (via :func:`marathon.audit.trust.compute_tier`), and the
        evidence block. Works even when the decl is absent/unknown — the
        kernel reports it unresolved and the evidence carries the
        computed UNKNOWN tier; the card is still renderable (it surfaces
        the gap rather than hiding it)."""
        # Local import: trust pulls the ledger/review surface; keep the
        # module importable without forcing that graph at import time.
        from marathon.audit.trust import compute_tier

        by_name = snapshot.by_name()
        decl = by_name.get(decl_name)
        kernel = compute_kernel(decl_name, snapshot)
        tier_result = compute_tier(decl_name, snapshot, ledger)
        evidence = _evidence_for(decl, tier_result)
        return cls(
            target=decl_name,
            kind=decl.kind if decl is not None else "?",
            type_pp=decl.type_pp if decl is not None else None,
            fingerprint_type=decl.fingerprint_type if decl is not None else None,
            fingerprint_value=(
                decl.fingerprint_value if decl is not None else None
            ),
            kernel=kernel,
            evidence=evidence,
        )

    def render_markdown(self) -> str:
        """The human-facing card.

        Sections: the statement, "Definitions you must read" (kernel
        members ONLY — never the whole file), the evidence table, and a
        footer noting Mathlib vocabulary is trusted (so the human knows
        the absent constants are deliberate, not lost)."""
        lines: list[str] = []
        lines.append(f"# Spec card: `{self.target}`")
        lines.append("")
        lines.append(f"- kind: `{self.kind}`")
        if self.fingerprint_type:
            lines.append(f"- type fingerprint: `{self.fingerprint_type[:12]}`")
        if self.fingerprint_value:
            lines.append(
                f"- value fingerprint: `{self.fingerprint_value[:12]}`"
            )
        lines.append(f"- trust tier: **{self.evidence.tier}**")
        if self.evidence.tier_qualifiers:
            lines.append(
                "  - qualifiers: "
                + ", ".join(f"`{q}`" for q in self.evidence.tier_qualifiers)
            )
        lines.append("")

        lines.append("## Statement")
        lines.append("")
        if self.type_pp is not None:
            lines.append("```lean")
            lines.append(self.type_pp)
            lines.append("```")
        else:
            lines.append(
                "_no elaborated type — declaration is absent or did not "
                "elaborate (status unknown)._"
            )
        lines.append("")

        if self.informal_rendering:
            lines.append("## Informal rendering")
            lines.append("")
            lines.append("> " + self.informal_rendering.replace("\n", "\n> "))
            lines.append("")

        lines.append("## Definitions you must read")
        lines.append("")
        if self.kernel.members:
            lines.append(
                f"_{self.kernel.size_decls} local definition(s), "
                f"{self.kernel.size_loc} pinned-pp line(s) — the entire "
                "human-read surface behind this statement._"
            )
            lines.append("")
            for member in self.kernel.members:
                lines.extend(_render_member(member))
        else:
            lines.append(
                "_none — the statement is phrased entirely in trusted "
                "(Mathlib/core) vocabulary. Zero new definitions to read._"
            )
        lines.append("")

        if self.kernel.local_lemmas:
            lines.append("### Local lemmas referenced (statements not read)")
            lines.append("")
            lines.append(
                "_These project-local lemmas appear in the statement cone; "
                "their **existence** is leaned on but their statements are "
                "not part of what this theorem means (each is audited "
                "separately)._"
            )
            lines.append("")
            for name in self.kernel.local_lemmas:
                lines.append(f"- `{name}`")
            lines.append("")

        if self.kernel.unresolved:
            lines.append("### Unresolved cone references")
            lines.append("")
            lines.append(
                "_Project-local references with no audit evidence (absent "
                "from the snapshot or did not elaborate) — reported, never "
                "hidden:_"
            )
            lines.append("")
            for name in self.kernel.unresolved:
                lines.append(f"- `{name}`")
            lines.append("")

        if self.kernel_shrink_suggestions:
            lines.append("## Kernel-shrink suggestions (advisory)")
            lines.append("")
            lines.append(
                "_Each must be mechanically certified by an emitted probe "
                "before it shrinks anything — moving trust is not shrinking "
                "it._"
            )
            lines.append("")
            for suggestion in self.kernel_shrink_suggestions:
                lines.append(f"- {suggestion}")
            lines.append("")

        lines.append("## Evidence")
        lines.append("")
        lines.extend(_render_evidence_table(self.evidence))
        lines.append("")

        if self.semantic_delta_prose:
            lines.append("## Semantic delta (advisory)")
            lines.append("")
            lines.append(self.semantic_delta_prose)
            lines.append("")

        lines.append("---")
        lines.append(
            "_Mathlib and core constants are trusted vocabulary and are not "
            "shown above — only project-local definitions are part of what "
            "you must read. Proof bodies are never in the kernel._"
        )
        return "\n".join(lines)


def _render_member(member: KernelMember) -> list[str]:
    lines = [f"### `{member.name}`  ({member.kind})", ""]
    if member.type_pp is not None:
        lines.append("```lean")
        lines.append("-- type")
        lines.append(member.type_pp)
        if member.value_pp is not None:
            lines.append("-- value (the meaning that matters)")
            lines.append(member.value_pp)
        lines.append("```")
    else:
        lines.append("_no audit evidence._")
    lines.append("")
    return lines


def _render_evidence_table(evidence: Evidence) -> list[str]:
    sorry = (
        "-" if evidence.sorry is None else ("yes" if evidence.sorry else "no")
    )
    axioms = ", ".join(evidence.axioms_beyond_whitelist) or "none"
    tags = "; ".join(evidence.deception_tags) or "none"
    return [
        "| facet | value |",
        "| --- | --- |",
        f"| trust tier | {evidence.tier} |",
        f"| axioms beyond whitelist | {axioms} |",
        f"| sorry | {sorry} |",
        f"| deception tags | {tags} |",
    ]


def _evidence_for(decl: DeclAudit | None, tier_result) -> Evidence:
    if decl is None or decl.is_unknown:
        return Evidence(
            tier=tier_result.tier,
            tier_qualifiers=list(tier_result.qualifiers),
            axioms_beyond_whitelist=[],
            sorry=None if decl is None else decl.has_sorry,
            deception_tags=[] if decl is None else list(decl.tags),
        )
    beyond = sorted(set(decl.axioms) - AXIOM_WHITELIST - {SORRY_AXIOM})
    return Evidence(
        tier=tier_result.tier,
        tier_qualifiers=list(tier_result.qualifiers),
        axioms_beyond_whitelist=beyond,
        sorry=decl.has_sorry,
        deception_tags=list(decl.tags),
    )


# ---------------------------------------------------------------------------
# Semantic delta — the machine signal (pure, fingerprint-derived)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SemanticDelta:
    """Structural classification of what changed for one declaration
    between two snapshots.

    ``classes`` is a subset of :data:`DELTA_CLASSES` (``["unchanged"]``
    when nothing moved). The specifics are populated per class:

    * ``type-changed`` — the elaborated-type fingerprint differs;
    * ``cone-changed`` — a project-local cone member changed meaning,
      vanished, or was added; the members are NAMED in
      ``cone_added`` / ``cone_removed`` / ``cone_meaning_changed``;
    * ``axioms-changed`` — the transitive axiom set differs
      (``axioms_added`` / ``axioms_removed``);
    * ``sorry-status-changed`` — ``has_sorry`` flipped.

    Strengthened/weakened are NOT here — they are not structurally
    decidable; the spec-auditor adds that prose guess as advisory."""

    decl_name: str
    classes: list[str]
    type_changed: bool = False
    cone_added: list[str] = field(default_factory=list)
    cone_removed: list[str] = field(default_factory=list)
    cone_meaning_changed: list[str] = field(default_factory=list)
    axioms_added: list[str] = field(default_factory=list)
    axioms_removed: list[str] = field(default_factory=list)
    sorry_before: bool | None = None
    sorry_after: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_unchanged(self) -> bool:
        return self.classes == ["unchanged"]


def semantic_delta(
    old: AuditSnapshot, new: AuditSnapshot, decl_name: str
) -> SemanticDelta:
    """Classify the structural change of ``decl_name`` from ``old`` to
    ``new``, purely from fingerprints.

    Comparison rules mirror the engine's ``diff_snapshots`` honesty: a
    fingerprint comparison fires only when BOTH sides carry the
    fingerprint; an ``unknown`` side yields a ``sorry-status-changed`` /
    note about lost evidence rather than a phantom meaning change. Cone
    membership change (added/removed local names) is structural and fires
    regardless; a cone member that PERSISTS but whose own type fingerprint
    moved is reported as ``cone_meaning_changed`` (the load-bearing #50
    case: a downstream theorem's own type is byte-identical but a def in
    its cone was reshaped)."""
    old_decl = old.by_name().get(decl_name)
    new_decl = new.by_name().get(decl_name)
    classes: list[str] = []
    notes: list[str] = []

    if old_decl is None and new_decl is None:
        return SemanticDelta(decl_name, ["unchanged"],
                             notes=["absent from both snapshots"])
    if new_decl is None:
        return SemanticDelta(
            decl_name, ["cone-changed"],
            notes=["declaration removed from the new snapshot"],
        )
    if old_decl is None:
        return SemanticDelta(
            decl_name, ["cone-changed"],
            notes=["declaration added in the new snapshot"],
        )

    # status / sorry-status
    sorry_before = old_decl.has_sorry
    sorry_after = new_decl.has_sorry
    sorry_changed = (
        sorry_before is not None
        and sorry_after is not None
        and sorry_before != sorry_after
    )
    if sorry_changed:
        classes.append("sorry-status-changed")
    if old_decl.is_unknown != new_decl.is_unknown:
        notes.append(
            f"status changed: {old_decl.status} -> {new_decl.status} "
            "(meaning-change comparison withheld — one side carries no "
            "evidence)"
        )

    # type fingerprint — only when both sides have one.
    type_changed = (
        old_decl.fingerprint_type is not None
        and new_decl.fingerprint_type is not None
        and old_decl.fingerprint_type != new_decl.fingerprint_type
    )
    if type_changed:
        classes.append("type-changed")

    # cone membership + cone-member meaning.
    old_cone = set(old_decl.cone)
    new_cone = set(new_decl.cone)
    cone_added = sorted(new_cone - old_cone)
    cone_removed = sorted(old_cone - new_cone)
    old_by = old.by_name()
    new_by = new.by_name()
    cone_meaning_changed: list[str] = []
    for member in sorted(old_cone & new_cone):
        om = old_by.get(member)
        nm = new_by.get(member)
        if om is None or nm is None:
            continue
        of = om.fingerprint_value or om.fingerprint_type
        nf = nm.fingerprint_value or nm.fingerprint_type
        if of is not None and nf is not None and of != nf:
            cone_meaning_changed.append(member)
    if cone_added or cone_removed or cone_meaning_changed:
        classes.append("cone-changed")

    # axioms — only when both sides ok (an unknown line has no axioms).
    axioms_added: list[str] = []
    axioms_removed: list[str] = []
    if not old_decl.is_unknown and not new_decl.is_unknown:
        oa, na = set(old_decl.axioms), set(new_decl.axioms)
        if oa != na:
            axioms_added = sorted(na - oa)
            axioms_removed = sorted(oa - na)
            classes.append("axioms-changed")

    if not classes:
        classes = ["unchanged"]

    return SemanticDelta(
        decl_name=decl_name,
        classes=classes,
        type_changed=type_changed,
        cone_added=cone_added,
        cone_removed=cone_removed,
        cone_meaning_changed=cone_meaning_changed,
        axioms_added=axioms_added,
        axioms_removed=axioms_removed,
        sorry_before=sorry_before,
        sorry_after=sorry_after,
        notes=notes,
    )

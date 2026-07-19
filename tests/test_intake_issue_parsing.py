"""Decl extraction must handle the chapter-intake issue format (a
`### Lean declarations` bullet list of backtick'd names + source links) in
addition to ```lean signature blocks — otherwise intake-bootstrapped chapters
(e.g. Ch15) resolve no decl and every card blocks with 'no audit evidence'."""
from __future__ import annotations

from marathon.fill import _extract_decls_from_issue_body
from marathon.review.verified_decls import (
    extract_declarations_from_declaration_sections,
    extract_declarations_from_issue_body,
)

INTAKE_BODY = """### Informal Statement
*📋 Anchored to the project tracker roadmap (user-curated).*

> (p. 380) Define a pointwise orientation on a manifold

A **pointwise orientation** on a smooth manifold `M` is a choice of orientation of each `T_p M`.

### Lean declarations
- [Orientation.lean:82-90](GeometricAnalysis/LeeSM/Chapter15/Orientation.lean#L82-L90) — `PointwiseOrientation`

### Scaffolding folded
- `instNegPointwiseOrientation`, `PointwiseOrientation.neg_apply` — the `Neg` instance and simp lemmas.
"""

LEAN_BLOCK_BODY = """### Lean signatures
```lean
theorem MixedTensorField.exists_of_tensorial : True
def Foo.bar : Nat
```
"""


def test_intake_format_resolves_the_primary_decl():
    got = extract_declarations_from_issue_body(INTAKE_BODY)
    assert "PointwiseOrientation" in got
    # prose backticks elsewhere (M, T_p in the informal; Neg in scaffolding) are NOT decls
    assert "M" not in got and "Neg" not in got


def test_declaration_sections_scoped_to_lean_declarations_only():
    # only the `### Lean declarations` section, not `### Scaffolding folded` prose
    assert extract_declarations_from_declaration_sections(INTAKE_BODY) == {"PointwiseOrientation"}


def test_declaration_bullet_uses_only_primary_anchor_not_description():
    """Backticked Lean terms in a bullet's explanation are prose.  Treating
    them as citations made dropping one erase the bullet's real anchor."""
    body = """### Lean declarations
- [`Prop_16_3`](GeometricAnalysis/LeeSM/Chapter16/IntegrationRn.lean) — extension of 16.1 to compactly supported forms on open subsets: `∫_V ω = σ · ∫_U G*ω`.
- [`Lemma_16_2`](GeometricAnalysis/LeeSM/Chapter16/IntegrationRn.lean) — every compact `K ⊆` open `U` lies in an open domain of integration `D` with `K ⊆ D ⊆ D̄ ⊆ U` (supporting lemma; no separate roadmap line — grouped here).
"""
    assert extract_declarations_from_declaration_sections(body) == {
        "Prop_16_3", "Lemma_16_2",
    }


def test_prop_16_5_description_terms_are_not_citations():
    body = """### Lean declarations
- [`Prop_16_5`](GeometricAnalysis/LeeSM/Chapter16/IntegrationManifold.lean) — `manifoldIntegral` equals the signed chart sum for any valid `IsSmoothChartCover`; independence of cover/partition/chart choice follows by transitivity.
"""
    assert extract_declarations_from_declaration_sections(body) == {"Prop_16_5"}


# the roadmap-granularity intake emits the keyword INSIDE the backticks
INTAKE_BODY_KEYWORD = """### Lean declarations
- `def DifferentialForm.IsNonvanishing` — [OrientationForms.lean](GeometricAnalysis/LeeSM/Chapter15/OrientationForms.lean#L67-L72)
- `theorem Prop_15_5_forward` — [OrientationForms.lean](GeometricAnalysis/LeeSM/Chapter15/OrientationForms.lean#L121-L128)

### Scaffolding folded
- `instFooBar` — a helper instance, not first-class.
"""


def test_intake_format_with_keyword_in_backticks_strips_keyword():
    got = extract_declarations_from_issue_body(INTAKE_BODY_KEYWORD)
    assert got == {"DifferentialForm.IsNonvanishing", "Prop_15_5_forward"}
    # the scaffolding helper is not a card
    assert "instFooBar" not in got


def test_lean_block_format_still_works():
    got = extract_declarations_from_issue_body(LEAN_BLOCK_BODY)
    assert {"MixedTensorField.exists_of_tensorial", "Foo.bar"} <= got


def test_fill_extraction_handles_intake_format():
    assert "PointwiseOrientation" in _extract_decls_from_issue_body(INTAKE_BODY)
    # and still handles the lean-block format
    assert "Foo.bar" in _extract_decls_from_issue_body(LEAN_BLOCK_BODY)

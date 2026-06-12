/-! Audit fixture, honest half: every "normal" declaration shape the audit
script must handle.  Golden output lives at
tests/golden/audit_fixture_output.txt — keep this file boring and stable. -/

namespace AuditFixture

/-- Plain theorem: empty project-local cone, no axioms beyond core. -/
theorem zero_add_self (n : Nat) : 0 + n = n := Nat.zero_add n

/-- A definition (fingerprints by type AND value). -/
def double (n : Nat) : Nat := n + n

/-- Theorem about `double`: its cone must be exactly {AuditFixture.double}. -/
theorem double_eq (n : Nat) : double n = n + n := rfl

/-- Reducible alias: must report kind `abbrev`, with a value. -/
abbrev twice (n : Nat) : Nat := double n

/-- Sorry'd theorem: `has_sorry` true, `sorryAx` among its axioms. -/
theorem double_is_two_mul (n : Nat) : double n = 2 * n := sorry

/-- A structure (constructor field types must reach the cone). -/
structure Point where
  x : Nat
  y : Nat

/-- A def on the structure (field projections in the value). -/
def Point.swap (p : Point) : Point := Point.mk p.y p.x

/-- An honest instance of a *core* class: must carry no deception tags
(`trivial_instance` only fires for project-local classes). -/
instance : Inhabited Point := ⟨Point.mk 0 0⟩

/-- A custom axiom; `double` must appear in its cone. -/
axiom double_growth (n : Nat) : n ≤ double n

/-- Theorem using the custom axiom: axioms = {AuditFixture.double_growth},
`has_sorry` false. -/
theorem le_double (n : Nat) : n ≤ double n := double_growth n

/-- Private declaration: audited under its full `_private...` name. -/
private def hiddenHelper (n : Nat) : Nat := n + 1

/-- Public theorem whose *statement* mentions a private def: the private
name must appear in this cone. -/
theorem hiddenHelper_eq (n : Nat) : hiddenHelper n = n + 1 := rfl

/-- Universe-polymorphic def: must not crash the script. -/
def polyId.{u} {α : Sort u} (a : α) : α := a

-- Mutual recursion: both decls emitted, matcher/aux machinery skipped.
-- (Doc comments are not allowed on `mutual` blocks.)
mutual
  def isEvenF : Nat → Bool
    | 0 => true
    | n + 1 => isOddF n
  def isOddF : Nat → Bool
    | 0 => false
    | n + 1 => isEvenF n
end

namespace Inner

/-- Nested-namespace decl: emitted fully qualified. -/
theorem nested_truth : 1 + 1 = 2 := rfl

end Inner

end AuditFixture

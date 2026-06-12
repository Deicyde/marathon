import AuditFixture.Basic

/-! Audit fixture, deceptive half: one declaration per deception tag the
audit script must detect, plus an honest control instance. -/

namespace AuditFixture

/-- vacuous_body: proves `True` by `True.intro`. -/
theorem vacuous_truth : True := True.intro

/-- proof_by_exfalso: head of the proof is `False.elim`. -/
theorem from_false (h : False) : 1 = 2 := False.elim h

/-- ignores_params: takes `n`, never uses it. -/
def ignores_input (n : Nat) : Nat := 0

/-- A project-local class for the instance tags below. -/
class Collapsible (α : Type) where
  collapse : α → α

/-- trivial_instance: the classic PUnit collapse — a constructor application
whose arguments reference nothing project-local. -/
instance : Collapsible PUnit := ⟨fun _ => PUnit.unit⟩

/-- Honest control: references project-local `double`, must stay untagged. -/
instance : Collapsible Nat := ⟨double⟩

/-- Cross-module statement cone: must contain `Collapsible.collapse`, the
`Collapsible Nat` instance, and `double` — and nothing from Lean core. -/
theorem collapse_nat (n : Nat) : Collapsible.collapse n = double n := rfl

end AuditFixture

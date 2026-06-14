import AuditFixture.Basic

/-! Audit fixture, probe-target half (Phase 6b pure-Lean probes). These
declarations exist purely so `tests/test_probes.py` can build REAL probes
against a real (Mathlib-free, toolchain-pinned) package: a known-good and a
known-bad kernel-shrink certificate, and a `PUnit`-collapse structure for
the sanity probe.

This module deliberately lives at the PACKAGE ROOT (module
`AuditFixtureProbes`, a sibling of the root `AuditFixture.lean`), OUTSIDE the
`AuditFixture/` source folder. The audited surface is the `AuditFixture/`
folder, and `marathon.audit.engine.run_audit` auto-derives its modules by
walking that folder (`AuditFixture.Basic`, `AuditFixture.Deception`). Keeping
the probe targets out of that folder is what keeps them out of the audited
surface — so adding them changes neither `FIXTURE_MODULES` (Basic +
Deception) nor `tests/golden/audit_fixture_output.txt`. The module is still
imported by the root `AuditFixture.lean`, so `lake build` builds it and
`tests/test_probes.py` can elaborate real probes against its `.olean`. The
declarations keep the `AuditFixture` namespace so the probe-target names
(`AuditFixture.triple`, …) are unchanged. -/

namespace AuditFixture

/-- A def that genuinely equals a core construction by `rfl` — the
known-GOOD kernel-shrink target. A certificate `triple n = n + n + n := rfl`
builds, confirming the shrink. -/
def triple (n : Nat) : Nat := n + n + n

/-- A def that does NOT equal the false claim a known-BAD certificate makes
(`quad n = n + n + n` is false — quad is 4n), so that certificate FAILS to
build and the shrink is rejected. -/
def quad (n : Nat) : Nat := n + n + n + n

/-- The classic `PUnit`-collapse trap (referee.md #1): a structure whose
sole field is `PUnit`. Every inhabitant is forced equal, so any "model" is
vacuous. The sanity probe's "two unequal inhabitants" witness must FAIL to
build here — that failure is the high-signal finding. -/
structure CollapsedModel where
  carrier : PUnit

/-- An honest single-field structure over `Nat`: two distinct inhabitants
are genuinely unequal, so a sanity witness PASSES — the T1-evidence case. -/
structure HonestModel where
  carrier : Nat

end AuditFixture

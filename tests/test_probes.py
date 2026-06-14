"""Tests for the pure-Lean probes (Phase 6b: unfolding / sanity / shrink
certificate).

Two layers, mirroring tests/test_audit_lean_script.py:

* always-run — the pure generators produce well-formed Lean source, degrade
  honestly to a ``needs witness`` marker (never a vacuous probe), and the
  report classification is correct;
* toolchain-gated — build REAL probes against the Mathlib-free fixture
  package via ``lake env lean`` (written OUTSIDE the repo tree, the binding
  no-bundle constraint): a known-GOOD shrink certificate passes, a known-BAD
  one fails (⇒ shrink rejected), a sanity witness against an honest model
  passes, and the same witness against a ``PUnit``-collapsed model fails
  (⇒ high-signal finding). Skipped unless lake/elan exist AND the fixture
  toolchain is installed (same probe as the audit test).
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from marathon.audit.probes import (
    NEEDS_WITNESS_MARKER,
    OUTCOMES,
    Probe,
    ProbeKind,
    ProbeOutcome,
    ProbeReport,
    probes_for_decl,
    run_probes,
    sanity_instance_probe,
    shrink_certificate_probe,
    unfolding_probe,
)
from marathon.audit.records import DeclAudit

TESTS_DIR = pathlib.Path(__file__).resolve().parent
FIXTURE_DIR = TESTS_DIR / "fixtures" / "audit_fixture"
PINNED_TOOLCHAIN = (FIXTURE_DIR / "lean-toolchain").read_text().strip()
# The probe targets live in the top-level `AuditFixtureProbes` module (a
# sibling of the root `AuditFixture.lean`), OUTSIDE the audited `AuditFixture/`
# folder — so `run_audit`'s folder-based module discovery never picks them up
# and they stay out of the audit golden's surface. Their decls keep the
# `AuditFixture` namespace, so the probe-target names are unchanged.
PROBES_MODULE = "AuditFixtureProbes"


# ---------------------------------------------------------------------------
# Decl factories (a DeclAudit without running Lean)
# ---------------------------------------------------------------------------

def _decl(
    name: str,
    kind: str,
    *,
    module: str = "AuditFixtureProbes",
    type_pp: str | None = "Nat",
    value_pp: str | None = None,
    status: str = "ok",
) -> DeclAudit:
    return DeclAudit(
        name=name,
        kind=kind,
        module=module,
        status=status,
        type_pp=type_pp,
        value_pp=value_pp,
        cone=[],
        axioms=[],
        has_sorry=False,
        tags=[],
        reason=None,
    )


# ---------------------------------------------------------------------------
# Always-run: unfolding probe
# ---------------------------------------------------------------------------

class TestUnfoldingProbe:
    def test_degraded_typecheck_not_rfl_trivial(self):
        d = _decl(
            "AuditFixture.double", "def", module="AuditFixture.Basic",
            value_pp="fun n => n + n",
        )
        probe = unfolding_probe(d)
        assert probe.kind is ProbeKind.UNFOLDING
        assert not probe.needs_witness
        src = probe.source
        # Imports the defining module so the probe elaborates downstream.
        assert "import AuditFixture.Basic" in src
        # Degraded form: a name-resolution / total-ness check, NOT a
        # vacuous `name = name := rfl`.
        assert "#check @AuditFixture.double" in src
        assert "example := @AuditFixture.double" in src
        assert "= AuditFixture.double := rfl" not in src
        assert ":= rfl" not in src  # no rfl-trivial probe slipped in

    def test_strong_with_expected(self):
        d = _decl("AuditFixture.triple", "def", value_pp="fun n => n + n + n")
        probe = unfolding_probe(d, expected="n + n + n", args="n")
        assert not probe.needs_witness
        assert "AuditFixture.triple n = n + n + n := by rfl" in probe.source

    def test_strong_by_simp(self):
        d = _decl("AuditFixture.triple", "def", value_pp="x")
        probe = unfolding_probe(d, expected="n + n + n", args="n", by_simp=True)
        assert "by simp [AuditFixture.triple]" in probe.source

    def test_non_def_degrades_to_needs_witness(self):
        # A theorem has no value to unfold — honest 'needs witness', not a
        # vacuous probe.
        t = _decl("AuditFixture.double_eq", "theorem", value_pp=None)
        probe = unfolding_probe(t)
        assert probe.needs_witness
        assert NEEDS_WITNESS_MARKER in probe.source
        assert "no value to unfold" in (probe.note or "")

    def test_unknown_decl_needs_witness(self):
        u = _decl("X.foo", "def", type_pp=None, status="unknown")
        probe = unfolding_probe(u)
        assert probe.needs_witness
        assert NEEDS_WITNESS_MARKER in probe.source

    def test_unsafe_module_not_spliced(self):
        # A module name we won't splice into an `import` → needs witness,
        # and the unsafe name never appears in the emitted source.
        d = _decl("Bad.foo", "def", module="Bad; #eval 1", value_pp="x")
        probe = unfolding_probe(d)
        assert probe.needs_witness
        assert "#eval 1" not in probe.source
        assert probe.imports == ()


# ---------------------------------------------------------------------------
# Always-run: sanity probe
# ---------------------------------------------------------------------------

class TestSanityProbe:
    def test_no_witness_is_needs_witness_not_vacuous(self):
        s = _decl("AuditFixture.HonestModel", "structure", type_pp="Type")
        probe = sanity_instance_probe(s)
        assert probe.needs_witness
        assert NEEDS_WITNESS_MARKER in probe.source
        # The marker is a Lean comment line — nothing that "passes".
        assert "example" not in probe.source.split(NEEDS_WITNESS_MARKER)[1]

    def test_collapse_heuristic_names_the_trap(self):
        # value_pp/type_pp mentioning PUnit trips the heuristic; the reason
        # NAMES the collapse so a human knows what to refute.
        s = _decl(
            "AuditFixture.CollapsedModel", "structure",
            type_pp="Type", value_pp="PUnit",
        )
        probe = sanity_instance_probe(s)
        assert probe.needs_witness
        assert "collapse" in (probe.note or "").lower()

    def test_unit_not_matched_inside_inhabited(self):
        # Word-boundary heuristic: `Unit` inside `Inhabited` must not fire.
        s = _decl("Foo.Bar", "structure", type_pp="Inhabited Foo", value_pp=None)
        probe = sanity_instance_probe(s)
        # No collapse detected → generic 'need a witnessing model' reason.
        assert "witnessing model" in (probe.note or "")

    def test_witness_supplied_builds_probe(self):
        s = _decl("AuditFixture.HonestModel", "structure", type_pp="Type")
        witness = (
            "example : ∃ a b : AuditFixture.HonestModel, a ≠ b := "
            "⟨⟨0⟩, ⟨1⟩, by intro h; cases h⟩"
        )
        probe = sanity_instance_probe(s, witness=witness)
        assert not probe.needs_witness
        assert "import AuditFixtureProbes" in probe.source
        assert witness in probe.source

    def test_non_structure_needs_witness(self):
        d = _decl("X.foo", "def", value_pp="x")
        probe = sanity_instance_probe(d)
        assert probe.needs_witness
        assert "not a structure/class" in (probe.note or "")


# ---------------------------------------------------------------------------
# Always-run: shrink-certificate probe
# ---------------------------------------------------------------------------

class TestShrinkCertificateProbe:
    def test_wraps_obligation_verbatim(self):
        cert = "example : AuditFixture.triple = fun n => n + n + n := rfl"
        probe = shrink_certificate_probe(
            cert, subject="triple-shrink", imports=("AuditFixtureProbes",),
        )
        assert probe.kind is ProbeKind.SHRINK_CERTIFICATE
        assert not probe.needs_witness
        assert cert in probe.source
        assert "import AuditFixtureProbes" in probe.source

    def test_blank_obligation_needs_witness(self):
        probe = shrink_certificate_probe("   ")
        assert probe.needs_witness
        assert "nothing to build" in (probe.note or "")


# ---------------------------------------------------------------------------
# Always-run: probes_for_decl + report classification (no Lean)
# ---------------------------------------------------------------------------

class TestProbesForDecl:
    def test_def_gets_unfolding_only(self):
        d = _decl("X.foo", "def", value_pp="x")
        kinds = {p.kind for p in probes_for_decl(d)}
        assert kinds == {ProbeKind.UNFOLDING}

    def test_structure_gets_sanity_only(self):
        d = _decl("X.S", "structure", type_pp="Type")
        kinds = {p.kind for p in probes_for_decl(d)}
        assert kinds == {ProbeKind.SANITY}

    def test_theorem_gets_nothing_auto(self):
        d = _decl("X.thm", "theorem", value_pp=None)
        assert probes_for_decl(d) == []

    def test_kind_filter(self):
        d = _decl("X.S", "structure", type_pp="Type")
        assert probes_for_decl(d, kinds=(ProbeKind.UNFOLDING,)) == []
        assert len(probes_for_decl(d, kinds=(ProbeKind.SANITY,))) == 1


class TestReportClassification:
    def test_needs_witness_never_built(self, monkeypatch):
        # run_probes must NOT shell out for a needs-witness probe.
        import marathon.audit.probes as probes_mod

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("needs-witness probe must not be built")

        monkeypatch.setattr(probes_mod, "_build_one", _boom)
        # No lake needed: with only a needs-witness probe there is nothing
        # runnable, so _find_lake is never consulted either.
        nw = sanity_instance_probe(_decl("X.S", "structure", type_pp="Type"))
        report = run_probes("/nonexistent", [nw])
        assert len(report.outcomes) == 1
        assert report.outcomes[0].outcome == "needs_witness"
        assert report.findings == []  # needs_witness is not a finding

    def test_findings_and_shrink_rejection(self):
        good = Probe(ProbeKind.UNFOLDING, "g", "src")
        bad = Probe(ProbeKind.UNFOLDING, "b", "src")
        shrink = Probe(ProbeKind.SHRINK_CERTIFICATE, "s", "src")
        report = ProbeReport(
            repo_dir="/r",
            outcomes=[
                ProbeOutcome(good, "pass"),
                ProbeOutcome(bad, "fail", "error: boom"),
                ProbeOutcome(shrink, "fail", "error: nope"),
                ProbeOutcome(Probe(ProbeKind.SANITY, "e", "s"), "error", "no lake"),
            ],
        )
        # A failing unfolding + a failing shrink are findings; error is not.
        assert {o.probe.subject for o in report.findings} == {"b", "s"}
        assert [o.probe.subject for o in report.rejected_shrinks] == ["s"]
        counts = report.counts()
        assert counts == {"pass": 1, "fail": 2, "error": 1, "needs_witness": 0}
        assert set(counts) == set(OUTCOMES)

    def test_passing_shrink_not_rejected(self):
        shrink = Probe(ProbeKind.SHRINK_CERTIFICATE, "s", "src")
        outcome = ProbeOutcome(shrink, "pass")
        assert not outcome.shrink_rejected
        assert not outcome.is_finding

    def test_lake_missing_records_error(self, monkeypatch):
        import marathon.audit.probes as probes_mod

        monkeypatch.setattr(probes_mod, "_find_lake", lambda: None)
        runnable = Probe(ProbeKind.UNFOLDING, "u", "import X\n#check @X.f")
        report = run_probes("/r", [runnable])
        assert report.failures  # honest run-level note
        assert report.outcomes[0].outcome == "error"
        assert report.findings == []  # an error is absence of evidence


# ---------------------------------------------------------------------------
# Toolchain-gated: build real probes against the fixture package
# ---------------------------------------------------------------------------

def _elan_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    cand = pathlib.Path.home() / ".elan" / "bin" / name
    return str(cand) if cand.exists() else None


def _skip_reason() -> str:
    if _elan_tool("lake") is None:
        return "lake not installed (no PATH lake, no ~/.elan/bin/lake)"
    elan = _elan_tool("elan")
    if elan is None:
        return "elan not installed"
    try:
        proc = subprocess.run(
            [elan, "toolchain", "list"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        return f"elan toolchain list failed: {exc}"
    installed = {
        line.split()[0] for line in proc.stdout.splitlines() if line.strip()
    }
    if PINNED_TOOLCHAIN not in installed:
        return f"fixture toolchain {PINNED_TOOLCHAIN} not installed"
    return ""


_REASON = _skip_reason()
needs_lake = pytest.mark.skipif(bool(_REASON), reason=_REASON or "unreachable")


@pytest.fixture(scope="session")
def built_fixture():
    """Build the fixture package once (so probes resolve its oleans)."""
    import os

    env = os.environ.copy()
    env["PATH"] = (
        str(pathlib.Path.home() / ".elan" / "bin")
        + os.pathsep + env.get("PATH", "")
    )
    lake = _elan_tool("lake")
    build = subprocess.run(
        [lake, "build"], cwd=FIXTURE_DIR, env=env,
        capture_output=True, text=True, timeout=600,
    )
    assert build.returncode == 0, (
        f"lake build failed:\n{build.stdout}\n{build.stderr}"
    )
    return FIXTURE_DIR


@needs_lake
class TestLiveProbes:
    def test_shrink_certificate_good_passes(self, built_fixture):
        # `triple n = n + n + n` is true by rfl — the shrink holds.
        cert = (
            "example : ∀ n, AuditFixture.triple n = n + n + n := fun n => rfl"
        )
        probe = shrink_certificate_probe(
            cert, subject="triple-shrink", imports=(PROBES_MODULE,),
        )
        report = run_probes(built_fixture, [probe])
        outcome = report.outcomes[0]
        assert outcome.outcome == "pass", outcome.error_tail
        assert not outcome.shrink_rejected
        assert report.rejected_shrinks == []

    def test_shrink_certificate_bad_rejects(self, built_fixture):
        # `quad n = n + n + n` is FALSE (quad is 4n) — the certificate fails,
        # so the shrink is REJECTED (trust not moved).
        cert = (
            "example : ∀ n, AuditFixture.quad n = n + n + n := fun n => rfl"
        )
        probe = shrink_certificate_probe(
            cert, subject="quad-shrink", imports=(PROBES_MODULE,),
        )
        report = run_probes(built_fixture, [probe])
        outcome = report.outcomes[0]
        assert outcome.outcome == "fail"
        assert outcome.shrink_rejected
        assert outcome.is_finding
        # The Lean error tail is captured (the exact phrasing — "Type
        # mismatch" / "expected to have type" — is toolchain-dependent, so
        # we only assert it is non-empty and mentions the failing claim).
        assert outcome.error_tail
        assert "quad" in outcome.error_tail
        assert [o.probe.subject for o in report.rejected_shrinks] == [
            "quad-shrink"
        ]

    def test_sanity_witness_passes_on_honest_model(self, built_fixture):
        s = _decl("AuditFixture.HonestModel", "structure", type_pp="Type")
        witness = (
            "example : ∃ a b : AuditFixture.HonestModel, a ≠ b := "
            "⟨⟨0⟩, ⟨1⟩, by intro h; cases h⟩"
        )
        probe = sanity_instance_probe(s, witness=witness)
        report = run_probes(built_fixture, [probe])
        outcome = report.outcomes[0]
        assert outcome.outcome == "pass", outcome.error_tail
        assert not outcome.is_finding

    def test_sanity_witness_fails_on_punit_collapse(self, built_fixture):
        # CollapsedModel's sole field is PUnit → every inhabitant is forced
        # equal, so "two unequal inhabitants" is unprovable: the probe FAILS,
        # which is the high-signal collapse finding (referee.md #1).
        s = _decl("AuditFixture.CollapsedModel", "structure", type_pp="Type")
        witness = (
            "example : ∃ a b : AuditFixture.CollapsedModel, a ≠ b := "
            "⟨⟨PUnit.unit⟩, ⟨PUnit.unit⟩, by intro h; cases h⟩"
        )
        probe = sanity_instance_probe(s, witness=witness)
        report = run_probes(built_fixture, [probe])
        outcome = report.outcomes[0]
        assert outcome.outcome == "fail"
        assert outcome.is_finding
        assert report.findings  # surfaced as a finding

    def test_degraded_unfolding_passes_for_real_def(self, built_fixture):
        d = _decl(
            "AuditFixture.double", "def", module="AuditFixture.Basic",
            type_pp="Nat → Nat", value_pp="fun n => n + n",
        )
        probe = unfolding_probe(d)
        assert not probe.needs_witness
        report = run_probes(built_fixture, [probe])
        outcome = report.outcomes[0]
        assert outcome.outcome == "pass", outcome.error_tail

    def test_probe_file_written_outside_repo(self, built_fixture, monkeypatch):
        # BINDING: probe files must live OUTSIDE the repo tree so they can
        # never enter a `git ls-files` Aristotle bundle. Intercept the
        # tempdir creation and assert the path is not under the repo.
        import marathon.audit.probes as probes_mod

        seen: list[str] = []
        real_td = probes_mod.tempfile.TemporaryDirectory

        class _Spy(real_td):  # type: ignore[misc, valid-type]
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                seen.append(self.name)

        monkeypatch.setattr(probes_mod.tempfile, "TemporaryDirectory", _Spy)
        cert = "example : ∀ n, AuditFixture.triple n = n + n + n := fun n => rfl"
        probe = shrink_certificate_probe(cert, imports=(PROBES_MODULE,))
        run_probes(built_fixture, [probe])
        assert seen, "probe build should have created a temp dir"
        repo_resolved = pathlib.Path(built_fixture).resolve()
        for path in seen:
            resolved = pathlib.Path(path).resolve()
            assert not resolved.is_relative_to(repo_resolved), (
                f"probe temp dir {resolved} is INSIDE the repo {repo_resolved} "
                "— it could leak into an Aristotle bundle"
            )

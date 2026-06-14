"""marathon.audit.probes — pure-Lean probes (Phase 6b, the cheap-first tier).

Probes are the only component that ACTIVELY HUNTS misformalization rather
than waiting for a human, AND the mechanism that mechanically CERTIFIES the
kernel-shrink claims the spec-auditor proposes (plan §2 ruling 5,
``design-verification-surface-first`` "Probes (machine evidence behind T1)").

This module is the **pure-Lean** tier — NO Aristotle, no network, no budget.
Per ruling 5 probes ship cheapest-first; the two pure-Lean kinds here
(unfolding + sanity) are "the real 80%" (crit-feas §4) and are the only
probes that cost nothing but a `lake env lean` elaboration:

* :data:`ProbeKind.UNFOLDING` — a typecheck / total-ness probe over a
  def-like member. **Honest limitation (documented below):** an unfolding
  probe in the design's strong sense (``example : myDef x = expected :=
  rfl``) needs a human/Claude-supplied *expected* value; we cannot
  synthesize one blind, and ``example : myDef = myDef := rfl`` is
  rfl-trivial (proves nothing). So absent an expected value the unfolding
  probe DEGRADES to a wellformedness probe — it confirms the name
  elaborates, is a real constant (not a free variable / metavariable), and
  has the audited type — which still catches a def that fails to elaborate
  in a fresh importing context. When an expected value IS supplied (by the
  spec-auditor or a human), :func:`unfolding_probe` emits the real
  ``= expected := by rfl`` / ``by simp`` probe.

* :data:`ProbeKind.SANITY` — the ``PUnit``-collapse catcher (referee.md
  item #1). For a project-local ``structure``/``class`` whose single field
  is ``Unit``/``PUnit`` (detected heuristically from the audit ``type_pp``/
  ``value_pp`` — see :func:`_punit_field_heuristic`), the structure forces
  every inhabitant equal, so any "instance" of it is vacuous. The probe
  asserts a *witnessing* fact (an instance exists / two values are
  inhabited) — but synthesizing a genuine *non-collapsing* model needs
  human/Claude input, so where the snapshot can't supply one the probe is
  emitted as a ``needs witness`` marker (NOT a vacuous pass).

* :data:`ProbeKind.SHRINK_CERTIFICATE` — wraps a
  :func:`marathon.spec_auditor.certificate_obligations` snippet (already a
  runnable ``example : … := …``) for building. A certificate that BUILDS
  confirms the shrink; one that FAILS means the shrink is REJECTED (trust
  not moved — see :class:`ProbeReport`).

BINDING constraints (from the task + crit-feas §4):

* Pure-Lean probes are **built but never imported by the library**:
  :func:`run_probes` writes them into a temp dir OUTSIDE the repo tree, so
  they can never enter a ``git ls-files`` Aristotle bundle (and Aristotle
  can never edit them). The probe imports the repo's already-built modules
  via ``lake env lean`` run with ``cwd=repo`` — the exact invocation the
  audit engine uses.
* A FAILING unfolding/sanity probe is POSITIVE evidence of a problem; a
  PASSING one is T1 evidence. A failing ``shrink_certificate`` ⇒ the shrink
  is rejected. An ``error`` outcome (toolchain trouble, not a probe verdict)
  is honest absence of evidence — never a finding.
* This module is the cheap tier only. Vacuity probes (Aristotle, "prove
  ``¬hypotheses``") live in the separate Aristotle prober; nothing here
  spends budget.

Degradation contract (mirrors the audit engine): lake missing, nonzero
exit, timeout, or unparseable output never raise — :func:`run_probes`
records the trouble on the per-probe outcome and callers see honest
absence of evidence.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from marathon.audit.lean_template import _LEAN_NAME_RE
from marathon.audit.records import DeclAudit

logger = logging.getLogger(__name__)


class ProbeKind(str, Enum):
    """The three pure-Lean probe kinds (Aristotle vacuity probes are
    elsewhere). ``str``-valued so a kind serializes/prints as its name."""

    UNFOLDING = "unfolding"
    SANITY = "sanity"
    SHRINK_CERTIFICATE = "shrink_certificate"


#: Marker prefix a generator emits (as a Lean comment) when the snapshot
#: lacks the structure to build a meaningful probe — a ``needs witness``
#: degradation, NOT a vacuous pass. :func:`run_probes` recognizes it and
#: classifies the probe ``needs_witness`` rather than running it.
NEEDS_WITNESS_MARKER = "-- MARATHON_PROBE_NEEDS_WITNESS:"


@dataclass(frozen=True)
class Probe:
    """One generated probe: Lean source + enough metadata to build & report.

    ``source`` is a complete, importing ``.lean`` file (the ``import``s plus
    the ``example``/``#check``). ``imports`` is recorded separately so the
    runner can dedup / inspect them. ``needs_witness`` is True when the
    generator could not synthesize a meaningful probe and emitted the
    :data:`NEEDS_WITNESS_MARKER` instead — such a probe is NOT run (it would
    be a vacuous pass) and is reported as ``needs_witness``."""

    kind: ProbeKind
    #: The declaration / obligation this probe is about (for reporting).
    subject: str
    source: str
    imports: tuple[str, ...] = ()
    needs_witness: bool = False
    #: Free-form note explaining the degradation / what the probe checks.
    note: str | None = None


# ---------------------------------------------------------------------------
# Lean-source helpers
# ---------------------------------------------------------------------------

def _import_lines(imports: tuple[str, ...]) -> str:
    return "\n".join(f"import {m}" for m in imports)


def _safe_module(module: str | None) -> str | None:
    """A module name we are willing to splice into a Lean ``import`` — the
    same conservative gate the audit template uses. ``None``/unsafe → None
    (the caller emits a ``needs witness`` probe rather than risk a splice)."""
    if module and _LEAN_NAME_RE.match(module):
        return module
    return None


def _needs_witness_probe(
    kind: ProbeKind, subject: str, reason: str,
    imports: tuple[str, ...] = (),
) -> Probe:
    """A probe the generator could not honestly synthesize: a single
    comment line carrying the :data:`NEEDS_WITNESS_MARKER` + reason. It is
    valid Lean (a comment) but is never run — it would prove nothing."""
    body = f"{NEEDS_WITNESS_MARKER} {reason}\n"
    source = (_import_lines(imports) + "\n\n" if imports else "") + body
    return Probe(
        kind=kind,
        subject=subject,
        source=source,
        imports=imports,
        needs_witness=True,
        note=reason,
    )


# ---------------------------------------------------------------------------
# Unfolding probe
# ---------------------------------------------------------------------------

#: Def-like kinds an unfolding probe can be generated for (they have a
#: value whose computational meaning a ``= expected := rfl`` probe pins).
_UNFOLDABLE_KINDS = ("def", "abbrev", "instance")


def unfolding_probe(
    decl: DeclAudit,
    *,
    expected: str | None = None,
    args: str | None = None,
    by_simp: bool = False,
) -> Probe:
    """Generate an unfolding probe for a def-like ``decl``.

    Two honest modes, depending on whether an *expected* value is supplied:

    * **Strong mode** (``expected`` given) — the design's real unfolding
      test: ``example : <name> <args> = <expected> := by rfl`` (or
      ``by simp [<name>]`` when ``by_simp=True``). Pins computational
      meaning. The ``expected`` value must come from a human or the
      spec-auditor; we never synthesize it (a synthesized expected would
      just re-derive the def — a tautology).

    * **Degraded mode** (no ``expected``) — a wellformedness / total-ness
      probe: ``#check @<name>`` followed by ``example : <type> := @<name>``
      (a "this name is a real constant of its audited type, not a free
      variable / metavariable, and elaborates in a fresh importing context"
      check). This is honest about what it proves: NOT computational
      correctness, only that the def still elaborates and typechecks where
      a downstream user imports it. We deliberately do NOT emit
      ``example : <name> = <name> := rfl`` — that is rfl-trivial and proves
      nothing.

    A non-def-like decl (theorem/axiom/structure/…), an ``unknown`` decl,
    or one whose module can't be safely imported yields a ``needs witness``
    probe rather than a meaningless one.
    """
    module = _safe_module(decl.module)
    imports = (module,) if module else ()
    if decl.is_unknown or decl.type_pp is None:
        return _needs_witness_probe(
            ProbeKind.UNFOLDING, decl.name,
            "no audit evidence (status unknown) — cannot probe", imports,
        )
    if decl.kind not in _UNFOLDABLE_KINDS:
        return _needs_witness_probe(
            ProbeKind.UNFOLDING, decl.name,
            f"kind {decl.kind!r} has no value to unfold; an unfolding probe "
            "needs a def/abbrev/instance", imports,
        )
    if module is None:
        return _needs_witness_probe(
            ProbeKind.UNFOLDING, decl.name,
            "defining module is not a safely-importable Lean module name",
        )

    if expected is not None and expected.strip():
        lhs = decl.name + (f" {args}" if args and args.strip() else "")
        tactic = f"by simp [{decl.name}]" if by_simp else "by rfl"
        body = (
            f"-- unfolding probe (strong): pins the computational meaning of "
            f"`{decl.name}`.\n"
            f"example : {lhs} = {expected} := {tactic}\n"
        )
        note = "strong unfolding probe (expected value supplied)"
    else:
        # Degraded: total-ness / wellformedness only. `example := @name`
        # forces the name to resolve to a real constant and elaborate (with
        # Lean inferring its type) in a fresh importing context — it fails
        # iff the def no longer resolves/elaborates downstream. We bind
        # `@name` with the type INFERRED rather than re-parsing the pinned-pp
        # type string (pinned pp — fullNames, notation off — does not always
        # round-trip as Lean source), so the probe is robust. `#check` adds
        # a human-legible echo of the resolved type.
        body = (
            f"-- unfolding probe (degraded: wellformedness only — no expected "
            f"value supplied,\n"
            f"-- so this checks that `{decl.name}` elaborates as a real "
            f"constant in a fresh\n"
            f"-- importing context, NOT its computational value; supply "
            f"`expected` for the strong probe).\n"
            f"#check @{decl.name}\n"
            f"example := @{decl.name}\n"
        )
        note = (
            "degraded unfolding probe: typecheck/total-ness only "
            "(no expected value supplied)"
        )
    source = _import_lines(imports) + "\n\n" + body
    return Probe(
        kind=ProbeKind.UNFOLDING,
        subject=decl.name,
        source=source,
        imports=imports,
        needs_witness=False,
        note=note,
    )


# ---------------------------------------------------------------------------
# Sanity / PUnit-collapse probe
# ---------------------------------------------------------------------------

#: Structure-like kinds whose single-field collapse the sanity probe hunts.
_SANITY_KINDS = ("structure", "class")

#: Substrings that, appearing as a structure/class's sole field type,
#: signal the PUnit-collapse trap. ``Unit``/``PUnit`` make every inhabitant
#: equal (subsingleton), so any instance is vacuous.
_COLLAPSE_TYPES = ("PUnit", "Unit", "True")


def _punit_field_heuristic(decl: DeclAudit) -> bool:
    """Heuristic: does ``decl`` look like a single-``Unit``/``PUnit``-field
    structure/class (the collapse trap)?

    We work from the audit snapshot, which does NOT carry per-field types
    (the audit script folds field types into the ``cone``, and a
    structure's ``type_pp`` is just its ``Sort``). So the heuristic is
    deliberately weak and documented as such:

    * the decl is a ``structure``/``class`` (a collapsible shape);
    * AND a collapse type name (``Unit``/``PUnit``/``True``) appears in the
      cone-free evidence we DO have — the ``value_pp`` of its *constructor*
      is not in the snapshot, so we fall back to scanning ``type_pp`` for a
      parameter of a collapse type, and otherwise return False.

    A False here is NOT evidence of safety — it just means we can't detect a
    collapse from the snapshot alone; the caller then emits a ``needs
    witness`` probe (a human/Claude must supply the non-collapsing model).
    This conservatism is required: a false ``looks fine`` would be the
    vacuous pass the design forbids."""
    if decl.kind not in _SANITY_KINDS:
        return False
    haystacks = [decl.type_pp or "", decl.value_pp or ""]
    text = " ".join(haystacks)
    # Word-ish boundary check so `Unit` doesn't match inside `Inhabited`.
    for name in _COLLAPSE_TYPES:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text):
            return True
    return False


def sanity_instance_probe(
    decl: DeclAudit, *, witness: str | None = None,
) -> Probe:
    """Generate a PUnit-collapse sanity probe for a project-local
    ``structure``/``class`` ``decl``.

    The probe we WANT asserts a non-trivial model exists — that two
    distinct-looking inhabitants are NOT forced equal (for a structure), or
    that a witnessing instance over a non-collapsing carrier exists (for a
    class). The non-collapsing model requires knowing the intended
    mathematics, which the audit snapshot does NOT carry (field types are
    folded into the cone, not the snapshot). So this generator has two
    honest modes:

    * **Witness supplied** (``witness`` given) — a human or Claude supplies
      the refutation as a Lean assertion, e.g.
      ``example : ∃ a b : Foo, a ≠ b := ⟨…, …, by decide⟩`` for a structure,
      or ``example : MyClass SomeNontrivialCarrier := …`` for a class. We
      build it verbatim. A BUILD PASS is T1 evidence the type is not
      collapsed; a FAIL is a high-signal finding — for a ``PUnit``-collapsed
      structure the "two unequal inhabitants" claim is unprovable, so the
      probe fails and the collapse is caught (referee.md #1).

    * **No witness** — we cannot synthesize a non-collapsing model blind, so
      we emit a ``needs witness`` probe (NOT a vacuous pass). When the
      snapshot itself looks collapsed (:func:`_punit_field_heuristic`), the
      ``needs witness`` reason NAMES the suspected collapse so a human/Claude
      knows exactly what to refute.

    A non-structure/class kind or an ``unknown`` decl yields ``needs
    witness`` with the reason."""
    module = _safe_module(decl.module)
    imports = (module,) if module else ()
    if decl.is_unknown:
        return _needs_witness_probe(
            ProbeKind.SANITY, decl.name,
            "no audit evidence (status unknown) — cannot probe", imports,
        )
    if decl.kind not in _SANITY_KINDS:
        return _needs_witness_probe(
            ProbeKind.SANITY, decl.name,
            f"kind {decl.kind!r} is not a structure/class; the PUnit-collapse "
            "sanity probe applies to structures and classes", imports,
        )
    if witness is not None and witness.strip():
        if module is None:
            return _needs_witness_probe(
                ProbeKind.SANITY, decl.name,
                "defining module is not a safely-importable Lean module name",
            )
        body = (
            f"-- sanity probe: a non-trivial model of `{decl.name}` "
            f"(a PASS is T1 evidence;\n"
            f"-- a FAIL means the type collapsed — every inhabitant forced "
            f"equal, referee.md #1).\n"
            f"{witness.rstrip()}\n"
        )
        source = _import_lines(imports) + "\n\n" + body
        return Probe(
            kind=ProbeKind.SANITY,
            subject=decl.name,
            source=source,
            imports=imports,
            needs_witness=False,
            note="sanity probe (witness supplied)",
        )
    if _punit_field_heuristic(decl):
        reason = (
            f"`{decl.name}` looks like a single-Unit/PUnit/True-field "
            f"{decl.kind} (collapse trap, referee.md #1): every inhabitant is "
            "forced equal, so any instance is vacuous. Supply a `witness=` "
            f"asserting two unequal inhabitants of `{decl.name}` to refute "
            "the collapse (a FAIL confirms the bug)."
        )
    else:
        reason = (
            f"need a witnessing model for {decl.kind} `{decl.name}`: a "
            "non-trivial instance over a non-collapsing carrier (the audit "
            "snapshot carries no field types, so a model cannot be "
            "synthesized blind). Supply a `witness=` to turn this into a "
            "buildable sanity probe."
        )
    return _needs_witness_probe(ProbeKind.SANITY, decl.name, reason, imports)


# ---------------------------------------------------------------------------
# Shrink-certificate probe
# ---------------------------------------------------------------------------

def shrink_certificate_probe(
    obligation: str,
    *,
    subject: str = "kernel-shrink",
    imports: tuple[str, ...] = (),
) -> Probe:
    """Wrap a spec-auditor certificate obligation for building.

    ``obligation`` is a verbatim
    :func:`marathon.spec_auditor.certificate_obligations` snippet — already
    a runnable ``example : myDef = Mathlib.thing := rfl`` (or ``:= by …``).
    We wrap it in a file with the supplied ``imports`` (the certificate
    mentions a project-local member, so its defining module must be
    imported, plus whatever Mathlib namespace it claims equivalence to — the
    caller passes those in). A certificate that BUILDS confirms the shrink;
    one that FAILS means the shrink is REJECTED.

    A blank obligation yields a ``needs witness`` probe — there is nothing
    to build (mirrors ``certificate_obligations`` skipping blank snippets)."""
    if not obligation or not obligation.strip():
        return _needs_witness_probe(
            ProbeKind.SHRINK_CERTIFICATE, subject,
            "empty certificate — nothing to build", imports,
        )
    body = (
        f"-- kernel-shrink certificate (a build PASS confirms the shrink; a "
        f"FAIL rejects it).\n{obligation.rstrip()}\n"
    )
    source = (_import_lines(imports) + "\n\n" if imports else "") + body
    return Probe(
        kind=ProbeKind.SHRINK_CERTIFICATE,
        subject=subject,
        source=source,
        imports=imports,
        needs_witness=False,
        note="kernel-shrink certificate",
    )


# ---------------------------------------------------------------------------
# The probe runner
# ---------------------------------------------------------------------------

#: Per-probe build outcomes.
#:   pass         — the probe file built clean (T1 evidence / shrink confirmed)
#:   fail         — the probe file FAILED to build: a real Lean error
#:                  (positive evidence of a problem / shrink rejected)
#:   error        — toolchain trouble (lake missing/timeout/no-module): NOT a
#:                  probe verdict, honest absence of evidence — never a finding
#:   needs_witness — the generator could not synthesize a meaningful probe
OUTCOMES = ("pass", "fail", "error", "needs_witness")


@dataclass(frozen=True)
class ProbeOutcome:
    """The result of building one :class:`Probe`."""

    probe: Probe
    outcome: str  # one of OUTCOMES
    #: Tail of the Lean error output on a ``fail``/``error`` (empty on pass).
    error_tail: str = ""

    @property
    def is_finding(self) -> bool:
        """A ``fail`` is a high-signal finding: a failing unfolding/sanity
        probe means a real problem; a failing shrink certificate means the
        shrink is rejected. ``error`` / ``needs_witness`` are NOT findings
        (absence of evidence / thin machine evidence)."""
        return self.outcome == "fail"

    @property
    def shrink_rejected(self) -> bool:
        """True iff this is a FAILED kernel-shrink certificate — the shrink
        does not hold, so trust must NOT be moved."""
        return (
            self.probe.kind is ProbeKind.SHRINK_CERTIFICATE
            and self.outcome == "fail"
        )

    def render_line(self) -> str:
        line = f"  [{self.outcome:<13}] {self.probe.kind.value}: {self.probe.subject}"
        if self.probe.kind is ProbeKind.SHRINK_CERTIFICATE and self.outcome == "fail":
            line += "  (shrink REJECTED)"
        if self.error_tail:
            line += f"\n      {self.error_tail}"
        return line


@dataclass
class ProbeReport:
    """The outcome of building a batch of probes against a repo.

    ``outcomes`` is per-probe. ``failures`` is the run-level honesty
    channel (lake missing, repo not built, …) — same role as the audit
    snapshot's ``failures``."""

    repo_dir: str
    outcomes: list[ProbeOutcome] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list[ProbeOutcome]:
        """The high-signal failures: failing unfolding/sanity probes and
        rejected shrink certificates."""
        return [o for o in self.outcomes if o.is_finding]

    @property
    def rejected_shrinks(self) -> list[ProbeOutcome]:
        return [o for o in self.outcomes if o.shrink_rejected]

    def counts(self) -> dict[str, int]:
        c = {o: 0 for o in OUTCOMES}
        for outcome in self.outcomes:
            c[outcome.outcome] = c.get(outcome.outcome, 0) + 1
        return c

    def render(self) -> str:
        lines = [
            f"probe report: {len(self.outcomes)} probe(s) against "
            f"{self.repo_dir}"
        ]
        counts = self.counts()
        lines.append(
            "  " + "  ".join(f"{k}={counts[k]}" for k in OUTCOMES)
        )
        for outcome in self.outcomes:
            lines.append(outcome.render_line())
        if self.failures:
            lines.append(f"  run failures ({len(self.failures)}):")
            for failure in self.failures:
                lines.append(f"    - {failure}")
        return "\n".join(lines)


def _find_lake() -> str | None:
    """``lake`` from PATH, else ``~/.elan/bin/lake``, else None (mirrors the
    audit engine)."""
    found = shutil.which("lake")
    if found:
        return found
    cand = Path.home() / ".elan" / "bin" / "lake"
    return str(cand) if cand.exists() else None


def _error_tail(text: str, n: int = 5) -> str:
    """Last ``n`` non-blank lines of a Lean error blob, joined with ``/``."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return " / ".join(lines[-n:]) if lines else ""


def _build_one(
    lake: str, repo: Path, probe: Probe, *, timeout: int
) -> ProbeOutcome:
    """Build a single probe file via ``lake env lean`` with ``cwd=repo``.

    CRITICAL: the file is written into a temp dir OUTSIDE the repo tree, so
    it can never enter a ``git ls-files`` Aristotle bundle (and Aristotle
    can never edit it). ``lake env lean`` still resolves the repo's built
    modules because the environment (LEAN_PATH etc.) is set by ``cwd=repo``,
    exactly as the audit engine runs its generated script."""
    # `marathon-probe-` temp dir lives in the OS temp area, never under repo.
    with tempfile.TemporaryDirectory(prefix="marathon-probe-") as td:
        path = Path(td) / "probe.lean"
        path.write_text(probe.source)
        try:
            proc = subprocess.run(
                [lake, "env", "lean", str(path)],
                cwd=str(repo),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ProbeOutcome(
                probe, "error",
                f"probe build timed out after {timeout}s — evidence "
                "undetermined",
            )
        except OSError as exc:
            return ProbeOutcome(
                probe, "error", f"failed to run `lake env lean`: {exc}"
            )
    if proc.returncode == 0:
        return ProbeOutcome(probe, "pass", "")
    # Nonzero exit: a genuine Lean elaboration error is a `fail` (positive
    # evidence). We do not try to distinguish "import not found" (a build
    # gap) from a real type error here — both mean the probe did not build,
    # and the error tail makes the cause legible to the reader.
    tail = _error_tail(proc.stderr) or _error_tail(proc.stdout) or (
        f"`lake env lean` exited {proc.returncode} (no diagnostic output)"
    )
    return ProbeOutcome(probe, "fail", tail)


def run_probes(
    repo_dir: str | Path,
    probes: list[Probe],
    *,
    timeout: int = 600,
) -> ProbeReport:
    """Build each probe outside the repo, against the repo's built modules.

    Probe files are written to a temp dir OUTSIDE the repo tree (so they can
    never enter an Aristotle bundle) and built with ``lake env lean`` run
    with ``cwd=repo_dir`` — the same mechanism the audit engine uses, so the
    repo's pinned toolchain and already-built ``.olean``s resolve. Each
    probe is classified pass/fail/error/needs_witness with the Lean error
    tail; ``needs witness`` probes are reported but NOT built (they would be
    a vacuous pass).

    Never raises on toolchain trouble: lake missing / nonzero exit / timeout
    record on the per-probe outcome (``error``) or the report's ``failures``;
    callers see honest absence of evidence (audit-engine degradation
    pattern). A FAILED ``shrink_certificate`` outcome means the shrink is
    REJECTED (:attr:`ProbeReport.rejected_shrinks`)."""
    repo = Path(repo_dir).resolve()
    report = ProbeReport(repo_dir=str(repo))

    # `needs witness` probes never run — classify them up front.
    runnable: list[Probe] = []
    for probe in probes:
        if probe.needs_witness:
            report.outcomes.append(
                ProbeOutcome(probe, "needs_witness", probe.note or "")
            )
        else:
            runnable.append(probe)

    if not runnable:
        return report

    lake = _find_lake()
    if lake is None:
        report.failures.append(
            "lake not found (no PATH lake, no ~/.elan/bin/lake) — probe "
            "evidence undetermined"
        )
        for probe in runnable:
            report.outcomes.append(
                ProbeOutcome(probe, "error", "lake not found")
            )
        return report

    for probe in runnable:
        report.outcomes.append(_build_one(lake, repo, probe, timeout=timeout))
    return report


# ---------------------------------------------------------------------------
# Convenience: generate the default probe set for a declaration
# ---------------------------------------------------------------------------

def probes_for_decl(
    decl: DeclAudit,
    *,
    kinds: tuple[ProbeKind, ...] | None = None,
) -> list[Probe]:
    """The default pure-Lean probe set for one audited ``decl``.

    Emits an unfolding probe for def-like kinds and a sanity probe for
    structure/class kinds — each kind only where it applies (the generators
    degrade to ``needs witness`` for an inapplicable decl, but here we skip
    the inapplicable ones so a caller asking for "the probes that make sense
    for this decl" doesn't get noise). ``kinds`` restricts to a subset;
    ``shrink_certificate`` is never auto-generated here (it needs a
    spec-auditor obligation, not a decl)."""
    want = set(kinds) if kinds is not None else {
        ProbeKind.UNFOLDING, ProbeKind.SANITY,
    }
    out: list[Probe] = []
    if ProbeKind.UNFOLDING in want and decl.kind in _UNFOLDABLE_KINDS:
        out.append(unfolding_probe(decl))
    if ProbeKind.SANITY in want and decl.kind in _SANITY_KINDS:
        out.append(sanity_instance_probe(decl))
    return out

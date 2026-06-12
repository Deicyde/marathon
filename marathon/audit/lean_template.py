"""Lean audit-script template — the Lean side of the phase-5 audit engine.

:func:`render_audit_script` produces a standalone ``.lean`` file that, when
run with ``lake env lean <file>`` inside the *target repo's own workspace*
(so it elaborates with the repo's pinned toolchain and dependencies), emits
one machine-parseable line per declaration of the requested modules.

Adapted from autoform-bot's dependency-graph metaprogram
(``autoform/eval/dependency_graph/lean_script.py``): constant collection,
lambda-stripping, and the deception detectors are ported from there; the
fingerprint-grade fields (pinned-pp elaborated type/value, trusted-package
partition, transitive axioms) are new.

Output contract (the Python audit engine parses exactly this)
=============================================================

All audit output lines start with a sentinel; anything else on stdout must
be ignored by parsers.  Lines, in emission order:

``AUDIT_BEGIN``
    Sacrificial first line.  Some toolchains fold ``#eval`` IO output into a
    ``file:line:col: info:`` diagnostic prefix on the *first* printed line;
    this line absorbs that prefix so every subsequent line is clean.

``AUDIT_META|<key>|<value>``
    Run metadata, one per line, before any declaration line:

    * ``schema`` — contract version (currently ``1``).
    * ``lean_version`` — ``Lean.versionString`` of the toolchain that
      *actually elaborated* this run.  This is the in-band ground truth; the
      Python engine additionally records the repo's ``lean-toolchain`` file
      content and ``.lake`` package revs out-of-band, so cross-version
      comparisons can be flagged instead of trusted.
    * ``module`` — one line per audited module.
    * ``trusted_prefixes`` — comma-joined trusted package root prefixes used
      for the project-local partition (``-`` if empty).

``AUDIT|name|kind|module|status|type_b64|value_b64|cone|axioms|has_sorry|tags|reason_b64``
    One line per declaration (12 pipe-separated fields incl. the sentinel):

    * ``name`` — fully qualified.  Private declarations keep their full
      ``_private.<Module>.0.<name>`` form (unambiguous; display mapping is
      the consumer's job).  Names containing ``|`` (exotic ``«...»`` atoms)
      are not defended against; split with ``maxsplit`` and validate.
    * ``kind`` — one of ``theorem`` ``def`` ``instance`` ``abbrev``
      ``structure`` ``inductive`` ``class`` ``opaque`` ``axiom`` ``other``.
      ``instance`` wins over ``abbrev`` wins over ``def`` (any
      ``@[reducible]`` def is reported as ``abbrev``).
    * ``module`` — the declaration's defining module.
    * ``status`` — ``ok`` or ``unknown``.  ``unknown`` means this script
      failed *mid-declaration* (pretty-printing or axiom collection threw);
      all evidence fields are ``-`` and ``reason_b64`` holds the error.
      Whole-module failures cannot be reported from in here — if the
      generated script itself fails to import/run, the *caller* must record
      every requested declaration as ``unknown`` (absence of evidence is
      reported, never punished or hidden).
    * ``type_b64`` — base64 (RFC 4648, ``=``-padded, over UTF-8) of the
      pretty-printed *elaborated* type.  Base64 guarantees one decl = one
      line no matter what pp emits (newlines, pipes).
    * ``value_b64`` — same for the value, ONLY for ``def``/``instance``/
      ``abbrev`` (a definition's meaning is its value; type-only is
      unsound).  ``-`` for every other kind: theorems by proof irrelevance,
      opaques because their value is intentionally not part of their
      interface, structures/inductives because their "value" is their
      constructors (covered via ``cone``, see below).
    * ``cone`` — comma-joined, ``toString``-sorted project-local constants
      referenced by the TYPE (``Expr.getUsedConstants``), self excluded.
      For inductives/structures/classes the constructor types are folded in
      (field types live in the constructor, not the inductive's type).
      Trusted-package constants are excluded by the partition rule: a
      constant is project-local iff its defining module's name does not
      start with any trusted prefix.  Constants with no defining module
      (this script's own helpers, kernel builtins) count as trusted.
    * ``axioms`` — comma-joined sorted transitive axioms (``collectAxioms``),
      ``-`` if none.  ``sorryAx`` appears here like any axiom and is
      additionally singled out by:
    * ``has_sorry`` — ``true``/``false``: whether ``sorryAx`` is among the
      transitive axioms (i.e. the *honest* measure, not just a syntactic
      ``sorry`` in this decl's own body).
    * ``tags`` — ``;``-joined deception tags, ``-`` if none.  Ported from
      autoform (see "Deception tags" below).
    * ``reason_b64`` — ``-`` unless ``status`` is ``unknown``.

``AUDIT_DONE|<n>``
    Trailer with the number of ``AUDIT|`` lines emitted.  Parsers must treat
    a missing/inconsistent trailer as a truncated run (status unknown for
    everything not seen).

Determinism: declarations are sorted by name, list fields are sorted, and
pp width is pinned, so byte-identical reruns on the same toolchain+source
are expected (the golden test relies on this).

Pretty-printer pinning (the fingerprint input)
==============================================

A curated option set, NOT ``pp.all``: ``pp.all`` drags instance-term
internals and universe arguments into every string, so any Mathlib bump or
instance-name change would invalidate every fingerprint — the exact storm
the plan forbids.  Pinned set (see ``ppOpts`` in the template):

* ``pp.fullNames true`` — immune to ``open`` context.
* ``pp.privateNames true`` — private constants print under their full
  ``_private...`` name instead of a hygiene dagger.
* ``pp.notation false`` — immune to notation added/changed in deps.
* ``pp.fieldNotation false``, ``pp.structureInstances false`` — print
  applications/constructors structurally; defaults here have churned across
  toolchains.
* ``pp.universes false`` — suppress universe args on constants
  (auto-bound ``u_1`` naming is unstable).
* ``pp.proofs false`` — proof subterms inside *values* print as ``⋯``:
  proof-irrelevant by design, and far more stable.
* ``pp.deepTerms true`` + ``pp.maxSteps 100000`` — avoid ``⋯`` elision of
  large non-proof terms (elision would alias distinct values).
* ``Format.pretty (width := 100000)`` — line-break positions are part of
  the string; a huge width means no breaks.

Known residual instabilities (documented, accepted):

* Binder *names* print as written, so renaming ``(n : Nat)`` to
  ``(m : Nat)`` changes the string.  Accepted: flagging a source-level
  rename for re-review is tolerable; missing a real change is not.
* Pretty-printer output may still change across *toolchain* versions; that
  is why ``lean_version`` rides in-band and the ledger must flag (not
  trust) cross-version comparisons.
* ``pp.proofs`` elision means defs differing only in an embedded proof term
  pp identically — sound, by proof irrelevance.

Deception tags
==============

Ported from the autoform reference:

* ``vacuous_body`` — body (after stripping lambdas) is ``True``,
  ``True.intro``, ``trivial``, ``PUnit.unit``/``Unit.unit``, or a bare
  literal.
* ``ignores_params`` — body has parameters but its stripped body references
  none of them.  (autoform's bvar walk replaced by ``Expr.hasLooseBVars``
  on the stripped body. NOT exactly equivalent: autoform's walk treats
  bvars bound by lambdas nested *inside* the body as parameter references,
  so e.g. ``fun n => List.map (fun x => x) []`` escapes autoform's tag,
  while ``hasLooseBVars`` correctly sees no reference to ``n`` and tags it
  here — strictly stricter, matching the documented intent.)
* ``proof_by_exfalso`` — stripped body's head is ``False.elim`` /
  ``absurd`` / ``False.casesOn`` / ``Empty.elim``.
* ``trivial_instance`` — autoform computes this at graph level (instance of
  a *project* class whose body is suspicious); folded here into one in-script
  tag: kind is ``instance``, the type's conclusion head is a project-local
  class, and either a suspicious body tag fired or the body is a constructor
  application none of whose arguments reference project-local constants
  (autoform's ``trivial_constructor``).  This is what catches the classic
  PUnit-collapse instance.

Not ported: ``proof_by_subsingleton`` / ``returns_assumption`` /
``field_projection_body`` / ``custom_hypothesis_in_type`` (outside this
contract's four-tag scope — portable later if wanted) and ``orphan_class``
(genuinely graph-level: needs instance *counts* across the whole project,
which is the Python engine's job, not a per-decl emission).

Declaration filtering
=====================

Skipped entirely (documented so the parser knows what absence means):
internal/auxiliary names (``Name.isInternal`` / ``isInternalDetail`` on the
private-stripped name — matchers, ``_proof_N``, …), constructors and
recursors (their content reaches the parent inductive's ``cone``), aux
recursors (``casesOn``/``recOn``/``brecOn``/…), and compiler-generated
companions (``noConfusion``, ``injEq``, ``sizeOf_spec``, ``below``…).
Everything else in the requested modules is emitted — including private
declarations, anonymous instances, universe-polymorphic and mutual defs.
"""

from __future__ import annotations

import re
from typing import Sequence

# --- Output-contract constants (importable by the Python engine + tests) ---

SCHEMA_VERSION = "1"
SENTINEL = "AUDIT"
BEGIN_SENTINEL = "AUDIT_BEGIN"
META_SENTINEL = "AUDIT_META"
DONE_SENTINEL = "AUDIT_DONE"
#: Pipe-separated fields per AUDIT line, sentinel included.
AUDIT_FIELD_COUNT = 12
#: ``kind`` vocabulary.
KINDS = (
    "theorem", "def", "instance", "abbrev", "structure",
    "inductive", "class", "opaque", "axiom", "other",
)
#: Kinds that carry a ``value_b64`` field.
VALUE_KINDS = ("def", "instance", "abbrev")

#: Fallback trusted-package root prefixes.  The audit engine should derive
#: the real list from the target workspace (lake manifest / lakefile) and
#: pass it in; this hardcoded list is the documented fallback per the plan
#: ruling.  Matching is on the *defining module name* by `Name.isPrefixOf`.
DEFAULT_TRUSTED_PREFIXES: tuple[str, ...] = (
    "Init",
    "Lean",
    "Std",
    "Lake",
    "Mathlib",
    "Batteries",
    "Aesop",
    "Qq",
    "Plausible",
    "ProofWidgets",
    "ImportGraph",
    "LeanSearchClient",
    "Cli",
)

# Lean module/identifier names we are willing to splice into Lean source.
# Conservative on purpose: rejects «...» atoms, guillemets, pipes — anything
# that could change the meaning of the generated script.
_LEAN_NAME_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$"
)

# Placeholders are substituted via str.replace (NOT str.format: Lean code is
# full of braces).  They are chosen so no legitimate Lean source contains
# them, and tests assert none survive rendering.
_P_IMPORTS = "__AUDIT_IMPORTS__"
_P_MODULES = "__AUDIT_MODULES__"
_P_TRUSTED = "__AUDIT_TRUSTED__"


def _check_names(names: Sequence[str], what: str) -> list[str]:
    out = []
    for n in names:
        if not isinstance(n, str) or not _LEAN_NAME_RE.match(n):
            raise ValueError(f"unsafe {what} name for Lean splice: {n!r}")
        out.append(n)
    return out


def _name_list_literal(names: Sequence[str]) -> str:
    """Render a Lean ``List Name`` literal from dotted name strings."""
    if not names:
        return "([] : List Name)"
    return "[" + ", ".join("`" + n for n in names) + "]"


def render_audit_script(
    modules: Sequence[str],
    trusted_prefixes: Sequence[str] | None = None,
) -> str:
    """Render the audit ``.lean`` source for *modules*.

    ``modules`` — fully qualified module names to import and audit (every
    declaration *defined in* one of them is emitted).  Must be non-empty.

    ``trusted_prefixes`` — package root prefixes treated as trusted
    vocabulary (excluded from cones / project-local checks).  Defaults to
    :data:`DEFAULT_TRUSTED_PREFIXES`; pass the workspace-derived list when
    available.
    """
    mods = _check_names(list(modules), "module")
    if not mods:
        raise ValueError("render_audit_script: need at least one module")
    trusted = _check_names(
        list(DEFAULT_TRUSTED_PREFIXES if trusted_prefixes is None
             else trusted_prefixes),
        "trusted prefix",
    )
    imports = "\n".join(f"import {m}" for m in mods)
    rendered = (
        LEAN_AUDIT_TEMPLATE
        .replace(_P_IMPORTS, imports)
        .replace(_P_MODULES, _name_list_literal(mods))
        .replace(_P_TRUSTED, _name_list_literal(trusted))
    )
    assert "__AUDIT_" not in rendered
    return rendered


# ---------------------------------------------------------------------------
# The Lean source template.  Substituted via str.replace — see render above.
# ---------------------------------------------------------------------------

LEAN_AUDIT_TEMPLATE = r'''-- Marathon audit script (GENERATED — do not edit).
-- Contract: marathon/audit/lean_template.py.  Run: `lake env lean <this>`
-- from the target repo so its own toolchain + deps elaborate it.
__AUDIT_IMPORTS__
import Lean

open Lean

namespace MarathonAudit

/-- RFC 4648 base64 alphabet. -/
def b64Table : Array Char :=
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".toList.toArray

/-- Standard base64 (`=`-padded) over the UTF-8 bytes of `s`.
The pp fields ride base64-encoded so one decl is always one line. -/
def b64 (s : String) : String := Id.run do
  let bytes := s.toUTF8
  let n := bytes.size
  let mut out : String := ""
  let mut i := 0
  while i < n do
    let b0 := (bytes[i]!).toNat
    let b1 := if i + 1 < n then (bytes[i + 1]!).toNat else 0
    let b2 := if i + 2 < n then (bytes[i + 2]!).toNat else 0
    let tr := (b0 <<< 16) ||| (b1 <<< 8) ||| b2
    out := out.push b64Table[(tr >>> 18) &&& 63]!
    out := out.push b64Table[(tr >>> 12) &&& 63]!
    out := out.push (if i + 1 < n then b64Table[(tr >>> 6) &&& 63]! else '=')
    out := out.push (if i + 2 < n then b64Table[tr &&& 63]! else '=')
    i := i + 3
  return out

/-- Pinned pretty-printer options — the fingerprint input.  Curated for
stability instead of `pp.all`; rationale and known instabilities are
documented in marathon/audit/lean_template.py. -/
def ppOpts (o : Options) : Options :=
  o.setBool `pp.fullNames true
    |>.setBool `pp.privateNames true
    |>.setBool `pp.notation false
    |>.setBool `pp.fieldNotation false
    |>.setBool `pp.structureInstances false
    |>.setBool `pp.universes false
    |>.setBool `pp.proofs false
    |>.setBool `pp.deepTerms true
    |>.set `pp.maxSteps (100000 : Nat)

/-- Structural kind only — total, env-free; used for `unknown` fallback. -/
def pureKind (ci : ConstantInfo) : String :=
  match ci with
  | .thmInfo _    => "theorem"
  | .axiomInfo _  => "axiom"
  | .opaqueInfo _ => "opaque"
  | .defnInfo _   => "def"
  | .inductInfo _ => "inductive"
  | _             => "other"

/-- Refined kind.  `instance` beats `abbrev` beats `def`; any reducible def
reports as `abbrev`. -/
def kindOf (env : Environment) (name : Name) (ci : ConstantInfo) :
    MetaM String := do
  match ci with
  | .defnInfo _ =>
    if (← Meta.isInstance name) then return "instance"
    else if (← getReducibilityStatus name) matches .reducible then
      return "abbrev"
    else return "def"
  | .inductInfo _ =>
    if isClass env name then return "class"
    else if isStructure env name then return "structure"
    else return "inductive"
  | _ => return pureKind ci

/-- Compiler-generated companions we do not emit (their content reaches the
parent inductive's cone instead). -/
def isAutoCompanion (name : Name) : Bool :=
  match name with
  | .str _ s =>
    s == "noConfusion" || s == "noConfusionType" || s == "below"
      || s == "ibelow" || s == "inj" || s == "injEq" || s == "sizeOf_spec"
      || s == "ctorIdx" || s == "toCtorIdx" || s == "ofNat"
  | _ => false

/-- Emission filter: skip internal/auxiliary machinery but keep private
declarations (audited under their full `_private...` name). -/
def shouldEmit (env : Environment) (name : Name) (ci : ConstantInfo) : Bool :=
  let display := (privateToUserName? name).getD name
  if display.isInternal || display.isInternalDetail then false
  else
    match ci with
    | .ctorInfo _ | .recInfo _ | .quotInfo _ => false
    | _ => !isAuxRecursor env name && !isAutoCompanion name

/-- Strip leading lambdas (autoform port). -/
partial def stripLambdas (e : Expr) (count : Nat := 0) : Expr × Nat :=
  match e with
  | .lam _ _ body _ => stripLambdas body (count + 1)
  | _ => (e, count)

/-- Head constant of the type's conclusion (strip all foralls) — autoform's
`_dg_typeHead`. -/
partial def typeHead (e : Expr) : Name :=
  if e.isForall then typeHead e.bindingBody!
  else match e.getAppFn with
    | .const n _ => n
    | _ => Name.anonymous

/-- Deception tags, ported from the autoform reference (see the Python
docstring for the exact provenance of each). -/
def deceptionTags (env : Environment) (ci : ConstantInfo)
    (isProjectLocal : Name → Bool) (isInst : Bool) : List String :=
  match ci.value? with
  | none => []
  | some val =>
    let (inner, lamCount) := stripLambdas val
    let headConst := inner.getAppFn
    -- vacuous_body
    let t1 := match inner with
      | .const n _ =>
        if n == ``True || n == ``PUnit.unit || n == `Unit.unit
            || n == ``True.intro || n == `trivial then ["vacuous_body"]
        else []
      | .app (.const n _) _ =>
        if n == ``True.intro || n == `trivial then ["vacuous_body"] else []
      | .lit _ => ["vacuous_body"]
      | _ => []
    -- ignores_params (hasLooseBVars on the stripped body ≡ autoform's
    -- bvar-below-depth walk, since constant values are closed terms)
    let t2 := if lamCount > 0 && !inner.hasLooseBVars then ["ignores_params"]
      else []
    -- proof_by_exfalso
    let t3 := match headConst with
      | .const n _ =>
        if n == ``False.elim || n == ``absurd || n == `False.casesOn
            || n == `Empty.elim then ["proof_by_exfalso"]
        else []
      | _ => []
    -- trivial_constructor core (autoform): body is a constructor of a
    -- project-local type applied to args none of which reference
    -- project-local constants.
    let trivialCtor := match headConst with
      | .const ctorName _ =>
        match env.find? ctorName with
        | some (.ctorInfo cinfo) =>
          if isProjectLocal cinfo.induct then
            let args := inner.getAppArgs
            if args.size > 0 then
              let argConsts := args.foldl (init := #[]) fun acc a =>
                acc ++ a.getUsedConstants
              let ctorParent := ctorName.getPrefix
              !(argConsts.any fun n =>
                isProjectLocal n && n != ctorName
                  && !(ctorParent.isPrefixOf n))
            else false
          else false
        | _ => false
      | _ => false
    -- trivial_instance (autoform's graph-level rule, folded in-script):
    -- an instance of a project-local class with a suspicious body.
    let t4 :=
      if isInst && isProjectLocal (typeHead ci.type)
          && (trivialCtor || !t1.isEmpty || !t2.isEmpty || !t3.isEmpty) then
        ["trivial_instance"]
      else []
    t1 ++ t2 ++ t3 ++ t4

/-- Project-local constants referenced by the TYPE (plus constructor types
for inductive-like decls), self excluded, sorted. -/
def typeCone (env : Environment) (name : Name) (ci : ConstantInfo)
    (isProjectLocal : Name → Bool) : Array Name := Id.run do
  let mut used := ci.type.getUsedConstants
  if let .inductInfo info := ci then
    for c in info.ctors do
      if let some cinfo := env.find? c then
        used := used ++ cinfo.type.getUsedConstants
  let mut set : NameSet := {}
  for n in used do
    if isProjectLocal n && n != name then
      set := set.insert n
  return set.toList.toArray.qsort (fun a b => a.toString < b.toString)

def joinNames (ns : Array Name) : String :=
  if ns.isEmpty then "-"
  else ",".intercalate (ns.toList.map (fun n => n.toString))

/-- The full evidence line for one declaration.  May throw (pp, axioms);
the caller catches and falls back to an `unknown` line. -/
def auditLine (name modName : Name) (ci : ConstantInfo)
    (isProjectLocal : Name → Bool) : Elab.Command.CommandElabM String :=
  Elab.Command.liftTermElabM do
    let env ← getEnv
    let kind ← kindOf env name ci
    let pp := fun (e : Expr) => withOptions ppOpts do
      return (← Meta.ppExpr e).pretty (width := 100000)
    let typePp ← pp ci.type
    let valueField ←
      if kind == "def" || kind == "instance" || kind == "abbrev" then
        match ci.value? with
        | some v => do pure (b64 (← pp v))
        | none => pure "-"
      else pure "-"
    let cone := typeCone env name ci isProjectLocal
    let axioms ← collectAxioms name
    let axioms := axioms.qsort (fun a b => a.toString < b.toString)
    let hasSorry := axioms.contains ``sorryAx
    let tags := deceptionTags env ci isProjectLocal (kind == "instance")
    let tagsStr := if tags.isEmpty then "-" else ";".intercalate tags
    return s!"AUDIT|{name}|{kind}|{modName}|ok|{b64 typePp}|{valueField}|{joinNames cone}|{joinNames axioms}|{hasSorry}|{tagsStr}|-"

/-- Evidence-free line: absence of evidence is reported, never hidden. -/
def unknownLine (name modName : Name) (ci : ConstantInfo) (reason : String) :
    String :=
  s!"AUDIT|{name}|{pureKind ci}|{modName}|unknown|-|-|-|-|-|-|{b64 reason}"

end MarathonAudit

-- #eval! (not #eval): the audited environment may transitively contain
-- sorryAx (that is the point of auditing), and plain #eval refuses to run.
open Lean Elab Command MarathonAudit in
#eval! show CommandElabM Unit from do
  let env ← getEnv
  let auditModules : List Name := __AUDIT_MODULES__
  let trustedPrefixes : List Name := __AUDIT_TRUSTED__
  let moduleOf? : Name → Option Name := fun n =>
    match env.getModuleIdxFor? n with
    | some idx => env.header.moduleNames[idx.toNat]?
    | none => none
  -- Partition rule (BINDING): project-local iff the defining module is not
  -- under a trusted package prefix.  No module (kernel builtins, this
  -- script's own helpers) counts as trusted.
  let isProjectLocal : Name → Bool := fun n =>
    match moduleOf? n with
    | some m => !trustedPrefixes.any (fun p => p.isPrefixOf m)
    | none => false
  IO.println "AUDIT_BEGIN"
  IO.println "AUDIT_META|schema|1"
  IO.println s!"AUDIT_META|lean_version|{Lean.versionString}"
  for m in auditModules do
    IO.println s!"AUDIT_META|module|{m}"
  IO.println s!"AUDIT_META|trusted_prefixes|{joinNames trustedPrefixes.toArray}"
  let decls := env.constants.fold (init := #[]) fun acc n ci =>
    match moduleOf? n with
    | some m =>
      if auditModules.contains m && shouldEmit env n ci then
        acc.push (n, m, ci)
      else acc
    | none => acc
  let decls := decls.qsort (fun a b => a.1.toString < b.1.toString)
  let mut emitted := 0
  for (n, m, ci) in decls do
    let line ←
      try
        auditLine n m ci isProjectLocal
      catch e => do
        let msg ← e.toMessageData.toString
        pure (unknownLine n m ci msg)
    IO.println line
    emitted := emitted + 1
  IO.println s!"AUDIT_DONE|{emitted}"
'''

"""Spec-auditor: the firewalled Claude role that renders a trust kernel.

WHY: phase-6a's spec card is the human-facing "diff-of-meaning" unit — the
minimized surface a human reads to sign a theorem to T2 (plan §2 ruling 5).
Three Claude jobs feed that card, and all three are about *legibility of the
kernel*, never about deciding anything:

* ``informal_rendering`` — plain-English statement of the target, rendered
  from the Lean STATEMENT alone (type + kernel members), so the human reads
  mathematics instead of Lean. Carries the standing ``⚠️ LLM-rendered,
  verification pending`` caveat: this is the human's comparison object, not
  a faithfulness verdict.
* ``kernel_shrink`` — suggestions that a project-local kernel member is
  really a Mathlib construction in disguise, EACH with a runnable Lean
  certificate (``example : myDef = Mathlib.thing := rfl`` and friends). The
  certificate is the whole point: a shrink the machine can't check just
  *moves* trust from "read this def" to "trust Claude's equivalence claim"
  (the "who audits the kernel-shrinker?" critique). Phase 6b actually runs
  these via :func:`certificate_obligations`.
* ``delta_prose`` — one ADVISORY sentence narrating a machine-computed
  semantic-delta class for the human; never gating, never overriding the
  deterministic class.

Binding constraints, mirroring the advisory jury (:mod:`marathon.jury`):

* **The firewall is absolute.** The spec-auditor is NEVER shown the source
  ``.tex`` — copyright, and circularity (grading an LLM rendering against
  the LLM's own guess at the source is worthless). It renders from the Lean
  alone; faithfulness-to-source stays the human's job. This module assembles
  context from audit evidence only and never reads a source text.
* **Advisory ⇒ never raises.** :func:`audit_spec_card` returns ``None`` on
  *any* failure (no CLI, no prompt file, subprocess error, unparseable
  output) and prints a soft one-line note. A broken spec-auditor must never
  break the card pipeline.

THE SPEC-CARD INTERFACE (thin, documented, decoupled). The kernel/card data
model (``marathon/audit/kernel.py``, ``spec_card.py``) is built concurrently
by another agent; this module codes to a *structural* interface so the two
can land independently:

* ``card.target`` — a member-like object: ``.name``, ``.type_pp``,
  ``.value_pp`` (``value_pp`` may be ``None`` for theorems).
* ``card.kernel_members`` — an iterable of member-like objects, each with
  ``.name``, ``.type_pp``, ``.value_pp`` — the project-local defs in the
  target's transitive statement cone (Mathlib excluded). May be empty.
* ``card.evidence`` — optional; rendered loosely (``str()``) into the
  context as a short evidence note (axioms / probe results / tags). Absent
  or ``None`` is fine.

A "member-like object" is anything with those three attributes — a
:class:`~marathon.audit.records.DeclAudit` already qualifies (it has
``name``/``type_pp``/``value_pp``), so a kernel member can be a DeclAudit or
a purpose-built dataclass. If the concurrent agent's final attribute names
differ, adapt here (or note the mismatch) rather than coupling tightly.

RECONCILED (phase-6a integration): the concurrent agent's final
:class:`~marathon.audit.spec_card.SpecCard` DID diverge from the thin shape
above — its ``.target`` is the decl *name string* (statement type on the
card's own ``.type_pp``), and its kernel members live under
``.kernel.members`` (a :class:`~marathon.audit.kernel.Kernel`), not a flat
``.kernel_members``. Per the standing instruction this is absorbed *here*,
in the consumer's adapter (:func:`_adapt_target` / :func:`_adapt_members`),
which accepts BOTH the thin documented shape and the real ``SpecCard`` — the
kernel agent's files are untouched and this module's public surface is
unchanged.
"""

from __future__ import annotations

import json
import os
import re
import shutil

# Imported (though the call itself lives in marathon.claude_proc) so tests
# can monkeypatch ``spec_auditor.subprocess.run`` — a setattr on the stdlib
# module object reaches claude_proc's ``subprocess.run`` too, matching the
# jury's test convention.
import subprocess  # noqa: F401
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from marathon.claude_proc import run_claude

# Default model, matching the jury / rater. Resolution order is computed per
# call (explicit ``model`` arg > MARATHON_CLAUDE_MODEL env > this constant)
# so long-lived processes and tests see env changes.
DEFAULT_MODEL = "claude-opus-4-7"

# Context cap on any single rendered pp string (a pathological generated type
# or value must not blow the model's context). Module-level so tests and
# unusual deployments can override without re-plumbing arguments.
MAX_PP_CHARS = 20_000

# The closed semantic-delta vocabulary (plan §2 ruling 5). Used only to
# label the machine class we pass through to the prompt; we never compute or
# override it here.
DELTA_CLASSES = (
    "strengthened",
    "weakened",
    "equivalent-refactor",
    "meaning-changed",
)

# The standing caveat every spec-card rendering carries. Surfaced by render
# helpers so the human always knows the rendering is an LLM guess to check
# against the book, never a faithfulness verdict.
LLM_RENDER_CAVEAT = "⚠️ LLM-rendered, verification pending"


# --- Result types -------------------------------------------------------------


@dataclass(frozen=True)
class KernelShrink:
    """One certifiable kernel-shrink suggestion.

    The ``certificate`` is load-bearing: it is the runnable Lean snippet a
    later probe phase compiles to confirm (or refute) the claim. A shrink
    without a checkable certificate is not a shrink — it just moves trust —
    so :func:`audit_spec_card` drops any suggestion missing one."""

    member: str
    claim: str
    certificate: str
    confidence: Optional[str] = None  # "high" | "medium" | "low"


@dataclass(frozen=True)
class SpecAudit:
    """One spec-auditor call's outcome: the three advisory renderings.

    All fields are best-effort: a usable audit needs at least one of an
    informal rendering, a shrink suggestion, or delta prose. ``parse_warning``
    carries lenient-parse notes for the JSONL trail without failing the call.
    """

    informal_rendering: Optional[str] = None
    kernel_shrink: tuple[KernelShrink, ...] = ()
    delta_prose: Optional[str] = None
    model: Optional[str] = None
    parse_warning: Optional[str] = None

    def render_rendering(self) -> str:
        """The informal rendering with the standing LLM-rendered caveat
        prepended — the form that goes onto a spec card / review body, so
        the human always sees it is an LLM guess to verify, never a verdict."""
        body = self.informal_rendering or "(no informal rendering produced)"
        return f"*{LLM_RENDER_CAVEAT}*\n\n{body}"

    def render_line(self) -> str:
        """One-line console summary, e.g.
        ``spec-audit (advisory): rendering=yes shrinks=2 delta=yes``."""
        rendering = "yes" if self.informal_rendering else "no"
        delta = "yes" if self.delta_prose else "no"
        line = (
            f"spec-audit (advisory): rendering={rendering} "
            f"shrinks={len(self.kernel_shrink)} delta={delta}"
        )
        if self.parse_warning:
            line += f"  [{self.parse_warning}]"
        return line


def certificate_obligations(audit: SpecAudit) -> list[str]:
    """Extract the kernel-shrink certificate snippets, in order, so a later
    probe phase (6b) can actually run them.

    Each obligation is the raw Lean ``certificate`` string of a suggested
    shrink; the probe phase is responsible for assembling them into a built
    (never imported) ``MarathonAudit/Probes/`` file and recording pass/fail.
    Blank/whitespace-only certificates are skipped — there is nothing to run.
    A shrink whose certificate fails to compile is simply not a real shrink;
    this function exposes the obligations cleanly and judges none of them."""
    return [
        s.certificate
        for s in audit.kernel_shrink
        if s.certificate and s.certificate.strip()
    ]


# --- Context assembly (audit evidence ONLY — never the .tex) ------------------


def _cap(text: Optional[str], cap: int) -> str:
    """Render a pp string into the prompt, truncating at ``cap`` with an
    explicit marker so the model knows it is looking at a prefix. ``None``
    (e.g. a theorem's absent value) becomes a short placeholder."""
    if text is None:
        return "(none)"
    if len(text) > cap:
        return text[:cap] + f"\n... (truncated at {cap:,} chars)"
    return text


def _member_block(member: Any) -> str:
    """One member rendered as ``name``/``type``/``value`` — the kernel
    member's full machine meaning (NOT any source text). Reads the structural
    ``.name``/``.type_pp``/``.value_pp`` interface; missing attributes degrade
    to ``(unknown)`` rather than raising (advisory robustness)."""
    name = getattr(member, "name", None) or "(unnamed)"
    type_pp = getattr(member, "type_pp", None)
    value_pp = getattr(member, "value_pp", None)
    parts = [f"- name: {name}", f"  type: {_cap(type_pp, MAX_PP_CHARS)}"]
    # Defs carry value (their meaning); theorems carry none (proof
    # irrelevance) — only show the value line when there is one.
    if value_pp is not None:
        parts.append(f"  value: {_cap(value_pp, MAX_PP_CHARS)}")
    return "\n".join(parts)


def _machine_delta_label(machine_delta: Optional[str]) -> Optional[str]:
    """Normalize a passed-in machine delta class to the closed vocabulary;
    an unrecognized value is passed through verbatim (the prompt treats it as
    ground truth either way — we never compute it here), and ``None`` stays
    ``None`` (no prior version)."""
    if machine_delta is None:
        return None
    return machine_delta


def _adapt_target(card: Any) -> Any:
    """Resolve the card's target into a member-like object (``.name`` /
    ``.type_pp`` / ``.value_pp``), bridging the two card shapes this module
    must accept:

    * the **documented thin interface** (``FakeCard`` / any card whose
      ``.target`` is already a member-like object) — used verbatim;
    * the **real** :class:`~marathon.audit.spec_card.SpecCard`, whose
      ``.target`` is the decl *name string* and whose statement type/value
      live in the card's own ``.type_pp`` / ``.fingerprint``-bearing fields.
      We synthesize a member-like view from those (the concurrent kernel
      agent's final field names diverged; this is the documented "adapt
      here rather than couple tightly" reconciliation).

    Returns ``None`` only when there is genuinely no target on the card."""
    target = getattr(card, "target", None)
    if target is None:
        return None
    # A member-like target already satisfies the interface (FakeCard path):
    # a non-string object exposing the pp attributes. Use it directly.
    if not isinstance(target, str):
        return target
    # Real SpecCard: ``.target`` is the decl name; its statement type is the
    # card's top-level ``type_pp`` (the value, for a def target, is not kept
    # on the card — theorems carry none, so ``value_pp=None`` is faithful).
    return SimpleNamespace(
        name=target,
        type_pp=getattr(card, "type_pp", None),
        value_pp=getattr(card, "value_pp", None),
    )


def _adapt_members(card: Any) -> list:
    """Resolve the card's kernel members into a list of member-like objects.

    Accepts both the documented ``card.kernel_members`` (thin interface) and
    the real :class:`~marathon.audit.spec_card.SpecCard`, which exposes them
    under ``card.kernel.members`` (a :class:`~marathon.audit.kernel.Kernel`
    whose ``KernelMember`` entries already have ``.name``/``.type_pp``/
    ``.value_pp``). The thin interface wins when present; otherwise we read
    ``card.kernel.members``."""
    members = getattr(card, "kernel_members", None)
    if members is not None:
        return list(members)
    kernel = getattr(card, "kernel", None)
    if kernel is not None:
        return list(getattr(kernel, "members", None) or [])
    return []


def _assemble_prompt(
    rubric: str,
    card: Any,
    machine_delta: Optional[str],
) -> str:
    """Rubric + target + kernel members + optional evidence + optional
    machine delta class, joined with ``---`` separators.

    Crucially this assembles context from the AUDIT EVIDENCE on the card
    ONLY — target/kernel pp strings, the loosely-rendered evidence note, and
    the machine delta class. There is no code path here that reads or embeds
    a source ``.tex``; the firewall is enforced by construction.

    The card's two shapes (documented thin interface, real ``SpecCard``) are
    reconciled by :func:`_adapt_target` / :func:`_adapt_members` — adapter
    only; neither the kernel agent's files nor this module's interface change."""
    target = _adapt_target(card)
    target_block = (
        _member_block(target) if target is not None else "(no target provided)"
    )

    members = _adapt_members(card)
    if members:
        members_block = "\n".join(_member_block(m) for m in members)
    else:
        members_block = (
            "(none — the statement's cone contains no project-local "
            "definitions; its vocabulary is entirely trusted Mathlib/core)"
        )

    parts = [
        rubric,
        "## Target declaration (the theorem/def under audit)\n\n" + target_block,
        "## Kernel members (project-local defs in the statement cone)\n\n"
        + members_block,
    ]

    evidence = getattr(card, "evidence", None)
    if evidence is not None:
        evidence_text = str(evidence).strip()
        if evidence_text:
            parts.append(
                "## Machine evidence (axioms / probes / tags — advisory "
                "context)\n\n" + evidence_text
            )

    delta = _machine_delta_label(machine_delta)
    if delta is not None:
        parts.append(
            "## Machine semantic-delta class (ground truth for your "
            "advisory delta sentence)\n\n"
            f"The deterministic fingerprint layer classified this change as: "
            f"`{delta}`.\nNarrate it; do not override it."
        )

    return "\n\n---\n\n".join(parts)


# --- Lenient JSON parse (jury.py pattern) -------------------------------------


def _extract_json_object(text: str) -> str:
    """Extract a JSON-object-shaped substring from a Claude response (bare
    object, fenced block, or embedded object — the jury's extractor ladder)."""
    text = text.strip()
    if text.startswith("{"):
        return text
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    bare = re.search(r"\{.*\}", text, re.DOTALL)
    if bare:
        return bare.group(0)
    raise ValueError("no JSON object found in response")


def _extract_lenient(response: str) -> tuple[Optional[dict], Optional[str]]:
    """Tolerant extraction of the spec-audit JSON. Strict ``json.loads``
    first; on failure, regex per-field recovery of the string fields
    (``informal_rendering``, ``delta_prose``).

    ``kernel_shrink`` is a structured list, so it is recovered ONLY on a
    clean strict parse — a malformed top-level object means we cannot trust
    the certificate snippets, and emitting an obligation we mis-parsed would
    be worse than emitting none. The warning records that shrinks were
    dropped so the human/JSONL knows the recovery was partial.

    Returns ``(data, partial_warning)``:
      - ``(dict, None)`` on clean strict parse;
      - ``(dict, "warning")`` on partial recovery (string fields only);
      - ``(None, None)`` if nothing could be extracted.
    """
    try:
        return json.loads(_extract_json_object(response)), None
    except (json.JSONDecodeError, ValueError):
        pass

    data: dict = {}
    # Brace in the trailing char class kept out of the f-string (3.14
    # disallows a literal '}' inside an f-string replacement region).
    tail = r'"\s*' + r"[},]"
    for field_name in ("informal_rendering", "delta_prose"):
        m = re.search(rf'"{field_name}"\s*:\s*"([\s\S]*?){tail}', response)
        if m:
            data[field_name] = (
                m.group(1)
                .replace(r"\n", "\n")
                .replace(r"\"", '"')
                .replace(r"\\", "\\")
            )

    if not data:
        return None, None

    return data, (
        "lenient-parse fallback used (strict json.loads failed); "
        "kernel_shrink suggestions dropped (structured field unrecoverable)"
    )


def _coerce_shrinks(raw: Any) -> tuple[list[KernelShrink], Optional[str]]:
    """Coerce the ``kernel_shrink`` field into validated :class:`KernelShrink`
    objects. Drops any entry that is not an object, lacks a ``member`` or a
    non-blank ``certificate`` — the iron rule: no certificate, no shrink, so a
    suggestion the machine can't check never becomes an obligation. Returns
    ``(shrinks, warning)`` where ``warning`` notes how many entries were
    dropped (advisory, never fatal)."""
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], "kernel_shrink was not a list; ignored"

    shrinks: list[KernelShrink] = []
    dropped = 0
    for item in raw:
        if not isinstance(item, dict):
            dropped += 1
            continue
        member = item.get("member")
        claim = item.get("claim")
        certificate = item.get("certificate")
        # No runnable certificate ⇒ not a shrink, just moved trust. Drop it.
        if not isinstance(member, str) or not member.strip():
            dropped += 1
            continue
        if not isinstance(certificate, str) or not certificate.strip():
            dropped += 1
            continue
        confidence = item.get("confidence")
        shrinks.append(
            KernelShrink(
                member=member,
                claim=claim if isinstance(claim, str) else "",
                certificate=certificate,
                confidence=(
                    confidence if isinstance(confidence, str) else None
                ),
            )
        )
    warning = (
        f"dropped {dropped} kernel_shrink suggestion(s) without a runnable "
        "certificate"
        if dropped
        else None
    )
    return shrinks, warning


def _resolve_model(model: Optional[str]) -> str:
    if model:
        return model
    return os.environ.get("MARATHON_CLAUDE_MODEL") or DEFAULT_MODEL


def audit_spec_card(
    card: Any,
    *,
    machine_delta: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[SpecAudit]:
    """Render a spec card's trust kernel via one ``claude -p`` call.

    Assembles context from the card's audit evidence ONLY — the target's
    type/value, the kernel members' types/values, the loose evidence note,
    and (if provided) the machine semantic-delta class. **Never** any source
    ``.tex``: the firewall is enforced by the assembly having no source-text
    code path at all.

    Returns ``None`` on any failure (no CLI, no prompt file, subprocess
    error, empty/unparseable output) — the spec-auditor is advisory, so a
    broken renderer must never break the card pipeline. Failures print a soft
    one-line note (post-pipeline console style).

    ``machine_delta`` is the deterministic fingerprint layer's delta class
    (one of :data:`DELTA_CLASSES`, or ``None`` when there is no prior
    version); it is passed to the prompt as ground truth for the advisory
    delta sentence and is never recomputed or overridden here. ``model``
    overrides ``MARATHON_CLAUDE_MODEL``, which overrides :data:`DEFAULT_MODEL`.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        print("  spec-audit: skipped — claude CLI not on PATH")
        return None

    prompt_path = Path(__file__).parent / "prompts" / "spec_audit.md"
    if not prompt_path.is_file():
        print(f"  spec-audit: skipped — spec_audit.md missing at {prompt_path}")
        return None
    rubric = prompt_path.read_text()

    prompt = _assemble_prompt(rubric, card, machine_delta)

    resolved_model = _resolve_model(model)

    # Subprocess conventions (stdin prompt against E2BIG, ANTHROPIC_API_KEY
    # scrub for Max OAuth, the cross-process slot limiter) live in
    # marathon.claude_proc.run_claude. The pre-resolved model is passed
    # explicitly so the audit's ``model`` field and the actual call agree.
    try:
        proc = run_claude(prompt, model=resolved_model)
    except OSError as e:
        print(
            f"  spec-audit: skipped — could not exec claude "
            f"(errno {e.errno}: {e.strerror})"
        )
        return None

    if proc.returncode != 0:
        err = ((proc.stderr or proc.stdout) or "").strip()[:300]
        print(f"  spec-audit: skipped — claude exited {proc.returncode}: {err}")
        return None

    response = (proc.stdout or "").strip()
    if not response:
        print("  spec-audit: skipped — claude returned empty stdout")
        return None

    data, partial_warning = _extract_lenient(response)
    if data is None:
        print(
            f"  spec-audit: skipped — could not extract JSON; "
            f"raw: {response[:200]}"
        )
        return None

    warnings: list[str] = [partial_warning] if partial_warning else []

    informal = data.get("informal_rendering")
    informal = informal.strip() if isinstance(informal, str) and informal.strip() else None

    delta = data.get("delta_prose")
    delta = delta.strip() if isinstance(delta, str) and delta.strip() else None

    shrinks, shrink_warning = _coerce_shrinks(data.get("kernel_shrink"))
    if shrink_warning:
        warnings.append(shrink_warning)

    # A usable audit needs at least one of the three renderings; an object
    # with no informal rendering, no shrink, and no delta is not worth a card.
    if informal is None and not shrinks and delta is None:
        print(
            f"  spec-audit: skipped — no usable fields in response; "
            f"raw: {response[:200]}"
        )
        return None

    return SpecAudit(
        informal_rendering=informal,
        kernel_shrink=tuple(shrinks),
        delta_prose=delta,
        model=resolved_model,
        parse_warning="; ".join(warnings) if warnings else None,
    )

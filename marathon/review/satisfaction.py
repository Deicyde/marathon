"""Reject-note SATISFACTION check — did Aristotle's iteration actually deliver
the human's KEEP / FIX / ADD asks, or should the refine send itself back?

A two-tier judge over a parsed reject note (:mod:`marathon.review.reject_note`):

* DETERMINISTIC tier (no LLM): a KEEP decl that has VANISHED from the code is
  unmet (the refine deleted something it was told to keep); an ADD ask that
  NAMES a specific declaration which is still ABSENT is unmet. ``scan_lean_source``
  is a line scanner, so this proves PRESENCE only.
* SEMANTIC tier (an injected ``llm_judge``): FIX correctness and FREE-TEXT ADD
  asks ("the corresponding definitions for CovariantTensorField") can't be
  proven by a name scan — they go to the LLM judge with the cumulative diff. When
  no LLM judge is available the semantic asks are UNDECIDED and the run falls
  back to today's behaviour (open the PR; the human re-reviews) rather than
  looping forever.

Pure + dependency-light: the only side effect is the optional ``llm_judge`` the
caller injects. Used by the refine loop to gate the PR-open and decide whether to
re-iterate with a :func:`compose_corrective_prompt` listing ONLY the unmet asks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from marathon.review.reject_note import ParsedRejectNote, parse_reject_note

#: A qualified Lean name token (has at least one dot, no whitespace) — used to
#: pull a concrete decl name out of a free-text ADD ask when one is present.
_QUALIFIED_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*\.[A-Za-z0-9_']+")
_REPO_SCAN_MAX_FILES = 4000
_REPO_SCAN_MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass
class SatisfactionResult:
    """The verdict on whether a reject note's asks were delivered.

    ``satisfied`` — every ask the judge could decide is met. ``unmet_items`` —
    the human-readable asks still outstanding (drives the corrective prompt).
    ``decided_by`` — ``deterministic`` / ``llm`` / ``fallback``. ``needs_human``
    — the judge could NOT decide a semantic ask (no LLM); the caller should stop
    iterating and open the PR for human re-review rather than loop."""

    satisfied: bool
    unmet_items: list[str] = field(default_factory=list)
    decided_by: str = "deterministic"
    needs_human: bool = False


def _scan_target_decls(repo_dir: Path, target_rel: Optional[str]) -> set[str]:
    """Qualified names of every declaration currently in the code under
    ``target_rel`` (or the whole repo when None). Pure read via
    ``scan_lean_source``; capped on file count + size; ``set()`` on error."""
    from marathon.formalization import scan_lean_source

    base = repo_dir / target_rel if target_rel else repo_dir
    out: set[str] = set()
    try:
        files = sorted(base.rglob("*.lean"))[:_REPO_SCAN_MAX_FILES]
    except OSError:
        return out
    for path in files:
        try:
            if path.stat().st_size > _REPO_SCAN_MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for s in scan_lean_source(text)[0]:
            if s.qualified:
                out.add(s.qualified)
    return out


def _present(name: str, decls: set[str]) -> bool:
    """True iff ``name`` is among ``decls`` — exact, or as a whole trailing
    dotted segment (a cited ``A.b`` matches ``X.A.b``), the deck's match rule."""
    if name in decls:
        return True
    suffix = "." + name
    return any(d.endswith(suffix) for d in decls)


def _named_add_target(item: str) -> Optional[str]:
    """The qualified decl name an ADD ask names (in backticks or inline), or
    ``None`` for a purely descriptive free-text ask."""
    m = _QUALIFIED_NAME_RE.search(item)
    return m.group(0) if m else None


def judge_satisfaction(
    note: str,
    repo_dir: Path,
    target_rel: Optional[str],
    *,
    diff_text: Optional[str] = None,
    llm_judge: Optional[Callable[[str, str, list[str]], "SatisfactionResult"]] = None,
) -> SatisfactionResult:
    """Judge whether the reject ``note``'s asks are delivered by the current code
    (+ ``diff_text`` for the semantic tier).

    DETERMINISTIC: a KEEP decl missing from the code, or a NAMED ADD decl still
    absent, is unmet. SEMANTIC (FIX items + free-text ADD): handed to
    ``llm_judge(note, diff_text, asks)`` when one is injected, else UNDECIDED →
    ``needs_human`` (caller opens the PR for human re-review). An unstructured /
    legacy note (``parse_ok`` False) is treated as satisfied (no regression to
    today's one-shot flow)."""
    parsed = parse_reject_note(note)
    if not parsed.parse_ok:
        return SatisfactionResult(True, [], "fallback")

    present = _scan_target_decls(repo_dir, target_rel)
    unmet: list[str] = []

    # KEEP: a verified decl the refine was told to keep must still be present.
    for name in parsed.keep:
        if not _present(name, present):
            unmet.append(f"KEEP decl was removed: {name}")

    # ADD: a NAMED target must be present; descriptive asks go to the LLM tier.
    semantic_asks: list[str] = []
    for item in parsed.add:
        named = _named_add_target(item)
        if named is not None:
            if not _present(named, present):
                unmet.append(f"ADD not yet present: {named}")
        else:
            semantic_asks.append(f"ADD: {item}")

    # FIX: correctness — always semantic.
    for name, fix_note in parsed.fix:
        semantic_asks.append(f"FIX {name}: {fix_note}")

    if semantic_asks:
        if llm_judge is not None and diff_text is not None:
            verdict = llm_judge(note, diff_text, semantic_asks)
            unmet.extend(verdict.unmet_items)
            return SatisfactionResult(
                satisfied=not unmet, unmet_items=unmet, decided_by="llm"
            )
        # No way to decide the semantic asks → don't loop; let the human re-review.
        return SatisfactionResult(
            satisfied=not unmet,
            unmet_items=unmet,
            decided_by="fallback",
            needs_human=True,
        )

    return SatisfactionResult(satisfied=not unmet, unmet_items=unmet,
                              decided_by="deterministic")


def compose_corrective_prompt(issue_num: int, unmet_items: list[str]) -> str:
    """The next-iteration prompt naming ONLY the still-unmet asks — so a refine
    that delivered part of a reject note is sent back for the REST, not the whole
    note again. Verbatim to Aristotle."""
    lines = [
        f"Your previous iteration on issue #{issue_num} did NOT fully satisfy "
        "the review. The following items are STILL outstanding — address ONLY "
        "these, and KEEP everything you already produced:",
        "",
    ]
    for item in unmet_items:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("Leave all other code untouched.")
    return "\n".join(lines)


def is_no_progress(prev_unmet: list[str], cur_unmet: list[str]) -> bool:
    """True when an iteration made NO relevant progress: the outstanding asks are
    identical to the previous round's (same set). The loop stops on this to avoid
    burning Aristotle calls on an issue it cannot resolve."""
    return bool(cur_unmet) and set(prev_unmet) == set(cur_unmet)

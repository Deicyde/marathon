"""Shared structure of the per-declaration REJECT NOTE — the KEEP/FIX/ADD
instruction the deck composes at ship (:func:`marathon.deck.subdeck.compose_ship_note`)
and Aristotle receives verbatim.

ONE definition of the section headers, imported by BOTH the composer AND the
parser the reject-note SATISFACTION check uses
(:mod:`marathon.review.satisfaction`) — so a header edit can never silently
desync the two (the design review's header-coupling fix). The parser matches on
the stable header PREFIX, not the full sentence, so wording tweaks don't break
parsing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The exact header lines the composer emits.
KEEP_HEADER = (
    "KEEP these declarations exactly as written (human-verified — do NOT "
    "change their statements or signatures):"
)
FIX_HEADER = "FIX these declarations (rejected):"
ADD_HEADER = (
    "ADD the missing declaration(s) — a result this issue's title promises is "
    "not yet formalized; add it (and keep everything above):"
)
FOOTER = "Leave all other code untouched."

# Stable prefixes the parser matches on (robust to header-wording edits).
KEEP_PREFIX = "KEEP these declarations"
FIX_PREFIX = "FIX these declarations"
ADD_PREFIX = "ADD the missing declaration"


@dataclass
class ParsedRejectNote:
    """A reject note split into its KEEP / FIX / ADD items.

    ``keep`` — decl names to leave exactly as written; ``fix`` — ``(decl,
    note)`` pairs the human rejected; ``add`` — free-text "what is still
    missing" lines (NOT decl names). ``parse_ok`` is False for an unstructured /
    legacy note (no recognised section header) — the satisfaction check then
    falls back to today's one-shot behaviour."""

    keep: list[str] = field(default_factory=list)
    fix: list[tuple[str, str]] = field(default_factory=list)
    add: list[str] = field(default_factory=list)
    parse_ok: bool = False


def _bullet(line: str) -> str:
    """The text of a ``- …`` bullet (stripped), or ``""`` for a non-bullet."""
    s = line.strip()
    return s[1:].strip() if s.startswith("- ") or s == "-" else ""


def parse_reject_note(note: str) -> ParsedRejectNote:
    """Parse a composed reject note into KEEP / FIX / ADD items.

    Matches on the stable header prefixes; ``- `` bullets under each header are
    that section's items (FIX items split on the first ``:`` into
    ``(decl, note)``). Blank lines do not end a section; the ``FOOTER`` does.
    ``parse_ok`` is False when no section header is found."""
    out = ParsedRejectNote()
    section: str | None = None
    for raw in (note or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith(KEEP_PREFIX):
            section, out.parse_ok = "keep", True
            continue
        if stripped.startswith(FIX_PREFIX):
            section, out.parse_ok = "fix", True
            continue
        if stripped.startswith(ADD_PREFIX):
            section, out.parse_ok = "add", True
            continue
        if stripped.startswith(FOOTER):
            section = None
            continue
        item = _bullet(raw)
        if not item or section is None:
            continue
        if section == "keep":
            out.keep.append(item)
        elif section == "fix":
            name, _sep, rest = item.partition(":")
            out.fix.append((name.strip(), rest.strip()))
        else:  # add
            out.add.append(item)
    return out

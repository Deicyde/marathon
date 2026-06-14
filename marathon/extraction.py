"""Phase-7 textbook intake: turn a source text into Target-shaped dicts.

WHY this module exists: goal 2's hands-off entry point is "point marathon
at a textbook and it builds a target ledger." There are two ways to get
targets out of a textbook, and the marathon-v2 plan (§2 ruling 6 "one
machinery, two modes"; the firewall paragraph at the end of §2) forces them
apart by a hard policy line:

* **copyrighted mode (the default, the GeometricAnalysis case).** Claude
  must NEVER read the source ``.tex``. Two independent reasons: copyright
  confinement (the firewall the whole project is built around — plan §1
  "what is load-bearing"), and the faithfulness-circularity reason (an LLM
  that read the book and then "renders" a statement has not been audited
  against anything). So copyrighted-mode targets come from human-supplied
  informal statements — the existing review convention, one markdown
  section per named result (see ``review/chapter_sessions.py``) — or a list
  of named results the human typed. Claude may STRUCTURE/normalize that
  human text, but is never handed the book. Each target is flagged
  ``source_mode="copyrighted"`` so downstream knows the informal statement
  is human-authored.

* **open mode (explicit opt-in).** For open-licensed sources marathon
  vendors autoform-bot's proven extraction pipeline
  (``autoform/statement_extraction/``): chunk the text into overlapping
  line windows, run K independent extractor Claude calls per chunk,
  consensus-accept the statements all K agreed on, let a reviewer call
  arbitrate the disputes, and a merger call dedup across chunk overlaps.
  Output targets are flagged ``source_mode="open"`` (LLM-extracted —
  verification is still the human's job).

The firewall is enforced *in code*, not just by convention: the
copyrighted path raises if anyone hands it a ``.tex`` path, and the prompt
it assembles is grep-able for "zero source text" (the test does exactly
that).

All Claude goes through :func:`marathon.claude_proc.run_claude` — prompt
via stdin (E2BIG), ``ANTHROPIC_API_KEY`` scrubbed for Max OAuth, the
cross-process slot limiter. The open path is robust to Claude failures: a
failed extractor call simply drops out and consensus is taken over the
survivors; an all-fail chunk yields no targets rather than crashing.

The output is a list of plain dicts (NOT a marathon ledger ``Target`` — the
``targets`` table is the plan-layer agent's to define). The shape is the
thin documented interface in :func:`extract_targets`'s docstring; the plan
layer persists it. If the plan layer's constructor differs at integration
time, adapt in :func:`to_plan_target` here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from marathon.claude_proc import run_claude

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

#: The two firewall-gated extraction modes. ``copyrighted`` is the safe
#: default (no Claude-reads-the-book); ``open`` opts into the autoform
#: chunk -> K-extract -> consensus -> merge pipeline.
SOURCE_MODES = ("copyrighted", "open")
DEFAULT_SOURCE_MODE = "copyrighted"

#: File suffixes we treat as "the copyrighted source itself". The
#: copyrighted path refuses these by construction — a ``.tex`` (or other
#: book source) must never reach a Claude prompt under that mode. ``.md``
#: is intentionally absent: the human-supplied informal-statements file IS
#: a ``.md`` and is the legitimate copyrighted-mode input.
SOURCE_TEXT_SUFFIXES = (".tex", ".latex", ".ltx")

#: Statement kinds we recognise (matches the extractor rubric + autoform).
KIND_KEYWORDS = (
    "theorem",
    "lemma",
    "proposition",
    "definition",
    "corollary",
    "axiom",
    "conjecture",
    "construction",
    "claim",
)

# Default chunking geometry (overlapping line windows), matching autoform's
# 500/50. Module-level so callers / tests can shrink them.
DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50

# Default number of independent extractor calls per chunk in open mode.
DEFAULT_K = 4

_PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.md"


class ExtractionError(RuntimeError):
    """Raised for firewall violations and unrecoverable caller errors
    (a ``.tex`` handed to copyrighted mode; copyrighted mode with no
    human-supplied input; an unknown mode). Distinct from the
    *recoverable* Claude failures the open path swallows — those never
    raise, they just drop the failed call's contribution."""


# ---------------------------------------------------------------------------
# Target-shaped output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedTarget:
    """One extracted target, the thin interface handed to the plan layer.

    Deliberately a plain record, not a marathon ledger ``Target`` — the
    ``targets`` table belongs to the plan-layer agent. :func:`as_dict`
    renders the documented dict shape the plan layer persists.

    Fields:

    * ``name`` — the statement's label, e.g. ``"Theorem 3.2 (Heine-Borel)"``.
    * ``kind`` — one of :data:`KIND_KEYWORDS`, or ``"unknown"``.
    * ``source_ref`` — provenance string: the source path/identifier this
      came from (a chunk's line range in open mode; the informal-statements
      file or ``"human-list"`` in copyrighted mode).
    * ``statement`` — the (human-authored or LLM-extracted) informal
      statement text.
    * ``source_mode`` — ``"copyrighted"`` or ``"open"``; the honesty marker
      that travels with the target so the deck/review layer knows whether
      the informal statement was human-authored or LLM-rendered.
    """

    name: str
    kind: str
    source_ref: str
    statement: str
    source_mode: str

    def as_dict(self) -> dict:
        """The documented dict the plan layer persists."""
        return {
            "name": self.name,
            "kind": self.kind,
            "source_ref": self.source_ref,
            "statement": self.statement,
            "source_mode": self.source_mode,
        }


def to_plan_target(target: ExtractedTarget) -> dict:
    """Map an :class:`ExtractedTarget` to the plan layer's persisted dict.

    A single named seam: if the plan-layer ``Target`` constructor ends up
    wanting different keys (e.g. ``gate_policy`` defaulting), adapt HERE
    rather than threading it through extraction. Today it's a pass-through
    onto :meth:`ExtractedTarget.as_dict`.
    """
    return target.as_dict()


def _normalize_kind(raw: Optional[str]) -> str:
    """Coerce a model/human-supplied ``kind`` to one of
    :data:`KIND_KEYWORDS`, else ``"unknown"``. Lenient: matches on the
    first recognised keyword appearing in the string (handles
    ``"Theorem"``, ``"theorem"``, ``"a theorem"``)."""
    if not raw:
        return "unknown"
    low = raw.strip().lower()
    for kw in KIND_KEYWORDS:
        if kw in low:
            return kw
    return "unknown"


def _normalize_name(name: str) -> str:
    """Normalized key for dedup/consensus: case-folded, whitespace-
    collapsed. Mirrors autoform's ``normalize_statement_name`` intent
    without importing it (different repo)."""
    return re.sub(r"\s+", " ", name).strip().lower()


# ---------------------------------------------------------------------------
# Lenient JSON-list parse (the extractor rubric asks for a strict JSON list;
# real models wander, so parse leniently)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)
_LIST_RE = re.compile(r"\[.*\]", re.DOTALL)


def parse_statement_list(response: str) -> Optional[list[dict]]:
    """Parse a JSON list of statement objects from a Claude response.

    Tries, in order: the bare/whole response, a ```json fenced block, the
    first ``[...]`` substring. Returns the list of dicts on success, ``[]``
    for an explicit empty list, or ``None`` if nothing parseable was found
    (the caller treats ``None`` as "this call produced nothing" and drops
    it — never crashes).
    """
    stripped = (response or "").strip()
    if not stripped:
        return None
    if stripped == "[]":
        return []

    candidates: list[str] = [stripped]
    m = _FENCE_RE.search(stripped)
    if m:
        candidates.append(m.group(1).strip())
    m = _LIST_RE.search(stripped)
    if m:
        candidates.append(m.group(0))

    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list) and all(isinstance(e, dict) for e in parsed):
            return parsed
    return None


def _entry_to_target(
    entry: dict, *, source_ref: str, source_mode: str
) -> Optional[ExtractedTarget]:
    """Turn one parsed dict into an :class:`ExtractedTarget`, or ``None``
    if it has no usable name. Tolerates both the extract.md schema
    (``name``/``statement``/``kind``/``citation``) and autoform's
    (``description``/``location``)."""
    name = str(entry.get("name", "")).strip()
    if not name:
        return None
    statement = str(
        entry.get("statement") or entry.get("description") or ""
    ).strip()
    kind = _normalize_kind(entry.get("kind"))
    citation = str(entry.get("citation") or entry.get("location") or "").strip()
    ref = f"{source_ref} | {citation}" if citation else source_ref
    return ExtractedTarget(
        name=name,
        kind=kind,
        source_ref=ref,
        statement=statement,
        source_mode=source_mode,
    )


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _load_rubric() -> str:
    if not _PROMPT_PATH.is_file():
        raise ExtractionError(f"extract.md prompt missing at {_PROMPT_PATH}")
    return _PROMPT_PATH.read_text()


def _build_open_prompt(rubric: str, chunk_text: str, ref: str) -> str:
    """Open-mode extractor prompt: rubric + the source-text chunk."""
    return (
        f"{rubric}\n\n---\n\n"
        f"## Source text chunk to extract from ({ref})\n\n"
        f"{chunk_text}"
    )


def _build_copyrighted_prompt(rubric: str, label: str, statement: str) -> str:
    """Copyrighted-mode normalize prompt.

    FIREWALL: this assembles ONLY the rubric and the human-supplied
    statement. The copyrighted source text never appears — by construction,
    because this function has no access to it (the caller never reads the
    ``.tex``). The test greps the assembled prompt for source text and
    asserts zero.
    """
    return (
        f"{rubric}\n\n---\n\n"
        f"## Human-supplied statement to normalize (copyrighted mode)\n\n"
        f"The source book is NOT shown to you. Normalize only the single "
        f"statement below; do not invent or reconstruct anything.\n\n"
        f"Label: {label}\n\n"
        f"Statement:\n{statement}"
    )


# ---------------------------------------------------------------------------
# Chunking (overlapping line windows — adapted from autoform's chunking.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    start_line: int
    end_line: int


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    """Split ``text`` into overlapping line windows. Overlap lets a
    statement straddling a window boundary be seen whole in one of the two
    windows; the merger dedups the resulting double-count."""
    lines = text.splitlines()
    if not lines:
        return []
    if overlap >= chunk_size:
        overlap = chunk_size - 1  # guard against a non-advancing window
    chunks: list[Chunk] = []
    start = 0
    index = 0
    while start < len(lines):
        end = min(start + chunk_size, len(lines))
        chunks.append(
            Chunk(
                index=index,
                text="\n".join(lines[start:end]),
                start_line=start + 1,
                end_line=end,
            )
        )
        index += 1
        if end >= len(lines):
            break
        start += chunk_size - overlap
    return chunks


# ---------------------------------------------------------------------------
# Open-mode pipeline: chunk -> K extract -> consensus + reviewer -> merge
# ---------------------------------------------------------------------------


def _run_claude_list(prompt: str, *, model: Optional[str]) -> Optional[list[dict]]:
    """One Claude call returning a parsed statement list, or ``None`` on
    ANY failure (no CLI, non-zero exit, empty output, unparseable). The
    open pipeline is built to lose individual calls gracefully — a dropped
    call is a survivor-consensus input, never a crash."""
    try:
        proc = run_claude(prompt, model=model)
    except Exception:  # noqa: BLE001 — open pipeline must never crash on a
        # single call: OSError from exec, TimeoutExpired, anything. The call
        # just drops out and survivor consensus carries on.
        return None
    if proc.returncode != 0:
        return None
    return parse_statement_list(proc.stdout or "")


def _extract_k_from_chunk(
    rubric: str,
    chunk: Chunk,
    *,
    k: int,
    model: Optional[str],
) -> list[list[ExtractedTarget]]:
    """Run K independent extractor calls over one chunk. Failed calls
    (``None``) are dropped — the returned list holds only survivors, so
    consensus downstream is over actual results."""
    ref = f"lines {chunk.start_line}-{chunk.end_line}"
    prompt = _build_open_prompt(rubric, chunk.text, ref)
    runs: list[list[ExtractedTarget]] = []
    for _ in range(k):
        parsed = _run_claude_list(prompt, model=model)
        if parsed is None:
            continue  # survivor consensus: this call dropped out
        targets = [
            t
            for e in parsed
            if (t := _entry_to_target(e, source_ref=ref, source_mode="open"))
            is not None
        ]
        runs.append(targets)
    return runs


@dataclass
class _Consensus:
    """Per-chunk consensus split: ``agreed`` (every surviving call found
    it) and ``disputed`` (some but not all did)."""

    agreed: list[ExtractedTarget] = field(default_factory=list)
    disputed: list[ExtractedTarget] = field(default_factory=list)


def _consensus(runs: list[list[ExtractedTarget]]) -> _Consensus:
    """Accept statements all surviving calls found; everything else is
    disputed. Keyed by normalized name. With zero survivors, returns an
    empty split (the all-fail-chunk case).

    A single survivor is NOT its own consensus. "Consensus" means
    *independent* corroboration; one lone extractor agreeing with itself is
    no signal at all (and the all-but-one-failed case is exactly the
    rate-limiting condition this build hit). So with one survivor every
    statement it found is routed to ``disputed`` — the reviewer arbitration
    call supplies the second independent look before anything is accepted.
    With two or more survivors, unanimity across them is genuine agreement
    and auto-accepts as before."""
    out = _Consensus()
    n = len(runs)
    if n == 0:
        return out
    by_call: list[dict[str, ExtractedTarget]] = []
    for run in runs:
        idx: dict[str, ExtractedTarget] = {}
        for t in run:
            idx.setdefault(_normalize_name(t.name), t)
        by_call.append(idx)
    all_keys: dict[str, None] = {}
    for idx in by_call:
        for key in idx:
            all_keys.setdefault(key, None)
    for key in all_keys:
        found = [idx[key] for idx in by_call if key in idx]
        # Unanimous across >=2 independent survivors → agreed; a lone
        # survivor (n == 1) has no independent corroborator, so its finds
        # are disputed and must clear arbitration.
        if len(found) == n and n >= 2:
            out.agreed.append(found[0])
        else:
            out.disputed.append(found[0])
    return out


def _review_disputes(
    rubric: str,
    chunk: Chunk,
    disputed: list[ExtractedTarget],
    *,
    model: Optional[str],
) -> list[ExtractedTarget]:
    """Arbitrate disputed statements with one reviewer Claude call: re-show
    the chunk, ask which disputed names genuinely appear. The reviewer
    re-extracts from the chunk; we keep only disputed names it confirms.
    On reviewer failure we conservatively drop the disputes (a statement
    only some extractors saw, that the arbiter couldn't confirm, stays
    out)."""
    if not disputed:
        return []
    ref = f"lines {chunk.start_line}-{chunk.end_line}"
    names = ", ".join(d.name for d in disputed)
    prompt = (
        f"{rubric}\n\n---\n\n"
        f"## Arbitration ({ref})\n\n"
        f"Independent extractors disagreed on these statements: {names}.\n"
        f"Re-read the chunk below and return the JSON list of ONLY those "
        f"disputed statements that genuinely appear as labeled statements "
        f"in it (drop any that are misidentifications, proof-internal "
        f"claims, or absent).\n\n"
        f"## Source text chunk\n\n{chunk.text}"
    )
    parsed = _run_claude_list(prompt, model=model)
    if not parsed:
        return []
    confirmed_keys = {
        _normalize_name(str(e.get("name", ""))) for e in parsed if e.get("name")
    }
    by_key = {_normalize_name(d.name): d for d in disputed}
    return [by_key[k] for k in confirmed_keys if k in by_key]


def _merge_chunks(
    per_chunk: list[list[ExtractedTarget]],
) -> list[ExtractedTarget]:
    """Dedup across chunk overlaps by normalized name, preserving order
    (first occurrence wins). Adapted from autoform's merger intent but done
    deterministically in Python: overlapping windows re-emit the same
    labeled statement, and the label is a reliable dedup key — no Claude
    call needed for the common case."""
    seen: set[str] = set()
    merged: list[ExtractedTarget] = []
    for chunk_targets in per_chunk:
        for t in chunk_targets:
            key = _normalize_name(t.name)
            if key in seen:
                continue
            seen.add(key)
            merged.append(t)
    return merged


def _extract_open(
    text: str,
    *,
    source_ref: str,
    k: int,
    model: Optional[str],
    chunk_size: int,
    overlap: int,
) -> list[ExtractedTarget]:
    """The full open-mode pipeline over an in-memory source ``text``."""
    rubric = _load_rubric()
    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    per_chunk: list[list[ExtractedTarget]] = []
    for chunk in chunks:
        runs = _extract_k_from_chunk(rubric, chunk, k=k, model=model)
        cons = _consensus(runs)
        reviewed = _review_disputes(rubric, chunk, cons.disputed, model=model)
        per_chunk.append(cons.agreed + reviewed)
    merged = _merge_chunks(per_chunk)
    # Stamp the book-level source_ref onto each (the per-chunk line range is
    # already folded into each target's source_ref).
    return [
        ExtractedTarget(
            name=t.name,
            kind=t.kind,
            source_ref=f"{source_ref} ({t.source_ref})",
            statement=t.statement,
            source_mode="open",
        )
        for t in merged
    ]


# ---------------------------------------------------------------------------
# Copyrighted-mode parsing of human-supplied informal statements
# ---------------------------------------------------------------------------

# An informal-statements markdown file is "one section per named result"
# (the review convention — see review/chapter_sessions.py). A section opens
# with a markdown header line; the header carries the name (and often a
# ``(ref)``), and the body is the informal statement.
_HEADER_RE = re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*$")
# Pull a trailing parenthetical citation off a header title, e.g.
# "Theorem 14.23 (Lee p. 352)" -> name="Theorem 14.23", cite="Lee p. 352".
_TRAILING_PAREN_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<cite>[^()]*)\)\s*$")


@dataclass(frozen=True)
class InformalSection:
    """One parsed section of a human informal-statements file."""

    name: str
    citation: str
    statement: str


def parse_informal_statements(text: str) -> list[InformalSection]:
    """Parse a human-authored informal-statements markdown file into
    sections — one per named result. A section is a header line plus the
    body up to the next header. The header title is the ``name``; a
    trailing ``(...)`` parenthetical is split off as ``citation``.

    Pure text parsing — no Claude. This is the firewall-safe path: the file
    is human-authored, so reading it is allowed; Claude only ever sees the
    human's wording (in the optional normalize pass), never the book.
    """
    sections: list[InformalSection] = []
    cur_name: Optional[str] = None
    cur_cite = ""
    body: list[str] = []

    def _flush() -> None:
        if cur_name is None:
            return
        sections.append(
            InformalSection(
                name=cur_name,
                citation=cur_cite,
                statement="\n".join(body).strip(),
            )
        )

    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            _flush()
            title = m.group("title").strip()
            pm = _TRAILING_PAREN_RE.match(title)
            if pm:
                cur_name = pm.group("name").strip()
                cur_cite = pm.group("cite").strip()
            else:
                cur_name = title
                cur_cite = ""
            body = []
        elif cur_name is not None:
            body.append(line)
    _flush()
    return sections


def _section_to_target(
    section: InformalSection, *, source_ref: str
) -> ExtractedTarget:
    """A parsed informal section becomes a copyrighted-mode target. The
    statement is the human's verbatim wording (no Claude needed); ``kind``
    is inferred from the name label."""
    citation = section.citation
    ref = f"{source_ref} | {citation}" if citation else source_ref
    return ExtractedTarget(
        name=section.name,
        kind=_normalize_kind(section.name),
        source_ref=ref,
        statement=section.statement,
        source_mode="copyrighted",
    )


def _normalize_copyrighted(
    target: ExtractedTarget, *, model: Optional[str]
) -> ExtractedTarget:
    """Optionally run Claude to STRUCTURE/normalize a human-supplied
    statement (clean wording, infer a tidier kind). The firewall holds: the
    prompt carries only the human's text, never the source. On any Claude
    failure we keep the human's verbatim statement — normalization is a
    nicety, not a requirement."""
    rubric = _load_rubric()
    prompt = _build_copyrighted_prompt(rubric, target.name, target.statement)
    parsed = _run_claude_list(prompt, model=model)
    if not parsed:
        return target
    norm = _entry_to_target(
        parsed[0], source_ref=target.source_ref, source_mode="copyrighted"
    )
    if norm is None:
        return target
    # Keep the human's name/source_ref authoritative; take only the
    # normalized statement (and a kind if the human's name was ambiguous).
    kind = target.kind if target.kind != "unknown" else norm.kind
    return ExtractedTarget(
        name=target.name,
        kind=kind,
        source_ref=target.source_ref,
        statement=norm.statement or target.statement,
        source_mode="copyrighted",
    )


def _extract_copyrighted(
    *,
    informal_statements: Optional[Path],
    named_results: Optional[list[str]],
    normalize: bool,
    model: Optional[str],
) -> list[ExtractedTarget]:
    """The copyrighted path. NEVER reads a source ``.tex``. Builds targets
    from (a) the human informal-statements markdown file and/or (b) a
    human-supplied list of named results. At least one must be present."""
    targets: list[ExtractedTarget] = []

    if informal_statements is not None:
        path = Path(informal_statements)
        _guard_not_source_text(path, "informal-statements file")
        if not path.is_file():
            raise ExtractionError(
                f"informal-statements file not found: {path}"
            )
        sections = parse_informal_statements(path.read_text())
        ref = str(path)
        targets.extend(_section_to_target(s, source_ref=ref) for s in sections)

    if named_results:
        for label in named_results:
            label = label.strip()
            if not label:
                continue
            targets.append(
                ExtractedTarget(
                    name=label,
                    kind=_normalize_kind(label),
                    source_ref="human-list",
                    statement="",
                    source_mode="copyrighted",
                )
            )

    if informal_statements is None and not named_results:
        raise ExtractionError(
            "copyrighted mode requires a human source: pass "
            "informal_statements (a markdown file) and/or named_results. "
            "Claude must never read the copyrighted source text (the "
            "firewall)."
        )

    if normalize:
        targets = [_normalize_copyrighted(t, model=model) for t in targets]
    return targets


# ---------------------------------------------------------------------------
# Firewall guard
# ---------------------------------------------------------------------------


def _guard_not_source_text(path: Path, what: str) -> None:
    """Raise if ``path`` looks like the copyrighted source itself. The
    firewall in code: copyrighted mode must never be handed (and thus never
    open) a ``.tex``/``.latex``. ``.md`` is allowed — that's the human's
    informal-statements file."""
    if path.suffix.lower() in SOURCE_TEXT_SUFFIXES:
        raise ExtractionError(
            f"firewall: refusing a source-text file ({path.suffix}) as a "
            f"{what} in copyrighted mode — Claude must never read the "
            f"copyrighted source. Supply a human-authored informal-"
            f"statements markdown file instead, or use --mode open for an "
            f"open-licensed source."
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_targets(
    source,
    *,
    mode: str = DEFAULT_SOURCE_MODE,
    k: int = DEFAULT_K,
    model: Optional[str] = None,
    informal_statements: Optional[Path] = None,
    named_results: Optional[list[str]] = None,
    normalize: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[dict]:
    """Extract Target-shaped dicts from a source, dispatching on ``mode``.

    This is the documented interface the plan layer consumes: a list of
    dicts ``{name, kind, source_ref, statement, source_mode}`` (see
    :meth:`ExtractedTarget.as_dict`). The plan layer persists them; this
    module never touches the ledger.

    ``mode``:

    * ``"copyrighted"`` (default) — the firewall-safe path. ``source`` is
      ignored as a text source; targets come from ``informal_statements``
      (a human-authored markdown file, one section per named result) and/or
      ``named_results`` (a list of labels). Passing a ``.tex`` anywhere a
      file is read raises :class:`ExtractionError`. With ``normalize=True``,
      Claude STRUCTURES the human wording — but is handed ONLY that wording,
      never the book.

    * ``"open"`` — the autoform consensus pipeline. ``source`` is a path to
      an open-licensed text file (or a directory of them), read and chunked
      into overlapping windows; K extractor Claude calls per chunk;
      consensus-accept unanimous statements; a reviewer call arbitrates
      disputes; dedup across overlaps. Robust to Claude failures (dropped
      calls -> survivor consensus; all-fail chunk -> no targets) — never
      crashes.

    Returns the list of dicts. May be empty (e.g. an open source with no
    labeled statements, or every Claude call failing).
    """
    if mode not in SOURCE_MODES:
        raise ExtractionError(
            f"unknown source mode {mode!r}; expected one of {SOURCE_MODES}"
        )

    if mode == "copyrighted":
        # Belt-and-braces: if a caller routed the copyrighted source in via
        # ``source`` (a path), refuse it before anything reads it.
        if source is not None:
            sp = Path(str(source))
            if sp.suffix.lower() in SOURCE_TEXT_SUFFIXES:
                _guard_not_source_text(sp, "source")
        targets = _extract_copyrighted(
            informal_statements=informal_statements,
            named_results=named_results,
            normalize=normalize,
            model=model,
        )
        return [to_plan_target(t) for t in targets]

    # open mode
    text = _read_source_text(source)
    source_ref = str(source)
    targets = _extract_open(
        text,
        source_ref=source_ref,
        k=k,
        model=model,
        chunk_size=chunk_size,
        overlap=overlap,
    )
    return [to_plan_target(t) for t in targets]


def _read_source_text(source) -> str:
    """Read an open-mode source into one text blob. ``source`` may be a
    file or a directory (concatenate ``.md``/``.tex`` files, sorted — the
    autoform convention). Open mode is the ONLY place a ``.tex`` is read,
    and only because the source is open-licensed."""
    if source is None:
        raise ExtractionError("open mode requires a --source path")
    path = Path(str(source))
    if path.is_dir():
        files = sorted(
            f
            for f in path.iterdir()
            if f.is_file() and f.suffix.lower() in (".md", ".tex")
        )
        if not files:
            raise ExtractionError(f"no .md or .tex files found in {path}")
        return "\n".join(f.read_text(encoding="utf-8") for f in files)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    raise ExtractionError(f"open-mode source not found: {path}")

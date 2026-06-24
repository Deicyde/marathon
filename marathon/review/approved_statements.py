"""Approved-statements manifest + the hard statement-immutability guard.

When a human verifies a sub-issue, the Lean *statements* (signatures) it cites
become FROZEN: subsequent ``marathon fill`` / ``marathon refine`` iterations may
fill proof bodies and add new sub-lemmas, but must NEVER change a frozen
signature. This module is the enforcement that earlier was only prompt-text plus
a soft post-hoc warning (see :mod:`marathon.review.verified_decls`).

Two halves:

* :func:`capture_on_verify` — called from
  :func:`marathon.review.state.record_verification`. Records each verified
  declaration's CURRENT signature (read from the chapter source, so it is in the
  same shape the guard will later compare against) into the sidecar manifest
  ``.marathon/review/approved_statements.json``.

* :func:`guard_touched_files` — called by a fill/refine iteration AFTER it writes
  its extracted changes but BEFORE it commits. For every frozen declaration whose
  file was touched, it compares the current signature to the frozen one and, on a
  mismatch, surgically restores that one declaration's full text from the
  pre-iteration baseline (so its signature returns to frozen and its body returns
  to the baseline ``sorry``), leaving every other declaration's fill intact. The
  violation therefore can never land.

Signature ::= the text from the declaration keyword up to the body separator
``:=`` (or ``where``) at bracket-depth zero, whitespace-normalized. Filling a
``sorry`` only changes the body (after ``:=``), so an unchanged signature ⇒ no
violation; any change to binders or the stated type ⇒ violation.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from marathon.review.config import ReviewConfig

# Mirror the decl grammar used across marathon (verified_decls / fill).
_DECL_KEYWORDS = (
    "def", "theorem", "lemma", "abbrev", "instance", "structure",
    "class", "inductive", "opaque", "axiom",
)
_ATTR = r"(?:@\[[^\]]*\]\s*)*"
_MOD = r"(?:noncomputable\s+)?(?:private\s+)?(?:protected\s+)?"
_DECL_RE = re.compile(
    r"^[ \t]*" + _ATTR + _MOD
    + r"(?:" + "|".join(_DECL_KEYWORDS) + r")\s+"
    + r"(?P<name>[A-Za-z_][\w'.]*)",
    re.MULTILINE,
)
_LEAN_BLOCK_RE = re.compile(r"```lean\n(.*?)\n```", re.DOTALL)

MANIFEST_VERSION = 1

# Open/close bracket pairs whose nesting we track so a `:=` inside binder
# defaults / instance-implicit brackets is not mistaken for the body separator.
_OPEN = {"(": ")", "[": "]", "{": "}", "⟨": "⟩"}
_CLOSE = {v: k for k, v in _OPEN.items()}


# --- signature extraction (shared by capture + guard) ----------------------

def normalize_sig(text: str) -> str:
    """Collapse all runs of whitespace to single spaces and strip, so two
    formattings of the same signature compare equal."""
    return re.sub(r"\s+", " ", text).strip()


def _body_separator_index(text: str) -> int:
    """Index of the top-level ``:=`` (bracket-depth 0) in ``text``, or -1.

    Also stops at a top-level ``where`` token (treated as a body separator for
    structures/defs). Used to slice off the proof body so only the signature is
    compared."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth = max(0, depth - 1)
        elif depth == 0:
            if c == ":" and i + 1 < n and text[i + 1] == "=":
                return i
            # a top-level ` where ` separates a body too
            if text.startswith("where", i) and (i == 0 or text[i - 1].isspace()) \
                    and (i + 5 >= n or not (text[i + 5].isalnum() or text[i + 5] == "_")):
                return i
        i += 1
    return -1


def extract_signature(text: str, decl_name: str) -> Optional[str]:
    """The normalized signature of ``decl_name`` within ``text`` (a Lean source
    string or an issue ``### Lean signatures`` block), or None if not found.

    The signature runs from the declaration keyword to the top-level ``:=``
    (exclusive); an issue block that already shows only the signature (no body)
    yields the whole declaration. Scans to the start of the NEXT declaration so a
    multi-line type is captured in full."""
    starts = [(m.start(), m.group("name")) for m in _DECL_RE.finditer(text)]
    for idx, (start, name) in enumerate(starts):
        if name != decl_name:
            continue
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(text)
        block = text[start:end]
        sep = _body_separator_index(block)
        sig = block if sep == -1 else block[:sep]
        return normalize_sig(sig)
    return None


def find_decl_block(lines: list[str], decl_name: str) -> Optional[tuple[int, int]]:
    """The ``[start, end)`` line span of ``decl_name`` in ``lines`` (0-based,
    end-exclusive), from its keyword line up to the line before the next
    declaration / ``end <ns>`` / EOF. None if not found. Used for the surgical
    per-declaration revert."""
    decl_lines: list[int] = []
    for i, line in enumerate(lines):
        m = _DECL_RE.match(line)
        if m:
            decl_lines.append(i)
    for j, start in enumerate(decl_lines):
        m = _DECL_RE.match(lines[start])
        if m and m.group("name") == decl_name:
            # End at the next decl-opening line, or an `end ` line at column 0,
            # or EOF — whichever comes first after `start`.
            nxt = decl_lines[j + 1] if j + 1 < len(decl_lines) else len(lines)
            end = nxt
            for k in range(start + 1, nxt):
                if re.match(r"^end(\s|$)", lines[k]):
                    end = k
                    break
            return (start, end)
    return None


# --- manifest --------------------------------------------------------------

@dataclass
class ApprovedStatement:
    """One frozen declaration: its approved signature and provenance."""
    decl_name: str
    signature: str          # normalized
    issue: int
    chapter: Optional[int] = None
    verified_ts: Optional[str] = None


def manifest_path(repo_dir: Path) -> Path:
    return Path(repo_dir) / ".marathon" / "review" / "approved_statements.json"


def load_manifest(repo_dir: Path) -> dict[str, ApprovedStatement]:
    """``{decl_name: ApprovedStatement}``; empty dict if the manifest is absent
    or unreadable (never raises — a missing manifest just means nothing frozen)."""
    p = manifest_path(repo_dir)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, ApprovedStatement] = {}
    for name, rec in (data.get("statements") or {}).items():
        try:
            out[name] = ApprovedStatement(
                decl_name=name,
                signature=rec["signature"],
                issue=int(rec.get("issue", 0)),
                chapter=rec.get("chapter"),
                verified_ts=rec.get("verified_ts"),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_manifest(repo_dir: Path, statements: dict[str, ApprovedStatement]) -> Path:
    p = manifest_path(repo_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MANIFEST_VERSION,
        "statements": {
            name: {k: v for k, v in asdict(st).items() if k != "decl_name"}
            for name, st in sorted(statements.items())
        },
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return p


# --- capture-on-verify (A1) ------------------------------------------------

def _chapter_source_text(cfg: ReviewConfig, chapter: int) -> str:
    """Concatenation of every ``.lean`` file under the chapter's target folder
    (best-effort, empty if the folder is absent). Used to read the current
    signature of a freshly-verified declaration.

    Thin wrapper over :func:`_chapter_source_read` that drops the read-failure
    flag — kept for callers (capture) that only need the text."""
    text, _read_failed = _chapter_source_read(cfg, chapter)
    return text


def _chapter_source_read(cfg: ReviewConfig, chapter: int) -> tuple[str, bool]:
    """``(text, read_failed)`` for the chapter's ``.lean`` source.

    ``text`` is the concatenation of every readable ``.lean`` file under the
    chapter's target folder (best-effort, empty if the folder is absent).
    ``read_failed`` is True iff AT LEAST ONE ``.lean`` file existed but could
    NOT be read (``OSError``) — i.e. ``text`` is a PARTIAL view of the source.

    The flag matters for staleness: a partial read makes a frozen decl that
    lives in the unread file look removed/changed even though the source is
    fine, so the staleness diff must suppress its source-derived checks just as
    it does for an all-empty source."""
    try:
        target = cfg.target_path(chapter)
    except Exception:  # noqa: BLE001
        return ("", False)
    if not target.is_dir():
        return ("", False)
    parts: list[str] = []
    read_failed = False
    for p in sorted(target.rglob("*.lean")):
        try:
            parts.append(p.read_text())
        except OSError:
            read_failed = True
            continue
    return ("\n\n".join(parts), read_failed)


def capture_on_verify(cfg: ReviewConfig, issue_num: int) -> list[str]:
    """Freeze the statements of every declaration cited in verified ``issue_num``.

    Reads the issue body for the cited declaration names, then captures each
    decl's CURRENT signature from the chapter source (falling back to the issue
    block's own signature text if the decl isn't found in source), and upserts
    them into the manifest. Best-effort: returns the list of frozen decl names,
    or ``[]`` on any failure — capture must never block a verification.
    """
    try:
        from marathon.review.verified_decls import extract_declarations_from_issue_body
        from marathon.review.github import issue_body as _issue_body
    except Exception:  # noqa: BLE001
        return []
    try:
        body = _issue_body(issue_num, cfg.github_repo)
    except Exception:  # noqa: BLE001
        body = None
    if not body:
        return []
    decls = extract_declarations_from_issue_body(body)
    if not decls:
        return []
    # the issue's own Lean text (block contents, fences stripped) — the fallback
    # signature source when a decl isn't found in the chapter files yet.
    issue_lean = "\n\n".join(m.group(1) for m in _LEAN_BLOCK_RE.finditer(body))
    chapter = None
    try:
        chapter = cfg.chapter_of_issue(issue_num)
    except Exception:  # noqa: BLE001
        chapter = None
    src = _chapter_source_text(cfg, chapter) if chapter is not None else ""
    ts = datetime.now().astimezone().isoformat(timespec="seconds")

    manifest = load_manifest(cfg.repo_dir)
    frozen: list[str] = []
    for name in sorted(decls):
        sig = extract_signature(src, name) if src else None
        if sig is None:
            sig = extract_signature(issue_lean, name)  # fallback: the reviewed block
        if sig is None:
            continue
        manifest[name] = ApprovedStatement(
            decl_name=name, signature=sig, issue=issue_num,
            chapter=chapter, verified_ts=ts,
        )
        frozen.append(name)
    if frozen:
        save_manifest(cfg.repo_dir, manifest)
    return frozen


# --- the hard guard (A2) ---------------------------------------------------

@dataclass
class RevertedDecl:
    """A frozen declaration whose signature a fill iteration changed, and which
    the guard restored from baseline before commit."""
    decl_name: str
    issue: int
    file: str
    frozen_sig: str
    attempted_sig: str


def _git(repo_dir: Path, *args: str) -> Optional[str]:
    """``git <args>`` in ``repo_dir``; stdout on success, else None."""
    try:
        cp = subprocess.run(
            ["git", *args], cwd=str(repo_dir),
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    return cp.stdout if cp.returncode == 0 else None


def _splice_decl_from_baseline(
    cur_text: str, base_text: str, decl_name: str
) -> Optional[str]:
    """Return ``cur_text`` with ``decl_name``'s block replaced by its baseline
    block (restoring the frozen signature AND the baseline ``sorry`` body), or
    None if the decl can't be located in either side."""
    cur_lines = cur_text.splitlines()
    base_lines = base_text.splitlines()
    cspan = find_decl_block(cur_lines, decl_name)
    bspan = find_decl_block(base_lines, decl_name)
    if cspan is None or bspan is None:
        return None
    cs, ce = cspan
    bs, be = bspan
    new_lines = cur_lines[:cs] + base_lines[bs:be] + cur_lines[ce:]
    out = "\n".join(new_lines)
    return out + "\n" if cur_text.endswith("\n") else out


def guard_touched_files(
    repo_dir: Path,
    baseline_ref: str,
    files: Iterable,
    *,
    manifest: Optional[dict] = None,
    run_git: Callable[..., Optional[str]] = _git,
) -> list[RevertedDecl]:
    """Enforce statement-immutability on a just-extracted (uncommitted) fill.

    For every frozen declaration whose CURRENT signature in a touched file
    differs from the manifest, surgically restore that one declaration's full
    text from ``baseline_ref`` (``git show <ref>:<file>``) — its signature
    returns to frozen and its body to the baseline ``sorry`` — leaving every
    other declaration's fill intact. If the decl can't be surgically located,
    the whole file is restored from baseline (coarse but still safe). Returns the
    list of reverted declarations (empty ⇒ nothing was violated). Call this AFTER
    extraction and BEFORE commit so a signature change can never land.
    """
    manifest = manifest if manifest is not None else load_manifest(repo_dir)
    if not manifest:
        return []
    reverted: list[RevertedDecl] = []
    for rel in files:
        rel = str(rel)
        if not rel.endswith(".lean"):
            continue
        fpath = Path(repo_dir) / rel
        try:
            cur = fpath.read_text()
        except OSError:
            continue
        violated: list[tuple[str, str]] = []  # (decl_name, attempted_sig)
        for name, st in manifest.items():
            cur_sig = extract_signature(cur, name)
            if cur_sig is not None and cur_sig != st.signature:
                violated.append((name, cur_sig))
        if not violated:
            continue
        base = run_git(repo_dir, "show", f"{baseline_ref}:{rel}")
        for name, attempted in violated:
            cur = fpath.read_text()
            restored = (
                _splice_decl_from_baseline(cur, base, name)
                if base is not None else None
            )
            if restored is not None:
                fpath.write_text(restored)
            elif base is not None:
                # surgical splice failed → restore the whole file from baseline
                fpath.write_text(base if base.endswith("\n") else base + "\n")
            # else: no baseline for this file (new file?) — cannot restore here;
            # still reported so the caller can refuse to commit.
            reverted.append(RevertedDecl(
                decl_name=name, issue=manifest[name].issue, file=rel,
                frozen_sig=manifest[name].signature, attempted_sig=attempted,
            ))
    return reverted

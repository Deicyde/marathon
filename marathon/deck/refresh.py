"""marathon.deck.refresh — the explicit "push refresh to GitHub" write.

The deck's reconciliation (``GET /api/subdeck``) is a PURE read: it shows the
cards matching the CODE without touching the issue. This module is the ONE
new write path — the explicit, token-gated "push refresh to GitHub" button.

:func:`refresh_issue_signatures` regenerates ONLY the issue body's
``### Lean signatures`` section so it matches the CURRENT code:

* it computes the reconciled decl set the deck shows — the real CITED decls
  that are still in the code, PLUS the auto-SURFACED in-namespace decls, MINUS
  the STALE citations (renamed/deleted decls);
* for each reconciled decl it emits one ```` ```lean ```` fenced block carrying
  the decl's current signature line (read from the source) followed by a
  GitHub PERMALINK pinned to the CURRENT HEAD commit SHA at the decl's
  ``file:Lstart-Lend`` (the same permalink shape the existing issue bodies use:
  ``[`<name>` at <file>:L<a>-L<b>](https://github.com/<owner/repo>/blob/<SHA>/<file>#L<a>-L<b>)``);
* EVERY OTHER section of the body is preserved VERBATIM — the regeneration
  splits the body on the ``### Lean signatures`` header and the next ``### ``
  header and replaces only that slice;
* the new body is written via ``gh issue edit``.

It is purely MECHANICAL — NO Claude. It is invoked ONLY from the token-gated
``POST /api/refresh-issue`` (see :mod:`marathon.deck.server`); it must NEVER run
on a GET, on navigation, or during card assembly.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover — type-only
    from marathon.review.config import ReviewConfig

#: The header that opens the section this refresh regenerates. Everything from
#: this line up to (but not including) the next ``### `` header is replaced;
#: every other section is preserved verbatim.
SIGNATURES_HEADER = "### Lean signatures"

#: The header of the section whose LLM-rendered marker the informal-approval
#: card flips at ship (:func:`mark_informal_verified`).
INFORMAL_HEADER = "### Informal Statement"

#: The LLM-rendered "verification pending" marker, in the two textual variants
#: the project emits — spec_auditor's ``⚠️ LLM-rendered, verification pending``
#: and the coreviewer/chapter_sessions's ``⚠️ LLM-rendered from common
#: knowledge; verification pending``. ``[^\n*]*?`` spans the variant middle
#: ("from common knowledge; " / ", ") WITHOUT crossing a markdown ``*`` italic
#: delimiter or a newline, and the trailing period (present in the coreviewer
#: body) is optional. The ``⚠️`` prefix is OPTIONAL so this flip matches EXACTLY
#: what the display side (:func:`marathon.deck.cards._is_llm_marker_line`, a
#: substring test) treats as pending — otherwise an emoji-stripped marker would
#: show the approval card yet silently fail to flip. Matched ONLY inside the
#: Informal Statement section; the optional ``⚠️`` is consumed when present so
#: the replacement leaves no orphaned emoji.
_LLM_MARKER_RE = re.compile(
    r"(?:⚠️\s*)?LLM-rendered[^\n*]*?verification pending\.?"
)


def head_sha(repo_dir: Path) -> Optional[str]:
    """The current HEAD commit SHA via ``git rev-parse HEAD`` in ``repo_dir``,
    or ``None`` when git is unavailable / the call fails. Pure read."""
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if cp.returncode != 0:
        return None
    sha = cp.stdout.strip()
    return sha or None


def _decl_site(text: str, name: str):
    """``(site, end_line)`` for ``name`` in ``text`` (1-based lines), or
    ``None`` when absent. ``end_line`` is the line BEFORE the next decl site
    (or EOF) — the decl's block end for the permalink line range.

    Matching reuses the deck's full-name rule (exact qualified/short match, or
    the cited name as a whole trailing dotted segment) so the same decl is
    located identically to the source-block extraction."""
    from marathon.formalization import scan_lean_source

    sites, _ = scan_lean_source(text)
    if not sites:
        return None
    target_i: Optional[int] = None
    for i, s in enumerate(sites):
        if s.qualified == name or s.name == name:
            target_i = i
            break
    if target_i is None:
        suffix = "." + name
        for i, s in enumerate(sites):
            if (s.qualified or "").endswith(suffix):
                target_i = i
                break
    if target_i is None:
        return None
    start = sites[target_i].line
    if target_i + 1 < len(sites):
        end = max(start, sites[target_i + 1].line - 1)
    else:
        end = len(text.splitlines())
    return sites[target_i], end


def _signature_line(text: str, line: int) -> str:
    """The decl's signature line (1-based ``line``) from ``text``, stripped of
    trailing whitespace. The keyword line is the signature the issue body
    quotes; we keep it verbatim (no re-flowing)."""
    lines = text.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].rstrip()
    return ""


def _permalink(repo: str, sha: str, file_rel: str, a: int, b: int) -> str:
    return f"https://github.com/{repo}/blob/{sha}/{file_rel}#L{a}-L{b}"


def _render_signatures_section(decls: list[dict]) -> str:
    """The full ``### Lean signatures`` section body for ``decls`` — the header
    line, a blank line, then one fenced block + permalink per decl (separated
    by blank lines). ``decls`` is ``[{name, signature, file, a, b, url}]`` in
    the order to render."""
    parts = [SIGNATURES_HEADER, ""]
    blocks = []
    for d in decls:
        blocks.append(
            f"```lean\n{d['signature']}\n```\n"
            f"[`{d['name']}` at {d['file']}:L{d['a']}-L{d['b']}]({d['url']})"
        )
    parts.append("\n\n".join(blocks))
    return "\n".join(parts)


def _splice_signatures(body: str, new_section: str) -> tuple[str, bool]:
    """Replace ONLY the ``### Lean signatures`` slice of ``body`` with
    ``new_section``, preserving every other section verbatim.

    The slice runs from the ``### Lean signatures`` header line up to (but not
    including) the next ``### `` header (or EOF). Returns ``(new_body,
    replaced)``; ``replaced`` is False when the body has no signatures header
    (the body is returned unchanged). Line-structure preserving: the text
    before the header and from the next header onward is byte-identical."""
    lines = body.splitlines(keepends=True)
    start: Optional[int] = None
    for i, line in enumerate(lines):
        if line.rstrip("\n").strip() == SIGNATURES_HEADER:
            start = i
            break
    if start is None:
        return body, False
    # Find the next "### " header after the signatures header; the slice ends
    # just before it (or at EOF). Everything in ``before`` / ``after`` is
    # preserved byte-for-byte.
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("### "):
            end = j
            break

    before = "".join(lines[:start])
    after = "".join(lines[end:])
    # The new section text carries no trailing newline; reattach the original
    # separators. When a section follows, keep a blank line between the
    # regenerated section and the next header (sections are blank-separated);
    # when this was the last section, restore the body's trailing newline.
    rebuilt = before + new_section
    if after:
        rebuilt += "\n\n" + after
    elif body.endswith("\n"):
        rebuilt += "\n"
    return rebuilt, True


def _gh_edit_body(repo: str, issue_num: int, body: str) -> None:
    """Write the new issue body via ``gh issue edit`` (the ONE side effect).
    Uses a ``--body-file`` so arbitrary body content (backticks, newlines)
    survives the shell intact, matching :mod:`marathon.review.subissues`."""
    from marathon.review.github import gh

    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(body)
        tmp_path = fh.name
    try:
        gh(
            "issue", "edit", str(issue_num),
            "--repo", repo,
            "--body-file", tmp_path,
        )
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


def _flip_informal_marker(body: str, date: str) -> tuple[str, bool]:
    """Within the ``### Informal Statement`` section ONLY, replace every
    LLM-rendered "verification pending" marker (:data:`_LLM_MARKER_RE`) with a
    human-approved verified marker, preserving every other section verbatim.

    Returns ``(new_body, flipped)``; ``flipped`` is False when the body has no
    Informal Statement section or that section carries no marker (the body is
    returned unchanged). The section is sliced exactly as
    :func:`_splice_signatures` slices the signatures section — from the header
    line up to the next ``### `` header (or EOF) — and only that slice is
    rewritten. Idempotent: a second call finds no marker and no-ops. Replaces
    ALL markers in the section (an issue may carry several informal
    statements)."""
    lines = body.splitlines(keepends=True)
    start: Optional[int] = None
    for i, line in enumerate(lines):
        if line.rstrip("\n").strip() == INFORMAL_HEADER:
            start = i
            break
    if start is None:
        return body, False
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("### "):
            end = j
            break
    section = "".join(lines[start:end])
    new_section, n = _LLM_MARKER_RE.subn(
        f"✅ Verified against the source (human-approved {date})", section
    )
    if n == 0:
        return body, False
    rebuilt = "".join(lines[:start]) + new_section + "".join(lines[end:])
    return rebuilt, True


def mark_informal_verified(cfg: "ReviewConfig", issue_num: int) -> dict:
    """Flip the issue body's ``### Informal Statement`` LLM-rendered marker to a
    human-approved verified marker — the durable realization of the deck's
    leading informal-approval card, written at SHIP (the explicit, token-gated
    ``POST /api/ship``).

    MECHANICAL — NO Claude: fetches the current body, flips the marker(s) in
    that one section (:func:`_flip_informal_marker`, preserving every other
    section verbatim), and writes the new body via ``gh issue edit``.

    Returns ``{ok, updated, message}``: ``updated`` is True only when a marker
    was actually flipped and the body written; it is False (writing NOTHING)
    when there is no Informal Statement section or no marker to flip. Like the
    surfaced-decl auto-add, the caller treats any failure as non-fatal (the
    verdict has already landed)."""
    from datetime import date as _date

    from marathon.review.github import issue_body as _issue_body

    repo = cfg.github_repo
    body = _issue_body(issue_num, repo)
    if body is None:
        return {
            "ok": False,
            "updated": False,
            "message": f"could not fetch issue #{issue_num} body",
        }
    new_body, flipped = _flip_informal_marker(body, _date.today().isoformat())
    if not flipped:
        return {
            "ok": True,
            "updated": False,
            "message": "no LLM-rendered informal marker to flip",
        }
    _gh_edit_body(repo, issue_num, new_body)
    return {
        "ok": True,
        "updated": True,
        "message": "flipped the informal statement marker to verified",
    }


def refresh_issue_signatures(
    cfg: "ReviewConfig", issue_num: int, *, only_surfaced=None
) -> dict:
    """Regenerate the issue's ``### Lean signatures`` section to match the
    CURRENT code, pinned to the CURRENT HEAD SHA — the explicit, token-gated
    "push refresh to GitHub" write (the ONLY new side effect; MECHANICAL, no
    Claude).

    Computes the reconciled decl set the deck shows (real cited decls still in
    the code + auto-surfaced in-namespace decls, minus stale citations), emits
    one ```` ```lean ```` block + a HEAD-pinned permalink per decl, splices that
    into the body (preserving every other section verbatim), and writes the new
    body via ``gh issue edit``.

    Returns ``{ok, updated, dropped, added, sha, message}``:

    * ``ok`` — the refresh ran (False on a fetch/SHA failure, with a message);
    * ``updated`` — the body actually changed (False when already in sync or no
      signatures section exists);
    * ``dropped`` — stale-cited decl names removed from the section;
    * ``added`` — surfaced decl names newly written into the section;
    * ``sha`` — the HEAD SHA the permalinks were pinned to;
    * ``message`` — a one-line human summary.

    PURE except for the single ``gh issue edit`` at the end: it reads the
    snapshot/ledger/source/body and computes everything before any write, and
    when nothing would change (or the body has no signatures section) it writes
    NOTHING."""
    from marathon.deck.cards import (
        _build_decl_view,
        _load_context,
        _decls_for_issue,
        _ledger,
        surfaced_decls_for_issue,
    )

    repo = cfg.github_repo
    repo_dir = Path(cfg.repo_dir)

    sha = head_sha(repo_dir)
    if not sha:
        return {
            "ok": False, "updated": False, "dropped": [], "added": [],
            "sha": None,
            "message": "could not resolve HEAD SHA (git rev-parse failed)",
        }

    # Fetch the current issue body (read-only).
    from marathon.review.github import issue_body as _issue_body

    body = _issue_body(issue_num, repo)
    if body is None:
        return {
            "ok": False, "updated": False, "dropped": [], "added": [],
            "sha": sha,
            "message": f"could not fetch issue #{issue_num} body",
        }

    # Build the reconciled decl views: cited decls still in the code + surfaced
    # in-namespace decls, minus stale citations. Reuse the same machinery the
    # subdeck reconciliation uses so the section matches what the deck shows.
    ctx = _load_context(cfg, [issue_num])
    ledger = _ledger(cfg)

    cited_raw = _decls_for_issue(ctx, issue_num)
    cited_views = []
    seen: set[str] = set()
    for raw in cited_raw:
        view = _build_decl_view(ctx, ledger, raw, repo_dir)
        if view.name in seen:
            continue
        seen.add(view.name)
        cited_views.append(view)

    kept_views = [v for v in cited_views if v.in_code]
    dropped = [v.name for v in cited_views if not v.in_code]

    cited_names = [v.name for v in kept_views]
    surfaced = surfaced_decls_for_issue(cfg, issue_num, cited_names)
    kept_set = {v.name for v in kept_views}
    surfaced = [v for v in surfaced if v.name not in kept_set]
    # ``only_surfaced`` restricts which surfaced decls are written — used by the
    # ship step to add ONLY the surfaced declarations the human KEPT (didn't
    # reject). None (the bulk "Refresh" button) adds every surfaced decl.
    if only_surfaced is not None:
        surfaced = [v for v in surfaced if v.name in only_surfaced]
    added = [v.name for v in surfaced]

    reconciled_views = list(kept_views) + list(surfaced)
    # Deterministic body order: by name (the section is mechanical, not the
    # tier-sorted swipe order).
    reconciled_views.sort(key=lambda v: v.name)

    # Resolve each reconciled decl to its file:Lstart-Lend from the CURRENT
    # source, and quote its current signature line. A decl whose source can't
    # be located is emitted with its name but no permalink (honest degrade).
    rendered: list[dict] = []
    for view in reconciled_views:
        file_rel, a, b, signature = _locate_in_source(repo_dir, view)
        if file_rel is None:
            rendered.append({
                "name": view.name,
                "signature": view.name,
                "file": "(source not found)",
                "a": 0, "b": 0,
                "url": _issue_link_fallback(repo, issue_num),
            })
            continue
        rendered.append({
            "name": view.name,
            "signature": signature,
            "file": file_rel,
            "a": a, "b": b,
            "url": _permalink(repo, sha, file_rel, a, b),
        })

    new_section = _render_signatures_section(rendered)
    new_body, replaced = _splice_signatures(body, new_section)

    if not replaced:
        return {
            "ok": True, "updated": False, "dropped": dropped, "added": added,
            "sha": sha,
            "message": (
                f"#{issue_num} has no '{SIGNATURES_HEADER}' section — "
                "nothing refreshed"
            ),
        }
    if new_body == body:
        return {
            "ok": True, "updated": False, "dropped": dropped, "added": added,
            "sha": sha,
            "message": (
                f"#{issue_num} signatures already match the code at {sha[:7]} "
                "— no edit needed"
            ),
        }

    _gh_edit_body(repo, issue_num, new_body)
    return {
        "ok": True, "updated": True, "dropped": dropped, "added": added,
        "sha": sha,
        "message": (
            f"#{issue_num} signatures refreshed to {sha[:7]}: "
            f"{len(rendered)} block(s), "
            f"{len(dropped)} stale dropped, {len(added)} surfaced added"
        ),
    }


def _issue_link_fallback(repo: str, issue_num: int) -> str:
    """A non-permalink URL used only when a decl's source can't be located, so
    the rendered block still links somewhere sensible (the issue itself)."""
    return f"https://github.com/{repo}/issues/{issue_num}"


def _locate_in_source(repo_dir: Path, view):
    """``(file_rel, start, end, signature)`` for ``view`` from the CURRENT
    source, or ``(None, None, None, None)`` when not locatable.

    Prefers the decl's own ``source_file`` (already resolved by the card
    machinery from the snapshot/repo scan); if that file is readable, scans it
    for the decl's line range. Pure read-only."""
    file_rel = view.source_file
    if not file_rel:
        return None, None, None, None
    path = repo_dir / file_rel
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None, None, None
    located = _decl_site(text, view.name)
    if located is None:
        return None, None, None, None
    site, end = located
    signature = _signature_line(text, site.line)
    return file_rel, site.line, end, signature

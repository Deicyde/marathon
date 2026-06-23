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


#: A rendered signatures block's trailing permalink line, e.g.
#: ``[`Foo.bar` at path/F.lean:L10-L20](https://github.com/.../F.lean#L10-L20)``.
#: Identifies which decl a block cites (group 1) for minimal-edit reconciliation.
_PERMALINK_LINE_RE = re.compile(r"^\[`([^`]+)` at .+?#L\d+-L\d+\)\s*$")

#: The ``### Lean declarations`` (chapter-intake) section bounds + the backtick'd
#: token matcher — REUSED, not forked, from :mod:`marathon.review.verified_decls`
#: (a single committed regex for that section; the map flags duplicated copies as
#: a hazard). ``_DECL_SECTION_RE`` group 1 is the section body (up to the next
#: ``### `` header / EOF); ``_BACKTICK_TOKEN_RE`` group 1 is one backtick'd token.
from marathon.review.verified_decls import (  # noqa: E402
    _ATTR as _DECL_ATTR,
    _BACKTICK_TOKEN_RE,
    _DECL_SECTION_RE,
    _MOD as _DECL_MOD,
    DECL_KEYWORDS as _DECL_KEYWORDS,
)

#: A keyword-shaped declaration token: an OPTIONAL leading ``@[…]`` attribute
#: cluster + ``noncomputable/private/protected`` modifiers (the intake emits e.g.
#: `` `@[simp] theorem Foo` `` / `` `noncomputable def Foo` ``), then a decl
#: keyword (`def`, `theorem`, …), then the name. Group ``kw`` keeps the whole
#: attr+modifier+keyword run (including trailing whitespace) verbatim so a swap
#: rewrites ONLY the ``name`` part. Built from the SAME ``_ATTR`` / ``_MOD`` /
#: keyword fragments :data:`marathon.review.verified_decls._DECL_RE` extracts with
#: (imported above — never re-listed here), so anything the extractor matches is
#: matched here too; otherwise an extracted-then-leaf-matched ``@[simp] theorem``
#: token would no-op in :func:`_swap_decl_token` and be dropped to ``kept_stale``.
_DECL_KW_PREFIX_RE = re.compile(
    r"(?P<kw>" + _DECL_ATTR + _DECL_MOD
    + r"(?:" + "|".join(_DECL_KEYWORDS) + r")\s+)"
    r"(?P<name>[A-Za-z_][\w'.]*)\Z"
)


def _proven_rename(
    repo_dir: Path, stale_name: str, candidate_names: "list[str]"
) -> Optional[str]:
    """The candidate that the stale decl ``stale_name`` was RENAMED to, proven by
    git history: the single commit that REMOVED ``stale_name``'s leaf also ADDED
    exactly one candidate's leaf. ``None`` when no such proof exists (then the
    stale citation is KEPT — never dropped on a guess).

    A symbol rename is not a git file-rename, so this is content-based: find the
    most recent commit whose diff deletes the stale leaf, and check its added
    lines for a candidate leaf. Pure read-only (``git log -S`` / ``git show``)."""
    stale_leaf = stale_name.rsplit(".", 1)[-1]
    cand_by_leaf = {c.rsplit(".", 1)[-1]: c for c in candidate_names}
    if not stale_leaf or not cand_by_leaf:
        return None
    try:
        log = subprocess.run(
            ["git", "log", "-1", "--format=%H", "-S", stale_leaf],
            cwd=str(repo_dir), capture_output=True, text=True, check=False,
        )
        if log.returncode != 0 or not log.stdout.strip():
            return None
        commit = log.stdout.strip().splitlines()[0]
        show = subprocess.run(
            ["git", "show", "--format=", "--unified=0", commit],
            cwd=str(repo_dir), capture_output=True, text=True, check=False,
        )
        if show.returncode != 0:
            return None
    except OSError:
        return None
    diff = show.stdout
    # The commit must REMOVE the stale leaf (a deleted line mentions it)…
    removed = any(
        ln.startswith("-") and not ln.startswith("---") and stale_leaf in ln
        for ln in diff.splitlines()
    )
    if not removed:
        return None
    # …and ADD exactly one candidate leaf (an inserted line mentions it).
    added = {
        leaf for leaf, full in cand_by_leaf.items()
        if any(
            ln.startswith("+") and not ln.startswith("+++") and leaf in ln
            for ln in diff.splitlines()
        )
    }
    if len(added) != 1:
        return None
    return cand_by_leaf[next(iter(added))]


def _leaf_match_rename(
    stale_name: str,
    live_names: "list[str]",
    cited_names: "set[str]",
) -> Optional[str]:
    """The live decl that ``stale_name`` was renamed to, proven by a UNIQUE
    leaf match across the candidate pool — the namespace-rename case
    :func:`_proven_rename` can't reach (issue #98).

    The caller passes the chapter's ORPHANED live decls (cited by NO issue) as
    ``live_names`` — a real rename's new home is orphaned, an unrelated live decl
    is owned by its own issue and so is NOT in the pool (the load-bearing safety
    scoping; see :func:`orphaned_chapter_decls`).

    The *leaf* is the last dotted component (``A.B.foo`` → ``foo``). A namespace
    rename keeps the leaf and moves the prefix (``MixedTensorField.foo`` →
    ``CovariantTensorField.foo``); ``git log -S foo`` then finds a refactor
    commit touching several decls, so :func:`_proven_rename` returns None. Here
    we instead look at the LIVE code: if EXACTLY ONE candidate shares the stale
    leaf and is not already cited by this issue, that's the rename target.

    Returns that unique name, or ``None`` when there are zero or more than one
    candidates (conservative — a guess could erase a genuinely-missing-decl
    signal, so an ambiguous leaf keeps the stale citation untouched). The
    ``cited_names`` exclusion stops a sibling already cited by the same issue
    (its own block stays) from being mistaken for the rename target."""
    stale_leaf = stale_name.rsplit(".", 1)[-1]
    if not stale_leaf:
        return None
    candidates = [
        n for n in live_names
        if n.rsplit(".", 1)[-1] == stale_leaf and n not in cited_names
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def live_chapter_decls(cfg: "ReviewConfig", chapter: int) -> "list[str]":
    """The fully-qualified names of every live decl in ``chapter``'s modules,
    read from the audit snapshot — the candidate pool :func:`_leaf_match_rename`
    searches for a renamed decl's new home.

    The chapter's module prefix is its target folder rendered as a dotted module
    path (``Demo/Chapter15`` → ``Demo.Chapter15``); a decl belongs to the
    chapter when its module is that prefix exactly OR a strict dotted descendant
    (``prefix + '.'``). The strict-descendant test (not a bare ``startswith``) is
    load-bearing: a bare prefix test would let ``Chapter15`` swallow
    ``Chapter150`` / ``Chapter12``'s decls. Returns ``[]`` when no snapshot
    exists (honest absence of evidence — no leaf match is then attempted)."""
    from marathon.audit.engine import load_snapshot

    snap = load_snapshot(cfg.repo_dir)
    if snap is None:
        return []
    rel = cfg.target_path(chapter).relative_to(cfg.repo_dir).as_posix()
    prefix = rel.replace("/", ".")
    return [
        d.name for d in snap.decls
        if d.module == prefix or d.module.startswith(prefix + ".")
    ]


def orphaned_chapter_decls(
    cfg: "ReviewConfig",
    chapter: int,
    live_names: "list[str]",
) -> "list[str]":
    """The chapter-live decls cited by NO issue — the ONLY admissible leaf-match
    rename targets. A real rename leaves its NEW decl orphaned (no issue cites it
    yet); an unrelated live decl is cited by its OWN issue, so it is excluded.

    Scoping the candidate pool to orphans is the load-bearing safety fix: without
    it ``_leaf_match_rename`` searched the WHOLE chapter and would silently rename
    a genuinely-DELETED decl carrying a generic leaf (``symm``, ``mk``, ``comm``,
    ``ext``, ``trans``, ``coe``, ``map`` …) onto the unrelated live decl that
    happens to be its only chapter-wide leaf match — erasing a real
    missing-decl signal on a guess. It also closes the qualified-vs-bare
    exclusion hole: the cited union is built from RESOLVED snapshot names (via
    :func:`marathon.deck.cards._resolve_to_snapshot`, the same rule the deck
    uses), so an already-cited decl is excluded whether the body wrote it bare
    (``symm``) or qualified (``…Tf.symm``).

    Computed as ``live_names − ⋃(issue cites, resolved to snapshot names)`` over
    EVERY issue in the chapter's registry. A SEPARATE deck context spanning the
    WHOLE chapter is loaded here (NOT the caller's single-issue ctx) so EVERY
    sibling's body is fetched — otherwise an unloaded sibling's citations would be
    invisible and its owned decl wrongly counted orphaned. An unregistered chapter
    → no issues → every live decl is orphaned (the honest fail-open: with no
    citation evidence at all we cannot prove a decl is owned). The returned order
    follows ``live_names`` (deterministic)."""
    from marathon.deck.cards import (
        _chapter_issue_nums, _decls_for_issue, _load_context,
        _resolve_to_snapshot,
    )

    chapter_nums = _chapter_issue_nums(cfg, chapter)
    if not chapter_nums:
        return list(live_names)
    ctx = _load_context(cfg, chapter_nums)
    cited_union: set[str] = set()
    for num in chapter_nums:
        for raw in _decls_for_issue(ctx, num):
            cited_union.add(_resolve_to_snapshot(ctx, raw) or raw)
    return [n for n in live_names if n not in cited_union]


def auto_reconcile_issue(
    cfg: "ReviewConfig", issue_num: int, *, dry_run: bool = False
) -> dict:
    """SAFE automatic reconcile of one issue's Lean-declaration citations against
    the LIVE code — the hands-off replacement for the manual "refresh from code"
    button. MECHANICAL (no Claude); the ONLY write is one ``gh issue edit``.

    Handles BOTH issue-body shapes:

    * ``### Lean signatures`` — the original review-issue shape (fenced ```` ```lean ````
      blocks + permalinks). Two MINIMAL edits, every other block byte-identical
      (no permalink re-pin, so a human-verified body's anchors don't drift):

      - ADD — uncited in-code decls that match the issue's textbook result-key
        and that NO issue cites (``missing_suggestions_for_issue`` with empty
        ``cited_by``). New blocks are APPENDED. This is what fixes #94: the
        in-code ``smul_of_contMDiff`` (Prop 12.22, uncited) is added.
      - RENAME — a stale signature citation (cited but no longer in the code) is
        replaced by its rename target. The target is found FIRST by
        :func:`_proven_rename` (git history); when that returns None (the
        namespace-rename case — same leaf, multi-decl refactor commit — that
        ``git log -S`` can't disambiguate, issue #98) it FALLS BACK to a UNIQUE
        leaf match across the chapter's ORPHANED live decls — decls cited by NO
        issue (:func:`_leaf_match_rename` over :func:`orphaned_chapter_decls`).
        Scoping to orphans is the safety fix: an unrelated live decl is cited by
        its own issue → excluded, so a deleted decl with a generic leaf can't be
        falsely renamed onto it.
        A stale citation with NEITHER a proven nor a leaf-matched rename is KEPT
        VERBATIM (never dropped on a guess — that would erase a
        genuinely-missing-decl signal).

    * ``### Lean declarations`` (and NO ``### Lean signatures``) — the
      chapter-intake bootstrapped shape (a bullet list of backtick'd names +
      source links). Only the RENAME edit applies: a stale cited name is replaced
      by its leaf-matched live name, swapping the backtick'd name token in place
      within that section; the source link is left as-is (best-effort). ADD /
      permalink-repin are signatures-only.

    VERIFIED-ISSUE SAFETY: if the issue is human-verified/frozen (status
    ``verified`` in ``state.json``, or any cited decl present in the
    approved-statements manifest), NOTHING is written and NO rename is
    auto-applied — the proposed renames are returned in ``flagged_renames`` for a
    human to apply. A human-verified statement must never be silently rewritten
    (mirrors :func:`marathon.review.approved_statements.guard_touched_files`).

    Returns ``{ok, updated, added, renamed, kept_stale, flagged_renames, sha,
    message}``. Writes NOTHING when there is no recognized section, nothing to
    add/rename, the issue is verified/frozen, or ``dry_run`` (then ``new_body``
    is included for inspection)."""
    from marathon.deck.cards import (
        _build_decl_view, _load_context, _decls_for_issue, _ledger,
        missing_suggestions_for_issue,
    )
    from marathon.review.github import issue_body as _issue_body

    repo = cfg.github_repo
    repo_dir = Path(cfg.repo_dir)
    empty = {"ok": True, "updated": False, "added": [], "renamed": [],
             "kept_stale": [], "flagged_renames": [], "sha": None}

    sha = head_sha(repo_dir)
    if not sha:
        return {**empty, "message": "could not resolve HEAD SHA"}
    body = _issue_body(issue_num, repo)
    if body is None:
        return {**empty, "ok": False,
                "message": f"could not fetch issue #{issue_num} body"}

    has_signatures = SIGNATURES_HEADER in body
    has_declarations = _DECL_SECTION_RE.search(body) is not None
    if not has_signatures and not has_declarations:
        return {**empty, "sha": sha,
                "message": f"#{issue_num}: no signatures section"}

    ctx = _load_context(cfg, [issue_num])
    ledger = _ledger(cfg)

    # The names this issue currently cites (raw, as written in the body) — both
    # the leaf-match exclusion set and the verified-frozen check key off these.
    cited_raw = _decls_for_issue(ctx, issue_num)
    cited_set = set(cited_raw)

    # The chapter that owns this issue (reverse registry lookup). None when the
    # issue is unregistered — then no leaf-match fallback is attempted (we have
    # no live-decl pool to match against). The leaf-match pool is scoped to
    # ORPHANED decls (cited by NO issue): a real rename's new home is orphaned,
    # an unrelated live decl is cited by its own issue → excluded. This is the
    # safety fix against false renames onto generic leaves (`symm`, `mk`, …).
    chapter = cfg.chapter_of_issue(issue_num)
    if chapter is not None:
        live_names = live_chapter_decls(cfg, chapter)
        orphaned = orphaned_chapter_decls(cfg, chapter, live_names)
    else:
        orphaned = []

    # Stale citations: decls cited in the body that are no longer in the code.
    # These are the ONLY drop/replace candidates.
    stale = []
    for raw in cited_raw:
        v = _build_decl_view(ctx, ledger, raw, repo_dir)
        if not v.in_code and v.name not in stale:
            stale.append(v.name)

    # The ``### Lean declarations`` (intake) format: NO signatures section. Only
    # the leaf-match rename applies, swapping the stale name token in place.
    if not has_signatures:
        return _reconcile_declarations_format(
            cfg, issue_num, body, sha, stale, orphaned, cited_set,
            empty=empty, dry_run=dry_run,
        )

    # ADD candidates: uncited (cited_by empty) in-code decls matching the
    # result-key — the orphaned-but-present decls the issue should cite.
    suggestions = missing_suggestions_for_issue(cfg, issue_num)
    add_names = [s["decl"] for s in suggestions if not s["cited_by"]]

    # Resolve renames first: a stale S maps to its rename target — proven by git
    # (:func:`_proven_rename` against the ADD candidates), else a UNIQUE leaf
    # match across the chapter's ORPHANED live decls (the namespace-rename case
    # #98). ``renamed_targets`` is excluded from the leaf-match pool so two
    # distinct stale citations sharing a leaf can't BOTH claim the same orphan
    # (the second collapses to kept_stale — many-to-one dedup).
    renamed: list[dict] = []
    renamed_targets: set[str] = set()
    kept_stale: list[str] = []
    for s in stale:
        r = _proven_rename(repo_dir, s, add_names)
        if r is None:
            pool = [n for n in orphaned if n not in renamed_targets]
            r = _leaf_match_rename(s, pool, cited_set)
        if r is not None and r not in renamed_targets:
            renamed.append({"old": s, "new": r})
            renamed_targets.add(r)
        else:
            kept_stale.append(s)
    # ADD set = candidates not consumed by a rename.
    add_only = [n for n in add_names if n not in renamed_targets]

    if not renamed and not add_only:
        return {**empty, "sha": sha, "kept_stale": kept_stale,
                "message": f"#{issue_num}: already reconciled ({len(kept_stale)} "
                           "stale citation(s) kept — no proven rename)"}

    # VERIFIED-ISSUE SAFETY: a human-verified/frozen statement is NEVER silently
    # rewritten — not by a rename, not by an ADD. Flag any proposed renames for a
    # human and write NOTHING (mirrors guard_touched_files' philosophy).
    if _issue_is_frozen(cfg, issue_num, cited_set):
        return {**empty, "sha": sha,
                "flagged_renames": [dict(r) for r in renamed],
                "message": (
                    f"#{issue_num} is verified/frozen — "
                    f"{len(renamed)} rename(s) flagged for human review, "
                    f"{len(add_only)} add(s) suppressed, NOT auto-applied")}

    # Render the blocks to write (ADD + rename targets); skip any whose source
    # can't be located (never emit a broken block).
    def _render(name: str) -> Optional[str]:
        view = _build_decl_view(ctx, ledger, name, repo_dir)
        file_rel, a, b, signature = _locate_in_source(repo_dir, view)
        if file_rel is None:
            return None
        return (f"```lean\n{signature}\n```\n"
                f"[`{name}` at {file_rel}:L{a}-L{b}]"
                f"({_permalink(repo, sha, file_rel, a, b)})")

    rename_blocks = {r["old"]: _render(r["new"]) for r in renamed}
    add_rendered = [(n, _render(n)) for n in sorted(add_only)]
    add_blocks = [b for (_n, b) in add_rendered if b]
    added_names = [n for (n, b) in add_rendered if b]
    renamed_ok = [r for r in renamed if rename_blocks.get(r["old"])]

    new_body = _reconcile_body(body, rename_blocks, add_blocks)
    if new_body == body:
        return {**empty, "sha": sha,
                "message": f"#{issue_num}: nothing to write (unlocatable sources)"}

    result = {
        "ok": True, "updated": True,
        "added": added_names,
        "renamed": renamed_ok,
        "kept_stale": kept_stale, "flagged_renames": [], "sha": sha,
        "message": (
            f"#{issue_num} reconciled to {sha[:7]}: "
            f"{len(added_names)} added, {len(renamed_ok)} renamed, "
            f"{len(kept_stale)} stale kept"
        ),
    }
    if dry_run:
        result["new_body"] = new_body
        result["updated"] = False
        return result
    _gh_edit_body(repo, issue_num, new_body)
    return result


def _reconcile_body(
    body: str, rename_blocks: "dict[str, Optional[str]]", add_blocks: "list[str]"
) -> str:
    """Apply minimal signature-section edits: replace each block whose decl is a
    proven-rename key with its new rendered block, and APPEND the add_blocks at
    the end of the section. Every non-renamed block stays byte-identical. When a
    rename's new block is None (unlocatable), the stale block is left as-is."""
    lines = body.splitlines(keepends=True)
    start = next(
        (i for i, l in enumerate(lines)
         if l.rstrip("\n").strip() == SIGNATURES_HEADER), None
    )
    if start is None:
        return body
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("### "):
            end = j
            break
    section = "".join(lines[start:end])

    # Replace a renamed decl's whole block (the run ending at its permalink line).
    if any(v for v in rename_blocks.values()):
        out_lines: list[str] = []
        buf: list[str] = []
        for line in section.splitlines(keepends=True):
            buf.append(line)
            m = _PERMALINK_LINE_RE.match(line.rstrip("\n"))
            if m:
                name = m.group(1)
                new_block = rename_blocks.get(name)
                if new_block is not None:
                    # Replace the buffered block; keep any leading non-block lines
                    # (e.g. the ```lean fence start sits in buf) by swapping the
                    # whole run for the new block + a trailing newline.
                    out_lines.append(new_block + "\n")
                else:
                    out_lines.extend(buf)
                buf = []
        out_lines.extend(buf)  # trailing non-block remainder (header, blanks)
        section = "".join(out_lines)

    if add_blocks:
        section = section.rstrip("\n") + "\n\n" + "\n\n".join(add_blocks)

    before = "".join(lines[:start])
    after = "".join(lines[end:])
    rebuilt = before + section
    if after:
        rebuilt += "\n\n" + after
    elif body.endswith("\n"):
        rebuilt += "\n"
    return rebuilt


def _issue_is_frozen(
    cfg: "ReviewConfig", issue_num: int, cited_names: "set[str]"
) -> bool:
    """Whether ``issue_num`` is human-verified/frozen and so must NEVER be
    silently rewritten — the gate the auto-rename applies before touching a body.

    Frozen iff EITHER the issue's ``state.json`` status is ``verified`` OR any
    name it cites appears in the approved-statements manifest (the durable freeze
    a verify captures). Either is enough; both are best-effort reads that degrade
    to "not frozen" only when the whole lookup raises (a missing state file /
    manifest is simply an empty freeze, handled by the loaders, not an error).

    CORRUPT-state.json fail-safe (load-bearing): :func:`load_state` SWALLOWS a
    ``json.JSONDecodeError`` and returns an EMPTY state — so a corrupt state.json
    would otherwise read as "not verified" and let a verified-by-state-alone issue
    (the normal ``### Lean declarations`` intake case, empty manifest) be SILENTLY
    rewritten. So BEFORE trusting ``load_state`` we probe the raw file: if the
    state file EXISTS but does not parse, freeze (return True). The bare
    ``except`` below is a second belt-and-braces fail-safe."""
    from marathon.review.approved_statements import load_manifest
    from marathon.review.state import load_state, state_path

    # Corrupt-but-present state.json → freeze. load_state can't see this (it
    # catches JSONDecodeError and returns an empty state), so probe the raw file.
    try:
        path = state_path(cfg)
        if path.is_file():
            import json as _json
            _json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — present-but-unparseable state ⇒ freeze
        return True

    try:
        st = load_state(cfg).issues.get(issue_num)
        if st is not None and st.status == "verified":
            return True
        manifest = load_manifest(Path(cfg.repo_dir))
    except Exception:  # noqa: BLE001 — a failed freeze-read must fail SAFE …
        return True     # … and SAFE here is "treat as frozen" (never rewrite)
    return any(name in manifest for name in cited_names)


def _swap_decl_token(section: str, old_name: str, new_name: str) -> tuple[str, bool]:
    """Within a ``### Lean declarations`` section body, replace the backtick'd
    ``old_name`` token with ``new_name`` — handling both the bare-name shape
    (`` `Foo.bar` ``) and the keyword shape (`` `def Foo.bar` ``, swapping ONLY
    the name part and preserving the keyword). The surrounding source link is
    left untouched (best-effort — recomputing the permalink for a moved decl is
    out of scope for the declarations format). Returns ``(new_section,
    replaced)``."""
    replaced = False

    def _sub(m: "re.Match[str]") -> str:
        nonlocal replaced
        content = m.group(1).strip()
        # Keyword shape: `def Foo.bar` — swap only the trailing name.
        dm = _DECL_KW_PREFIX_RE.match(content)
        if dm and dm.group("name") == old_name:
            replaced = True
            return f"`{dm.group('kw')}{new_name}`"
        # Bare-name shape: `Foo.bar`.
        if content == old_name:
            replaced = True
            return f"`{new_name}`"
        return m.group(0)

    return _BACKTICK_TOKEN_RE.sub(_sub, section), replaced


def _reconcile_declarations_format(
    cfg: "ReviewConfig",
    issue_num: int,
    body: str,
    sha: str,
    stale: "list[str]",
    orphaned: "list[str]",
    cited_set: "set[str]",
    *,
    empty: dict,
    dry_run: bool,
) -> dict:
    """Reconcile a ``### Lean declarations`` (chapter-intake) body: the ONLY edit
    is a leaf-match RENAME — replace each stale cited name token with its unique
    ORPHANED live leaf-match, in place inside the declarations section(s). No ADD,
    no permalink re-pin (those are signatures-only).

    Verified/frozen issues are flagged, never rewritten (mirrors the signatures
    path). Writes NOTHING when there is no stale rename to apply or on
    ``dry_run`` (then ``new_body`` is returned for inspection)."""
    # Resolve renames against the ORPHANED pool, with many-to-one dedup: a second
    # stale sharing a leaf with an already-claimed target collapses to kept_stale.
    renamed: list[dict] = []
    claimed: set[str] = set()
    kept_stale: list[str] = []
    for s in stale:
        pool = [n for n in orphaned if n not in claimed]
        r = _leaf_match_rename(s, pool, cited_set)
        if r is not None:
            renamed.append({"old": s, "new": r})
            claimed.add(r)
        else:
            kept_stale.append(s)

    if not renamed:
        return {**empty, "sha": sha, "kept_stale": kept_stale,
                "message": f"#{issue_num}: already reconciled ({len(kept_stale)} "
                           "stale declaration(s) kept — no leaf match)"}

    if _issue_is_frozen(cfg, issue_num, cited_set):
        return {**empty, "sha": sha, "kept_stale": kept_stale,
                "flagged_renames": [dict(r) for r in renamed],
                "message": (
                    f"#{issue_num} is verified/frozen — {len(renamed)} rename(s) "
                    "flagged for human review, NOT auto-applied")}

    # Replace ONLY within ``### Lean declarations`` sections so prose backticks
    # elsewhere are never touched. The extractor reads ALL such sections
    # (``finditer``), so splice EVERY one — a stale token can live in any of them.
    # Splice matches right-to-left so each (start, end) stays valid as we rewrite.
    matches = list(_DECL_SECTION_RE.finditer(body))
    if not matches:  # pragma: no cover — caller only enters with a section present
        return {**empty, "sha": sha, "kept_stale": kept_stale,
                "message": f"#{issue_num}: declarations section vanished"}
    new_body = body
    applied_set: set[str] = set()
    for m in reversed(matches):
        section = m.group(1)
        new_section = section
        for r in renamed:
            swapped, ok = _swap_decl_token(new_section, r["old"], r["new"])
            if ok:
                new_section = swapped
                applied_set.add(r["old"])
        if new_section != section:
            new_body = new_body[:m.start(1)] + new_section + new_body[m.end(1):]
    applied = [r for r in renamed if r["old"] in applied_set]
    kept_stale.extend(r["old"] for r in renamed if r["old"] not in applied_set)
    if not applied or new_body == body:
        return {**empty, "sha": sha, "kept_stale": kept_stale,
                "message": f"#{issue_num}: nothing to write (no token swapped)"}

    result = {
        "ok": True, "updated": True, "added": [],
        "renamed": applied, "kept_stale": kept_stale,
        "flagged_renames": [], "sha": sha,
        "message": (
            f"#{issue_num} declarations reconciled to {sha[:7]}: "
            f"{len(applied)} renamed, {len(kept_stale)} stale kept"),
    }
    if dry_run:
        result["new_body"] = new_body
        result["updated"] = False
        return result
    _gh_edit_body(cfg.github_repo, issue_num, new_body)
    return result


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

"""Current-source definition lookup for the review deck.

The audit kernel is deliberately small, but reviewers also need to inspect a
project-local identifier that appears in a source/proof snippet and is not in
that kernel.  This module builds a read-only index from the *current* Lean
files, then enriches matches with audit evidence and owning review cards.

Source is authoritative for identity: a declaration present only in an old
audit snapshot is not returned as current code.  Conversely, a declaration
added or renamed since the last audit is returned immediately with honest
``tier='-'`` / ``in_audit=False`` metadata until the next audit catches up.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover
    from marathon.review.config import ReviewConfig


_INDEX_LOCK = threading.RLock()
_INDEX_CACHE: dict[str, tuple[tuple, dict[str, list[dict[str, Any]]]]] = {}
_MAX_LOOKUP_MATCHES = 50
_MAX_DEFINITION_TERMS = 10_000


def _contained_source(path: Path, repo_dir: Path) -> bool:
    """Reject tracked/untracked symlinks that escape the reviewed repository."""
    try:
        path.resolve().relative_to(repo_dir.resolve())
        return True
    except (OSError, ValueError):
        return False


def _source_signature(files: list[Path], repo_dir: Path) -> tuple:
    """Cheap invalidation key for the source index, including dirty files."""
    rows = []
    for path in files:
        if not _contained_source(path, repo_dir):
            continue
        try:
            stat = path.stat()
            rel = str(path.relative_to(repo_dir))
        except (OSError, ValueError):
            continue
        rows.append((rel, stat.st_size, stat.st_mtime_ns))
    return tuple(rows)


def _head_revision(repo_dir: Path) -> Optional[str]:
    """Best-effort HEAD identity for long-lived browser cache invalidation."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(repo_dir), capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def definition_source_token(repo_dir: Path) -> str:
    """Fingerprint the current Lean-file inventory plus HEAD.

    The term index is a browser-side optimization, so a long-lived deck needs a
    cheap way to notice Aristotle adding/removing/renaming declarations.  File
    mtimes/sizes also cover dirty worktree edits between commits; HEAD covers a
    commit move whose checked-out paths happen to retain identical metadata.
    """
    from marathon.deck.cards import _tracked_lean_files

    repo_dir = Path(repo_dir)
    payload = {
        "head": _head_revision(repo_dir),
        "files": _source_signature(_tracked_lean_files(repo_dir), repo_dir),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_source_index(
    repo_dir: Path, files: list[Path]
) -> dict[str, list[dict[str, Any]]]:
    from marathon.deck.cards import _REPO_SCAN_MAX_FILE_BYTES
    from marathon.formalization import scan_lean_source

    out: dict[str, list[dict[str, Any]]] = {}
    for path in files:
        if not _contained_source(path, repo_dir):
            continue
        try:
            if path.stat().st_size > _REPO_SCAN_MAX_FILE_BYTES:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sites, _ = scan_lean_source(source)
        try:
            rel = str(path.relative_to(repo_dir))
        except ValueError:
            rel = str(path)
        for index, site in enumerate(sites):
            name = site.qualified or site.name
            if not name:
                continue
            end_line = (
                sites[index + 1].line - 1
                if index + 1 < len(sites)
                else max(site.line, len(source.splitlines()))
            )
            out.setdefault(name, []).append({
                "name": name,
                "kind": site.kind,
                "source_file": rel,
                "source_line": site.line,
                "source_end_line": end_line,
            })
    for entries in out.values():
        entries.sort(key=lambda item: (
            item["source_file"], item["source_line"], item["name"]
        ))
    return out


def _source_block(repo_dir: Path, entry: dict[str, Any]) -> Optional[str]:
    """Read one matched declaration lazily; never retain every body in cache."""
    from marathon.deck.cards import _locate_decl_site, _slice_decl_block

    path = repo_dir / entry["source_file"]
    if not _contained_source(path, repo_dir):
        return None
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    sites, index = _locate_decl_site(source, entry["name"])
    if index is None:
        return None
    return _slice_decl_block(source, sites, index)


def definition_source_fingerprint(
    repo_dir: Path, name: str
) -> Optional[str]:
    """Hash the exact current-source evidence for one qualified declaration.

    ``None`` is fail-closed: a heuristic dismissal must not become durable when
    its proposed target cannot be located in current source.  Line numbers are
    deliberately excluded, so inserting an unrelated declaration above the
    target does not resurrect a reviewed false mapping; moving files or changing
    the target's declaration block does.
    """
    repo_dir = Path(repo_dir)
    entries = list(_current_source_index(repo_dir).get(name, ()))
    if not entries:
        return None
    evidence = []
    for entry in entries:
        block = _source_block(repo_dir, entry)
        if block is None:
            return None
        evidence.append({
            "name": name,
            "kind": entry.get("kind"),
            "source_file": entry.get("source_file"),
            "source_block": block,
        })
    evidence.sort(key=lambda item: (
        item.get("source_file") or "",
        item.get("kind") or "",
        item.get("source_block") or "",
    ))
    encoded = json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _current_source_index(repo_dir: Path) -> dict[str, list[dict[str, Any]]]:
    from marathon.deck.cards import _tracked_lean_files

    repo_dir = Path(repo_dir)
    files = _tracked_lean_files(repo_dir)
    signature = _source_signature(files, repo_dir)
    key = str(repo_dir.resolve())
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        index = _build_source_index(repo_dir, files)
        _INDEX_CACHE[key] = (signature, index)
        return index


def clear_definition_index_cache(repo_dir: Optional[Path] = None) -> None:
    """Test/maintenance hook; normal source mtime changes invalidate the cache."""
    with _INDEX_LOCK:
        if repo_dir is None:
            _INDEX_CACHE.clear()
        else:
            _INDEX_CACHE.pop(str(Path(repo_dir).resolve()), None)


def _name_matches(candidate: str, query: str) -> bool:
    return candidate == query or candidate.endswith("." + query)


def _owner_issues(decl_to_issues: dict, name: str) -> list[int]:
    # Source identity is authoritative.  A suffix match here can attach a new
    # root ``Widget`` to a stale ``Demo.Old.Widget`` citation after a rename —
    # exactly the identity confusion this lookup exists to prevent.  The shared
    # issue parser already resolves genuinely-unqualified citations when they
    # are unique in the *current* audit; otherwise ownership is honestly absent.
    return sorted(decl_to_issues.get(name, ()))


def _evidence(decl) -> dict:
    from marathon.gate import AXIOM_WHITELIST, SORRY_AXIOM

    if decl is None:
        return {
            "axioms_beyond_whitelist": [],
            "sorry": None,
            "deception_tags": [],
        }
    beyond = []
    if not decl.is_unknown:
        beyond = sorted(
            set(decl.axioms) - AXIOM_WHITELIST - {SORRY_AXIOM}
        )
    return {
        "axioms_beyond_whitelist": beyond,
        "sorry": decl.has_sorry,
        "deception_tags": list(decl.tags),
    }


def lookup_definition(
    cfg: "ReviewConfig", query: str, *, issue_num: Optional[int] = None
) -> dict:
    """Resolve a Lean identifier against current project source.

    Exact qualified matches win.  Otherwise every whole dotted-suffix match is
    returned, allowing the client to present explicit choices for an ambiguous
    short name.  The function is a pure read and returns a stable JSON-ready
    shape with ``status`` in ``exact | unique | ambiguous | unresolved``.
    """
    query = str(query or "").strip().strip("`")
    if query.startswith("_root_."):
        query = query[len("_root_."):]
    if not query or len(query) > 256 or any(ch.isspace() for ch in query):
        return {
            "query": query,
            "status": "invalid",
            "matches": [],
            "message": "enter one Lean identifier (maximum 256 characters)",
        }

    repo_dir = Path(cfg.repo_dir)
    source_index = _current_source_index(repo_dir)

    exact = list(source_index.get(query, ()))
    if exact:
        source_matches = exact
        status = "exact" if len(exact) == 1 else "ambiguous"
    else:
        source_matches = [
            entry
            for name, entries in source_index.items()
            if _name_matches(name, query)
            for entry in entries
        ]
        source_matches.sort(key=lambda item: (
            item["name"], item["source_file"], item["source_line"]
        ))
        status = (
            "unresolved" if not source_matches
            else "unique" if len(source_matches) == 1
            else "ambiguous"
        )

    total_matches = len(source_matches)
    source_matches = source_matches[:_MAX_LOOKUP_MATCHES]
    if not source_matches:
        return {
            "query": query,
            "status": "unresolved",
            "matches": [],
            "truncated": False,
            "message": (
                f"`{query}` is not a current project declaration; it may be "
                "Mathlib/core vocabulary, a local variable, or unresolved code"
            ),
        }

    from marathon.deck.cards import (
        _chapter_issue_nums,
        _decl_to_issues,
        _load_context,
    )

    issue_nums = _chapter_issue_nums(cfg, None)
    if issue_num is not None and issue_num not in issue_nums:
        issue_nums = [issue_num, *issue_nums]
    ctx = _load_context(cfg, issue_nums)
    decl_to_issues = _decl_to_issues(ctx, issue_nums)
    by_name = ctx.snapshot.by_name() if ctx.snapshot is not None else {}
    matches: list[dict] = []
    for source in source_matches:
        # A declaration site yields a current, qualified identity.  Never feed
        # it through citation-oriented suffix resolution: after a rename that
        # could enrich new code with a different stale snapshot declaration.
        resolved = source["name"] if source["name"] in by_name else None
        decl = by_name.get(resolved) if resolved is not None else None
        tier_result = ctx.tiers_by_decl.get(resolved) if resolved else None
        owner_ids = _owner_issues(decl_to_issues, source["name"])
        owners = []
        for owner in owner_ids:
            meta = ctx.issue_meta.get(owner) or {}
            owners.append({
                "issue": owner,
                "title": meta.get("title") or f"#{owner}",
                "chapter": cfg.chapter_of_issue(owner),
            })
        matches.append({
            **dict(source),
            "source_block": _source_block(repo_dir, source),
            "kind": decl.kind if decl is not None else source["kind"],
            "statement_pp": decl.type_pp if decl is not None else None,
            "value_pp": decl.value_pp if decl is not None else None,
            "tier": tier_result.tier if tier_result is not None else "-",
            "qualifiers": (
                list(tier_result.qualifiers) if tier_result is not None else []
            ),
            "evidence": _evidence(decl),
            "in_audit": decl is not None,
            "audit_status": decl.status if decl is not None else "missing",
            "owners": owners,
        })

    if status == "ambiguous":
        message = f"`{query}` matches {total_matches} current declarations"
    else:
        message = f"resolved `{query}` to `{matches[0]['name']}`"
    return {
        "query": query,
        "status": status,
        "matches": matches,
        "truncated": total_matches > len(matches),
        "message": message,
    }


def definition_terms(cfg: "ReviewConfig") -> dict:
    """Compact current-source name index used by the syntax highlighter.

    Qualified names are always selectable.  A short tail maps to every current
    qualified declaration carrying it, so the browser can make both unique and
    ambiguous terms keyboard-accessible without guessing which namespace was
    intended.
    """
    repo_dir = Path(cfg.repo_dir)
    # Capture the invalidation token first. If source moves during the scan, the
    # next status frame sees a newer token and refreshes again; capturing it after
    # an older cached index could incorrectly label stale names as current.
    source_token = definition_source_token(repo_dir)
    source_index = _current_source_index(repo_dir)
    all_names = sorted(source_index)
    names = all_names[:_MAX_DEFINITION_TERMS]
    short: dict[str, list[str]] = {}
    for name in names:
        short.setdefault(name.rsplit(".", 1)[-1], []).append(name)
    return {
        "names": names,
        "short": {name: values for name, values in sorted(short.items())},
        "count": len(all_names),
        "truncated": len(all_names) > len(names),
        "source_token": source_token,
    }

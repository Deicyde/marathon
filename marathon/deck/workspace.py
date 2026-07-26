"""Project-wide read models for the deck's file reader and dependency graph.

The swipe deck intentionally focuses on one declaration in one issue.  This
module supplies the complementary wide-angle views without creating a second
source of truth:

* :func:`build_review_workspace` groups every review target in a chapter by
  source file and source line, overlaying the same local per-declaration
  verdict records as the subdeck;
* :func:`build_project_graph` projects those targets into a navigable graph of
  issue and declaration nodes.  Dependency edges point predecessor-first.

Both functions are pure reads.  Issue bodies are fetched in one bulk request
through the cards layer's shared context, rather than rebuilding one subdeck
per issue (which would turn a project view into O(issue-count) GitHub calls).
The small private helpers imported from ``cards``/``subdeck`` are the same
ones used by the canonical one-issue builders; keeping the adaptation here
isolates the dashboard payload while preserving their resolution and carried
review semantics.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:  # pragma: no cover - type-only
    from marathon.review.config import ReviewConfig


def _issue_numbers(
    cfg: "ReviewConfig", chapter: Optional[int] = None
) -> list[int]:
    """Registered issue numbers in chapter/registry order, de-duplicated."""
    numbers: list[int] = []
    seen: set[int] = set()
    chapters = [chapter] if chapter is not None else sorted(cfg.chapters)
    for number in chapters:
        registry = cfg.chapters.get(number)
        for issue, _label in getattr(registry, "entries", ()) or ():
            if issue not in seen:
                seen.add(issue)
                numbers.append(issue)
    return numbers


def _name_match(left: str, right: str) -> bool:
    return (
        left == right
        or left.endswith("." + right)
        or right.endswith("." + left)
    )


def _source_line_index(repo_dir: Path, paths: Iterable[str]) -> dict[str, dict[str, int]]:
    """``{source_file: {qualified_decl: line}}`` for the requested files.

    ``DeclView`` deliberately carries only the file and source block.  The
    reader needs a stable source-order key, so scan each distinct file once
    with Marathon's shared Lean declaration scanner.  Unreadable/moved files
    simply contribute no line information and sort after located declarations.
    """
    from marathon.deck.cards import _REPO_SCAN_MAX_FILE_BYTES
    from marathon.formalization import scan_lean_source

    out: dict[str, dict[str, int]] = {}
    repo = Path(repo_dir).resolve()
    for rel in sorted({p for p in paths if p}):
        try:
            candidate = (repo / rel).resolve(strict=True)
            candidate.relative_to(repo)
            if not candidate.is_file() or candidate.suffix.lower() != ".lean":
                raise ValueError("not a contained Lean source file")
            if candidate.stat().st_size > _REPO_SCAN_MAX_FILE_BYTES:
                raise ValueError("Lean source file exceeds the review scan limit")
            text = candidate.read_text(encoding="utf-8", errors="replace")
            sites, _sorries = scan_lean_source(text)
        except (OSError, RuntimeError, ValueError):
            out[rel] = {}
            continue
        names: dict[str, int] = {}
        for site in sites:
            if site.qualified:
                names.setdefault(site.qualified, site.line)
            if site.name:
                names.setdefault(site.name, site.line)
        out[rel] = names
    return out


def _line_for(name: str, source_file: Optional[str], lines: dict) -> Optional[int]:
    if not source_file:
        return None
    index = lines.get(source_file) or {}
    if name in index:
        return index[name]
    matches = {line for candidate, line in index.items() if _name_match(name, candidate)}
    return next(iter(matches)) if len(matches) == 1 else None


def _wire_status(records: list[dict]) -> str:
    """Conservative aggregate status for one declaration graph node."""
    verdicts = [record.get("verdict") for record in records]
    if "rejected" in verdicts:
        return "rejected"
    if any(verdict is None for verdict in verdicts):
        return "unreviewed"
    if "deferred" in verdicts:
        return "deferred"
    if verdicts and all(verdict == "verified" for verdict in verdicts):
        return "verified"
    return "unreviewed"


def _issue_status(ctx, issue: int, ready: bool, blocked_reason: Optional[str]) -> str:
    status = ctx.verdict_status.get(issue)
    if status in {"verified", "rejected", "stalled", "deferred"}:
        return status
    if ready:
        return "ready"
    return "blocked" if blocked_reason else "pending"


def _audit_summary(ctx) -> dict:
    """Honest freshness/build context for wide review consumers.

    ``load_snapshot`` already withholds revision-mismatched evidence.  Preserve
    that distinction in the wire payload instead of making an empty stale
    snapshot look like a genuinely empty, fresh audit.
    """
    snapshot = ctx.snapshot
    if snapshot is None:
        return {
            "status": "missing",
            "created_at": None,
            "source_revision": None,
            "failures": [],
        }
    failures = list(getattr(snapshot, "failures", None) or [])
    lowered = "\n".join(failures).lower()
    stale_markers = (
        "stale", "withheld", "uncommitted", "source revision",
    )
    if any(marker in lowered for marker in stale_markers):
        status = "stale"
    elif failures:
        status = "partial"
    else:
        status = "fresh"
    return {
        "status": status,
        "created_at": getattr(snapshot, "created_at", None),
        "source_revision": getattr(snapshot, "source_revision", None),
        "failures": failures,
    }


def _aggregate_build_status(
    statuses: Iterable[Optional[str]], *, fallback: str = "unknown"
) -> str:
    """Worst honest build state for an issue/multi-owner declaration."""
    present = {status for status in statuses if status}
    for status in ("failed", "unknown", "stale", "unaudited", "ok"):
        if status in present:
            return status
    return fallback


def _decl_build_status(cards, ctx, name: str, audit_status: str) -> str:
    """Build status independent of the human verdict/trust tier."""
    if audit_status == "missing":
        return "unknown"
    resolved = cards._resolve_to_snapshot(ctx, name)
    if resolved and ctx.snapshot is not None:
        audited = ctx.snapshot.by_name().get(resolved)
        if audited is not None:
            return audited.status
    # A merged snapshot may contain a fresh subset while explicitly
    # withholding older modules.  Declarations present above are current;
    # only absent evidence inherits the aggregate stale state.
    if audit_status == "stale":
        return "stale"
    short = name.rsplit(".", 1)[-1]
    if short in (ctx.broken_module_decls or set()):
        return "failed"
    return "unaudited"


def _catalog(cfg: "ReviewConfig", chapter: Optional[int]) -> dict:
    """Build the shared issue/declaration catalog behind both public views."""
    from marathon.deck import cards
    from marathon.deck.subdeck import (
        DECL_FINGERPRINT_VERSION,
        _load_store,
        _decl_card,
        _decl_fingerprint,
        _depends_on_any,
    )

    issue_nums = _issue_numbers(cfg, chapter)
    # The reader displays only the requested chapter, but ship authorization
    # must still see owners and unresolved predecessors from every registered
    # chapter.  One global context provides that evidence without constructing
    # rich declaration cards for out-of-scope issues.
    global_nums = _issue_numbers(cfg, None)
    for issue in issue_nums:
        if issue not in global_nums:
            global_nums.append(issue)
    ctx = cards._load_context(cfg, global_nums)
    audit = _audit_summary(ctx)
    ledger = cards._ledger(cfg)
    verdict_store = _load_store(cfg)

    errors: list[dict] = []
    surfaces = {
        issue: cards._build_reconciled_issue_surface(
            cfg, ctx, ledger, issue
        )
        for issue in issue_nums
    }
    (
        _dependency_surfaces,
        _owned_decls,
        global_raw_issue_deps,
        global_issue_deps,
    ) = cards._global_reconciled_dependency_model(
        cfg, ctx, ledger, issue_nums, surfaces
    )
    # Keep the payload scoped to the visible issues while retaining
    # out-of-chapter predecessor IDs in each visible issue's dependency list.
    raw_issue_deps = {
        issue: list(global_raw_issue_deps.get(issue, []))
        for issue in issue_nums
    }
    issue_deps = {
        issue: list(global_issue_deps.get(issue, []))
        for issue in issue_nums
    }

    issue_rows: list[dict] = []
    targets: list[dict] = []
    registry_rank = {issue: rank for rank, issue in enumerate(issue_nums)}

    for issue in issue_nums:
        meta = ctx.issue_meta.get(issue) or {}
        surface = surfaces[issue]
        views = list(surface.review_views)
        stale = [view.name for view in surface.stale_views]
        if surface.error:
            errors.append({
                "issue": issue,
                "phase": "reconciliation",
                "error": surface.error,
            })

        records = verdict_store.get(str(issue), {})
        effective: dict[str, tuple[Optional[dict], Optional[str]]] = {}
        for view in views:
            record = records.get(view.name)
            carry = None
            if record:
                if (
                    record.get("fingerprint") is None
                    or record.get("fingerprint_version")
                    != DECL_FINGERPRINT_VERSION
                ):
                    # Legacy/current-session records that were not bound to an
                    # exact code + kernel surface cannot authorize a decision.
                    record, carry = None, "changed"
                elif _decl_fingerprint(view) != record["fingerprint"]:
                    record, carry = None, "changed"
                elif record.get("carried_over"):
                    carry = "carried_over"
            effective[view.name] = (record, carry)

        rejected = {
            name
            for name, (record, _carry) in effective.items()
            if (record or {}).get("verdict") == "reject"
        }
        for view in views:
            record, _carry = effective[view.name]
            if (record or {}).get("verdict") == "verify" and _depends_on_any(view, rejected):
                effective[view.name] = (None, "dependency_invalidated")

        cards_for_issue: list[dict] = []
        for view in views:
            card = _decl_card(view, *effective[view.name])
            card.update({
                "issue": issue,
                "issue_title": meta.get("title") or f"#{issue}",
                "chapter": cfg.chapter_of_issue(issue),
                "permalink": cards._issue_permalink(cfg, issue),
                "registry_order": registry_rank[issue],
                "build_status": _decl_build_status(
                    cards, ctx, view.name, audit["status"]
                ),
            })
            cards_for_issue.append(card)

        # Project rejected predecessors onto each target.  These names drive
        # the contextual reader guard and are also useful graph annotations.
        for card in cards_for_issue:
            blocked_by = []
            for candidate in cards_for_issue:
                if candidate["name"] == card["name"] or candidate.get("verdict") != "rejected":
                    continue
                if any(
                    _name_match(member.get("name") or "", candidate["name"])
                    for member in card.get("kernel") or []
                ):
                    blocked_by.append(candidate["name"])
            card["blocked_by"] = sorted(set(blocked_by))
            targets.append(card)

        tier = surface.tier
        qualifiers = surface.qualifiers
        ready, reason = cards._readiness(
            ctx, issue, tier, issue_deps.get(issue, [])
        )
        inspectable, inspection_reason = cards._inspectability(
            ctx, issue, tier
        )
        for card in cards_for_issue:
            card.update({
                "inspectable": inspectable,
                "shippable": ready,
                "provisional": inspectable and not ready,
                "ship_blocked_reason": reason,
                "inspection_blocked_reason": inspection_reason,
            })
        issue_rows.append({
            "id": issue,
            "title": meta.get("title") or f"#{issue}",
            "chapter": cfg.chapter_of_issue(issue),
            "tier": tier,
            "tier_qualifiers": qualifiers,
            "status": _issue_status(ctx, issue, ready, reason),
            "build_status": _aggregate_build_status(
                (card.get("build_status") for card in cards_for_issue),
                fallback=(
                    "stale" if audit["status"] == "stale" else "unknown"
                ),
            ),
            "ready": ready,
            "inspectable": inspectable,
            "shippable": ready,
            "provisional": inspectable and not ready,
            "blocked_reason": reason,
            "inspection_blocked_reason": inspection_reason,
            "deps": list(issue_deps.get(issue, [])),
            "graph_deps": list(raw_issue_deps.get(issue, [])),
            "permalink": cards._issue_permalink(cfg, issue),
            "stale_citations": stale,
            "declarations": [card["name"] for card in cards_for_issue],
        })

    lines = _source_line_index(
        Path(cfg.repo_dir), (target.get("source_file") for target in targets)
    )
    # Extend the issue-local guard across the whole selected scope. A locally
    # rejected definition in issue A must pause a dependent theorem in issue B
    # just as surely as two cards in the same subdeck; otherwise the chapter
    # reader could approve downstream results while an upstream FIX is pending.
    rejected_names = {
        target["name"] for target in targets if target.get("verdict") == "rejected"
    }
    target_names = {target["name"] for target in targets}
    # A chapter-scoped view must still respect rejected definitions owned by an
    # earlier/later registered chapter. The sidecar is already keyed by the
    # fully-qualified declaration name, so this adds the global pending-FIX set
    # without another GitHub/audit read.
    for records in verdict_store.values():
        for name, record in records.items():
            if name.startswith("__") or (record or {}).get("verdict") != "reject":
                continue
            fingerprint = (record or {}).get("fingerprint")
            if fingerprint is None:
                # An unbound legacy decision is not authoritative and must not
                # continue blocking otherwise-current declarations.
                continue
            if name in target_names:
                # In-scope records were already validated through ``effective``.
                continue
            # A chapter-scoped reader may depend on a rejected declaration from
            # another chapter. Validate that out-of-scope record against the
            # same current audit/source context before allowing it to block.
            try:
                view = cards._build_decl_view(
                    ctx, ledger, name, Path(cfg.repo_dir)
                )
            except Exception:
                continue
            if view.in_code and _decl_fingerprint(view) == fingerprint:
                rejected_names.add(name)
    for target in targets:
        target["source_line"] = _line_for(
            target["name"], target.get("source_file"), lines
        )
        blocked = set(target.get("blocked_by") or [])
        for member in target.get("kernel") or []:
            member_name = member.get("name") or ""
            blocked.update(
                rejected
                for rejected in rejected_names
                if rejected != target["name"] and _name_match(member_name, rejected)
            )
        target["blocked_by"] = sorted(blocked)

    targets.sort(key=lambda target: (
        target.get("source_file") or "\uffff",
        target.get("source_line") if target.get("source_line") is not None else 10**12,
        target["registry_order"],
        target["name"],
    ))
    return {
        "repo": cfg.github_repo,
        "chapters": sorted(cfg.chapters),
        "issues": issue_rows,
        "targets": targets,
        "issue_deps": issue_deps,
        "raw_issue_deps": raw_issue_deps,
        "audit": audit,
        "errors": errors,
    }


def build_review_workspace(
    cfg: "ReviewConfig", chapter: Optional[int] = None
) -> dict:
    """Chapter/file reading view in source order (pure read)."""
    catalog = _catalog(cfg, chapter)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for target in catalog["targets"]:
        grouped[target.get("source_file") or "(source unavailable)"].append(target)

    files = []
    for path in sorted(grouped, key=lambda item: (item == "(source unavailable)", item)):
        declarations = grouped[path]
        reviewed = sum(
            1 for item in declarations if item.get("verdict") in {"verified", "rejected"}
        )
        files.append({
            "path": path,
            "declarations": declarations,
            "progress": {"reviewed": reviewed, "total": len(declarations)},
            "issues": sorted({item["issue"] for item in declarations}),
        })

    total = len(catalog["targets"])
    reviewed = sum(
        1
        for item in catalog["targets"]
        if item.get("verdict") in {"verified", "rejected"}
    )
    return {
        "repo": catalog["repo"],
        "chapter": chapter,
        "chapters": catalog["chapters"],
        "files": files,
        "issues": catalog["issues"],
        "progress": {
            "reviewed": reviewed,
            "total": total,
            "verified": sum(item.get("verdict") == "verified" for item in catalog["targets"]),
            "rejected": sum(item.get("verdict") == "rejected" for item in catalog["targets"]),
            "deferred": sum(item.get("verdict") == "deferred" for item in catalog["targets"]),
        },
        "audit": catalog["audit"],
        "errors": catalog["errors"],
    }


def _resolve_known(name: str, known: set[str]) -> Optional[str]:
    if name in known:
        return name
    matches = [candidate for candidate in known if _name_match(name, candidate)]
    return matches[0] if len(matches) == 1 else None


_TOPOLOGY_NOTICE = (
    "Exploratory only: dependency topology never affects trust, build status, "
    "review readiness, or dependency gating."
)


def _topology_source(cfg: "ReviewConfig", audit: dict) -> tuple[object | None, dict]:
    """Load the last persisted dependency topology without trusting it.

    The canonical catalog above deliberately consumes a revision-validated
    snapshot: stale audit evidence must never make a declaration reviewable.
    The project graph has a different job.  It is an exploratory map, so a
    last-known cone remains useful as long as its age and non-authoritative
    status are explicit.  Keeping this second read here prevents stale rows
    from leaking back into tiers, readiness, or the review dependency gate.
    """
    from marathon.audit.engine import load_snapshot, source_state

    try:
        snapshot = load_snapshot(cfg.repo_dir, validate_revision=False)
    except Exception:  # pragma: no cover - corrupt/unreadable cache is absence
        snapshot = None
    try:
        current_revision, clean = source_state(cfg.repo_dir)
    except Exception:  # pragma: no cover - git unavailable is explicit below
        current_revision, clean = None, None

    source_revision = getattr(snapshot, "source_revision", None)
    if snapshot is None or not getattr(snapshot, "decls", None):
        return None, {
            "status": "unavailable",
            "available": False,
            "source_revision": source_revision,
            "current_revision": current_revision,
            "authoritative": False,
            "notice": (
                "No audited dependency topology is available. "
                + _TOPOLOGY_NOTICE
            ),
        }

    if current_revision is None:
        # Outside a Git checkout there is no stronger revision comparison than
        # the validated audit view already used by the catalog (notably in
        # isolated/offline review workspaces and unit tests).
        fresh = audit.get("status") == "fresh"
    elif clean is not True:
        fresh = False
    else:
        # Per-module bindings are authoritative after merged/partial audits.
        # Requiring every represented module to match is deliberately
        # conservative: an overall source_revision can describe the latest
        # successful subset while older module rows remain in the same file.
        represented = {decl.module for decl in snapshot.decls}
        revisions = getattr(snapshot, "module_revisions", {}) or {}
        fresh = bool(represented) and all(
            revisions.get(module) == current_revision for module in represented
        )

    status = "fresh" if fresh else "stale"
    qualifier = (
        "Dependency topology comes from the current audited source. "
        if fresh else
        "Dependency topology is last-known data from an older or unverifiable "
        "audit and may differ from the current code. "
    )
    return snapshot, {
        "status": status,
        "available": True,
        "source_revision": source_revision,
        "current_revision": current_revision,
        "authoritative": False,
        "notice": qualifier + _TOPOLOGY_NOTICE,
    }


def _nearest_review_dependencies(
    snapshot: object | None, known: set[str]
) -> tuple[set[tuple[str, str]], int]:
    """Project a raw declaration cone onto the nearest review targets.

    ``DeclAudit.cone`` is direct, but many direct constants are implementation
    helpers without their own review card.  Walk transparently through those
    helpers until another registered target is reached, then stop that branch.
    This is the transitive reduction appropriate to the projected node set:
    for ``A -> helper -> B -> C`` it emits ``A -> B`` and ``B -> C``, never the
    redundant ``A -> C`` edge produced by the old transitive-kernel projection.

    Current target names that do not resolve uniquely in the persisted
    snapshot are omitted.  In particular, a renamed declaration never inherits
    the old declaration's edges merely because both occupy the same issue.
    """
    if snapshot is None:
        return set(), 0
    by_raw = snapshot.by_name()
    raw_names = set(by_raw)
    if not raw_names:
        return set(), 0

    current_to_raw: dict[str, str] = {}
    candidates_by_raw: dict[str, list[str]] = defaultdict(list)
    for current in sorted(known):
        resolved = _resolve_known(current, raw_names)
        if resolved is not None:
            current_to_raw[current] = resolved
            candidates_by_raw[resolved].append(current)

    # An ambiguous reverse projection would fabricate ownership.  Remove every
    # current target sharing the same historical declaration instead.
    ambiguous = {
        raw for raw, currents in candidates_by_raw.items() if len(currents) != 1
    }
    for current, raw in list(current_to_raw.items()):
        if raw in ambiguous:
            del current_to_raw[current]
    raw_to_current = {raw: current for current, raw in current_to_raw.items()}

    dependencies: set[tuple[str, str]] = set()
    for dependent in sorted(current_to_raw):
        target_raw = current_to_raw[dependent]
        target = by_raw.get(target_raw)
        if target is None:
            continue
        frontier = sorted(set(target.cone))
        visited: set[str] = set()
        while frontier:
            raw_name = frontier.pop(0)
            resolved = _resolve_known(raw_name, raw_names)
            if resolved is None or resolved in visited:
                continue
            visited.add(resolved)
            predecessor = raw_to_current.get(resolved)
            if predecessor is not None:
                if predecessor != dependent:
                    dependencies.add((predecessor, dependent))
                # Stop at the nearest visible target.  Its own incoming edges
                # describe everything farther upstream.
                continue
            intermediate = by_raw.get(resolved)
            if intermediate is not None:
                frontier.extend(
                    name for name in sorted(set(intermediate.cone))
                    if name not in visited
                )
                frontier.sort()
    return dependencies, len(current_to_raw)


def _cyclic_edge_keys(edges: list[dict]) -> set[tuple[str, str, str]]:
    """Dependency edges whose endpoints share a non-trivial SCC.

    ``contains`` is ownership, not dependency, and must not manufacture a
    cycle.  Tarjan traversal and all emitted collections are sorted so graph
    diagnostics remain deterministic across processes and Python versions.
    The two iterative Kosaraju passes avoid Python's recursion limit on a
    large, deep project graph.
    """
    dependency_edges = [
        edge for edge in edges
        if edge.get("kind") in {"issue-dependency", "declaration-dependency"}
    ]
    adjacency: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)
    vertices: set[str] = set()
    for edge in dependency_edges:
        source, target = edge["source"], edge["target"]
        vertices.update((source, target))
        adjacency[source].add(target)
        reverse[target].add(source)

    visited: set[str] = set()
    finish_order: list[str] = []
    for start in sorted(vertices):
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            vertex, expanded = stack.pop()
            if expanded:
                finish_order.append(vertex)
                continue
            if vertex in visited:
                continue
            visited.add(vertex)
            stack.append((vertex, True))
            for target in sorted(adjacency.get(vertex, ()), reverse=True):
                if target not in visited:
                    stack.append((target, False))

    component_by: dict[str, int] = {}
    cyclic_components: set[int] = set()
    assigned: set[str] = set()
    for start in reversed(finish_order):
        if start in assigned:
            continue
        component_id = len(set(component_by.values()))
        component: list[str] = []
        stack = [start]
        assigned.add(start)
        while stack:
            vertex = stack.pop()
            component.append(vertex)
            for source in sorted(reverse.get(vertex, ()), reverse=True):
                if source not in assigned:
                    assigned.add(source)
                    stack.append(source)
        for member in component:
            component_by[member] = component_id
        if len(component) > 1:
            cyclic_components.add(component_id)
    return {
        (edge["source"], edge["target"], edge["kind"])
        for edge in dependency_edges
        if component_by.get(edge["source"]) == component_by.get(edge["target"])
        and component_by.get(edge["source"]) in cyclic_components
    }


def build_project_graph(cfg: "ReviewConfig") -> dict:
    """Project-wide issue/declaration dependency graph (pure read).

    Edge direction is predecessor -> dependent.  ``contains`` edges connect an
    issue to each declaration it reviews; declaration and issue dependency
    edges are separate kinds so the client can filter or style them.
    """
    catalog = _catalog(cfg, None)
    nodes: list[dict] = []
    edges: list[dict] = []
    edge_keys: set[tuple[str, str, str]] = set()

    def add_edge(
        source: str, target: str, kind: str, *, topology_status: Optional[str] = None
    ) -> None:
        key = (source, target, kind)
        if source == target or key in edge_keys:
            return
        edge_keys.add(key)
        edge = {"source": source, "target": target, "kind": kind}
        if topology_status is not None:
            edge.update({
                "topology_status": topology_status,
                "freshness": topology_status,
                "authoritative": False,
            })
        edges.append(edge)

    issues_by_id = {issue["id"]: issue for issue in catalog["issues"]}
    for issue in catalog["issues"]:
        nodes.append({
            "id": f"issue:{issue['id']}",
            "type": "issue",
            "label": f"#{issue['id']}",
            "title": issue["title"],
            "issue": issue["id"],
            "chapter": issue["chapter"],
            "chapters": [issue["chapter"]] if issue["chapter"] is not None else [],
            "status": issue["status"],
            "build_status": issue["build_status"],
            "tier": issue["tier"],
            "permalink": issue["permalink"],
            "blocked_reason": issue["blocked_reason"],
        })
    by_decl: dict[str, list[dict]] = defaultdict(list)
    for target in catalog["targets"]:
        by_decl[target["name"]].append(target)
    known = set(by_decl)
    # Only current project declarations can be truthfully described as an
    # unresolved review edge.  Kernel entries outside this set are ordinary
    # external/library dependencies (or unavailable source), not missing graph
    # nodes. Reuse the definition inspector's bounded, mtime-cached index.
    try:
        from marathon.deck.definitions import _current_source_index

        current_project_names = set(_current_source_index(Path(cfg.repo_dir)))
    except Exception:  # pragma: no cover - fail-soft diagnostic enrichment
        current_project_names = set()
    current_by_short: dict[str, list[str]] = defaultdict(list)
    for project_name in current_project_names:
        current_by_short[project_name.rsplit(".", 1)[-1]].append(project_name)

    def current_project_match(name: str) -> Optional[str]:
        if name in current_project_names:
            return name
        candidates = current_by_short.get(name.rsplit(".", 1)[-1], [])
        matches = [candidate for candidate in candidates if _name_match(name, candidate)]
        return matches[0] if len(matches) == 1 else None

    unresolved_edges: list[dict] = []
    unresolved_keys: set[tuple[str, str]] = set()

    for name in sorted(by_decl):
        records = by_decl[name]
        issues = sorted({record["issue"] for record in records})
        chapters = sorted({record["chapter"] for record in records if record["chapter"] is not None})
        located = next((record for record in records if record.get("source_file")), records[0])
        title = located.get("statement_pp") or located.get("source_block") or name
        nodes.append({
            "id": "decl:" + name,
            "type": "declaration",
            "label": name,
            "title": str(title)[:800],
            "kind": located.get("kind"),
            "status": _wire_status(records),
            "build_status": _aggregate_build_status(
                record.get("build_status") for record in records
            ),
            "tier": located.get("tier"),
            "issues": issues,
            "chapter": chapters[0] if len(chapters) == 1 else None,
            "chapters": chapters,
            "source_file": located.get("source_file"),
            "source_line": located.get("source_line"),
            "blocked_by": sorted({dep for record in records for dep in record.get("blocked_by", [])}),
            "changed": any(
                record.get("changed_since_review")
                or record.get("dependency_invalidated")
                for record in records
            ),
        })
        for issue in issues:
            add_edge(f"issue:{issue}", "decl:" + name, "contains")

    # Retain fresh-kernel diagnostics for project declarations that do not yet
    # have a review target.  Structural graph edges below come from the separate
    # topology source; this block must never promote stale topology into review
    # evidence or readiness.
    for dependent, records in sorted(by_decl.items()):
        for record in records:
            for member in record.get("kernel") or []:
                raw_name = member.get("name") or ""
                resolved = _resolve_known(raw_name, known)
                if (
                    raw_name
                    and resolved is None
                    and current_project_match(raw_name) is not None
                ):
                    key = (raw_name, dependent)
                    if key not in unresolved_keys:
                        unresolved_keys.add(key)
                        unresolved_edges.append({
                            "source": "decl:" + raw_name,
                            "target": "decl:" + dependent,
                            "kind": "declaration-dependency",
                            "dependency": raw_name,
                            "status": "unreviewed-target",
                            "reason": "current project dependency has no registered review target",
                        })

    # Topology is deliberately a second, non-authoritative read.  The catalog's
    # validated snapshot remains the sole source for trust/build/readiness;
    # stale last-known cones are useful only for this exploratory map.
    topology_snapshot, topology = _topology_source(cfg, catalog["audit"])
    declaration_dependencies, resolved_targets = _nearest_review_dependencies(
        topology_snapshot, known
    )
    for dependency, dependent in sorted(declaration_dependencies):
        add_edge(
            "decl:" + dependency,
            "decl:" + dependent,
            "declaration-dependency",
            topology_status=topology["status"],
        )

    # Roll visible declaration topology up to issue topology without touching
    # ``catalog['issue_deps']`` or any readiness/gating state.  A declaration
    # can be cited by more than one issue, but treating every citation as an
    # owner and taking their Cartesian product fabricates edges (and even
    # cycles) between duplicate review cards.  Choose its earliest registered
    # issue as the one structural owner; the ownership-map ``contains`` edges
    # above still retain every citation for inspection.
    issue_dependency_pairs: set[tuple[int, int]] = set()
    owners: dict[str, int] = {}
    for name, records in by_decl.items():
        ranked_issues: dict[int, int] = {}
        for record in records:
            issue = record["issue"]
            rank = int(record.get("registry_order", 10**12))
            ranked_issues[issue] = min(rank, ranked_issues.get(issue, rank))
        if ranked_issues:
            owners[name] = min(
                ranked_issues, key=lambda issue: (ranked_issues[issue], issue)
            )
    for dependency, dependent in sorted(declaration_dependencies):
        predecessor_issue = owners.get(dependency)
        dependent_issue = owners.get(dependent)
        if predecessor_issue is None or dependent_issue is None:
            continue
        if predecessor_issue == dependent_issue:
            continue
        predecessor = issues_by_id.get(predecessor_issue) or {}
        successor = issues_by_id.get(dependent_issue) or {}
        predecessor_chapter = predecessor.get("chapter")
        dependent_chapter = successor.get("chapter")
        if (
            predecessor_chapter is not None
            and dependent_chapter is not None
            and predecessor_chapter > dependent_chapter
        ):
            continue
        issue_dependency_pairs.add((predecessor_issue, dependent_issue))
    for predecessor, dependent in sorted(issue_dependency_pairs):
        add_edge(
            f"issue:{predecessor}",
            f"issue:{dependent}",
            "issue-dependency",
            topology_status=topology["status"],
        )

    topology.update({
        "targets": len(known),
        "resolved_targets": resolved_targets,
        "declaration_edges": len(declaration_dependencies),
        "issue_edges": len(issue_dependency_pairs),
    })
    for edge in unresolved_edges:
        edge.update({
            "topology_status": topology["status"],
            "freshness": topology["status"],
            "authoritative": False,
        })

    cyclic_keys = _cyclic_edge_keys(edges)
    for edge in edges:
        edge["cyclic"] = (
            edge["source"], edge["target"], edge["kind"]
        ) in cyclic_keys
    cyclic_edges = [dict(edge) for edge in edges if edge["cyclic"]]

    # Highlight the transitive impact of a declaration whose carried human
    # approval was invalidated.  Propagation follows declaration dependency
    # edges only; ownership edges must not make every declaration in an issue
    # appear downstream merely because one sibling changed.
    outgoing: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge["kind"] == "declaration-dependency":
            outgoing[edge["source"]].add(edge["target"])
    changed_ids = {
        node["id"] for node in nodes
        if node["type"] == "declaration" and node.get("changed")
    }
    impacted = set(changed_ids)
    frontier = sorted(changed_ids)
    while frontier:
        source = frontier.pop()
        for target in sorted(outgoing.get(source, ())):
            if target not in impacted:
                impacted.add(target)
                frontier.append(target)

    declaration_by_id = {
        node["id"]: node for node in nodes if node["type"] == "declaration"
    }
    issue_impact: dict[int, str] = {}
    for node_id in sorted(impacted):
        node = declaration_by_id.get(node_id)
        if node is None:
            continue
        impact = "changed" if node_id in changed_ids else "downstream"
        node["impact"] = impact
        node["downstream_of_changed"] = impact == "downstream"
        for issue in node.get("issues") or []:
            if impact == "changed" or issue not in issue_impact:
                issue_impact[issue] = impact
    for node in nodes:
        if node["type"] == "issue" and node.get("issue") in issue_impact:
            node["impact"] = issue_impact[node["issue"]]
            node["downstream_of_changed"] = node["impact"] == "downstream"

    unresolved_edges.sort(key=lambda edge: (
        edge["target"], edge["dependency"], edge["kind"]
    ))

    status_counts: dict[str, int] = defaultdict(int)
    type_counts: dict[str, int] = defaultdict(int)
    build_counts: dict[str, int] = defaultdict(int)
    for node in nodes:
        status_counts[node["status"]] += 1
        type_counts[node["type"]] += 1
        build_counts[node["build_status"]] += 1
    return {
        "repo": catalog["repo"],
        "chapters": catalog["chapters"],
        "nodes": nodes,
        "edges": edges,
        "unresolved_edges": unresolved_edges,
        "cyclic_edges": cyclic_edges,
        "audit": catalog["audit"],
        "topology": topology,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "by_status": dict(sorted(status_counts.items())),
            "by_type": dict(sorted(type_counts.items())),
            "by_build_status": dict(sorted(build_counts.items())),
            "changed": len(changed_ids),
            "impacted": len(impacted - changed_ids),
            "unresolved_edges": len(unresolved_edges),
            "cyclic_edges": len(cyclic_edges),
        },
        "errors": catalog["errors"],
    }

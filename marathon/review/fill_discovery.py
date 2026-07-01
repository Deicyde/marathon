"""Fill discovery — the work-list for hands-off sorry-filling.

A *fill target* is a sub-issue whose declarations a human has VERIFIED (so their
statements are frozen by the approved-statements guard) but at least one of which
still has a ``sorry`` body. The conductor dispatches ``marathon fill --issue N``
for each such issue; the immutability guard ([[marathon-v2 deployment]]) ensures
the fill can only fill bodies, never change an approved statement.

Discovery uses :func:`marathon.review.verified_decls.verified_declarations`
(GitHub ``review:verified`` labels + issue bodies) so it covers every verified
issue, not just ones captured into the local manifest since capture-on-verify
shipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from marathon.review.config import ReviewConfig


@dataclass
class FillTarget:
    """One verified sub-issue with at least one still-``sorry`` declaration."""
    issue: int
    chapter: int
    decls: list[str]  # verified decls in this issue whose body is still `sorry`


def discover_fill_targets(
    cfg: ReviewConfig,
    chapter: int,
    *,
    _sorry_finder: Optional[Callable] = None,
    _verified: Optional[Callable] = None,
) -> list[FillTarget]:
    """Verified sub-issues in ``chapter`` that still have ``sorry``-bodied
    declarations, sorted by issue number. Empty when nothing is verified or the
    chapter source is absent. Never raises — a discovery failure must not crash
    the conductor (injectable helpers are for tests)."""
    from marathon.fill import _find_sorries_in_file
    from marathon.review.verified_decls import verified_declarations

    finder = _sorry_finder or _find_sorries_in_file
    verified_fn = _verified or verified_declarations
    try:
        verified = verified_fn(cfg, chapter)  # {issue_num: {decl_name, ...}}
    except Exception:  # noqa: BLE001
        return []
    if not verified:
        return []

    try:
        target = cfg.target_path(chapter)
    except Exception:  # noqa: BLE001
        return []
    sorry_decls: set[str] = set()
    if target.is_dir():
        for f in sorted(target.rglob("*.lean")):
            try:
                sorry_decls.update(finder(f))
            except Exception:  # noqa: BLE001 — one bad file shouldn't sink discovery
                continue

    out: list[FillTarget] = []
    for issue in sorted(verified):
        unproved = sorted(verified[issue] & sorry_decls)
        if unproved:
            out.append(FillTarget(issue=issue, chapter=chapter, decls=unproved))
    return out


def chapter_skeleton_done(
    cfg: ReviewConfig,
    chapter: int,
    *,
    _verified: Optional[Callable] = None,
) -> bool:
    """True iff EVERY issue registered for ``chapter`` has a VERIFIED
    statement — i.e. the skeleton (statement review) is complete, so proving
    that chapter won't waste effort on signatures that may still change under
    re-review. A chapter with no registry, no entries, or any still-pending /
    rejected / stalled issue returns False (the prover refuses to start on a
    chapter whose statements are still churning). Never raises — a discovery
    failure must not crash the conductor."""
    chapters = getattr(cfg, "chapters", None)
    registry = chapters.get(chapter) if chapters else None
    entries = getattr(registry, "entries", None) if registry is not None else None
    if not entries:
        return False
    registered = {num for num, *_ in entries}
    if not registered:
        return False
    from marathon.review.verified_decls import verified_declarations

    verified_fn = _verified or verified_declarations
    try:
        verified = verified_fn(cfg, chapter)  # {issue_num: {decl_name, ...}}
    except Exception:  # noqa: BLE001
        return False
    verified_issues = {i for i, decls in verified.items() if decls}
    return registered <= verified_issues


@dataclass
class ChapterProverStatus:
    """Read-only per-chapter proving progress (``marathon prover status``)."""
    chapter: int
    registered: int        # issues registered for the chapter
    verified: int          # of those, with a verified statement
    skeleton_done: bool    # every registered issue verified
    sorries_left: int      # distinct sorry-bodied decls in the chapter source
    fill_targets: int      # verified issues with ≥1 still-sorry decl
    unproved_decls: int    # total still-sorry verified decls across targets


def prover_status(cfg: ReviewConfig, chapters) -> "list[ChapterProverStatus]":
    """Per-chapter proving progress for ``chapters``. Pure + read-only; a
    per-chapter discovery failure degrades that row to zeros, never raises.
    ``verified_declarations`` is fetched once per chapter and threaded into
    the skeleton-done + discovery helpers so the gh read isn't repeated."""
    from marathon.fill import _find_sorries_in_file
    from marathon.review.verified_decls import verified_declarations

    out: list[ChapterProverStatus] = []
    for chapter in sorted(set(chapters)):
        registry = cfg.chapters.get(chapter) if cfg.chapters else None
        registered = len(getattr(registry, "entries", None) or [])
        try:
            verified = verified_declarations(cfg, chapter)
        except Exception:  # noqa: BLE001
            verified = {}
        cached = lambda _cfg, _ch, _v=verified: _v  # noqa: E731 — reuse the fetch
        n_verified = len({i for i, d in verified.items() if d})
        done = chapter_skeleton_done(cfg, chapter, _verified=cached)
        sorries: set[str] = set()
        try:
            target = cfg.target_path(chapter)
            if target.is_dir():
                for f in sorted(target.rglob("*.lean")):
                    try:
                        sorries.update(_find_sorries_in_file(f))
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            pass
        targets = discover_fill_targets(cfg, chapter, _verified=cached)
        out.append(ChapterProverStatus(
            chapter=chapter,
            registered=registered,
            verified=n_verified,
            skeleton_done=done,
            sorries_left=len(sorries),
            fill_targets=len(targets),
            unproved_decls=sum(len(t.decls) for t in targets),
        ))
    return out

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

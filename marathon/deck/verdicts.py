"""marathon.deck.verdicts — the deck's verdict ROUTER.

The deck must NEVER own a second verdict write path. verify and reject are
irreversible (verify merges the marathon PR + flips the tracker; reject
records the rejection and dispatches the conductor/daemon with the note
VERBATIM to Aristotle), and that logic — ledger dual-write, GitHub label +
comment, tracker emoji, daemon/conductor trigger, the Claude-bypass on the
reject path — already lives, battle-tested, in
:func:`marathon.review.review.cmd_verify` / :func:`~.cmd_reject`. This
module ROUTES through those exact functions: it constructs the argparse
namespace they expect and calls them, so the deck and the CLI fire the
identical side effects. It reimplements none of it.

``defer`` is the one deck-only verdict: a small marker, no Aristotle, no
GitHub verdict (plan §3 Phase 8 row: "v/r/space/d/o"; space = defer = skip
for now). It is stored in a self-contained JSON sidecar
(``.marathon/review/deck-defers.json``) so it stays purely additive — it
never touches the committed ``review/state.json`` verdict schema (whose
statuses are only rejected/verified/stalled). The cards layer overlays
defers as a non-ready ``'deferred'`` status; a later real verify/reject of
the same issue supersedes the defer.

BINDING SAFETY: :func:`apply_verdict` is the deck's ONLY side-effecting
entry point and is called ONLY from an explicit, token-checked
``POST /api/verdict`` (see :mod:`marathon.deck.server`). It is never
reached by a GET, a page load, or a prefetch.
"""

from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover — type-only
    from marathon.deck.cards import CardSummary
    from marathon.review.config import ReviewConfig

#: The deck's defer marker sidecar (additive — never the committed
#: review/state.json). Self-contained JSON: ``{"deferred": {num: ts}}``.
DEFER_RELPATH = Path(".marathon") / "review" / "deck-defers.json"

#: The three verdicts the deck offers.
VERDICTS = ("verify", "reject", "defer")


class VerdictError(ValueError):
    """A verdict request the deck refuses to route (unknown verdict, an
    empty reject note). Raised — never silently swallowed — so the server
    can return a clean 4xx instead of firing a malformed side effect."""


@dataclass(frozen=True)
class VerdictResult:
    """Outcome of :func:`apply_verdict`. ``advanced_to`` is the next ready
    card to swipe (or None when the queue is drained), matching the shared
    POST /api/verdict response contract."""

    ok: bool
    verdict: str
    issue_num: int
    message: str
    advanced_to: Optional["CardSummary"]

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "issue_num": self.issue_num,
            "message": self.message,
            "advanced_to": (
                self.advanced_to.to_json()
                if self.advanced_to is not None else None
            ),
        }


# ---------------------------------------------------------------------------
# Defer marker (deck-only state — additive, no Aristotle, no GitHub)
# ---------------------------------------------------------------------------


def _defer_path(cfg: "ReviewConfig") -> Path:
    return Path(cfg.repo_dir) / DEFER_RELPATH


def _load_defers(cfg: "ReviewConfig") -> dict:
    """``{str(issue_num): iso_ts}`` of deferred issues, or ``{}``. A
    missing/corrupt file is "nothing deferred", never an error."""
    path = _defer_path(cfg)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    deferred = data.get("deferred") if isinstance(data, dict) else None
    return deferred if isinstance(deferred, dict) else {}


def deferred_issue_nums(cfg: "ReviewConfig") -> set[int]:
    """The set of currently-deferred issue numbers (read-only — the cards
    layer overlays these as a non-ready ``'deferred'`` status)."""
    out: set[int] = set()
    for key in _load_defers(cfg):
        try:
            out.add(int(key))
        except (TypeError, ValueError):
            continue
    return out


def _record_defer(cfg: "ReviewConfig", issue_num: int) -> None:
    """Mark ``issue_num`` deferred (atomic-ish write). Idempotent: the
    timestamp refreshes on re-defer. Pure-local; never calls Aristotle,
    GitHub, the ledger verdict path, or the conductor."""
    path = _defer_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    deferred = _load_defers(cfg)
    deferred[str(issue_num)] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"deferred": deferred}, indent=2) + "\n")
    tmp.replace(path)


def clear_defer(cfg: "ReviewConfig", issue_num: int) -> None:
    """Drop a defer marker (used when the issue gets a real verdict, so a
    stale defer never lingers). No-op when the issue isn't deferred."""
    deferred = _load_defers(cfg)
    if str(issue_num) in deferred:
        deferred.pop(str(issue_num))
        path = _defer_path(cfg)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"deferred": deferred}, indent=2) + "\n")
        tmp.replace(path)


# ---------------------------------------------------------------------------
# Verdict routing (through the committed review verdict path — never forked)
# ---------------------------------------------------------------------------


def _verify_args(issue_num: int) -> Namespace:
    """The argparse namespace ``cmd_verify`` reads (``issue_num`` /
    ``close`` / ``comment``). The deck verifies statements-accepted but
    keeps the issue OPEN (``close=False``) — sorrys may remain on a
    skeleton; closing belongs to the CLI's explicit ``--close``."""
    return Namespace(issue_num=issue_num, close=False, comment=None)


def _reject_args(issue_num: int, note: str) -> Namespace:
    """The argparse namespace ``cmd_reject`` reads. ``notes`` carries the
    human's note as INLINE TEXT: ``cmd_reject`` treats a non-file value as
    the verbatim note and records it through ``record_rejection`` →
    refine/conductor with NO Claude in the loop (the committed reject
    path's Claude-bypass, preserved). ``no_refine=False`` so the rejection
    dispatches exactly as a CLI reject would."""
    return Namespace(
        issue_num=issue_num, notes=note, comment=None, no_refine=False,
    )


def apply_verdict(
    cfg: "ReviewConfig",
    issue_num: int,
    verdict: str,
    note: Optional[str] = None,
) -> VerdictResult:
    """Route one deck verdict and return the next ready card.

    * ``verify`` → :func:`marathon.review.review.cmd_verify` (merges the
      marathon PR, records the verdict, flips the tracker 🟠→🟡). Clears
      any stale defer marker first.
    * ``reject`` → :func:`marathon.review.review.cmd_reject` with the
      operator's ``note`` passed VERBATIM (Claude-bypass preserved); this
      records the rejection and dispatches the conductor/daemon. Clears
      any stale defer marker.
    * ``defer`` → a local marker only (:func:`_record_defer`): no
      Aristotle, no GitHub verdict, no ledger verdict event.

    Returns a :class:`VerdictResult` whose ``advanced_to`` is the next
    ready card in the same chapter (or None when the queue is drained).
    Raises :class:`VerdictError` on an unknown verdict or an empty reject
    note — BEFORE any side effect fires."""
    if verdict not in VERDICTS:
        raise VerdictError(
            f"unknown verdict {verdict!r}; expected one of {VERDICTS}"
        )

    # Import the committed verdict handlers lazily so this module stays
    # importable on a checkout without the gh-shaped review deps, and so
    # tests monkeypatch the SAME functions the CLI uses.
    from marathon.review import review as review_mod

    if verdict == "verify":
        clear_defer(cfg, issue_num)
        review_mod.cmd_verify(_verify_args(issue_num))
        message = f"#{issue_num} verified"
    elif verdict == "reject":
        if note is None or not str(note).strip():
            raise VerdictError(
                "a reject requires a non-empty note (it goes verbatim to "
                "Aristotle); refusing to route an empty rejection"
            )
        clear_defer(cfg, issue_num)
        review_mod.cmd_reject(_reject_args(issue_num, str(note)))
        message = f"#{issue_num} rejected; note dispatched verbatim"
    else:  # defer
        _record_defer(cfg, issue_num)
        message = f"#{issue_num} deferred"

    advanced_to = _next_ready_card(cfg, issue_num)
    return VerdictResult(
        ok=True,
        verdict=verdict,
        issue_num=issue_num,
        message=message,
        advanced_to=advanced_to,
    )


def _next_ready_card(
    cfg: "ReviewConfig", just_acted_issue: int
) -> Optional["CardSummary"]:
    """The next ready (swipeable) card after acting on ``just_acted_issue``,
    scoped to that issue's chapter. Rebuilds the queue freshly (the verdict
    just changed the readiness graph — a verify may have unblocked a
    dependent) and returns the first ready card that isn't the one just
    acted on. None when the chapter's ready queue is drained.

    Pure read over the now-updated state — the verdict's side effects
    already fired above."""
    from marathon.deck.cards import build_queue

    chapter = cfg.chapter_of_issue(just_acted_issue)
    queue = build_queue(cfg, chapter)
    for card in queue.cards:
        if card.id != just_acted_issue and card.ready:
            return card
    return None

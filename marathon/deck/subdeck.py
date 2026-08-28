"""marathon.deck.subdeck — the SUBDECK backend (per-declaration review).

A ready issue is the entry point to a SUBDECK: a small stack of cards, one
per cited DECLARATION. The human reviews each declaration on its own card
(keep / fix / skip-for-now), and finishing the subdeck SHIPS the whole issue.
This composition aligns with the existing harness, where refine / PR /
conductor / labels are all per-ISSUE — so review is per-decl, but the
irreversible outcome stays per-issue.

Three responsibilities, all here:

1. **A per-(issue,decl) verdict store** — a self-contained JSON sidecar
   ``<repo>/.marathon/review/deck-subdecks.json``, shape
   ``{issue_num(str): {decl_name: {verdict, note, ts}}}``. This is purely
   LOCAL review state (the human's running notes on each declaration); it
   touches neither the ledger, GitHub, nor Aristotle. It is additive — like
   the deck's defer markers (:mod:`marathon.deck.verdicts`) — and lives
   under the self-gitignored ``.marathon`` dir.

2. **The subdeck assembly** (:func:`build_subdeck`) — reuses
   :func:`marathon.deck.cards.build_card_detail` to get the issue's
   per-declaration views (each already carrying source / kernel / evidence /
   tier / informal rendering), overlays the LOCAL verdict/note from the
   store, orders the cards (unreviewed first, then worst-tier, then name),
   and computes the subdeck's progress / completeness / summary.

3. **The ship composition** (:func:`compose_ship_note`, :func:`ship_issue`)
   — composes the per-issue outcome from the subdeck and routes it through
   the committed verdict path (:func:`marathon.deck.verdicts.apply_verdict`,
   which calls ``cmd_verify`` / ``cmd_reject``). All-verified → verify the
   issue (no Aristotle). Any rejected → reject the issue with a human-authored
   composite note carried VERBATIM (the committed Claude-bypass) listing the
   KEEP and FIX declarations. SHIP is the only irreversible step and is
   reached only from an explicit ``POST /api/ship`` (see
   :mod:`marathon.deck.server`).

BINDING SAFETY: per-decl verdicts (:func:`record_decl_verdict`) are LOCAL
ONLY — no GitHub, no Aristotle, no ledger. The single irreversible action
lives in :func:`ship_issue`, which fires only on an explicit, token-checked
``POST /api/ship``. An all-verify ship requires every declaration to be
reviewed; a reject ship may stop early, sending only explicit KEEP/FIX choices
and leaving later declarations untouched for the next review round.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

try:  # POSIX advisory locks (Linux/macOS).
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX hosts
    _fcntl = None

try:  # Windows advisory byte-range locks.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised on non-Windows hosts
    _msvcrt = None

if TYPE_CHECKING:  # pragma: no cover — type-only
    from marathon.review.config import ReviewConfig

#: The per-(issue,decl) verdict sidecar (additive — never the committed
#: review/state.json). Self-contained JSON keyed by issue number (str), then
#: by decl name: ``{num: {decl: {"verdict", "note", "ts"}}}``.
SUBDECK_RELPATH = Path(".marathon") / "review" / "deck-subdecks.json"

#: The per-declaration verdicts the subdeck offers. ``verify`` = keep the
#: declaration exactly as written; ``reject`` = it needs a fix (note
#: required); ``defer`` = skip for now (the subdeck stays incomplete).
DECL_VERDICTS = ("verify", "reject", "defer")
DECL_FINGERPRINT_VERSION = 2


class ReviewContextChanged(ValueError):
    """The declaration shown to the reviewer is no longer current.

    This is a conflict, rather than a malformed verdict: callers should reload
    the current subdeck and ask the human to decide against that exact source
    and elaborated dependency context.
    """

#: The per-decl verdicts that count a declaration as REVIEWED (toward
#: progress / completeness). ``defer`` does NOT count — a deferred decl
#: leaves the subdeck incomplete (the contract).
REVIEWED_DECL_VERDICTS = frozenset({"verify", "reject"})

#: Reserved store key for the subdeck's ONE informal-text APPROVAL verdict (the
#: leading approval card: "approve/reject the LLM-rendered informal text").
#: It is NOT a Lean declaration name — it can never collide with a real decl —
#: so it never matches a card view and is invisible to the per-decl cards,
#: progress, completeness, summary, and ship composition (all of which key off
#: real decl names). It is advisory: approving it clears the deck's
#: LLM-rendered warning and, at ship, flips the issue body's informal marker.
INFORMAL_KEY = "__informal__"

#: Reserved store key for the issue's "something is MISSING" note — a per-issue
#: free-text instruction that a result the title promises has NO declaration yet
#: (e.g. Prop 12.22 cites the tensor-product case but not the function-
#: multiplication case). It is NOT a decl name, so (like INFORMAL_KEY) it never
#: becomes a card or enters progress/summary. At ship it makes the outcome route
#: to Aristotle with an ADD section even when every existing decl is verified.
MISSING_KEY = "__missing__"

#: Audit trail for declaration-identity changes.  It is reserved like the
#: informal/missing slots, so it never becomes a review card or counts toward
#: progress.  Active verdicts are deliberately not copied through a rename or
#: split unless the reviewer re-opens the new declaration and decides again.
RECONCILIATION_HISTORY_KEY = "__reconciliation_history__"

#: The STORED per-decl verdict (``verify``/``reject``/``defer``) → the
#: PAST-TENSE verdict the deck FRONTEND consumes on a serialized card
#: (``app.js`` renders ``card.verdict`` as the tag text + the ``dv-<verdict>``
#: CSS class — ``dv-verified``/``dv-rejected``/``dv-deferred`` — and its
#: ``deriveSummary``/``applyDeclResult`` match the same past-tense set). The
#: store stays present-tense (the ship composition + record API key off it);
#: only the wire face is mapped, so the GET seam matches the client.
_VERDICT_WIRE = {"verify": "verified", "reject": "rejected", "defer": "deferred"}

#: The wire (past-tense) verdicts that count a serialized card as REVIEWED —
#: the post-serialization mirror of :data:`REVIEWED_DECL_VERDICTS`, used by
#: :func:`next_unreviewed_card` which inspects the already-serialized cards.
_REVIEWED_WIRE_VERDICTS = frozenset({"verified", "rejected"})

_MutationResult = TypeVar("_MutationResult")

# ThreadingHTTPServer may execute verdict/lock/missing handlers concurrently.
# The RLock serializes those writers within this process; it is deliberately
# reentrant because ship cleanup and future composite mutations may call store
# helpers while already inside a transaction.
_STORE_RLOCK = threading.RLock()
_STORE_LOCK_LOCAL = threading.local()

# SHIP is keyed per issue: unrelated issues may ship concurrently, while two
# requests for the same issue share one in-process + cross-process critical
# section around authoritative rebuild, external side effect, and cleanup.
_ISSUE_LOCKS_GUARD = threading.Lock()
_ISSUE_LOCKS: dict[str, threading.RLock] = {}
_ISSUE_LOCK_LOCAL = threading.local()


# ---------------------------------------------------------------------------
# Per-(issue,decl) verdict store (LOCAL — no Aristotle, no GitHub, no ledger)
# ---------------------------------------------------------------------------


def _store_path(cfg: "ReviewConfig") -> Path:
    return Path(cfg.repo_dir) / SUBDECK_RELPATH


def _store_lock_path(cfg: "ReviewConfig") -> Path:
    """A stable lock inode beside the atomically-replaced JSON store.

    Locking the JSON file itself would be incorrect: :func:`Path.replace`
    changes its inode, allowing another process to lock the new file while the
    first still holds a lock on the old one.
    """
    path = _store_path(cfg)
    return path.with_name(path.name + ".lock")


def _issue_lock_path(cfg: "ReviewConfig", issue_num: int) -> Path:
    return (
        Path(cfg.repo_dir) / ".marathon" / "review" / "deck-ship-locks"
        / f"{issue_num}.lock"
    )


def _ship_receipt_path(cfg: "ReviewConfig", issue_num: int) -> Path:
    return (
        Path(cfg.repo_dir) / ".marathon" / "review" / "deck-ship-receipts"
        / f"{issue_num}.json"
    )


def _lock_file(handle) -> None:
    """Acquire an exclusive advisory lock when the platform provides one."""
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows-only branch
        # msvcrt.locking locks a byte range; ensure byte zero exists.
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)


def _unlock_file(handle) -> None:
    """Release the advisory lock acquired by :func:`_lock_file`."""
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows-only branch
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


@contextmanager
def _store_mutation_lock(cfg: "ReviewConfig"):
    """Serialize one complete store mutation in-process and cross-process.

    The separate lock file remains stable while ``deck-subdecks.json`` is
    atomically replaced. Nested acquisition for the same store reuses the
    already-held advisory lock instead of opening and flocking it again.
    """
    lock_path = _store_lock_path(cfg)
    key = str(lock_path.resolve())
    with _STORE_RLOCK:
        held = getattr(_STORE_LOCK_LOCAL, "held", None)
        if held is None:
            held = {}
            _STORE_LOCK_LOCAL.held = held
        existing = held.get(key)
        if existing is not None:
            existing["depth"] += 1
            try:
                yield
            finally:
                existing["depth"] -= 1
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            _lock_file(handle)
            held[key] = {"depth": 1, "handle": handle}
            try:
                yield
            finally:
                held.pop(key, None)
                _unlock_file(handle)
        finally:
            handle.close()


def _issue_thread_lock(key: str) -> threading.RLock:
    with _ISSUE_LOCKS_GUARD:
        return _ISSUE_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _issue_operation_lock(cfg: "ReviewConfig", issue_num: int):
    """Reentrant keyed lock for one issue's mutations and irreversible SHIP."""
    lock_path = _issue_lock_path(cfg, issue_num)
    key = str(lock_path.resolve())
    with _issue_thread_lock(key):
        held = getattr(_ISSUE_LOCK_LOCAL, "held", None)
        if held is None:
            held = {}
            _ISSUE_LOCK_LOCAL.held = held
        existing = held.get(key)
        if existing is not None:
            existing["depth"] += 1
            try:
                yield
            finally:
                existing["depth"] -= 1
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            _lock_file(handle)
            held[key] = {"depth": 1, "handle": handle}
            try:
                yield
            finally:
                held.pop(key, None)
                _unlock_file(handle)
        finally:
            handle.close()


def _load_ship_receipt(cfg: "ReviewConfig", issue_num: int) -> Optional[dict]:
    path = _ship_receipt_path(cfg, issue_num)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("response"), dict) else None


def _save_ship_receipt(
    cfg: "ReviewConfig",
    issue_num: int,
    response: dict,
    *,
    review_token: Optional[str] = None,
) -> None:
    """Persist a generation-bound idempotency receipt under the issue lock."""
    path = _ship_receipt_path(cfg, issue_num)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "issue": issue_num,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "review_token": review_token,
        "response": response,
    }
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _clear_ship_receipt(cfg: "ReviewConfig", issue_num: int) -> None:
    """A new human mutation starts a fresh shippable review generation."""
    try:
        _ship_receipt_path(cfg, issue_num).unlink()
    except FileNotFoundError:
        pass


def _load_store_unlocked(cfg: "ReviewConfig") -> dict:
    """The whole subdeck store ``{num(str): {decl: {...}}}``, or ``{}``. A
    missing/corrupt file is "nothing recorded", never an error."""
    path = _store_path(cfg)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only well-shaped issue → decl → record entries.
    out: dict = {}
    for num, decls in data.items():
        if isinstance(decls, dict):
            out[str(num)] = {
                d: rec for d, rec in decls.items() if isinstance(rec, dict)
            }
    return out


def _load_store(cfg: "ReviewConfig") -> dict:
    """Read the current atomic snapshot.

    Readers do not need the writer lock: replacement means they observe either
    the complete old file or the complete new file, never a partial write.
    """
    return _load_store_unlocked(cfg)


def _save_store_unlocked(cfg: "ReviewConfig", store: dict) -> None:
    """Persist the store atomically (tmp write + replace). Lives in the
    same ``.marathon/review`` dir as ``state.json`` / ``deck-defers.json``
    (the gitignored ``.marathon`` convention), created on first write.

    Caller MUST hold :func:`_store_mutation_lock`.
    """
    path = _store_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _mutate_store(
    cfg: "ReviewConfig",
    mutation: Callable[[dict], tuple[_MutationResult, bool]],
) -> _MutationResult:
    """Run one locked read-modify-write transaction.

    ``mutation`` receives the latest whole store and returns ``(result,
    changed)``. When changed, the same transaction writes via atomic replace
    before releasing either lock. This seam prevents callers from accidentally
    taking separate load/save locks and recreating a lost-update window.
    """
    with _store_mutation_lock(cfg):
        store = _load_store_unlocked(cfg)
        result, changed = mutation(store)
        if changed:
            _save_store_unlocked(cfg, store)
        return result


def _save_store(cfg: "ReviewConfig", store: dict) -> None:
    """Replace the complete store under the mutation lock (test/setup helper)."""
    replacement = dict(store)

    def replace(current: dict) -> tuple[None, bool]:
        current.clear()
        current.update(replacement)
        return None, True

    _mutate_store(cfg, replace)


def issue_verdicts(cfg: "ReviewConfig", issue_num: int) -> dict:
    """``{decl_name: {"verdict", "note", "ts"}}`` for one issue, or ``{}``.
    Read-only; the per-decl LOCAL review state the subdeck overlays."""
    return _load_store(cfg).get(str(issue_num), {})


def _current_decl_cards(
    cfg: "ReviewConfig",
    issue_num: int,
    *,
    current_subdeck: Optional[dict] = None,
) -> dict[str, dict]:
    """Return the current code-backed declaration cards for one issue.

    Store-only callers used by migrations/tests may target an issue outside the
    configured registry.  Such a target has no authoritative review surface,
    so avoid a GitHub/audit lookup and return an empty mapping. Production deck
    handlers pass ``require_current=True`` and therefore fail closed.
    """
    if cfg.chapter_of_issue(issue_num) is None:
        return {}
    subdeck = (
        current_subdeck
        if current_subdeck is not None
        else build_subdeck(cfg, issue_num, skip_api=False)
    )
    cards = [
        {
            **card,
            "_issue_inspectable": bool(subdeck.get("inspectable")),
            "_inspection_blocked_reason": subdeck.get(
                "inspection_blocked_reason"
            ),
        }
        for card in (
            list(subdeck.get("cards") or [])
            + list(subdeck.get("auto_approved") or [])
        )
        if isinstance(card.get("name"), str)
    ]
    counts: dict[str, int] = {}
    for card in cards:
        counts[card["name"]] = counts.get(card["name"], 0) + 1
    return {
        card["name"]: card
        for card in cards
        if counts.get(card["name"]) == 1
    }


def _current_decl_card(
    cfg: "ReviewConfig",
    issue_num: int,
    decl: str,
    *,
    current_subdeck: Optional[dict] = None,
) -> Optional[dict]:
    """Return the unique current code-backed card for ``decl``, if present."""
    return _current_decl_cards(
        cfg, issue_num, current_subdeck=current_subdeck
    ).get(decl)


def _bound_context_fingerprint(
    cfg: "ReviewConfig",
    issue_num: int,
    decl: str,
    *,
    expected_fingerprint: Optional[str],
    require_current: bool,
    require_inspectable: bool = False,
    current_subdeck: Optional[dict] = None,
) -> Optional[str]:
    """Resolve and optionally compare the declaration's current fingerprint."""
    if require_current and expected_fingerprint is None:
        raise ReviewContextChanged(
            "This review request is missing the declaration context that was "
            "shown. Reload the review and decide again."
        )
    if (
        current_subdeck is None
        and not require_current
        and expected_fingerprint is None
        and not require_inspectable
    ):
        return None
    try:
        card = _current_decl_card(
            cfg,
            issue_num,
            decl,
            current_subdeck=current_subdeck,
        )
    except Exception:
        if require_current or expected_fingerprint is not None:
            raise
        return None
    current = _decl_fingerprint(card) if card is not None else None
    if expected_fingerprint is not None and current != expected_fingerprint:
        raise ReviewContextChanged(
            "The declaration or one of its elaborated dependencies changed "
            "after it was shown. Reload the review and decide again."
        )
    if require_current and current is None:
        raise ReviewContextChanged(
            f"{decl!r} is not a unique declaration in the current code-backed "
            f"subdeck for issue #{issue_num}; reload the review."
        )
    if require_inspectable and card is not None and not card.get(
        "_issue_inspectable"
    ):
        raise ReviewContextChanged(
            str(
                card.get("_inspection_blocked_reason")
                or "This issue does not currently have machine-audited code "
                "that is eligible for review."
            )
        )
    return current


def reconcile_decl_verdict_ownership(
    cfg: "ReviewConfig",
    issue_num: int,
    *,
    renames: list[dict],
    splits: list[dict],
    additions: list[str],
) -> dict:
    """Atomically reconcile local verdict keys after a successful body edit.

    A confirmed one-to-one rename moves an old record to the exact new key but
    clears its active verdict/lock/fingerprint: human confirmation establishes
    declaration identity, not that the changed declaration remains correct.
    The new card is therefore owned and explicitly marked for re-review.  If the
    target already has an independently reviewed record, it wins and the old
    record is archived instead of overwritten.

    Splits never guess how one parent verdict applies to multiple children.  Any
    parent record is removed from the active keyspace and archived with the
    exact target list.  Additions already use their current declaration key, so
    an existing surfaced-card record is preserved byte-for-byte.
    """
    normalized_renames = [
        {"old": item.get("old"), "new": item.get("new")}
        for item in renames
        if isinstance(item, dict)
        and isinstance(item.get("old"), str)
        and isinstance(item.get("new"), str)
        and item.get("old") != item.get("new")
    ]
    normalized_splits = [
        {"old": item.get("old"), "targets": sorted(item.get("targets") or [])}
        for item in splits
        if isinstance(item, dict)
        and isinstance(item.get("old"), str)
        and all(isinstance(target, str) for target in (item.get("targets") or []))
        and item.get("targets")
    ]
    normalized_additions = sorted({
        name for name in additions if isinstance(name, str) and name
    })

    def mutate(store: dict) -> tuple[dict, bool]:
        decls = store.get(str(issue_num))
        if not isinstance(decls, dict):
            return {
                "migrated": [], "collisions": [], "split_archived": [],
                "additions_preserved": [],
            }, False
        history_slot = decls.get(RECONCILIATION_HISTORY_KEY) or {}
        old_events = history_slot.get("events") or []
        events = [event for event in old_events if isinstance(event, dict)]
        migrated: list[dict] = []
        collisions: list[dict] = []
        split_archived: list[dict] = []
        changed = False

        def archive(event: dict) -> None:
            nonlocal changed
            if event not in events:
                events.append(event)
                changed = True

        for mapping in normalized_renames:
            old, new = mapping["old"], mapping["new"]
            previous = decls.pop(old, None)
            if not isinstance(previous, dict):
                continue
            changed = True
            event = {
                "kind": "rename", "old": old, "new": new,
                "previous_record": previous,
            }
            if new in decls:
                collisions.append(dict(mapping))
                event["target_record_preserved"] = True
                archive(event)
                continue
            decls[new] = {
                "requires_review": True,
                "renamed_from": old,
                "previous_record": previous,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            migrated.append(dict(mapping))
            archive(event)

        for mapping in normalized_splits:
            old = mapping["old"]
            previous = decls.pop(old, None)
            if not isinstance(previous, dict):
                continue
            changed = True
            event = {
                "kind": "split", "old": old,
                "targets": list(mapping["targets"]),
                "previous_record": previous,
            }
            archive(event)
            split_archived.append(dict(mapping))

        if events:
            decls[RECONCILIATION_HISTORY_KEY] = {"events": events}
        additions_preserved = [name for name in normalized_additions if name in decls]
        return {
            "migrated": migrated,
            "collisions": collisions,
            "split_archived": split_archived,
            "additions_preserved": additions_preserved,
        }, changed

    with _issue_operation_lock(cfg, issue_num):
        _clear_ship_receipt(cfg, issue_num)
        return _mutate_store(cfg, mutate)


def record_decl_verdict(
    cfg: "ReviewConfig",
    issue_num: int,
    decl: str,
    verdict: str,
    note: Optional[str] = None,
    *,
    expected_fingerprint: Optional[str] = None,
    require_current: bool = False,
    return_subdeck: bool = False,
):
    """Record one per-(issue,decl) verdict to the local sidecar and return
    the stored record.

    ``verdict`` must be one of :data:`DECL_VERDICTS`. A ``reject`` REQUIRES a
    non-empty ``note`` (it composes into the verbatim ship note that reaches
    Aristotle) — :class:`ValueError` otherwise. A ``verify`` / ``defer`` note
    is optional and preserved if given. Idempotent on (issue, decl): a later
    verdict for the same declaration supersedes the earlier one (the human
    changed their mind).

    LOCAL ONLY: this writes the JSON sidecar and nothing else — no GitHub, no
    Aristotle, no ledger verdict. The irreversible step is :func:`ship_issue`.
    """
    if verdict not in DECL_VERDICTS:
        raise ValueError(
            f"unknown decl verdict {verdict!r}; expected one of {DECL_VERDICTS}"
        )
    if verdict == "reject" and (note is None or not str(note).strip()):
        raise ValueError(
            "a reject requires a non-empty note (it composes into the ship "
            "note carried verbatim to Aristotle)"
        )

    def mutate(
        store: dict,
        fingerprint: Optional[str],
        basis: Optional[dict] = None,
    ) -> tuple[dict, bool]:
        decls = store.setdefault(str(issue_num), {})
        prev = decls.get(decl) or {}
        entry = {
            "verdict": verdict,
            "note": str(note) if note is not None else None,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        # Every verdict accepted from the live code-backed deck is pinned to
        # the exact declaration + elaborated kernel the reviewer saw.  A
        # subsequent source, statement, definition-value, or kernel change
        # makes build_subdeck ignore it until the human decides again.
        if fingerprint is not None:
            entry["fingerprint"] = fingerprint
            entry["fingerprint_version"] = DECL_FINGERPRINT_VERSION
        # Keep the reviewed-at text beside the pin: when a later change masks
        # this verdict as "changed", the deck can show WHAT changed and offer
        # a one-key re-affirm instead of an unexplained full re-review.
        if fingerprint is not None and basis is not None:
            entry["basis"] = basis
        # The hard-freeze LOCK is a modifier on a verify: carry it over on a
        # re-verify, but DROP it when the verdict changes to reject/defer (a decl
        # the human now wants changed must not be frozen).
        if verdict == "verify" and prev.get("locked"):
            entry["locked"] = True
        decls[decl] = entry
        return entry, True

    with _issue_operation_lock(cfg, issue_num):
        inputs = None
        current_subdeck = None
        if (
            (
                decl != INFORMAL_KEY
                or require_current
                or expected_fingerprint is not None
                or return_subdeck
            )
            and cfg.chapter_of_issue(issue_num) is not None
        ):
            try:
                inputs = _load_subdeck_inputs(cfg, issue_num)
                current_subdeck = build_subdeck(
                    cfg, issue_num, skip_api=False, _inputs=inputs
                )
            except Exception:
                if require_current or expected_fingerprint is not None:
                    raise
        if decl == INFORMAL_KEY and (
            require_current or expected_fingerprint is not None
        ):
            informal = (
                (current_subdeck or {}).get("informal") or {}
            )
            current_fingerprint = informal.get("context_fingerprint")
            if expected_fingerprint is None:
                raise ReviewContextChanged(
                    "This informal-text review is missing the exact prose that "
                    "was shown. Reload the review and decide again."
                )
            if (
                current_fingerprint is None
                or expected_fingerprint != current_fingerprint
            ):
                raise ReviewContextChanged(
                    "The informal text changed after it was shown. Reload the "
                    "review and decide again."
                )
            fingerprint = current_fingerprint
        elif decl == INFORMAL_KEY:
            # Store-only compatibility for migrations/tests. The unbound record
            # is deliberately ignored by _informal_slot until reviewed live.
            fingerprint = None
        else:
            fingerprint = _bound_context_fingerprint(
                cfg,
                issue_num,
                decl,
                expected_fingerprint=expected_fingerprint,
                require_current=require_current,
                require_inspectable=(
                    require_current and verdict in REVIEWED_DECL_VERDICTS
                ),
                current_subdeck=(
                    current_subdeck
                    if current_subdeck is not None
                    else ({} if require_current else None)
                ),
            )
        basis: Optional[dict] = None
        if (
            fingerprint is not None
            and decl != INFORMAL_KEY
            and current_subdeck is not None
        ):
            try:
                basis_card = _current_decl_card(
                    cfg, issue_num, decl, current_subdeck=current_subdeck
                )
            except Exception:  # noqa: BLE001 — basis is best-effort context
                basis_card = None
            if basis_card is not None:
                basis = {
                    "source": basis_card.get("source_block"),
                    "statement": basis_card.get("statement_pp"),
                }
        _clear_ship_receipt(cfg, issue_num)
        stored = _mutate_store(
            cfg, lambda store: mutate(store, fingerprint, basis)
        )
        if not return_subdeck:
            return stored
        if inputs is None:
            inputs = _load_subdeck_inputs(cfg, issue_num)
        refreshed = build_subdeck(
            cfg, issue_num, skip_api=False, _inputs=inputs
        )
        return stored, refreshed


def record_decl_verdict_batch(
    cfg: "ReviewConfig",
    items: list[dict],
    *,
    require_current: bool = False,
) -> list[dict]:
    """Atomically record several LOCAL per-declaration verdicts.

    The file/chapter reader uses this for deliberate batch KEEP/DEFER actions.
    Shape validation is performed for the complete request before any write;
    duplicate ``(issue, decl)`` targets are refused.  All affected issue locks
    are then acquired in numeric order and the whole sidecar is replaced in one
    store transaction, so a failure cannot leave a half-applied batch.

    Like :func:`record_decl_verdict`, this is LOCAL ONLY: no GitHub, git,
    Aristotle, or committed issue verdict is touched.  Reject notes remain
    verbatim and are required for every batched reject.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("batch must contain at least one declaration verdict")

    normalized: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("each batch item must be an object")
        try:
            issue_num = int(raw.get("issue"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"bad or missing issue {raw.get('issue')!r}") from exc
        decl = raw.get("decl")
        verdict = raw.get("verdict")
        note = raw.get("note")
        if not isinstance(decl, str) or not decl.strip():
            raise ValueError(f"bad or missing decl {decl!r}")
        decl = decl.strip()
        if verdict not in DECL_VERDICTS:
            raise ValueError(
                f"unknown decl verdict {verdict!r}; expected one of {DECL_VERDICTS}"
            )
        if verdict == "reject" and (note is None or not str(note).strip()):
            raise ValueError(
                f"a reject for {decl} requires a non-empty note carried to Aristotle"
            )
        key = (issue_num, decl)
        if key in seen:
            raise ValueError(
                f"duplicate declaration target in batch: issue #{issue_num} {decl}"
            )
        seen.add(key)
        normalized.append({
            "issue": issue_num,
            "decl": decl,
            "verdict": verdict,
            "note": str(note) if note is not None else None,
            "expected_fingerprint": raw.get("expected_fingerprint"),
        })

    issues = sorted({item["issue"] for item in normalized})
    timestamp = datetime.now(timezone.utc).isoformat()

    def mutate(
        store: dict, fingerprints: dict[tuple[int, str], Optional[str]]
    ) -> tuple[list[dict], bool]:
        results: list[dict] = []
        for item in normalized:
            decls = store.setdefault(str(item["issue"]), {})
            previous = decls.get(item["decl"]) or {}
            entry = {
                "verdict": item["verdict"],
                "note": item["note"],
                "ts": timestamp,
            }
            fingerprint = fingerprints[(item["issue"], item["decl"])]
            if fingerprint is not None:
                entry["fingerprint"] = fingerprint
                entry["fingerprint_version"] = DECL_FINGERPRINT_VERSION
            if item["verdict"] == "verify" and previous.get("locked"):
                entry["locked"] = True
            decls[item["decl"]] = entry
            results.append({
                "issue": item["issue"],
                "decl": item["decl"],
                "verdict": item["verdict"],
                "note": item["note"],
                "record": dict(entry),
            })
        return results, True

    # Numeric lock order prevents two overlapping multi-issue batches from
    # deadlocking each other.  The shared store transaction is acquired only
    # after every issue lock, matching single-issue mutation ordering.
    with ExitStack() as stack:
        for issue_num in issues:
            stack.enter_context(_issue_operation_lock(cfg, issue_num))
        # One authoritative subdeck build per issue, not per declaration: a
        # chapter-sized DEFER batch may contain hundreds of cards.
        current_cards: dict[int, dict[str, dict]] = {}
        for issue_num in issues:
            try:
                current_cards[issue_num] = _current_decl_cards(
                    cfg, issue_num
                )
            except Exception:
                if require_current or any(
                    item.get("expected_fingerprint") is not None
                    for item in normalized
                    if item["issue"] == issue_num
                ):
                    raise
                current_cards[issue_num] = {}
        fingerprints: dict[tuple[int, str], Optional[str]] = {}
        for item in normalized:
            card = current_cards[item["issue"]].get(item["decl"])
            current = _decl_fingerprint(card) if card is not None else None
            expected = item.get("expected_fingerprint")
            if expected is not None and expected != current:
                raise ReviewContextChanged(
                    "A declaration or one of its elaborated dependencies "
                    "changed after the batch was shown. Reload the review."
                )
            if require_current and current is None:
                raise ReviewContextChanged(
                    f"{item['decl']!r} is not a unique current code-backed "
                    f"declaration for issue #{item['issue']}."
                )
            fingerprints[(item["issue"], item["decl"])] = current
        for issue_num in issues:
            _clear_ship_receipt(cfg, issue_num)
        return _mutate_store(
            cfg, lambda store: mutate(store, fingerprints)
        )


def set_decl_lock(
    cfg: "ReviewConfig",
    issue_num: int,
    decl: str,
    locked: bool,
    *,
    expected_fingerprint: Optional[str] = None,
    require_current: bool = False,
    return_subdeck: bool = False,
):
    """Toggle the HARD-FREEZE lock on a declaration (opt-in, per-decl).

    A LOCKED decl's signature is frozen into the approved-statements manifest at
    reject-ship, so no sibling-decl refine can change it (the hard guard reverts
    any attempt). An UNLOCKED (soft) verified decl stays rewritable — Aristotle
    may re-type it and the change re-surfaces for review. Locking implies KEEP,
    so an unreviewed decl is recorded ``verify`` when first locked. LOCAL ONLY —
    the sidecar; the freeze is realized at :func:`ship_issue`."""
    def mutate(
        store: dict, fingerprint: Optional[str]
    ) -> tuple[dict, bool]:
        decls = store.setdefault(str(issue_num), {})
        entry = decls.get(decl)
        if (
            not locked
            and entry is not None
            and fingerprint is not None
            and entry.get("fingerprint") is None
        ):
            raise ReviewContextChanged(
                "This legacy decision was not bound to the current "
                "declaration. Review it again before changing its lock."
            )
        if (
            entry is not None
            and fingerprint is not None
            and entry.get("fingerprint") not in (None, fingerprint)
        ):
            raise ReviewContextChanged(
                "The declaration changed since this decision was recorded. "
                "Reload and review it again before changing its lock."
            )
        if entry is None or entry.get("verdict") not in (
            "verify", "reject", "defer"
        ):
            entry = {
                "verdict": "verify",
                "note": None,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            decls[decl] = entry
        if fingerprint is not None:
            entry["fingerprint"] = fingerprint
            entry["fingerprint_version"] = DECL_FINGERPRINT_VERSION
        if locked:
            entry["locked"] = True
        else:
            entry.pop("locked", None)
        return entry, True

    with _issue_operation_lock(cfg, issue_num):
        inputs = None
        current_subdeck = None
        if cfg.chapter_of_issue(issue_num) is not None:
            try:
                inputs = _load_subdeck_inputs(cfg, issue_num)
                current_subdeck = build_subdeck(
                    cfg, issue_num, skip_api=False, _inputs=inputs
                )
            except Exception:
                if require_current or expected_fingerprint is not None:
                    raise
        fingerprint = _bound_context_fingerprint(
            cfg,
            issue_num,
            decl,
            expected_fingerprint=expected_fingerprint,
            require_current=require_current,
            require_inspectable=require_current,
            current_subdeck=(
                current_subdeck
                if current_subdeck is not None
                else ({} if require_current else None)
            ),
        )
        _clear_ship_receipt(cfg, issue_num)
        stored = _mutate_store(
            cfg, lambda store: mutate(store, fingerprint)
        )
        if not return_subdeck:
            return stored
        if inputs is None:
            inputs = _load_subdeck_inputs(cfg, issue_num)
        refreshed = build_subdeck(
            cfg, issue_num, skip_api=False, _inputs=inputs
        )
        return stored, refreshed


def record_missing(
    cfg: "ReviewConfig", issue_num: int, note: Optional[str]
) -> Optional[dict]:
    """Record (or CLEAR) the issue's "something is MISSING" note — a per-issue
    instruction that the issue is incomplete (a result the title promises has no
    declaration). A non-empty ``note`` stores it under :data:`MISSING_KEY`; an
    empty/None ``note`` clears the flag. Returns the stored record (or None when
    cleared). LOCAL ONLY — writes the sidecar; the irreversible step is
    :func:`ship_issue`, where a present note routes the outcome to Aristotle."""
    text = (note or "").strip()

    def mutate(store: dict) -> tuple[Optional[dict], bool]:
        decls = store.setdefault(str(issue_num), {})
        if not text:
            decls.pop(MISSING_KEY, None)
            if not decls:
                store.pop(str(issue_num), None)
            return None, True
        entry = {
            "verdict": "missing",
            "note": text,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        decls[MISSING_KEY] = entry
        return entry, True

    with _issue_operation_lock(cfg, issue_num):
        _clear_ship_receipt(cfg, issue_num)
        return _mutate_store(cfg, mutate)


def clear_issue(cfg: "ReviewConfig", issue_num: int) -> None:
    """Drop an issue's whole subdeck store (called after a successful VERIFY
    ship so a finished issue's local review state never lingers). No-op when
    absent."""
    def mutate(store: dict) -> tuple[None, bool]:
        if str(issue_num) not in store:
            return None, False
        store.pop(str(issue_num))
        return None, True

    _mutate_store(cfg, mutate)


def _persist_kept_verdicts(
    cfg: "ReviewConfig", issue_num: int, subdeck: dict, kept_names: list
) -> list[str]:
    """After a REJECT ship, REWRITE the issue's store to keep the human KEPT
    (verified) decls — each pinned to its CURRENT source
    :func:`_decl_fingerprint` — plus DEFER records (their notes are stored
    reviewer context, re-flagged as carried over) and the informal approval;
    rejected decls (the ones Aristotle will fix) and not-yet-reviewed decls
    are dropped and return for review. So when the refined issue returns, the
    kept decls are already verified (no re-review) UNLESS their source changed
    (then the fingerprint mismatch re-opens them). Returns the VERIFIED decl
    names carried over. The store is wiped if nothing survives."""
    card_by_name = {
        c.get("name"): c
        for c in [
            *subdeck.get("cards", []),
            *subdeck.get("auto_approved", []),
        ]
    }
    informal_slot = subdeck.get("informal") or {}

    def mutate(store: dict) -> tuple[list[str], bool]:
        old = store.get(str(issue_num), {})
        new_entry: dict = {}
        carried: list[str] = []
        for name in kept_names:
            rec = old.get(name)
            if not rec or rec.get("verdict") != "verify":
                continue
            card = card_by_name.get(name)
            carried_entry = {
                "verdict": "verify",
                "note": rec.get("note"),
                "ts": rec.get("ts"),
                "fingerprint": _decl_fingerprint(card) if card else None,
                "fingerprint_version": DECL_FINGERPRINT_VERSION,
                "carried_over": True,
            }
            if card is not None:
                carried_entry["basis"] = {
                    "source": card.get("source_block"),
                    "statement": card.get("statement_pp"),
                }
            elif rec.get("basis"):
                carried_entry["basis"] = rec.get("basis")
            if rec.get("locked"):
                carried_entry["locked"] = True
            new_entry[name] = carried_entry
            carried.append(name)
        # DEFER records survive the ship too: a defer note ("skipped because…")
        # is exactly the context the reviewer needs when the declaration
        # returns, and silently discarding it destroys stored work. The decl
        # still counts as unreviewed (defer never satisfies completeness) and
        # the carried_over flag re-surfaces it as a previous deliberate skip,
        # re-opened by the fingerprint mask if its source changed.
        for name, rec in old.items():
            if name == INFORMAL_KEY or name in new_entry:
                continue
            if not isinstance(rec, dict) or rec.get("verdict") != "defer":
                continue
            card = card_by_name.get(name)
            carried_defer = {
                "verdict": "defer",
                "note": rec.get("note"),
                "ts": rec.get("ts"),
                "fingerprint": (
                    _decl_fingerprint(card) if card else rec.get("fingerprint")
                ),
                "fingerprint_version": DECL_FINGERPRINT_VERSION,
                "carried_over": True,
            }
            if card is not None:
                carried_defer["basis"] = {
                    "source": card.get("source_block"),
                    "statement": card.get("statement_pp"),
                }
            elif rec.get("basis"):
                carried_defer["basis"] = rec.get("basis")
            new_entry[name] = carried_defer
        # The informal approval is issue-level (the body marker was flipped at
        # ship) — keep it so the human is not re-asked to approve the same text.
        informal_rec = old.get(INFORMAL_KEY) or {}
        if (
            informal_slot.get("verdict") in {"verified", "rejected"}
            and informal_rec.get("fingerprint")
            == informal_slot.get("context_fingerprint")
        ):
            new_entry[INFORMAL_KEY] = informal_rec
        if new_entry:
            store[str(issue_num)] = new_entry
        else:
            store.pop(str(issue_num), None)
        return carried, True

    return _mutate_store(cfg, mutate)


# ---------------------------------------------------------------------------
# Subdeck assembly (reuses cards.build_card_detail — never forked)
# ---------------------------------------------------------------------------


def _decl_fingerprint(view_or_card) -> str:
    """Hash a declaration plus the elaborated dependency context it relies on.

    Source text alone is insufficient: a theorem can remain byte-for-byte
    unchanged while an upstream definition changes meaning. Include the
    elaborated statement and stable kernel member identity/type/value so such a
    dependency change invalidates a carried approval. Accepts a DeclView object
    or its serialized card and produces the same canonical hash for either.
    """
    import hashlib

    def field(obj, name):
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

    kernel = []
    for member in field(view_or_card, "kernel") or []:
        kernel.append({
            "name": field(member, "name") or "",
            "kind": field(member, "kind") or "",
            "type_pp": field(member, "type_pp"),
            "value_pp": field(member, "value_pp"),
        })
    kernel.sort(key=lambda item: (
        item["name"], item["kind"], item["type_pp"] or "", item["value_pp"] or ""
    ))
    basis = {
        "name": field(view_or_card, "name") or "",
        "kind": field(view_or_card, "kind") or "",
        "source_block": field(view_or_card, "source_block"),
        # Live DeclViews carry a digest of the complete declaration even when
        # the browser source block is capped at 400 lines.  Legacy/test dicts
        # without it remain bound to their complete ``source_block`` value.
        "source_fingerprint": field(
            view_or_card, "source_fingerprint"
        ),
        "statement_pp": field(view_or_card, "statement_pp"),
        "kernel": kernel,
    }
    encoded = json.dumps(
        basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decl_name_matches(left: str, right: str) -> bool:
    """Conservative qualified/unqualified declaration-name equivalence."""
    return (
        left == right
        or left.endswith("." + right)
        or right.endswith("." + left)
    )


def _depends_on_any(view_or_card, declarations: set[str]) -> bool:
    """Whether a card's transitive audit kernel contains any named decl."""
    if not declarations:
        return False
    if isinstance(view_or_card, dict):
        kernel = view_or_card.get("kernel") or []
    else:
        kernel = getattr(view_or_card, "kernel", None) or []
    for member in kernel:
        name = member.get("name") if isinstance(member, dict) else member.name
        if name and any(_decl_name_matches(name, rejected) for rejected in declarations):
            return True
    return False


def _decl_card(view, record: Optional[dict], carry: Optional[str] = None) -> dict:
    """One subdeck card: a :class:`marathon.deck.cards.DeclView`'s JSON face
    plus the overlaid LOCAL verdict/note from the store (``None`` when the
    declaration has not been reviewed yet).

    The verdict is mapped to the PAST-TENSE wire form (:data:`_VERDICT_WIRE`)
    the frontend renders/styles — the stored sidecar stays present-tense (the
    ship composition keys off it); only this serialized face is mapped so the
    GET /api/subdeck seam matches what ``app.js`` consumes.

    ``carry`` flags a verdict CARRIED OVER from a prior ship: ``"carried_over"``
    (the decl was approved before and is unchanged — pre-verified, no re-review)
    or ``"changed"`` (approved before but its source changed — the verdict was
    dropped by the caller, so this is shown unreviewed with a re-review flag)."""
    card = view.to_json()
    card["context_fingerprint"] = _decl_fingerprint(view)
    stored = (record or {}).get("verdict")
    card["verdict"] = _VERDICT_WIRE.get(stored, stored)
    card["note"] = (record or {}).get("note")
    card["locked"] = bool((record or {}).get("locked"))
    card["carried_over"] = carry == "carried_over"
    card["changed_since_review"] = carry == "changed"
    card["dependency_invalidated"] = carry == "dependency_invalidated"
    return card


def _is_reviewed(record: Optional[dict]) -> bool:
    """True iff this declaration carries a verify/reject verdict (a defer or
    no-verdict leaves it unreviewed — the contract's progress rule)."""
    return bool(record) and record.get("verdict") in REVIEWED_DECL_VERDICTS


def _informal_fingerprint(statement: str, is_llm: bool) -> str:
    """Bind one informal approval to the exact normalized prose shown."""
    normalized = str(statement).replace("\r\n", "\n").replace("\r", "\n").strip()
    encoded = json.dumps(
        {"statement": normalized, "is_llm": bool(is_llm)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _informal_slot(detail, records: dict) -> Optional[dict]:
    """The subdeck's ONE TOP-LEVEL informal statement (the issue's
    ``### Informal Statement`` — the textbook result, e.g. "Proposition 12.19")
    plus the human's single APPROVAL verdict over it. This is the ONLY text the
    leading approval card shows — NOT the per-declaration renderings (the human
    reviews the result once at the issue level, never decl-by-decl).

    Prefers the issue body's informal statement (``detail.issue_informal``);
    falls back to the headline declaration's LLM rendering when the body has no
    such section. Returns ``None`` when there is no top-level informal text at
    all — there is then nothing to approve (no approval card). ``is_llm`` is
    True iff that text is LLM-rendered and still PENDING human verification.

    The verdict lives OUTSIDE ``cards`` (keyed by the reserved
    :data:`INFORMAL_KEY`, never a real Lean name), so it never matches a view
    and is invisible to the cards / progress / completeness / summary / ship
    composition. It is advisory: approving clears the deck's LLM-rendered
    warning and, at ship, flips the issue body's informal marker."""
    statement = detail.issue_informal
    is_llm = detail.issue_informal_is_llm
    if not statement:
        # Fall back to the headline declaration's rendering when the body has no
        # Informal Statement section (so there is still ONE text to approve).
        head = detail.decls[0] if detail.decls else None
        if head is not None and head.informal_is_llm and head.informal_rendering:
            statement, is_llm = head.informal_rendering, True
    if not statement:
        return None
    context_fingerprint = _informal_fingerprint(statement, bool(is_llm))
    rec = records.get(INFORMAL_KEY) or {}
    if rec.get("fingerprint") != context_fingerprint:
        # Legacy/unbound approval or changed prose: fail closed and ask again.
        rec = {}
    stored = rec.get("verdict")
    return {
        "is_llm": bool(is_llm),
        "verdict": _VERDICT_WIRE.get(stored, stored),
        "note": rec.get("note"),
        "statement": statement,
        "context_fingerprint": context_fingerprint,
    }


def subdeck_review_token(subdeck: dict) -> str:
    """Fingerprint the exact code/issue surface presented to the reviewer.

    The token binds both the code/issue surface and the effective LOCAL decisions
    visible in this exact subdeck.  That second half is load-bearing: a second
    browser tab may change a KEEP/FIX note after the first tab rendered its
    summary, and the first tab must not silently ship the newer decisions.  The
    worker operation id additionally fences mid-flight mutations, but only this
    submitted token proves which decisions the human actually confirmed.
    """
    cards = []
    for lane in ("cards", "auto_approved"):
        for card in subdeck.get(lane) or []:
            cards.append({
                "name": card.get("name"),
                "fingerprint": _decl_fingerprint(card),
                "surfaced": bool(card.get("surfaced")),
                "api": bool(card.get("api")),
                "verdict": card.get("verdict"),
                "note": card.get("note"),
                "locked": bool(card.get("locked")),
                "carried_over": bool(card.get("carried_over")),
                "changed_since_review": bool(
                    card.get("changed_since_review")
                ),
                "dependency_invalidated": bool(
                    card.get("dependency_invalidated")
                ),
            })
    # ``skip_api`` changes only which presentation lane contains an untouched
    # API card. The explicit ship body/operation id binds that choice separately,
    # so the review token stays canonical across mutation responses (which
    # rebuild the full, non-skip presentation) and the browser's skip-API view.
    cards.sort(key=lambda card: str(card.get("name") or ""))
    payload = {
        "version": 2,
        "issue": subdeck.get("issue"),
        "title": subdeck.get("title"),
        "chapter": subdeck.get("chapter"),
        "cards": cards,
        "stale": sorted(
            str(item.get("name") if isinstance(item, dict) else item)
            for item in (subdeck.get("stale_citations") or [])
        ),
        "reconcile": subdeck.get("reconcile"),
        "deps": sorted(
            int(dep["id"])
            for dep in (subdeck.get("deps") or [])
            if isinstance(dep, dict) and isinstance(dep.get("id"), int)
        ),
        "informal": {
            "statement": (subdeck.get("informal") or {}).get("statement"),
            "is_llm": bool((subdeck.get("informal") or {}).get("is_llm")),
            "verdict": (subdeck.get("informal") or {}).get("verdict"),
            "note": (subdeck.get("informal") or {}).get("note"),
        },
        "missing": subdeck.get("missing"),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_subdeck_inputs(
    cfg: "ReviewConfig", issue_num: int
) -> tuple[object, list]:
    """Load the GitHub/audit-backed half of a subdeck exactly once.

    A declaration mutation needs the same immutable review surface both before
    and after its local sidecar write.  Keeping these inputs separate from the
    cheap verdict overlay avoids repeating GitHub metadata and source
    reconciliation fetches on every swipe.
    """
    from marathon.deck.cards import build_card_detail_cached

    detail = build_card_detail_cached(cfg, issue_num)
    return detail, list(detail.surfaced_decls)


def build_subdeck(
    cfg: "ReviewConfig",
    issue_num: int,
    *,
    skip_api: bool = False,
    _inputs: Optional[tuple[object, list]] = None,
) -> dict:
    """Assemble the per-declaration subdeck for ``issue_num`` (the shared API
    contract).

    Reuses :func:`marathon.deck.cards.build_card_detail` for the issue's
    per-declaration views (source / kernel / evidence / tier / informal
    rendering, plus the ``api``/``api_reason``/``in_code`` flags), overlays
    each declaration's LOCAL verdict/note from the store, and orders the cards
    UNREVIEWED-first, then worst-tier, then name (so the next declaration
    needing review leads). Computes:

    * ``card_tier`` — the worst tier over the declarations (the gate);
    * ``cards`` — the reviewable cards (see ``skip_api``);
    * ``auto_approved`` — the API cards that WILL be auto-approved at ship
      (only when ``skip_api``; ``[]`` otherwise);
    * ``progress`` — ``{reviewed, total}`` (deferred does NOT count reviewed);
    * ``complete`` — every reviewable card carries a verify/reject verdict;
    * ``summary`` — ``{kept, rejected:[{name,note}], deferred, invalidated}``;
      ``invalidated`` are previously-verified dependents of a rejected
      declaration, excluded from KEEP until the upstream fix is re-reviewed.

    When ``skip_api`` is truthy this is the SKIP-API view: ``cards`` holds
    ONLY the MAIN-result decls (the human reviews these), ``auto_approved``
    lists the easy-API decls (full DeclCards) that ship will fold into KEEP,
    and progress/complete count over the MAIN cards only. **Pure + read-only**
    — it LISTS what ship would auto-approve but writes nothing and fires no
    verdict (auto-approval is realized only in :func:`ship_issue`).

    When ``skip_api`` is false (the default / current behavior) ``cards`` is
    every decl and ``auto_approved`` is ``[]``.

    RECONCILIATION (PURE — writes NOTHING, never touches GitHub/git-write).
    Before partitioning, the cited DeclViews are reconciled against the CODE so
    the deck matches it, not a stale issue body:

    * a cited decl with ``in_code=false`` (renamed/deleted — absent everywhere)
      is DROPPED from the cards and listed in ``stale_citations`` (a FOOTNOTE,
      never a swipe card);
    * decls found in the code under one of the cited decls' NAMESPACES but not
      cited by the body are SURFACED as cards (``surfaced=true``,
      ``surfaced_reason`` set) so the deck shows the whole result;
    * ``reconcile`` summarizes what changed vs the raw citations:
      ``{dropped:[name], surfaced:[name]}``.

    ``skip_api`` partitions the RECONCILED card set AFTER reconciliation."""
    from marathon.audit.trust import TIER_ORDER

    detail, loaded_surfaced_views = (
        _inputs if _inputs is not None
        else _load_subdeck_inputs(cfg, issue_num)
    )
    records = issue_verdicts(cfg, issue_num)

    # --- RECONCILIATION (pure, read-only) ---------------------------------
    # Partition the cited views by whether they are still in the code. A stale
    # citation (in_code=false) is dropped to the footnote, NOT a card.
    cited_in_code = [v for v in detail.decls if v.in_code]
    stale_views = [v for v in detail.decls if not v.in_code]
    stale_citations = [{"name": v.name} for v in stale_views]
    dropped_names = [v.name for v in stale_views]

    # Surface decls present in the code but not cited by the body (so the deck
    # shows the whole result). Pass ALL cited names — in-code AND stale: the
    # in-code ones seed RENAME recovery (namespace members), while a STALE
    # citation's name seeds SPLIT recovery (its ``<name>_<suffix>`` children,
    # e.g. Prop_16_1 → Prop_16_1_preserving/_reversing) — otherwise a split
    # result whose only citation went stale is left with ZERO reviewable cards.
    # Never re-surfaces a decl the body already cites. PURE — writes nothing.
    surfaced_views = list(loaded_surfaced_views)
    # Defensive: never surface a name that is already a kept cited card.
    _kept_names = {v.name for v in cited_in_code}
    surfaced_views = [v for v in surfaced_views if v.name not in _kept_names]
    surfaced_names = [v.name for v in surfaced_views]

    reconcile = {"dropped": dropped_names, "surfaced": surfaced_names}

    # The reconciled decl set the subdeck reviews: kept cited decls + surfaced
    # decls (stale dropped). This is what the partition below operates on.
    reconciled_views = list(cited_in_code) + list(surfaced_views)

    # Every code-backed verdict carries a context fingerprint and only STANDS
    # while the declaration + elaborated dependency kernel are unchanged.
    # Legacy unbound records are deliberately fail-closed: they are shown as
    # changed and must be decided again.  A record explicitly carried through a
    # reject/refine round additionally receives the ``carried_over`` badge.
    view_by_name = {v.name: v for v in reconciled_views}

    def _effective(name):
        rec = records.get(name)
        if not rec:
            return None, None
        if rec.get("requires_review"):
            return None, "changed"
        if (
            rec.get("fingerprint") is None
            or rec.get("fingerprint_version") != DECL_FINGERPRINT_VERSION
        ):
            return None, "changed"
        view = view_by_name.get(name)
        if view is not None and _decl_fingerprint(view) != rec["fingerprint"]:
            return None, "changed"
        carry = (
            "carried_over"
            if rec.get("carried_over")
            else None
        )
        return rec, carry

    eff = {v.name: _effective(v.name) for v in reconciled_views}

    # Mask a verified declaration when its transitive audit kernel contains a
    # declaration rejected in this same review round. Keeping the stored record
    # untouched lets a changed-mind reject→verify restore it, but every read
    # model treats it as unreviewed until then. This MUST happen before
    # partition/cards/progress so it appears in WAIT and makes the deck partial.
    rejected_effective = {
        view.name for view in reconciled_views
        if (eff[view.name][0] or {}).get("verdict") == "reject"
    }
    dependency_invalidated: list[str] = []
    for view in reconciled_views:
        rec, _carry = eff[view.name]
        if ((rec or {}).get("verdict") == "verify"
                and _depends_on_any(view, rejected_effective)):
            dependency_invalidated.append(view.name)
            eff[view.name] = (None, "dependency_invalidated")

    # Partition the views into MAIN vs API. ANY explicit human verdict pulls an
    # API declaration back into MAIN: verify must not be counted twice, defer
    # must not disappear after reload, and a changed/invalidated carried verdict
    # must be reviewed rather than silently auto-approved. Only declarations
    # with no store record at all are eligible for Skip-API auto-approval.
    main_views = []
    api_views = []
    for view in reconciled_views:
        explicitly_reviewed = records.get(view.name) is not None
        if skip_api and view.api and not explicitly_reviewed:
            api_views.append(view)
        else:
            main_views.append(view)

    def _build_cards(views: list) -> list[dict]:
        return [_decl_card(v, *eff[v.name]) for v in views]

    cards = _build_cards(main_views)
    auto_approved = _build_cards(api_views) if skip_api else []

    # A "changed" mask alone forces a blind re-review. When the invalidated
    # record stored its reviewed-at basis, hand the old text (and the old
    # decision) to the client so it can render a diff and offer a one-key
    # re-affirm via the ordinary decl-verdict endpoint.
    for card in cards:
        if not card.get("changed_since_review"):
            continue
        rec = records.get(card["name"]) or {}
        basis = rec.get("basis") or {}
        stored = rec.get("verdict")
        card["previous_review"] = {
            "verdict": _VERDICT_WIRE.get(stored, stored),
            "note": rec.get("note"),
            "ts": rec.get("ts"),
            "source_block": basis.get("source"),
            "statement_pp": basis.get("statement"),
        }

    # Topological rank over the issue's own declarations: a card is never
    # dealt before one its statement kernel leans on (the design's
    # dependency-order promise — reviewing a lemma before the definition it
    # uses forces the reviewer out of the swipe flow). Kernel membership is
    # the same edge the dependency guard already uses; a cycle degrades to a
    # shared rank rather than wedging the ordering.
    def _topo_ranks(views: list) -> dict[str, int]:
        names = [v.name for v in views]
        name_set = set(names)
        deps: dict[str, set[str]] = {n: set() for n in names}
        for view in views:
            for member in (getattr(view, "kernel", None) or []):
                mname = (
                    member.get("name") if isinstance(member, dict)
                    else getattr(member, "name", None)
                )
                if not mname:
                    continue
                for other in name_set:
                    if other != view.name and _decl_name_matches(mname, other):
                        deps[view.name].add(other)
        ranks: dict[str, int] = {}
        remaining = {n: set(d) for n, d in deps.items()}
        rank = 0
        while remaining:
            pending = set(remaining)
            ready = sorted(
                n for n, d in remaining.items() if not (d & pending)
            )
            if not ready:
                for n in remaining:
                    ranks[n] = rank
                break
            for n in ready:
                ranks[n] = rank
                del remaining[n]
            rank += 1
        return ranks

    topo_rank = _topo_ranks(reconciled_views)

    # Order: UNREVIEWED first (so the next decl to review leads), then in
    # dependency order (predecessors before their dependents), then by worst
    # tier (the gating decl), then by name — a total, deterministic key.
    def _sort_key(card: dict) -> tuple:
        reviewed = _is_reviewed(eff.get(card["name"], (None, None))[0])
        tier = card.get("tier")
        tier_rank = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
        return (
            1 if reviewed else 0,
            topo_rank.get(card["name"], 0),
            tier_rank,
            card["name"],
        )

    cards.sort(key=_sort_key)
    auto_approved.sort(key=_sort_key)

    # Progress / completeness count over the REVIEWABLE (cards) set only — when
    # skip_api, the API decls are not the human's to review (ship approves
    # them), so they are excluded from the denominator.
    total = len(cards)
    reviewed = sum(1 for c in cards if _is_reviewed(eff.get(c["name"], (None, None))[0]))
    complete = total > 0 and reviewed == total

    kept: list[str] = []
    locked: list[str] = []
    rejected: list[dict] = []
    deferred: list[str] = []
    # The summary follows the RECONCILED decl order (kept cited + surfaced),
    # not the raw citations — a stale-cited decl is dropped (it is never a
    # reviewable card) and a surfaced decl is included.
    for view in reconciled_views:
        rec = eff[view.name][0]
        verdict = (rec or {}).get("verdict")
        if verdict == "verify":
            kept.append(view.name)
            if (rec or {}).get("locked"):
                locked.append(view.name)  # opt-in HARD freeze; kept ⊇ locked
        elif verdict == "reject":
            rejected.append({"name": view.name, "note": (rec or {}).get("note")})
        elif verdict == "defer":
            deferred.append(view.name)

    result = {
        "issue": issue_num,
        "title": detail.title,
        "chapter": detail.chapter,
        "card_tier": detail.tier,
        "inspectable": detail.inspectable,
        "inspection_blocked_reason": detail.inspection_blocked_reason,
        "shippable": detail.shippable,
        "ship_blocked_reason": detail.ship_blocked_reason,
        "provisional": detail.provisional,
        # Read-only navigation/dependency metadata from CardDetail.  These were
        # previously dropped while adapting the issue card into a subdeck,
        # leaving the frontend's "deps" and "open" controls with no data.
        "permalink": detail.permalink,
        "deps": [dep.to_json() for dep in detail.deps],
        "cards": cards,
        "auto_approved": auto_approved,
        "progress": {"reviewed": reviewed, "total": total},
        "complete": complete,
        "summary": {"kept": kept, "locked": locked,
                    "rejected": rejected, "deferred": deferred,
                    "invalidated": dependency_invalidated},
        # The leading informal-approval card's data: the issue's ONE top-level
        # informal statement (### Informal Statement) + the human's single
        # approval verdict, or None when there is no informal text. One approval
        # at the issue level covers the whole result (NOT decl-by-decl). Lives
        # OUTSIDE cards — never counted in progress/completeness/summary.
        "informal": _informal_slot(detail, records),
        # The issue-level "something is MISSING" note (or None) — a per-issue
        # flag that the issue is incomplete; at ship it routes to Aristotle with
        # an ADD instruction even when every existing decl is verified. OUTSIDE
        # cards (keyed by MISSING_KEY) — never counted in progress/summary.
        "missing": (records.get(MISSING_KEY) or {}).get("note"),
        # Reconciliation outputs (PURE — computed, written nowhere):
        "stale_citations": stale_citations,
        "reconcile": reconcile,
    }
    result["review_token"] = subdeck_review_token(result)
    return result


def next_unreviewed_card(subdeck: dict) -> Optional[dict]:
    """The first card in the (already-ordered) subdeck without a
    verified/rejected verdict, or ``None`` when every declaration is reviewed
    (subdeck complete). Used as the ``next_decl`` of a decl-verdict response.

    Inspects the SERIALIZED cards, whose ``verdict`` is the past-tense wire
    form (:data:`_VERDICT_WIRE`), so it tests against
    :data:`_REVIEWED_WIRE_VERDICTS` (a deferred/unreviewed card still leads)."""
    for card in subdeck.get("cards", []):
        if card.get("verdict") not in _REVIEWED_WIRE_VERDICTS:
            return card
    return None


# ---------------------------------------------------------------------------
# Ship composition (the per-issue outcome composed from the subdeck)
# ---------------------------------------------------------------------------


def compose_ship_note(
    issue_num: int, summary: dict, missing: Optional[str] = None
) -> str:
    """The composite reject note (the shared contract's exact format).

    Human-authored composite — NO Claude — listing the KEPT declarations
    (verified exactly as written, never to be changed), the FIXED declarations
    (rejected, each with the human's verbatim note), and — when the human flagged
    the issue incomplete — an ADD section carrying the verbatim "what is missing"
    note (a result the title promises but no declaration covers). This is the
    note :func:`ship_issue` carries VERBATIM to ``cmd_reject`` (the committed
    Claude-bypass), so Aristotle sees precisely what the human wrote.

    Pure: derives entirely from ``summary`` + ``missing``. A KEEP/FIX section
    with no members still renders its header so the note's structure is stable;
    the ADD section is omitted entirely when nothing is missing."""
    from marathon.review.reject_note import (
        ADD_HEADER, FIX_HEADER, FOOTER, KEEP_HEADER,
    )

    kept = summary.get("kept", [])
    rejected = summary.get("rejected", [])

    lines = [f"Per-declaration review of issue #{issue_num}.", ""]
    lines.append(KEEP_HEADER)
    for name in kept:
        lines.append(f"- {name}")
    lines.append("")
    lines.append(FIX_HEADER)
    for item in rejected:
        lines.append(f"- {item.get('name')}: {item.get('note')}")
    lines.append("")
    missing_text = (missing or "").strip()
    if missing_text:
        lines.append(ADD_HEADER)
        for ln in missing_text.splitlines():
            ln = ln.rstrip()
            lines.append(ln if ln.lstrip().startswith("-") else f"- {ln}")
        lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def _ship_operation_id(
    cfg: "ReviewConfig",
    issue_num: int,
    verdict: str,
    note: Optional[str],
) -> str:
    """Stable ID for one saved human-review generation.

    Per-declaration records include mutation timestamps, so a genuine later
    review receives a different ID even when its prose happens to be identical.
    A retry after a partial external failure sees the same records and therefore
    reuses the original GitHub comment instead of duplicating it.
    """
    payload = {
        "issue": issue_num,
        "verdict": verdict,
        "note": note,
        "records": issue_verdicts(cfg, issue_num),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ship_issue_once(
    cfg: "ReviewConfig",
    issue_num: int,
    *,
    skip_api: bool = False,
    expected_review_token: Optional[str] = None,
) -> dict:
    """Compose and SHIP the per-issue outcome from the subdeck — the single
    irreversible step (reached only from an explicit ``POST /api/ship``).

    Reads the LOCAL per-decl store and:

    * refuses an incomplete ALL-VERIFY outcome (some reviewable declaration is
      unreviewed or merely deferred) → ``{ok: False, reason}`` with NO side
      effect;
    * once ANY declaration is rejected, permits a PARTIAL reject ship: verified
      declarations go to KEEP, rejected declarations go to FIX, and untouched /
      deferred declarations are deliberately absent from both sections (the
      note's footer tells Aristotle to leave all other code untouched).  This
      lets the UI stop at the first bad upstream declaration and re-review its
      dependents only after Aristotle repairs it;
    * if EVERY declaration is verified → routes
      :func:`marathon.deck.verdicts.apply_verdict` ``"verify"`` (marks the
      issue VERIFIED, merges the marathon PR, flips the tracker — NO
      Aristotle). Outcome ``"verified"``;
    * if ANY declaration is rejected → composes the verbatim KEEP/FIX note
      (:func:`compose_ship_note`) and routes
      :func:`marathon.deck.verdicts.apply_verdict` ``"reject"`` with that
      note inline (the committed Claude-bypass — the note reaches Aristotle
      verbatim). Outcome ``"shipped"``.

    SKIP-API auto-approval (realized ONLY here, at this explicit token-bearing
    ship): on a COMPLETE non-partial review, when ``skip_api`` is truthy, every
    API decl WITHOUT an explicit human verdict is AUTO-APPROVED — folded into
    KEEP in the composed outcome. A PARTIAL reject suppresses auto-approval;
    hidden API cards join WAIT and remain untouched for the next round.
    A human's explicit REJECT of an API decl overrides (it stays a MAIN decl
    and goes to FIX). The result ``message`` (and, when there's a reject, the
    composed note's KEEP list) states the auto-approved names. Auto-approval
    is NOT realized on a GET or in decl-verdict — only here.

    On success clears the issue's subdeck store. The client refreshes the queue
    after the asynchronous ship completes, so this path deliberately skips
    rebuilding the full queue. Routing through ``apply_verdict`` (never a
    second verdict path) preserves the daemon/conductor dispatch and the
    Claude-bypass."""
    from marathon.deck.verdicts import apply_verdict

    subdeck = build_subdeck(cfg, issue_num, skip_api=skip_api)
    if (
        expected_review_token is not None
        and subdeck.get("review_token") != expected_review_token
    ):
        return {
            "ok": False,
            "reason": (
                "The declarations or issue text changed after this review was "
                "opened. Nothing was shipped; reload the issue and review the "
                "current version."
            ),
        }
    summary = subdeck["summary"]
    invalidated = list(summary.get("invalidated", []))
    auto_candidates = [c["name"] for c in subdeck.get("auto_approved", [])]
    partial_reject = bool(summary["rejected"]) and (
        not subdeck["complete"] or bool(invalidated) or bool(auto_candidates)
    )
    if not subdeck["complete"] and not partial_reject:
        progress = subdeck["progress"]
        unreviewed = progress["total"] - progress["reviewed"]
        return {
            "ok": False,
            "reason": (
                f"subdeck not complete — {unreviewed} of {progress['total']} "
                "declaration(s) still unreviewed or deferred; review every "
                "declaration before shipping the issue"
            ),
        }

    # Fold the auto-approved API decls (skip_api: API decls with no explicit
    # verdict) into KEEP — the realization of auto-approval, at this explicit
    # ship only. ``auto_approved`` is [] when skip_api is false, so this is a
    # no-op in the default path.
    auto_names = [] if partial_reject else auto_candidates
    kept = list(summary["kept"]) + auto_names
    composed_summary = {
        "kept": kept,
        "rejected": summary["rejected"],
        "deferred": summary["deferred"],
    }

    auto_clause = ""
    if auto_names:
        auto_clause = (
            f"; auto-approved {len(auto_names)} API declaration(s): "
            + ", ".join(auto_names)
        )

    waiting = [
        card["name"] for card in subdeck.get("cards", [])
        if card.get("verdict") not in _REVIEWED_WIRE_VERDICTS
    ]
    for name in [*invalidated, *(auto_candidates if partial_reject else [])]:
        if name not in waiting:
            waiting.append(name)

    # The issue's "something is MISSING" note (a result the title promises but no
    # decl covers). When present it routes to Aristotle with an ADD section EVEN
    # IF every existing decl was verified — the issue is incomplete, not done.
    missing = subdeck.get("missing")

    if summary["rejected"] or missing:
        note = compose_ship_note(issue_num, composed_summary, missing)
        operation_id = _ship_operation_id(cfg, issue_num, "reject", note)
        # HARD-FREEZE only the LOCKED decls (opt-in) — a sibling-decl refine
        # cannot change their signature. Soft-kept (verified, unlocked) decls
        # stay REWRITABLE: Aristotle may re-type them when a fix legitimately
        # cascades, and any such change re-surfaces for review (fingerprint
        # mismatch) rather than being reverted. Rejected decls are unfrozen so
        # the refine may change them.
        try:
            from marathon.review.approved_statements import capture_kept_on_reject

            frozen, unfrozen = capture_kept_on_reject(
                cfg, issue_num,
                kept=list(summary.get("locked", [])),
                rejected=[r.get("name") for r in summary["rejected"] if r.get("name")],
            )
            if frozen or unfrozen:
                print(
                    f"  freeze: hard-froze {len(frozen)} LOCKED decl(s), "
                    f"unfroze {len(unfrozen)} rejected decl(s) for #{issue_num}",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 — freeze is a hardening, never a blocker
            print(f"  freeze: skipped ({exc})", flush=True)
        apply_verdict(
            cfg,
            issue_num,
            "reject",
            note=note,
            operation_id=operation_id,
            compute_advance=False,
        )
        outcome = "shipped"
        n_fix = len(summary["rejected"])
        bits = []
        if n_fix:
            bits.append(f"{n_fix} sent back to Aristotle to fix")
        if missing:
            bits.append("a missing declaration to add")
        message = (
            f"#{issue_num} shipped: {len(kept)} kept, "
            + " + ".join(bits) + f" (note verbatim){auto_clause}"
        )
        if partial_reject:
            message += (
                f"; stopped at first rejection — {len(waiting)} pending "
                "declaration(s) "
                "left untouched for the next review round"
            )
    else:
        apply_verdict(
            cfg,
            issue_num,
            "verify",
            operation_id=_ship_operation_id(cfg, issue_num, "verify", None),
            compute_advance=False,
        )
        outcome = "verified"
        message = (
            f"#{issue_num} verified: all {len(kept)} declaration(s) "
            f"kept exactly as written{auto_clause}"
        )

    # Auto-add the KEPT surfaced declarations into the issue body (part of THIS
    # explicit ship — never on a GET). A surfaced decl the human reviewed and
    # did NOT reject genuinely belongs to the issue, so it is written into the
    # `### Lean signatures` section (which also drops any stale citation); a
    # rejected surfaced decl is excluded. Best-effort: a write failure prints a
    # warning and never fails the ship (the verdict already landed).
    surfaced_names = {
        c["name"] for c in subdeck.get("cards", []) if c.get("surfaced")
    }
    kept_surfaced = sorted(n for n in kept if n in surfaced_names)
    added_to_issue: list[str] = []
    if kept_surfaced:
        try:
            from marathon.deck.refresh import refresh_issue_signatures

            res = refresh_issue_signatures(
                cfg, issue_num, only_surfaced=set(kept_surfaced)
            )
            if res.get("updated"):
                added_to_issue = list(res.get("added", []))
                message += (
                    f"; added {len(added_to_issue)} surfaced declaration(s) "
                    "to the issue body"
                )
        except Exception as exc:  # noqa: BLE001 — never fail a landed ship
            print(f"  ship: WARN — could not add surfaced decls to #{issue_num}: {exc}")

    # If the human APPROVED the issue's LLM-rendered informal statement (the
    # leading approval card), flip the body's "⚠️ LLM-rendered … verification
    # pending" marker to a "✅ Verified …" marker — the honest, durable record
    # of that verification. Part of THIS explicit ship only; best-effort (a
    # failure never fails the landed verdict); independent of the KEEP/FIX
    # outcome (the informal English is verified whatever the Lean verdicts).
    # Read the verdict BEFORE clear_issue wipes the store.
    informal_verified = False
    if (subdeck.get("informal") or {}).get("verdict") == "verified":
        try:
            from marathon.deck.refresh import mark_informal_verified

            res = mark_informal_verified(cfg, issue_num)
            informal_verified = bool(res.get("updated"))
            if informal_verified:
                message += "; marked the issue's informal statement verified"
        except Exception as exc:  # noqa: BLE001 — never fail a landed ship
            print(
                f"  ship: WARN — could not mark informal verified on "
                f"#{issue_num}: {exc}"
            )

    # Local store cleanup. A VERIFY ship finishes the issue → wipe everything.
    # A REJECT ship sends only the rejected decls to Aristotle (the KEPT ones are
    # told to stay unchanged), so PERSIST the human-verified verdicts — pinned to
    # their current source — and drop the rest. When the refined issue comes back
    # the human re-reviews only the FIXED decls; an untouched 'kept' decl stays
    # approved (and is re-reviewed automatically if Aristotle altered it anyway).
    carried_over: list[str] = []
    if outcome == "shipped":
        carried_over = _persist_kept_verdicts(cfg, issue_num, subdeck, summary["kept"])
    else:
        clear_issue(cfg, issue_num)
    return {
        "ok": True,
        "outcome": outcome,
        "message": message,
        "auto_approved": auto_names,
        "added_to_issue": added_to_issue,
        "informal_verified": informal_verified,
        "carried_over": carried_over,
        "partial": partial_reject,
        "waiting": waiting if partial_reject else [],
        # The client refreshes the authoritative queue after the background
        # ship completes; avoid another expensive synchronous queue rebuild.
        "advanced_to_issue": None,
    }


def ship_issue(
    cfg: "ReviewConfig",
    issue_num: int,
    *,
    skip_api: bool = False,
    expected_review_token: Optional[str] = None,
) -> dict:
    """Single-flight, idempotent wrapper around the irreversible ship.

    The keyed issue lock spans authoritative subdeck rebuild, external verdict
    application, and local cleanup, but does NOT hold the global store lock
    during external GitHub/Aristotle work. Human mutations take the same keyed
    lock and invalidate the receipt, defining the next review generation.
    """
    with _issue_operation_lock(cfg, issue_num):
        receipt = _load_ship_receipt(cfg, issue_num)
        if receipt is not None and (
            expected_review_token is None
            or receipt.get("review_token") == expected_review_token
        ):
            duplicate = dict(receipt["response"])
            duplicate["duplicate"] = True
            duplicate["message"] = (
                f"duplicate ship suppressed for #{issue_num}; "
                + str(duplicate.get("message") or "already shipped")
            )
            return duplicate

        # Rebuild only after winning single-flight: no stale pre-lock decision
        # can cross the irreversible boundary.
        response = _ship_issue_once(
            cfg,
            issue_num,
            skip_api=skip_api,
            expected_review_token=expected_review_token,
        )
        if response.get("ok"):
            response = dict(response)
            response["duplicate"] = False
            _save_ship_receipt(
                cfg,
                issue_num,
                response,
                review_token=expected_review_token,
            )
        return response

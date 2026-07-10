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

import json
import threading
from contextlib import contextmanager
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
    cfg: "ReviewConfig", issue_num: int, response: dict
) -> None:
    """Persist a per-issue idempotency receipt; caller holds the issue lock."""
    path = _ship_receipt_path(cfg, issue_num)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "issue": issue_num,
        "completed_at": datetime.now(timezone.utc).isoformat(),
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


def record_decl_verdict(
    cfg: "ReviewConfig",
    issue_num: int,
    decl: str,
    verdict: str,
    note: Optional[str] = None,
) -> dict:
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

    def mutate(store: dict) -> tuple[dict, bool]:
        decls = store.setdefault(str(issue_num), {})
        prev = decls.get(decl) or {}
        entry = {
            "verdict": verdict,
            "note": str(note) if note is not None else None,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        # The hard-freeze LOCK is a modifier on a verify: carry it over on a
        # re-verify, but DROP it when the verdict changes to reject/defer (a decl
        # the human now wants changed must not be frozen).
        if verdict == "verify" and prev.get("locked"):
            entry["locked"] = True
        decls[decl] = entry
        return entry, True

    with _issue_operation_lock(cfg, issue_num):
        _clear_ship_receipt(cfg, issue_num)
        return _mutate_store(cfg, mutate)


def set_decl_lock(
    cfg: "ReviewConfig", issue_num: int, decl: str, locked: bool
) -> dict:
    """Toggle the HARD-FREEZE lock on a declaration (opt-in, per-decl).

    A LOCKED decl's signature is frozen into the approved-statements manifest at
    reject-ship, so no sibling-decl refine can change it (the hard guard reverts
    any attempt). An UNLOCKED (soft) verified decl stays rewritable — Aristotle
    may re-type it and the change re-surfaces for review. Locking implies KEEP,
    so an unreviewed decl is recorded ``verify`` when first locked. LOCAL ONLY —
    the sidecar; the freeze is realized at :func:`ship_issue`."""
    def mutate(store: dict) -> tuple[dict, bool]:
        decls = store.setdefault(str(issue_num), {})
        entry = decls.get(decl)
        if entry is None or entry.get("verdict") not in (
            "verify", "reject", "defer"
        ):
            entry = {
                "verdict": "verify",
                "note": None,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            decls[decl] = entry
        if locked:
            entry["locked"] = True
        else:
            entry.pop("locked", None)
        return entry, True

    with _issue_operation_lock(cfg, issue_num):
        _clear_ship_receipt(cfg, issue_num)
        return _mutate_store(cfg, mutate)


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
    """After a REJECT ship, REWRITE the issue's store to keep ONLY the human
    KEPT (verified) decls — each pinned to its CURRENT source
    :func:`_decl_fingerprint` — plus the informal approval; everything else
    (the rejected decls Aristotle will fix) is dropped. So when the refined
    issue returns, the kept decls are already verified (no re-review) UNLESS
    their source changed (then the fingerprint mismatch re-opens them). Rejected,
    deferred, and not-yet-reviewed declarations are all absent from the rewritten
    store and therefore return for review. Returns the decl names carried over.
    The store is wiped if nothing is kept."""
    card_by_name = {
        c.get("name"): c
        for c in [
            *subdeck.get("cards", []),
            *subdeck.get("auto_approved", []),
        ]
    }

    def mutate(store: dict) -> tuple[list[str], bool]:
        old = store.get(str(issue_num), {})
        new_entry: dict = {}
        carried: list[str] = []
        for name in kept_names:
            rec = old.get(name)
            if not rec or rec.get("verdict") != "verify":
                continue
            card = card_by_name.get(name)
            new_entry[name] = {
                "verdict": "verify",
                "note": rec.get("note"),
                "ts": rec.get("ts"),
                "fingerprint": _decl_fingerprint(card) if card else None,
            }
            carried.append(name)
        # The informal approval is issue-level (the body marker was flipped at
        # ship) — keep it so the human is not re-asked to approve the same text.
        if INFORMAL_KEY in old:
            new_entry[INFORMAL_KEY] = old[INFORMAL_KEY]
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
    rec = records.get(INFORMAL_KEY) or {}
    stored = rec.get("verdict")
    return {
        "is_llm": bool(is_llm),
        "verdict": _VERDICT_WIRE.get(stored, stored),
        "note": rec.get("note"),
        "statement": statement,
    }


def build_subdeck(
    cfg: "ReviewConfig", issue_num: int, *, skip_api: bool = False
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
    from marathon.deck.cards import build_card_detail, surfaced_decls_for_issue

    detail = build_card_detail(cfg, issue_num)
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
    cited_names = [v.name for v in detail.decls]
    surfaced_views = surfaced_decls_for_issue(cfg, issue_num, cited_names)
    # Defensive: never surface a name that is already a kept cited card.
    _kept_names = {v.name for v in cited_in_code}
    surfaced_views = [v for v in surfaced_views if v.name not in _kept_names]
    surfaced_names = [v.name for v in surfaced_views]

    reconcile = {"dropped": dropped_names, "surfaced": surfaced_names}

    # The reconciled decl set the subdeck reviews: kept cited decls + surfaced
    # decls (stale dropped). This is what the partition below operates on.
    reconciled_views = list(cited_in_code) + list(surfaced_views)

    # CARRIED-OVER verdicts: a verdict persisted from a prior ship carries a
    # ``fingerprint`` and only STANDS while the decl is unchanged. Compute the
    # effective record + a carry-status per decl ONCE, and read it everywhere
    # the store is consulted (partition / cards / progress / summary) — so a
    # 'kept' decl Aristotle actually altered is re-reviewed, while an untouched
    # one is not. An ordinary current-session verdict (no fingerprint) is
    # unaffected.
    view_by_name = {v.name: v for v in reconciled_views}

    def _effective(name):
        rec = records.get(name)
        if not rec:
            return None, None
        if rec.get("fingerprint") is None:
            return rec, None
        view = view_by_name.get(name)
        if view is not None and _decl_fingerprint(view) != rec["fingerprint"]:
            return None, "changed"
        return rec, "carried_over"

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

    # Order: UNREVIEWED first (so the next decl to review leads), then by
    # worst tier (the gating decl), then by name — a total, deterministic key.
    def _sort_key(card: dict) -> tuple:
        reviewed = _is_reviewed(eff.get(card["name"], (None, None))[0])
        tier = card.get("tier")
        tier_rank = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
        return (1 if reviewed else 0, tier_rank, card["name"])

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

    return {
        "issue": issue_num,
        "title": detail.title,
        "chapter": detail.chapter,
        "card_tier": detail.tier,
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


def _next_ready_issue(cfg: "ReviewConfig", just_shipped: int) -> Optional[int]:
    """The next ready issue to enter (its subdeck) after shipping
    ``just_shipped``, scoped to that issue's chapter. Rebuilds the ISSUE
    queue freshly (shipping just changed the readiness graph — a verify may
    have unblocked a dependent) and returns the first ready card that isn't
    the one just shipped. ``None`` when the chapter's ready queue is drained.

    Reuses the queue's readiness (:func:`marathon.deck.cards.build_queue`) so
    "ready" means exactly what it means everywhere else in the deck."""
    from marathon.deck.cards import build_queue

    chapter = cfg.chapter_of_issue(just_shipped)
    try:
        queue = build_queue(cfg, chapter)
    except Exception:  # noqa: BLE001 — a queue-rebuild failure must not abort a ship
        return None
    for card in queue.cards:
        if card.id != just_shipped and card.ready:
            return card.id
    return None


def _ship_issue_once(
    cfg: "ReviewConfig", issue_num: int, *, skip_api: bool = False
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

    On success clears the issue's subdeck store and reports the next ready
    issue to enter. Routing through ``apply_verdict`` (never a second verdict
    path) preserves the daemon/conductor dispatch and the Claude-bypass."""
    from marathon.deck.verdicts import apply_verdict

    subdeck = build_subdeck(cfg, issue_num, skip_api=skip_api)
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
        apply_verdict(cfg, issue_num, "reject", note=note)
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
        apply_verdict(cfg, issue_num, "verify")
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
    informal_rec = issue_verdicts(cfg, issue_num).get(INFORMAL_KEY) or {}
    informal_verified = False
    if informal_rec.get("verdict") == "verify":
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
        "advanced_to_issue": _next_ready_issue(cfg, issue_num),
    }


def ship_issue(
    cfg: "ReviewConfig", issue_num: int, *, skip_api: bool = False
) -> dict:
    """Single-flight, idempotent wrapper around the irreversible ship.

    The keyed issue lock spans authoritative subdeck rebuild, external verdict
    application, and local cleanup, but does NOT hold the global store lock
    during external GitHub/Aristotle work. Human mutations take the same keyed
    lock and invalidate the receipt, defining the next review generation.
    """
    with _issue_operation_lock(cfg, issue_num):
        receipt = _load_ship_receipt(cfg, issue_num)
        if receipt is not None:
            duplicate = dict(receipt["response"])
            duplicate["duplicate"] = True
            duplicate["message"] = (
                f"duplicate ship suppressed for #{issue_num}; "
                + str(duplicate.get("message") or "already shipped")
            )
            return duplicate

        # Rebuild only after winning single-flight: no stale pre-lock decision
        # can cross the irreversible boundary.
        response = _ship_issue_once(cfg, issue_num, skip_api=skip_api)
        if response.get("ok"):
            response = dict(response)
            response["duplicate"] = False
            _save_ship_receipt(cfg, issue_num, response)
        return response

"""marathon.audit.trust — the trust-tier ladder, computed on read.

Plan §2 ruling 4 (BINDING): **trust is computed, never stored.** A tier
is a pure function of (current audit snapshot, append-only decl-verdict
event log); nothing here — or anywhere — persists a bare tier label.
The ledger's ``decl_verdicts`` table stores *events* (what a human
attested, pinned to what fingerprints, when); :func:`compute_tier`
re-derives the live tier from those pins against the CURRENT snapshot
every time it is asked.

The ladder:

* ``UNKNOWN`` — the declaration is absent from the snapshot or carries
  ``status='unknown'`` (it didn't elaborate). Reported, never hidden,
  never punished: absence of evidence is not evidence of failure.
* ``T0`` — present in the snapshot, ``status='ok'`` (it elaborated).
* ``T1`` — T0 + axiom-clean (nothing beyond ``propext`` /
  ``Classical.choice`` / ``Quot.sound``; ``sorryAx`` is *accounted*,
  not failed — a sorry'd skeleton statement is still machine-audited)
  + no deception tags.
* ``T2`` — T1 + a human spec-verdict whose pinned type fingerprint
  matches the CURRENT snapshot AND whose pinned cone fingerprints all
  match current.
* ``T3`` — T2 + a line-review verdict, same pinning. Documented v1
  limitation: the Lean contract does not yet emit proof hashes, so
  proof bodies are not pinned — a proof rewrite that keeps the type
  does not degrade T3.

Verdict events are append-only (enforced by schema triggers): a
revocation or a re-pin appends a NEW event, and for tier purposes the
*newest* event at each claimed level wins. A 'revoked' newest event
means that rung is not satisfied, full stop — the history underneath
is preserved but never consulted for the live tier.

Toolchain staleness (BINDING): a Mathlib/toolchain bump must neither
silently invalidate nor silently re-validate a human verdict.
Pinned-pp fingerprints may legitimately drift across toolchains, so
when the pinned toolchain differs from the snapshot's the comparison
is *flagged* with a ``stale-toolchain`` qualifier in BOTH directions:
matching pins still grant the rung (loudly qualified — matching
project-local fingerprints across toolchains is strong evidence), and
mismatching pins withhold the rung WITHOUT a ``fingerprint-changed`` /
``cone-changed`` claim (a cross-toolchain mismatch is unverifiable, not
a detected meaning change). Either way the qualifier persists until the
explicit amnesty/re-pin command appends a fresh pin.

Backfill (one-human-attestation ruling, plan §3 Phase 5 row): the 29
historical VERIFIED review issues predate the audit engine and their
verdict-time SHAs include red builds — unbuildable. So
:func:`plan_backfill` maps each verified issue's cited declarations
(reusing ``marathon.review.verified_decls``' extraction — never a
forked parser) onto the LATEST snapshot, and the human attests once,
via ``--attest``, that *current main matches what I verified back
then*. Decls missing from the snapshot are skipped with a printed
reason, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.gate import AXIOM_WHITELIST, SORRY_AXIOM
from marathon.ledger import DeclVerdictEvent, Ledger

if TYPE_CHECKING:  # pragma: no cover — type-only; avoids review-package dep
    from marathon.review.config import ReviewConfig

#: Ladder order, worst to best — index = rank for summaries/sorting.
TIER_ORDER = ("UNKNOWN", "T0", "T1", "T2", "T3")

#: The two human-verdict levels an event may claim.
VERDICT_TIERS = ("T2", "T3")


class TrustError(RuntimeError):
    """Raised when a verdict cannot be recorded (decl absent/unknown in
    the snapshot, unpinnable cone, bad tier). Never raised by the
    read-side ``compute_tier*`` functions — those report, not refuse."""


@dataclass(frozen=True)
class TierResult:
    """One declaration's computed tier.

    ``qualifiers`` are machine-readable degradation/staleness markers
    (``stale-toolchain``, ``fingerprint-changed``,
    ``cone-changed:Foo.bar``, ``cone-missing:Foo.bar``); ``evidence``
    is the human-readable trail of which rung checks passed or failed
    and why."""

    decl_name: str
    tier: str  # one of TIER_ORDER
    qualifiers: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tier computation (read side — pure over snapshot + events)
# ---------------------------------------------------------------------------

def compute_tier(
    decl_name: str, snapshot: AuditSnapshot, ledger: Ledger
) -> TierResult:
    """The live tier for one declaration, computed on read."""
    return _tier_from(
        decl_name,
        snapshot.by_name(),
        snapshot.toolchain,
        ledger.decl_verdict_events(decl_name),
    )


def compute_tiers(
    snapshot: AuditSnapshot, ledger: Ledger
) -> list[TierResult]:
    """Tiers for every declaration in the snapshot, PLUS any decl that
    carries a verdict in the ledger but has vanished from the snapshot.

    The latter is the load-bearing case for the "absent = UNKNOWN,
    reported never hidden" ruling: a decl a human verified that no
    longer elaborates (renamed, deleted, broken upstream) must surface
    as an UNKNOWN row, not silently drop off the table. Snapshot decls
    come first in snapshot order; verdict-bearing absentees follow in
    name order. One ledger query for the whole event log."""
    by_name = snapshot.by_name()
    all_events = ledger.all_decl_verdict_events()
    results = [
        _tier_from(
            d.name, by_name, snapshot.toolchain, all_events.get(d.name, [])
        )
        for d in snapshot.decls
    ]
    absent = sorted(name for name in all_events if name not in by_name)
    results.extend(
        _tier_from(name, by_name, snapshot.toolchain, all_events[name])
        for name in absent
    )
    return results


def _effective_event(
    events: list[DeclVerdictEvent], level: str
) -> Optional[DeclVerdictEvent]:
    """The newest event claiming ``level`` (events arrive newest-first).
    Newest wins: a 'revoked' here supersedes every older 'verified'."""
    for event in events:
        if event.tier_claimed == level:
            return event
    return None


def _check_pins(
    event: DeclVerdictEvent,
    decl: DeclAudit,
    by_name: dict[str, DeclAudit],
    snapshot_toolchain: Optional[str],
    qualifiers: list[str],
    evidence: list[str],
) -> bool:
    """Validate one verified event's pins against the CURRENT snapshot.

    Appends qualifiers/evidence as a side effect; returns whether the
    rung is granted. Cross-toolchain rules per the module docstring:
    qualify ``stale-toolchain`` either way, and never claim
    ``fingerprint-changed``/``cone-changed`` for a mismatch we cannot
    verify across toolchains."""
    label = f"{event.tier_claimed} verdict (event {event.id}, {event.ts})"
    stale = event.toolchain != snapshot_toolchain
    if stale:
        _add_qualifier(qualifiers, "stale-toolchain")
        evidence.append(
            f"{label}: pinned under toolchain {event.toolchain!r} but "
            f"snapshot is {snapshot_toolchain!r} — flagged stale until "
            "re-pinned (amnesty)"
        )
    ok = True
    if event.fingerprint_type != decl.fingerprint_type:
        ok = False
        if stale:
            evidence.append(
                f"{label}: type fingerprint differs, but cross-toolchain "
                "pp drift makes that unverifiable — withheld, not "
                "invalidated; re-pin to resolve"
            )
        else:
            _add_qualifier(qualifiers, "fingerprint-changed")
            evidence.append(
                f"{label}: pinned type fingerprint no longer matches the "
                "current snapshot — the statement's meaning changed"
            )
    for pin in event.cone:
        name = pin.get("name", "")
        member = by_name.get(name)
        if member is None or member.fingerprint_type is None:
            ok = False
            _add_qualifier(qualifiers, f"cone-missing:{name}")
            evidence.append(
                f"{label}: pinned cone member {name} is absent or "
                "unknown in the current snapshot"
            )
        elif member.fingerprint_type != pin.get("fingerprint"):
            ok = False
            if stale:
                evidence.append(
                    f"{label}: cone member {name} fingerprint differs "
                    "under a different toolchain — unverifiable; re-pin "
                    "to resolve"
                )
            else:
                _add_qualifier(qualifiers, f"cone-changed:{name}")
                evidence.append(
                    f"{label}: pinned cone member {name} changed meaning "
                    "— re-read that card"
                )
    if ok:
        evidence.append(
            f"{label}: type + {len(event.cone)} cone pin(s) match current "
            "snapshot" + (" (cross-toolchain match)" if stale else "")
        )
    return ok


def _add_qualifier(qualifiers: list[str], qualifier: str) -> None:
    if qualifier not in qualifiers:
        qualifiers.append(qualifier)


def _tier_from(
    decl_name: str,
    by_name: dict[str, DeclAudit],
    snapshot_toolchain: Optional[str],
    events: list[DeclVerdictEvent],
) -> TierResult:
    """The pure ladder. ``events`` newest-first (ledger order)."""
    qualifiers: list[str] = []
    evidence: list[str] = []
    decl = by_name.get(decl_name)
    if decl is None:
        evidence.append(
            "absent from the current snapshot — no audit evidence "
            "(reported, never hidden)"
        )
        return TierResult(decl_name, "UNKNOWN", qualifiers, evidence)
    if decl.is_unknown:
        evidence.append(
            "status=unknown — did not elaborate"
            + (f" ({decl.reason})" if decl.reason else "")
        )
        return TierResult(decl_name, "UNKNOWN", qualifiers, evidence)

    tier = "T0"
    evidence.append("T0: present in snapshot, status ok")

    # T1 — axiom-clean + no deception tags. sorryAx is accounted, not
    # failed (skeleton statements are still machine-auditable).
    dirty = sorted(set(decl.axioms) - AXIOM_WHITELIST - {SORRY_AXIOM})
    blocked = False
    if dirty:
        blocked = True
        evidence.append(
            "T1 blocked: axioms beyond whitelist: " + ", ".join(dirty)
        )
    if decl.tags:
        blocked = True
        evidence.append("T1 blocked: deception tags: " + ";".join(decl.tags))
    if blocked:
        return TierResult(decl_name, tier, qualifiers, evidence)
    if SORRY_AXIOM in decl.axioms:
        evidence.append("T1: sorryAx present — accounted, not failed")
    tier = "T1"
    evidence.append("T1: axiom-clean, no deception tags")

    # T2/T3 — human verdicts, newest event per claimed level wins.
    # Pin checks are memoized per event so the T3 event consulted for
    # both rungs contributes its qualifiers exactly once.
    pin_ok: dict[int, bool] = {}

    def verified_with_pins(event: Optional[DeclVerdictEvent]) -> bool:
        if event is None or event.verdict != "verified":
            return False
        if event.id not in pin_ok:
            pin_ok[event.id] = _check_pins(
                event, decl, by_name, snapshot_toolchain,
                qualifiers, evidence,
            )
        return pin_ok[event.id]

    spec_event = _effective_event(events, "T2")
    line_event = _effective_event(events, "T3")
    for event in (spec_event, line_event):
        if event is not None and event.verdict == "revoked":
            evidence.append(
                f"{event.tier_claimed} verdict revoked (event {event.id}, "
                f"{event.ts}) — newest event wins"
            )
    # A valid line-review event also attests the spec rung: a
    # line-by-line read subsumes the spec read.
    spec_ok = verified_with_pins(spec_event) or verified_with_pins(line_event)
    if not spec_ok:
        if spec_event is None and line_event is None:
            evidence.append("T2: no human spec-verdict recorded")
        return TierResult(decl_name, tier, qualifiers, evidence)
    tier = "T2"
    if verified_with_pins(line_event):
        tier = "T3"
        evidence.append(
            "T3: proof bodies are not pinned in v1 (Lean contract emits "
            "no proof hashes) — a type-preserving proof rewrite does not "
            "degrade this tier"
        )
    return TierResult(decl_name, tier, qualifiers, evidence)


# ---------------------------------------------------------------------------
# Verdict recording (write side — always appends, never rewrites)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cone_pins(
    decl: DeclAudit, by_name: dict[str, DeclAudit]
) -> tuple[list[dict], list[str]]:
    """Current fingerprints for the decl's full statement cone.
    Returns ``(pins, unpinnable member names)``."""
    pins: list[dict] = []
    unpinnable: list[str] = []
    for member in decl.cone:
        audit = by_name.get(member)
        if audit is None or audit.fingerprint_type is None:
            unpinnable.append(member)
        else:
            pins.append(
                {"name": member, "fingerprint": audit.fingerprint_type}
            )
    return pins, unpinnable


def record_spec_verdict(
    ledger: Ledger,
    decl_name: str,
    snapshot: AuditSnapshot,
    *,
    tier: str = "T2",
    issue_num: Optional[int] = None,
    source: str = "cli",
    notes: str = "",
) -> int:
    """Append a human verdict event pinning the decl's CURRENT type
    fingerprint, full cone fingerprints, and toolchain from
    ``snapshot``. Returns the event id.

    Refuses (:class:`TrustError`) when the decl is absent or unknown in
    the snapshot — you cannot verify what didn't elaborate — and when
    any cone member is unpinnable (an unpinned cone would make the T2
    invalidation feed blind to upstream meaning changes)."""
    if tier not in VERDICT_TIERS:
        raise TrustError(
            f"verdict tier must be one of {VERDICT_TIERS}, got {tier!r}"
        )
    by_name = snapshot.by_name()
    decl = by_name.get(decl_name)
    if decl is None:
        raise TrustError(
            f"{decl_name} is absent from the audit snapshot — you cannot "
            "verify what didn't elaborate (run `marathon audit run` first)"
        )
    if decl.is_unknown or decl.fingerprint_type is None:
        raise TrustError(
            f"{decl_name} has status=unknown in the audit snapshot"
            + (f" ({decl.reason})" if decl.reason else "")
            + " — you cannot verify what didn't elaborate"
        )
    pins, unpinnable = _cone_pins(decl, by_name)
    if unpinnable:
        raise TrustError(
            f"cannot pin the cone of {decl_name}: no audit evidence for "
            + ", ".join(unpinnable)
        )
    return ledger.append_decl_verdict(
        decl_name,
        tier_claimed=tier,
        verdict="verified",
        fingerprint_type=decl.fingerprint_type,
        cone=pins,
        toolchain=snapshot.toolchain,
        issue_num=issue_num,
        ts=_now_iso(),
        source=source,
        notes=notes or None,
    )


def record_revocation(
    ledger: Ledger,
    decl_name: str,
    *,
    tier: str = "T2",
    issue_num: Optional[int] = None,
    source: str = "cli",
    notes: str = "",
) -> int:
    """Append a 'revoked' event for ``tier``. Pins nothing (there is
    nothing to assert about the current code); supersedes earlier
    events at that level for tier purposes while preserving them in
    history. Returns the event id."""
    if tier not in VERDICT_TIERS:
        raise TrustError(
            f"verdict tier must be one of {VERDICT_TIERS}, got {tier!r}"
        )
    return ledger.append_decl_verdict(
        decl_name,
        tier_claimed=tier,
        verdict="revoked",
        fingerprint_type=None,
        cone=[],
        toolchain=None,
        issue_num=issue_num,
        ts=_now_iso(),
        source=source,
        notes=notes or None,
    )


# ---------------------------------------------------------------------------
# Backfill (one-time: VERIFIED review issues -> T2 pins on current main)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BackfillItem:
    """One decl that would be (or was) pinned by the backfill."""

    decl_name: str
    issue_num: int
    fingerprint_type: str
    cone_size: int


@dataclass(frozen=True)
class BackfillSkip:
    """One cited decl the backfill will NOT pin, and why. Skips are
    printed, never guessed around."""

    name: str  # as cited in the issue body (maybe unqualified)
    issue_num: int
    reason: str


@dataclass
class BackfillPlan:
    items: list[BackfillItem] = field(default_factory=list)
    skipped: list[BackfillSkip] = field(default_factory=list)


def _resolve_decl(
    raw: str, by_name: dict[str, DeclAudit]
) -> tuple[Optional[DeclAudit], Optional[str]]:
    """Issue bodies cite declaration names as written in source — often
    namespace-unqualified — while the snapshot is fully qualified.
    Exact match first, else a UNIQUE dotted-suffix match (deterministic,
    same convenience rule as `marathon audit show`); anything else is a
    skip reason, never a guess."""
    if raw in by_name:
        return by_name[raw], None
    matches = [d for name, d in by_name.items() if name.endswith("." + raw)]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, (
            "ambiguous in snapshot: "
            + ", ".join(sorted(d.name for d in matches))
        )
    return None, "not in the latest snapshot"


def _already_pinned(
    events: list[DeclVerdictEvent],
    decl: DeclAudit,
    pins: list[dict],
    toolchain: Optional[str],
) -> bool:
    """True when the newest effective T2-level event already pins
    exactly the current fingerprints+toolchain — re-running the
    backfill then appends nothing (append-only stays clean of
    duplicate attestations)."""
    newest = _effective_event(events, "T2")
    if newest is None or newest.verdict != "verified":
        return False
    return (
        newest.fingerprint_type == decl.fingerprint_type
        and newest.toolchain == toolchain
        and sorted(
            (p.get("name", ""), p.get("fingerprint", "")) for p in newest.cone
        ) == sorted((p["name"], p["fingerprint"]) for p in pins)
    )


def plan_backfill(
    cfg: "ReviewConfig",
    snapshot: AuditSnapshot,
    ledger: Ledger,
    *,
    chapters: Optional[list[int]] = None,
) -> BackfillPlan:
    """Map existing VERIFIED review issues onto the latest snapshot.

    Reuses ``marathon.review.verified_decls.verified_declarations``
    for the issue-body extraction (the single parser of that format —
    never forked). Pure planning: writes nothing; the caller gates the
    write on the human's ``--attest``."""
    # Local import: marathon.audit must stay importable without the
    # review package's gh-shaped dependencies.
    from marathon.review.verified_decls import verified_declarations

    by_name = snapshot.by_name()
    plan = BackfillPlan()
    planned: dict[str, int] = {}  # decl -> first citing issue
    for chapter in chapters if chapters is not None else sorted(cfg.chapters):
        for issue_num, names in sorted(
            verified_declarations(cfg, chapter).items()
        ):
            for raw in sorted(names):
                decl, why = _resolve_decl(raw, by_name)
                if decl is None:
                    plan.skipped.append(BackfillSkip(raw, issue_num, why))
                    continue
                if decl.name in planned:
                    plan.skipped.append(BackfillSkip(
                        raw, issue_num,
                        f"already planned via issue #{planned[decl.name]}",
                    ))
                    continue
                if decl.is_unknown or decl.fingerprint_type is None:
                    plan.skipped.append(BackfillSkip(
                        raw, issue_num,
                        "status=unknown — did not elaborate",
                    ))
                    continue
                pins, unpinnable = _cone_pins(decl, by_name)
                if unpinnable:
                    plan.skipped.append(BackfillSkip(
                        raw, issue_num,
                        "cone member(s) without audit evidence: "
                        + ", ".join(unpinnable),
                    ))
                    continue
                if _already_pinned(
                    ledger.decl_verdict_events(decl.name), decl, pins,
                    snapshot.toolchain,
                ):
                    plan.skipped.append(BackfillSkip(
                        raw, issue_num,
                        "already pinned at the current fingerprints",
                    ))
                    continue
                planned[decl.name] = issue_num
                plan.items.append(BackfillItem(
                    decl_name=decl.name,
                    issue_num=issue_num,
                    fingerprint_type=decl.fingerprint_type,
                    cone_size=len(pins),
                ))
    return plan


def apply_backfill(
    ledger: Ledger, snapshot: AuditSnapshot, plan: BackfillPlan
) -> int:
    """Write the plan: one T2 'verified' event per item, source
    ``'backfill'``. Only reached behind ``--attest`` — each event
    asserts 'current main matches what I verified back then' with one
    human attestation. Returns the number of events written."""
    for item in plan.items:
        record_spec_verdict(
            ledger,
            item.decl_name,
            snapshot,
            tier="T2",
            issue_num=item.issue_num,
            source="backfill",
            notes=(
                f"backfill of VERIFIED review issue #{item.issue_num}: "
                "pinned to current main under one human attestation"
            ),
        )
    return len(plan.items)

"""marathon.audit.invalidate — the T2/T3 invalidation feed + amnesty's
notification side (Phase 5b part 2).

A human spec/line verdict (``decl_verdicts`` event) pins the decl's
type fingerprint, its full statement-cone fingerprints, and the
toolchain. When a fresh audit snapshot lands, some of those pins may no
longer match: the decl's own statement changed, a pinned cone member
changed meaning or vanished, or the decl stopped elaborating entirely.
Each such case is an *invalidation* — the human's verdict no longer
covers what is on disk, and the trust ladder
(:mod:`marathon.audit.trust`) has already silently degraded the tier
below what the human attested. This module surfaces that degradation:

* :func:`compute_invalidations` is PURE — it diffs (old snapshot, new
  snapshot) against the ledger's live verdicts and returns one
  :class:`Invalidation` per affected decl, NAMING the changed cone
  member where that is the cause, plus a SEPARATE
  ``stale_toolchain`` list (decls whose only problem is a
  cross-toolchain pin mismatch — wholesale staleness that is amnesty's
  job to resolve, never a per-decl meaning-change alarm).
* :func:`notify_invalidations` is the I/O side. Dry-run (the default)
  prints the table and writes NOTHING. ``apply=True`` performs exactly
  two kinds of GitHub write, both batched/circuit-broken per the
  write-storm ruling (crit-feas §5):

  1. ONE parent-issue body rewrite flipping every affected issue's
     emoji 🟡→🟠 (via :func:`marathon.review.tracker.update_tracker_emojis`
     — substring surgery once, not once per decl); and
  2. one idempotent marker-comment per affected issue, behind a circuit
     breaker mirroring ``landing.py``'s (content-hash dedup forever +
     a per-issue daily cap). Failed gh posts are best-effort and are
     NOT counted against the cap, so the next run retries the notice.

Binding rulings honored here: tiers are computed never stored (we ask
:func:`marathon.audit.trust.compute_tier` rather than re-deriving pin
logic); verdict events are append-only (this module never writes the
ledger — invalidation is a *report*, re-pinning is the operator's
explicit ``marathon audit repin``); and a cross-toolchain bump
surfaces as wholesale staleness resolved only by amnesty, never as a
silent per-decl invalidation.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from marathon.audit.engine import audit_state_dir
from marathon.audit.records import AuditSnapshot
from marathon.audit.trust import VERDICT_TIERS, compute_tier
from marathon.ledger import DeclVerdictEvent, Ledger

if TYPE_CHECKING:  # pragma: no cover — type-only; avoids review-package dep
    from marathon.review.config import ReviewConfig

#: Invalidation cause classes, in display order.
CAUSE_TYPE = "type-changed"
CAUSE_CONE = "cone-changed"
CAUSE_ABSENT = "absent"

#: Circuit-breaker state lives inside the self-gitignored audit dir (it
#: IS invalidation-comment bookkeeping), same convention as landing's
#: breaker living under the self-ignored bounces/ dir.
BREAKER_FILENAME = "invalidation-breaker.json"

#: Per-issue daily cap on invalidation marker-comments, mirroring
#: landing.py's BOUNCE_COMMENT_DAILY_CAP. A content-hash dedup
#: (``posted``) suppresses an identical notice forever; the daily cap
#: bounds churn when the signature legitimately keeps changing.
INVALIDATION_COMMENT_DAILY_CAP = 3


@dataclass(frozen=True)
class Invalidation:
    """One declaration whose live human verdict no longer covers the new
    snapshot. ``tier_claimed`` is the rung the human attested (T2/T3);
    ``tier_now`` is what :func:`compute_tier` computes against the new
    snapshot (always below ``tier_claimed`` — that is what makes it an
    invalidation). ``cause`` is one of :data:`CAUSE_TYPE` /
    :data:`CAUSE_CONE` / :data:`CAUSE_ABSENT`; ``cone_member`` NAMES the
    changed/missing cone member when ``cause`` is :data:`CAUSE_CONE`
    (the load-bearing "tell the human which card to re-read" datum).
    ``issue_num`` is the verdict event's pinned issue (None if the
    verdict was recorded without one), used to flip the tracker and post
    the marker comment."""

    decl_name: str
    tier_claimed: str  # 'T2' | 'T3'
    tier_now: str
    cause: str
    detail: str
    cone_member: Optional[str] = None
    issue_num: Optional[int] = None


@dataclass
class InvalidationReport:
    """The result of :func:`compute_invalidations`.

    ``invalidations`` are per-decl meaning-change alarms (the human must
    re-read or re-pin). ``stale_toolchain`` is the SEPARATE list of decl
    names whose only problem is a cross-toolchain pin mismatch —
    wholesale staleness, not a detected change — surfaced for amnesty
    (``marathon audit repin``), never flipped or commented on per decl
    (a Mathlib/toolchain bump must not nuke 29 verified decls and post
    29 comments)."""

    invalidations: list[Invalidation] = field(default_factory=list)
    stale_toolchain: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.invalidations or self.stale_toolchain)


# ---------------------------------------------------------------------------
# Pure computation (snapshot diff × live verdicts → invalidations)
# ---------------------------------------------------------------------------

def _live_verdict(
    events: list[DeclVerdictEvent],
) -> Optional[DeclVerdictEvent]:
    """The newest verdict event whose effective (newest-per-level)
    record is a 'verified' (not revoked), claiming the highest rung the
    human stands behind. ``events`` arrive newest-first (ledger order).

    A decl is "holding a live T2/T3 verdict" iff, at its highest claimed
    level, the newest event at that level is a verification — a newest
    'revoked' at a level means the human walked it back, so that decl is
    not invalidated (there is nothing to invalidate). Returns the
    governing event (the one whose pins we check), or None."""
    # Effective newest event per claimed level (newest wins).
    newest_per_level: dict[str, DeclVerdictEvent] = {}
    for event in events:  # newest-first: first seen at a level is newest
        newest_per_level.setdefault(event.tier_claimed, event)
    # Highest claimed level the human still stands behind (T3 before T2).
    for level in reversed(VERDICT_TIERS):  # ('T3', 'T2')
        event = newest_per_level.get(level)
        if event is not None and event.verdict == "verified":
            return event
    return None


def _tier_rank(tier: str) -> int:
    from marathon.audit.trust import TIER_ORDER

    return TIER_ORDER.index(tier) if tier in TIER_ORDER else 0


def compute_invalidations(
    old_snapshot: Optional[AuditSnapshot],
    new_snapshot: AuditSnapshot,
    ledger: Ledger,
) -> InvalidationReport:
    """PURE: which live T2/T3 verdicts no longer cover ``new_snapshot``.

    For every decl carrying a live (newest-event-not-revoked) T2/T3
    verdict, ask :func:`marathon.audit.trust.compute_tier` for its tier
    against ``new_snapshot``. If that tier dropped below the rung the
    human claimed, classify why from the computed qualifiers — reusing
    trust's pin semantics rather than re-deriving them:

    * a ``stale-toolchain`` qualifier with NO change-claim qualifier
      means the only problem is a cross-toolchain mismatch → it goes in
      :attr:`InvalidationReport.stale_toolchain` (amnesty's job), NOT a
      per-decl invalidation;
    * ``fingerprint-changed`` → :data:`CAUSE_TYPE` (own statement
      changed);
    * ``cone-changed:X`` / ``cone-missing:X`` → :data:`CAUSE_CONE`,
      NAMING ``X`` (which upstream card to re-read);
    * an UNKNOWN tier (decl absent/unknown in the new snapshot) →
      :data:`CAUSE_ABSENT`.

    ``old_snapshot`` is accepted for symmetry with
    :func:`marathon.audit.engine.diff_snapshots` and to let callers
    confirm a from→to transition; the invalidation set itself is a
    property of (new snapshot, ledger) — a pin either matches current or
    does not — so it is computed against ``new_snapshot`` alone. Writes
    nothing."""
    report = InvalidationReport()
    all_events = ledger.all_decl_verdict_events()
    for decl_name in sorted(all_events):
        event = _live_verdict(all_events[decl_name])
        if event is None:
            continue
        tier = compute_tier(decl_name, new_snapshot, ledger)
        quals = tier.qualifiers
        change_quals = [
            q for q in quals
            if q.startswith(("fingerprint-changed", "cone-changed:",
                             "cone-missing:"))
        ]
        # Cross-toolchain wholesale staleness comes FIRST and is checked
        # on the QUALIFIER, not on a dropped rung: a clean toolchain bump
        # leaves the pins matching (rung still granted, tier unchanged)
        # yet trust.py flags 'stale-toolchain' until re-pinned — and a
        # mismatch across toolchains withholds the rung WITHOUT a
        # change-claim (unverifiable, not detected). Both are amnesty's
        # job (one re-pin, not 29 per-decl alarms), so whenever
        # stale-toolchain is present with no concrete change-claim, the
        # decl goes in stale_toolchain — never a per-decl invalidation.
        if ("stale-toolchain" in quals and not change_quals
                and tier.tier != "UNKNOWN"):
            report.stale_toolchain.append(decl_name)
            continue

        # A per-decl invalidation requires the rung to have actually
        # dropped below the human's claim (a concrete change or a
        # vanished decl). A matching same-toolchain verdict drops through
        # here untouched.
        if _tier_rank(tier.tier) >= _tier_rank(event.tier_claimed):
            continue  # rung still satisfied — not invalidated
        report.invalidations.append(
            _classify(decl_name, event, tier.tier, quals)
        )
    report.stale_toolchain.sort()
    return report


def _classify(
    decl_name: str,
    event: DeclVerdictEvent,
    tier_now: str,
    qualifiers: list[str],
) -> Invalidation:
    """Turn one degraded decl's computed qualifiers into a typed
    :class:`Invalidation`, naming the cone member when that is the
    cause. Order of precedence when several qualifiers coincide: a
    vanished decl (UNKNOWN) is the loudest signal, then a cone-member
    problem (it tells the human exactly which other card to re-read),
    then the decl's own type change."""
    cone_quals = [
        q for q in qualifiers
        if q.startswith(("cone-changed:", "cone-missing:"))
    ]
    if tier_now == "UNKNOWN":
        return Invalidation(
            decl_name=decl_name,
            tier_claimed=event.tier_claimed,
            tier_now=tier_now,
            cause=CAUSE_ABSENT,
            detail=(
                f"{decl_name} is absent or no longer elaborates in the new "
                "snapshot — the verdict covers code that is gone"
            ),
            issue_num=event.issue_num,
        )
    if cone_quals:
        member = cone_quals[0].split(":", 1)[1]
        kind = "vanished from" if cone_quals[0].startswith("cone-missing:") \
            else "changed meaning in"
        return Invalidation(
            decl_name=decl_name,
            tier_claimed=event.tier_claimed,
            tier_now=tier_now,
            cause=CAUSE_CONE,
            detail=(
                f"pinned cone member {member} {kind} the new snapshot — "
                f"re-read that card before trusting {decl_name}"
            ),
            cone_member=member,
            issue_num=event.issue_num,
        )
    # Default / fingerprint-changed: the decl's own statement changed.
    return Invalidation(
        decl_name=decl_name,
        tier_claimed=event.tier_claimed,
        tier_now=tier_now,
        cause=CAUSE_TYPE,
        detail=(
            f"{decl_name}'s own type fingerprint no longer matches the "
            "pin — the statement's meaning changed"
        ),
        issue_num=event.issue_num,
    )


# ---------------------------------------------------------------------------
# Notification (dry-run prints; apply flips one body + posts dedup comments)
# ---------------------------------------------------------------------------

def _render_table(report: InvalidationReport) -> str:
    """Human-readable invalidation table (used by both the dry-run print
    and apply's console echo)."""
    lines: list[str] = []
    if report.invalidations:
        name_w = max(len(i.decl_name) for i in report.invalidations)
        lines.append(
            f"{len(report.invalidations)} invalidated verdict(s) "
            "(tier dropped below the human's claim):"
        )
        for inv in report.invalidations:
            issue = f" (#{inv.issue_num})" if inv.issue_num is not None else ""
            lines.append(
                f"  {inv.decl_name:<{name_w}}  {inv.tier_claimed}→"
                f"{inv.tier_now}  {inv.cause}{issue}"
            )
            lines.append(f"  {' ' * name_w}    {inv.detail}")
    else:
        lines.append("no invalidated verdicts")
    if report.stale_toolchain:
        lines.append(
            f"{len(report.stale_toolchain)} verdict(s) stale across a "
            "toolchain bump (resolve with `marathon audit repin`, NOT a "
            "per-decl alarm):"
        )
        for name in report.stale_toolchain:
            lines.append(f"  {name}")
    return "\n".join(lines)


def _breaker_path(repo_dir: Path) -> Path:
    return audit_state_dir(repo_dir) / BREAKER_FILENAME


def _load_breaker(path: Path) -> dict:
    """Load the comment circuit-breaker state, tolerating a corrupt or
    absent file (a broken breaker is fail-open into a fresh empty state,
    same as landing's ``_load_breaker``)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data.get("posted"), dict):
        data["posted"] = {}
    if not isinstance(data.get("daily"), dict):
        data["daily"] = {}
    return data


def _comment_body(inv: Invalidation) -> str:
    return (
        f"**Trust invalidated** (`{inv.cause}`): a fresh audit shows the "
        f"human {inv.tier_claimed} verdict for `{inv.decl_name}` no longer "
        f"covers the current code (computed tier is now **{inv.tier_now}**).\n\n"
        f"{inv.detail}\n\n"
        "The tracker emoji has been flipped back to 🟠. Re-read the affected "
        "card; once the statement is re-verified, run `marathon audit repin "
        f"--decl {inv.decl_name} --yes` to re-pin the verdict to current "
        "main (an explicit operator attestation — never silent)."
    )


def _maybe_post_comment(
    cfg: "ReviewConfig",
    inv: Invalidation,
    state: dict,
) -> bool:
    """Post ONE marker comment for ``inv`` behind the circuit breaker
    (mutating ``state`` in place on success). Mirrors landing.py's
    ``_maybe_post_bounce_comment``: an identical content signature is
    never posted twice (``posted`` set, forever), each issue is capped
    at :data:`INVALIDATION_COMMENT_DAILY_CAP` comments per day, and a
    failed/suppressed gh post is best-effort — NOT counted against the
    cap — so the next run retries the notice. Returns whether a comment
    was actually posted."""
    if inv.issue_num is None:
        print(
            f"  invalidation for {inv.decl_name} has no pinned issue; "
            "no comment to post (tracker-only)"
        )
        return False
    signature = f"{inv.issue_num}\n{inv.decl_name}\n{inv.cause}\n{inv.detail}"
    sig_hash = hashlib.sha256(signature.encode("utf-8", "replace")).hexdigest()
    if sig_hash in state["posted"]:
        print(
            f"  invalidation comment for #{inv.issue_num} suppressed "
            f"(identical signature already posted: {sig_hash[:12]})"
        )
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    day_counts = state["daily"].get(today, {})
    posted_today = int(day_counts.get(str(inv.issue_num), 0) or 0)
    if posted_today >= INVALIDATION_COMMENT_DAILY_CAP:
        print(
            f"  invalidation comment for #{inv.issue_num} suppressed "
            f"(daily cap of {INVALIDATION_COMMENT_DAILY_CAP} reached)"
        )
        return False
    cmd = [
        "gh", "issue", "comment", str(inv.issue_num),
        "--repo", cfg.github_repo,
        "--body", _comment_body(inv),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(
                f"  warning: invalidation comment for #{inv.issue_num} "
                f"failed (gh exit {result.returncode}): "
                f"{(result.stderr or '').strip()}"
            )
            return False
    except Exception as e:  # noqa: BLE001 — best-effort; see docstring
        print(
            f"  warning: invalidation comment for #{inv.issue_num} "
            f"failed ({e})"
        )
        return False
    state["posted"][sig_hash] = datetime.now().astimezone().isoformat()
    # Keep only today's tallies (old days can never matter again; the
    # posted-signature set is the long-term dedup) — same trim as landing.
    state["daily"] = {
        today: {**day_counts, str(inv.issue_num): posted_today + 1}
    }
    return True


def notify_invalidations(
    cfg: "ReviewConfig",
    report: InvalidationReport,
    *,
    apply: bool = False,
) -> str:
    """Surface ``report``. Dry-run (``apply=False``, the default) prints
    the table and writes NOTHING — no GitHub call, no breaker file.

    ``apply=True`` performs the two batched/circuit-broken writes:

    1. ONE parent-issue body rewrite flipping every affected issue's
       emoji 🟡→🟠 via :func:`update_tracker_emojis` (substring surgery
       happens once for all N flips — the write-storm ruling); and
    2. one idempotent marker-comment per affected issue, behind the
       content-hash + daily-cap circuit breaker, with the breaker state
       persisted under the self-gitignored ``.marathon/audit/`` dir.

    ``stale_toolchain`` decls are reported but never flipped or
    commented (amnesty resolves them). Returns a human-readable summary
    string."""
    table = _render_table(report)
    if not apply:
        return table + (
            "\n\n(dry run — nothing written; pass --apply to flip trackers "
            "and post notices)"
            if report.invalidations else ""
        )
    if not report.invalidations:
        return table  # nothing to flip or comment

    from marathon.review.tracker import update_tracker_emojis

    lines = [table, ""]
    # 1) ONE batched tracker rewrite for every flip. Dedup issue numbers
    # (two invalidated decls can share one sub-issue — flip its line
    # once) and drop verdicts that carry no pinned issue.
    flip_issues: list[int] = []
    for inv in report.invalidations:
        if inv.issue_num is not None and inv.issue_num not in flip_issues:
            flip_issues.append(inv.issue_num)
    if flip_issues:
        try:
            ok, msg = update_tracker_emojis(
                cfg, [(num, "🟠") for num in flip_issues]
            )
        except (RuntimeError, OSError) as e:
            ok, msg = False, f"tracker rewrite failed: {e}"
        lines.append(f"tracker: {'' if ok else 'WARN — '}{msg}")
    else:
        lines.append("tracker: no pinned issues to flip")

    # 2) One circuit-broken marker comment per affected issue.
    breaker = _breaker_path(cfg.repo_dir)
    state = _load_breaker(breaker)
    posted = 0
    for inv in report.invalidations:
        if _maybe_post_comment(cfg, inv, state):
            posted += 1
    # Persist breaker state only if we actually posted (a no-op run must
    # not rewrite the file — keeps `git status` and mtimes quiet).
    if posted:
        breaker.parent.mkdir(parents=True, exist_ok=True)
        breaker.write_text(json.dumps(state, indent=2) + "\n")
    lines.append(f"comments: posted {posted} new invalidation notice(s)")
    return "\n".join(lines)

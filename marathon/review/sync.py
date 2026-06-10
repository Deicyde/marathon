"""Two-way reconciliation of GitHub verdict labels ↔ local review state.

``marathon review sync`` is the reconciliation job that the review
subsystem never had: GitHub labels and ``state.json`` "drift by design"
(recon ``report-review-subsystem.md`` §6 item 4 — the audit briefing
asked the *human* to fix mismatches; ``open_session`` just rendered both
states side-by-side because they disagree). The live consequence is the
plan's §1 headline number: state.json carried 21 entries against 29
``review:verified`` labels, and PR #74 exists solely to record one
verify that never made it into git.

**Reconciliation policy (binding, from the Phase-1 task):**

* **GitHub labels are authoritative for HUMAN verdicts.** The operator
  demonstrably drives verdicts from GitHub (plan §2 ruling 1's "inbound
  verdict channel"), so a ``review:verified`` / ``review:rejected``
  label that is absent or stale locally proposes an *inbound* update —
  applied via the existing :func:`record_verification` /
  :func:`record_rejection` so the Phase-1 dual-write (ledger) and the
  tracked ``verdicts.jsonl`` fire, with the provenance tag ``'sync'``
  instead of ``'cli'`` (see :func:`_sync_tagged_verdicts`).
* **Local-only operational fields are NEVER overwritten.** ``attempts``,
  ``stalled``, ``last_iteration_ts`` and rejection notes are daemon /
  queue bookkeeping that GitHub knows nothing about: a stalled local
  entry whose GitHub label still says rejected stays stalled and is
  reported as ``local-only state (kept)``; an in-sync rejection keeps
  its real notes.
* **Local verdicts missing on GitHub propose outbound label pushes.**
  Listed in every run, but applied only under ``--apply
  --push-labels`` — sync never writes to GitHub unless explicitly
  asked. Pushes use the same ``gh issue edit --add-label X
  --remove-label Y`` (``check=False``) shape as ``cmd_verify`` /
  ``cmd_reject`` in ``review.py``.
* **Dry-run by default, idempotent on apply.** The default invocation
  prints the drift table and exits 0 having written nothing; a second
  run immediately after ``--apply`` reports no remaining drift (the
  one deliberate exception: ``keep`` rows for stalled entries persist —
  they are informational, not actionable, and stalling is resolved by a
  human re-reject, never by sync).

A GitHub→local *rejection* pull has no notes to import (reject notes
live only in state.json; the label carries none), so it is recorded
with :data:`SYNC_REJECT_NOTES` and immediately **parked** via
:func:`record_iteration` — otherwise the refine daemon would dispatch a
20–45 min Aristotle iteration whose prompt is the placeholder text
verbatim (the reject-notes-verbatim bypass, ``refine.py``). The
placeholder tells the human to re-reject with real notes, which
un-parks it through the normal flow.

Reads are bulk-first: one :func:`fetch_issues_bulk` GraphQL call for
every registered issue, with the documented per-issue fallback (the
Phase-0 N+1 fix).
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

import marathon.review.state as review_state
from marathon.review.config import ReviewConfig, load_config
from marathon.review.github import fetch_issues_bulk, gh, issue_labels
from marathon.review.state import (
    VERDICTS_RELPATH,
    load_state,
    record_iteration,
    record_rejection,
    record_verification,
)


# --- proposed-action kinds -----------------------------------------------------

# Inbound (GitHub → local; applied with --apply).
ACTION_PULL_VERIFY = "pull-verify"
ACTION_PULL_REJECT = "pull-reject"
# Outbound (local → GitHub; applied only with --apply --push-labels).
ACTION_PUSH_VERIFIED = "push-verified"
ACTION_PUSH_REJECTED = "push-rejected"
# Informational rows — reported, never applied.
ACTION_KEEP = "keep"    # local-only operational state preserved (stalled)
ACTION_SKIP = "skip"    # GitHub labels unfetchable for this issue

_INBOUND = frozenset({ACTION_PULL_VERIFY, ACTION_PULL_REJECT})
_OUTBOUND = frozenset({ACTION_PUSH_VERIFIED, ACTION_PUSH_REJECTED})
_ACTIONABLE = _INBOUND | _OUTBOUND


# Placeholder notes for a GitHub-label-only rejection (the real notes
# were never recorded locally — or were lost to the documented
# branch-reset clobber of the tracked state.json). Opens with "- **" so
# `render_pending_rejections_md` keeps it verbatim, and says exactly
# what the human must do, because these notes are what Aristotle would
# otherwise be prompted with.
SYNC_REJECT_NOTES = (
    "- **Rejection synced from GitHub** — this issue carries the "
    "rejected label on GitHub but the local queue had no matching "
    "entry, so the original reject notes are unavailable. Sync parked "
    "this entry (the refine daemon will NOT dispatch an iteration from "
    "it); re-run `marathon review reject <issue> --notes <real notes>` "
    "to queue an actual fix iteration."
)


@dataclass(frozen=True)
class DriftRow:
    """One issue whose GitHub/local verdict states diverge (or whose
    divergence is deliberately kept / unknowable)."""

    issue: int
    chapter: Optional[int]          # None: issue not in any registry
    github_status: Optional[str]    # 'verified'/'rejected'/'inflight'/None
    local_status: Optional[str]     # 'verified'/'rejected'/'stalled'/None
    action: str                     # one of the ACTION_* kinds
    detail: str                     # human-readable proposed action


@dataclass(frozen=True)
class DriftReport:
    rows: list[DriftRow] = field(default_factory=list)
    issues_checked: int = 0

    @property
    def actionable(self) -> list[DriftRow]:
        """Rows that --apply would act on (keep/skip rows excluded —
        this is what makes apply-then-rerun report clean even while a
        stalled entry's 'kept' row persists)."""
        return [r for r in self.rows if r.action in _ACTIONABLE]


@dataclass(frozen=True)
class ApplySummary:
    pulled: int = 0     # inbound verdict updates recorded locally
    pushed: int = 0     # outbound label edits sent to GitHub
    deferred: int = 0   # outbound rows listed but --push-labels absent


# --- pure policy core ----------------------------------------------------------


def _status_from_labels(cfg: ReviewConfig, labels: set[str]) -> Optional[str]:
    """Label set → 'verified' / 'inflight' / 'rejected' / None.

    Mirrors ``review._status_from_labels`` (same precedence: verified >
    inflight > rejected). Duplicated rather than imported: that helper
    is private to the command-handler module, and sync's import surface
    is deliberately limited to config/state/github/ledger so the policy
    core stays testable without dragging in the verify/reject handlers.
    """
    if cfg.labels.verified in labels:
        return "verified"
    if cfg.labels.inflight in labels:
        return "inflight"
    if cfg.labels.rejected in labels:
        return "rejected"
    return None


def propose_action(
    github_status: Optional[str], local_status: Optional[str]
) -> Optional[str]:
    """The whole reconciliation policy as one pure decision table.

    Returns an ACTION_* kind, or ``None`` when the two states agree
    (no row at all). Inputs: ``github_status`` from the label mapping
    ('verified'/'inflight'/'rejected'/None = no verdict labels);
    ``local_status`` from state.json ('verified'/'rejected'/'stalled'/
    None = no entry).
    """
    if github_status == "verified":
        # GitHub's human verdict wins — including over a local stall:
        # a verify on GitHub supersedes the stalled rejection outright
        # (record_verification clears the queue entry), unlike a
        # still-rejected label, which leaves the stall alone below.
        return None if local_status == "verified" else ACTION_PULL_VERIFY
    if github_status == "rejected":
        if local_status == "rejected":
            return None  # in sync — local notes/attempts are richer, kept
        if local_status == "stalled":
            # The daemon exhausted its retry budget; GitHub still says
            # rejected, i.e. no NEW human verdict. Local operational
            # state is never overwritten by sync.
            return ACTION_KEEP
        return ACTION_PULL_REJECT  # absent, or stale local 'verified'
    if github_status == "inflight":
        # 'in-flight-fix' is GitHub-side operational state, not a human
        # verdict: compatible with a local rejection/stall (a fix IS in
        # flight) and with no local entry. Only a local verified verdict
        # is missing from GitHub here.
        return ACTION_PUSH_VERIFIED if local_status == "verified" else None
    # github_status is None: no verdict labels on the issue at all.
    if local_status == "verified":
        return ACTION_PUSH_VERIFIED
    if local_status in ("rejected", "stalled"):
        # A stalled entry's underlying HUMAN verdict is the rejection
        # (stalling is daemon bookkeeping on top of it) — that verdict
        # is what GitHub is missing.
        return ACTION_PUSH_REJECTED
    return None  # unreviewed on both sides


def _detail_for(cfg: ReviewConfig, action: str) -> str:
    return {
        ACTION_PULL_VERIFY: "pull: record verification locally (source='sync')",
        ACTION_PULL_REJECT: (
            "pull: record rejection locally (source='sync'; placeholder "
            "notes, parked until re-rejected)"
        ),
        ACTION_PUSH_VERIFIED: (
            f"push: gh label → {cfg.labels.verified} "
            "(needs --apply --push-labels)"
        ),
        ACTION_PUSH_REJECTED: (
            f"push: gh label → {cfg.labels.rejected} "
            "(needs --apply --push-labels)"
        ),
        ACTION_KEEP: "local-only state (kept)",
        ACTION_SKIP: "GitHub labels unavailable; skipped",
    }[action]


# --- drift computation ----------------------------------------------------------


def _issue_nums(cfg: ReviewConfig, chapter: Optional[int]) -> list[int]:
    """Issues to reconcile, in registry order.

    Chapter-scoped: that chapter's registry only (state.json entries
    outside it are ignored, matching ``pending_rejections``' chapter
    semantics). Project-wide: every registered issue, plus any
    state.json entry not in ANY registry — an unregistered local
    verdict is exactly the kind of orphan a reconciliation job exists
    to surface.
    """
    if chapter is not None:
        registry = cfg.chapter_registry(chapter)  # sys.exits if unknown
        nums = [num for num, _ in registry.entries]
    else:
        nums = [
            num
            for chap in sorted(cfg.chapters)
            for num, _ in cfg.chapters[chap].entries
        ]
        known = set(nums)
        state = load_state(cfg)
        nums.extend(sorted(n for n in state.issues if n not in known))
    return list(dict.fromkeys(nums))  # dedupe, order-preserving


def _fetch_labels(
    cfg: ReviewConfig, nums: list[int]
) -> dict[int, Optional[set[str]]]:
    """Bulk-fetch every issue's labels; per-issue fallback for misses.

    ``None`` for an issue means its labels could not be determined at
    all (bulk node absent AND per-issue view failed) — the caller must
    treat that as "skip", never as "unlabeled" (pushing or pulling
    against unknown labels would manufacture drift).
    """
    meta = fetch_issues_bulk(nums, cfg.github_repo)
    if meta is None:
        print(
            "  warn: bulk GraphQL issue fetch failed; "
            "falling back to one `gh issue view` per issue (slower)",
            file=sys.stderr,
        )
    out: dict[int, Optional[set[str]]] = {}
    for n in nums:
        if meta is not None and n in meta:
            out[n] = meta[n]["labels"]
        else:
            out[n] = issue_labels(n, cfg.github_repo)  # None on failure
    return out


def compute_drift(cfg: ReviewConfig, chapter: Optional[int] = None) -> DriftReport:
    """Reconciliation read pass: bulk-fetch GitHub labels, load
    state.json, and classify every registered issue through
    :func:`propose_action`. Pure read — writes nothing anywhere.

    Returns only the rows worth showing (divergent, kept, or
    unfetchable); issues whose states agree produce no row, so an
    empty ``rows`` list IS the no-drift signal.
    """
    nums = _issue_nums(cfg, chapter)
    state = load_state(cfg)
    labels_by_issue = _fetch_labels(cfg, nums)

    rows: list[DriftRow] = []
    for n in nums:
        labels = labels_by_issue.get(n)
        entry = state.get(n)
        local_status = entry.status if entry is not None else None
        if labels is None:
            # Unknown GitHub state: report it (if there is anything
            # local to disagree with) but never act on it.
            if local_status is None:
                continue
            action: Optional[str] = ACTION_SKIP
            github_status: Optional[str] = None
        else:
            github_status = _status_from_labels(cfg, labels)
            action = propose_action(github_status, local_status)
        if action is None:
            continue
        rows.append(
            DriftRow(
                issue=n,
                chapter=cfg.chapter_of_issue(n),
                github_status=github_status,
                local_status=local_status,
                action=action,
                detail=_detail_for(cfg, action),
            )
        )
    return DriftReport(rows=rows, issues_checked=len(nums))


# --- applying drift ---------------------------------------------------------------


@contextmanager
def _sync_tagged_verdicts() -> Iterator[None]:
    """Make verdicts recorded inside this block carry ``source='sync'``.

    WHY this exists: the binding policy requires inbound updates to go
    *via the existing* :func:`record_verification` /
    :func:`record_rejection` — they own the verdict semantics (attempts
    reset, un-stall, atomic state.json save) and the Phase-1 dual-write
    — but those helpers hard-code ``source='cli'`` in both mirrors (the
    shim predates sync, and this change may not modify ``state.py``).
    Mislabeling sync-applied verdicts as operator keystrokes would
    falsify exactly the provenance the tracked JSONL exists to keep, so
    for the duration of one apply we swap ``state.py``'s two private
    mirror hooks for source-aware equivalents:

    * the JSONL writer is re-implemented with the identical record
      shape/key order (it is append-only — a written line can never be
      retagged after the fact);
    * the ledger hook delegates the source-free ``issues``-row mirror
      to the original (keeping its fail-soft + warn-once semantics) and
      appends the ``verdict_events`` history row itself, through the
      public :class:`Ledger` API, with ``source='sync'``.

    Single-threaded CLI, restored in ``finally`` — the swap can't leak
    into a daemon or a concurrent command. Follow-up for the cutover
    phase: grow ``record_*`` a ``source=`` parameter and delete this.
    """
    orig_jsonl = review_state._append_verdict_jsonl
    orig_upsert = review_state._ledger_upsert

    def jsonl_sync(cfg, issue_num, verdict, notes, ts):
        try:
            path = cfg.repo_dir / VERDICTS_RELPATH
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "issue": issue_num,
                "verdict": verdict,
                "notes": notes,
                "ts": ts,
                "source": "sync",
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            print(
                f"  warning: could not append to {VERDICTS_RELPATH} ({e}); "
                "verdict recorded in state.json only"
            )

    def upsert_sync(cfg, issue_num, entry, verdict_event=None):
        orig_upsert(cfg, issue_num, entry, verdict_event=None)
        if verdict_event is None:
            return
        try:
            from marathon.ledger import Ledger  # lazy, like the shim

            Ledger.for_review_config(cfg).append_verdict_event(
                issue_num,
                verdict_event,
                notes=entry.notes,
                ts=entry.verdict_ts,
                source="sync",
            )
        except Exception as e:  # noqa: BLE001 — ledger is never load-bearing
            review_state._warn_ledger_once(e)

    review_state._append_verdict_jsonl = jsonl_sync
    review_state._ledger_upsert = upsert_sync
    try:
        yield
    finally:
        review_state._append_verdict_jsonl = orig_jsonl
        review_state._ledger_upsert = orig_upsert


def _push_label(cfg: ReviewConfig, issue_num: int, add: str, remove: str) -> None:
    """One outbound label edit — same shape (and same ``check=False``
    best-effort semantics) as the label flips in ``cmd_verify`` /
    ``cmd_reject``."""
    gh(
        "issue", "edit", str(issue_num),
        "--repo", cfg.github_repo,
        "--add-label", add,
        "--remove-label", remove,
        check=False,
    )


def apply_drift(
    cfg: ReviewConfig,
    rows: list[DriftRow],
    *,
    push_labels: bool = False,
) -> ApplySummary:
    """Perform the proposed actions from a drift report.

    Inbound rows always apply; outbound rows apply only when
    ``push_labels`` is set (otherwise they are counted as deferred so
    the caller can say how to release them). Keep/skip rows are never
    touched. Idempotent: every action moves the pair of states to
    agreement, so re-running :func:`compute_drift` right after yields
    no actionable rows.
    """
    pulled = pushed = deferred = 0
    for row in rows:
        if row.action == ACTION_PULL_VERIFY:
            with _sync_tagged_verdicts():
                record_verification(cfg, row.issue)
            print(f"  #{row.issue}: recorded verification locally (source='sync')")
            pulled += 1
        elif row.action == ACTION_PULL_REJECT:
            with _sync_tagged_verdicts():
                record_rejection(cfg, row.issue, SYNC_REJECT_NOTES)
                # Park immediately: the placeholder notes must never
                # become an Aristotle prompt (reject notes go to the
                # prover verbatim). Setting last_iteration_ts on a row
                # sync itself just created is not "overwriting" local
                # operational state — there was none.
                record_iteration(cfg, row.issue)
            print(
                f"  #{row.issue}: recorded rejection locally (source='sync'; "
                "parked — re-reject with real notes to queue a fix)"
            )
            pulled += 1
        elif row.action in _OUTBOUND:
            if not push_labels:
                deferred += 1
                continue
            if row.action == ACTION_PUSH_VERIFIED:
                add, remove = cfg.labels.verified, cfg.labels.rejected
            else:
                add, remove = cfg.labels.rejected, cfg.labels.verified
            _push_label(cfg, row.issue, add, remove)
            print(f"  #{row.issue}: pushed label {add} (removed {remove})")
            pushed += 1
        # keep / skip: deliberately untouched.
    return ApplySummary(pulled=pulled, pushed=pushed, deferred=deferred)


# --- CLI ---------------------------------------------------------------------------


def print_drift_report(report: DriftReport, chapter: Optional[int] = None) -> None:
    scope = f"chapter {chapter}" if chapter is not None else "all registered chapters"
    if not report.rows:
        print(
            f"sync: no drift — GitHub labels and local state agree for "
            f"{report.issues_checked} issue(s) ({scope})."
        )
        return
    print(
        f"GitHub ↔ local review-state drift ({scope}; "
        f"{report.issues_checked} issue(s) checked):\n"
    )
    print(f"  {'issue':>6}  {'chapter':>7}  {'github':<11}  {'local':<8}  proposed action")
    print(f"  {'-----':>6}  {'-------':>7}  {'------':<11}  {'-----':<8}  ---------------")
    for row in report.rows:
        chap = str(row.chapter) if row.chapter is not None else "-"
        github = (
            "unfetchable" if row.action == ACTION_SKIP
            else (row.github_status or "none")
        )
        local = row.local_status or "none"
        print(
            f"  #{row.issue:>5}  {chap:>7}  {github:<11}  {local:<8}  {row.detail}"
        )
    print()


def cmd_sync(args) -> None:
    """``marathon review sync [--chapter N] [--apply] [--push-labels]``.

    Default is a pure read: print the drift table, exit 0. ``--apply``
    performs the inbound updates; outbound label pushes additionally
    require ``--push-labels``.
    """
    if getattr(args, "push_labels", False) and not args.apply:
        sys.exit(
            "--push-labels only takes effect together with --apply "
            "(sync is dry-run by default; nothing was changed)"
        )
    cfg = load_config()
    report = compute_drift(cfg, chapter=args.chapter)
    print_drift_report(report, chapter=args.chapter)

    actionable = report.actionable
    if not args.apply:
        if actionable:
            inbound = sum(1 for r in actionable if r.action in _INBOUND)
            outbound = sum(1 for r in actionable if r.action in _OUTBOUND)
            hint = (
                f"dry-run: nothing changed. Re-run with --apply to perform "
                f"{inbound} inbound update(s)"
            )
            if outbound:
                hint += f"; add --push-labels for {outbound} outbound label push(es)"
            print(hint + ".")
        return
    if not actionable:
        print("nothing to apply.")
        return
    summary = apply_drift(cfg, report.rows, push_labels=args.push_labels)
    line = (
        f"sync applied: {summary.pulled} inbound update(s), "
        f"{summary.pushed} outbound label push(es)"
    )
    if summary.deferred:
        line += (
            f"; {summary.deferred} outbound push(es) NOT sent "
            "(re-run with --apply --push-labels)"
        )
    print(line + ".")

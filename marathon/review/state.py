"""Per-issue review state for the rejection queue.

`marathon review reject 22 --notes …` records the rejection here (in a
project-wide JSON file under ``.marathon/review/state.json``) rather
than appending bullets to ``referee.md``. This decouples per-issue
review state from the chapter-wide rubric layer, fixing several
failure modes of the prior referee.md-as-queue design:

* **Auto-nesting.** The referee.md queue wrapped every reject under a
  fixed ``- **Review #N REJECTED**`` heading and indented notes by two
  spaces, producing double-indentation when notes themselves opened
  with a top-level bullet.
* **No dedup or update.** Each ``marathon review reject`` of the same
  issue appended *another* block; stale entries accumulated and
  ``marathon review verify`` never cleared the corresponding queue
  entry from referee.md.
* **Cross-issue contamination.** Entries for issue #M diluted Hermes's
  read when working on #N.
* **Stale top-level notes.** Per-issue rejections living in
  referee.md's user header never aged out when the issue flipped to
  verified.

The state file is project-wide (one file across all chapters); the
daemon's chapter filter is applied at query time via the chapter
registry in ``ReviewConfig``.

Schema (versioned for forward compatibility)::

    {
      "schema_version": 1,
      "issues": {
        "<issue_num>": {
          "status": "rejected" | "verified" | "stalled",
          "verdict_ts": "2026-05-15T20:52:04-04:00",
          "notes": "...markdown body...",         // present iff status is
                                                  // "rejected" or "stalled"
          "last_iteration_ts": "...iso 8601...",  // optional; set by the
                                                  // refine daemon after
                                                  // dispatching an
                                                  // iteration for this
                                                  // issue. A rejection
                                                  // "needs iteration"
                                                  // iff `last_iteration_ts`
                                                  // is absent OR strictly
                                                  // less than `verdict_ts`.
          "attempts": 2                           // optional (absent ⇒ 0);
                                                  // consecutive FAILED refine
                                                  // dispatches since the
                                                  // current verdict_ts.
                                                  // Incremented by the daemon
                                                  // on non-zero refine exit;
                                                  // reset to 0 by
                                                  // record_rejection /
                                                  // record_verification.
        }
      }
    }

The ``"stalled"`` status is set by the daemon (via :func:`record_stall`)
after a rejection accumulates ``--max-attempts`` consecutive failed
refine dispatches. A stalled entry keeps its notes / verdict_ts /
attempt count for post-mortems, but no longer satisfies
``needs_iteration`` — the daemon stops retrying it. Re-rejecting the
issue (:func:`record_rejection`) resets the counter and returns the
entry to the normal pending flow. Older state files predating the
``attempts`` field load fine (the field defaults to 0); the schema
change is purely additive, so ``schema_version`` stays at 1.

The ``last_iteration_ts`` field lets the daemon track *per-issue*
queue progress, rather than treating the whole pending-rejections
set as a single batch. The prior single-hash design lost any
rejection that arrived during a refine iteration — see
``pending_rejections_needing_iteration`` for the corrected query.

Concurrent writes are not protected; the CLI's per-issue command
granularity makes overlap rare and the worst-case is a lost write
that the next ``reject``/``verify`` reapplies.

**Phase-1 dual-write (docs/marathon-v2-plan.md §3 Phase 1).** Every
``record_*`` write additionally mirrors into the SQLite ledger at
``<repo>/.marathon/marathon.db`` (``marathon.ledger``), and the two
human-verdict writes (:func:`record_rejection` /
:func:`record_verification`) also append one line to the TRACKED
append-only ``verdicts.jsonl`` beside this file. Reads stay on
state.json — the ledger is write-only here until a later-phase cutover,
so any ledger failure degrades to legacy-only behavior with ONE printed
warning per process and never breaks a verdict.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from marathon.review.config import ReviewConfig


SCHEMA_VERSION = 1
STATE_RELPATH = Path(".marathon/review/state.json")
# Tracked, append-only, merge-friendly verdict log (one JSON object per
# line, stable key order, never rewritten — the wall-time-v2-sidecar
# pattern). Exists for git provenance of human verdicts: the
# GeometricAnalysis operator hand-made PR #74 just to record a verify
# in git, which this file makes a one-line auto-appended diff instead.
VERDICTS_RELPATH = Path(".marathon/review/verdicts.jsonl")


@dataclass
class IssueState:
    status: str                              # "rejected", "verified" or "stalled"
    verdict_ts: str                          # ISO 8601
    notes: Optional[str] = None              # markdown; only when status is
                                             # "rejected" (or "stalled", which
                                             # preserves the rejection notes)
    last_iteration_ts: Optional[str] = None  # ISO 8601; set by the daemon
                                             # after dispatching an iteration
                                             # for this issue. None ⇒ never
                                             # iterated since current
                                             # verdict_ts.
    attempts: int = 0                        # consecutive FAILED refine
                                             # dispatches since the current
                                             # verdict_ts. Incremented by the
                                             # daemon (record_failed_attempt);
                                             # reset to 0 on any new verdict.
                                             # Default 0 keeps older state
                                             # files (which lack the field)
                                             # loading unchanged.

    def needs_iteration(self) -> bool:
        """A rejection needs iteration iff status=='rejected' and either
        it has never been iterated, or the last iteration is older than
        the current verdict (i.e., the rejection was re-recorded after
        the last iteration).

        "stalled" entries deliberately fail this test: the daemon
        exhausted its retry budget on them, and only a fresh human
        verdict (re-reject) re-queues them."""
        if self.status != "rejected":
            return False
        if self.last_iteration_ts is None:
            return True
        return self.last_iteration_ts < self.verdict_ts


@dataclass
class ReviewState:
    issues: dict[int, IssueState]

    def get(self, issue_num: int) -> Optional[IssueState]:
        return self.issues.get(issue_num)

    def to_json(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "issues": {
                str(num): {
                    "status": st.status,
                    "verdict_ts": st.verdict_ts,
                    **({"notes": st.notes} if st.notes is not None else {}),
                    **(
                        {"last_iteration_ts": st.last_iteration_ts}
                        if st.last_iteration_ts is not None
                        else {}
                    ),
                    # Omitted when 0, matching the other optional fields'
                    # absent-means-default convention — keeps state files
                    # written before the retry feature byte-identical on
                    # round-trip.
                    **({"attempts": st.attempts} if st.attempts else {}),
                }
                for num, st in sorted(self.issues.items())
            },
        }


def state_path(cfg: ReviewConfig) -> Path:
    return cfg.repo_dir / STATE_RELPATH


def load_state(cfg: ReviewConfig) -> ReviewState:
    """Load state.json. Returns an empty state if the file doesn't exist
    or is unparseable (logged but non-fatal — callers can `record_*`
    against a fresh empty state)."""
    path = state_path(cfg)
    if not path.is_file():
        return ReviewState(issues={})
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"  warning: {path} unparseable ({e}); treating as empty state")
        return ReviewState(issues={})
    raw_issues = data.get("issues", {})
    issues: dict[int, IssueState] = {}
    for key, val in raw_issues.items():
        try:
            issues[int(key)] = IssueState(
                status=val["status"],
                verdict_ts=val["verdict_ts"],
                notes=val.get("notes"),
                last_iteration_ts=val.get("last_iteration_ts"),
                # Absent in state files written before the daemon grew
                # retry tracking — default to 0, never an error.
                attempts=int(val.get("attempts", 0)),
            )
        except (KeyError, ValueError) as e:
            print(f"  warning: {path} issue {key!r} malformed ({e}); skipping")
    return ReviewState(issues=issues)


def save_state(cfg: ReviewConfig, state: ReviewState) -> None:
    """Atomic-ish write: write to ``state.json.tmp`` then rename."""
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_json(), indent=2) + "\n")
    tmp.replace(path)


def _now_iso() -> str:
    # Local-timezone ISO 8601 with offset, matching the prior verdict-ts
    # style seen in issue comments. astimezone() picks the local TZ.
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --- Phase-1 dual-write shim (docs/marathon-v2-plan.md §3 Phase 1) ----------
#
# state.json stays the read-side truth; the ledger and verdicts.jsonl
# are write-only mirrors until a later-phase cutover. The binding
# constraint: a missing/uninitializable ledger must NEVER break a
# verdict — degrade to legacy-only with one printed warning.

# Process-wide once-flag for the ledger-failure warning. Once the
# ledger has failed it will keep failing for the same reason (missing
# parent dir, locked db, newer schema), and the daemon calls record_*
# in a loop — warn on the first failure, stay silent after.
_ledger_warn_emitted = False


def _warn_ledger_once(exc: Exception) -> None:
    global _ledger_warn_emitted
    if _ledger_warn_emitted:
        return
    _ledger_warn_emitted = True
    print(
        f"  warning: ledger write failed ({exc}); continuing with legacy "
        "state.json only (further ledger warnings suppressed)"
    )


def _ledger_upsert(
    cfg: ReviewConfig,
    issue_num: int,
    entry: IssueState,
    verdict_event: Optional[str] = None,
) -> None:
    """Best-effort mirror of one state.json write into the ledger.

    Mirrors the just-saved ``entry`` as the issue's latest-verdict row;
    when ``verdict_event`` is given ("rejected"/"verified" — i.e. a
    human verdict, not daemon bookkeeping), also appends one row to the
    append-only ``verdict_events`` history. The ledger import is lazy
    and the whole body is fail-soft: Phase 1 forbids the ledger from
    being load-bearing, so ANY failure (no db, locked db, future
    schema) warns once and returns — the legacy write already
    succeeded."""
    try:
        from marathon.ledger import Ledger  # lazy: keep read paths ledger-free

        ledger = Ledger.for_review_config(cfg)
        ledger.upsert_issue(
            issue_num,
            chapter=cfg.chapter_of_issue(issue_num),
            status=entry.status,
            verdict_ts=entry.verdict_ts,
            notes=entry.notes,
            attempts=entry.attempts,
            last_iteration_ts=entry.last_iteration_ts,
        )
        if verdict_event is not None:
            ledger.append_verdict_event(
                issue_num,
                verdict_event,
                notes=entry.notes,
                ts=entry.verdict_ts,
                source="cli",
            )
    except Exception as e:  # noqa: BLE001 — never break verdict recording
        _warn_ledger_once(e)


def _append_verdict_jsonl(
    cfg: ReviewConfig,
    issue_num: int,
    verdict: str,
    notes: Optional[str],
    ts: str,
) -> None:
    """Append one human verdict to the TRACKED ``verdicts.jsonl``.

    Append-only by contract: one self-contained JSON object per line,
    never rewritten, so parallel branches that each append distinct
    verdicts merge line-by-line without conflicts (the same pattern
    that fixed the wall-time counter-merge race). Unlike state.json —
    which only keeps the latest verdict per issue — this file is the
    durable git history of every verdict ever issued. Failures are
    non-fatal (warn and continue): the verdict already landed in
    state.json."""
    try:
        path = cfg.repo_dir / VERDICTS_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "issue": issue_num,
            "verdict": verdict,
            "notes": notes,
            "ts": ts,
            "source": "cli",
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(
            f"  warning: could not append to {VERDICTS_RELPATH} ({e}); "
            "verdict recorded in state.json only"
        )


def record_rejection(cfg: ReviewConfig, issue_num: int, notes: str) -> IssueState:
    """Record a rejection: status=rejected, notes set, timestamp now.
    Overwrites any prior state for this issue. Resets ``last_iteration_ts``
    to None so the daemon will re-dispatch — re-rejecting an issue that
    was already iterated against re-queues it correctly.

    Also resets ``attempts`` to 0, which is what un-stalls a "stalled"
    entry: a fresh human verdict means the daemon should retry from a
    clean slate (the stall notification on the GitHub issue tells the
    human exactly this). Implemented here — rather than in the daemon —
    so every rejection entry point (``marathon review reject``, future
    callers) gets the reset for free.

    Phase-1 dual-write: also mirrored into the ledger (issues row + one
    verdict_events row) and appended to the tracked ``verdicts.jsonl``;
    both are best-effort and never block the legacy write."""
    state = load_state(cfg)
    entry = IssueState(
        status="rejected",
        verdict_ts=_now_iso(),
        notes=notes.strip(),
        last_iteration_ts=None,
        attempts=0,
    )
    state.issues[issue_num] = entry
    save_state(cfg, state)
    _append_verdict_jsonl(cfg, issue_num, "rejected", entry.notes, entry.verdict_ts)
    _ledger_upsert(cfg, issue_num, entry, verdict_event="rejected")
    return entry


def record_verification(cfg: ReviewConfig, issue_num: int) -> IssueState:
    """Record a verification: status=verified, notes cleared,
    ``last_iteration_ts`` cleared. This is the canonical way to clear a
    prior rejection's queue entry.

    Phase-1 dual-write: also mirrored into the ledger (issues row + one
    verdict_events row) and appended to the tracked ``verdicts.jsonl``;
    both are best-effort and never block the legacy write."""
    state = load_state(cfg)
    entry = IssueState(
        status="verified",
        verdict_ts=_now_iso(),
        notes=None,
        last_iteration_ts=None,
        attempts=0,
    )
    state.issues[issue_num] = entry
    save_state(cfg, state)
    _append_verdict_jsonl(cfg, issue_num, "verified", None, entry.verdict_ts)
    _ledger_upsert(cfg, issue_num, entry, verdict_event="verified")
    return entry


def record_iteration(cfg: ReviewConfig, issue_num: int) -> Optional[IssueState]:
    """Mark that the daemon dispatched a refine iteration for
    ``issue_num``. Sets ``last_iteration_ts = now()`` on the existing
    state entry. No-ops (with a warning) if no entry exists for the
    issue — the daemon should only dispatch against issues already in
    ``state.json``.

    Called by the refine daemon AFTER a refine subprocess exits
    *cleanly* (exit 0), so the issue is excluded from the next
    queue-pick and the human verdict (verify / re-reject) becomes the
    gate. Failed dispatches go through :func:`record_failed_attempt`
    instead — they must NOT consume the rejection (the old
    mark-iterated-regardless behavior silently dropped rejections on
    refine crashes and made the human the retry logic)."""
    state = load_state(cfg)
    entry = state.issues.get(issue_num)
    if entry is None:
        print(
            f"  warning: record_iteration(#{issue_num}) called but no "
            "state.json entry exists; skipping"
        )
        return None
    entry.last_iteration_ts = _now_iso()
    save_state(cfg, state)
    # Daemon bookkeeping, not a human verdict — mirror the row but
    # append no verdict_events entry.
    _ledger_upsert(cfg, issue_num, entry)
    return entry


def record_failed_attempt(cfg: ReviewConfig, issue_num: int) -> Optional[IssueState]:
    """Increment the failed-dispatch counter for ``issue_num`` after a
    refine subprocess exited non-zero (and was NOT interrupted by a
    daemon stop signal — interrupted runs record nothing at all).

    Deliberately does NOT touch ``last_iteration_ts``: the issue keeps
    satisfying ``needs_iteration`` and stays at the head of the
    daemon's dispatch queue, so the daemon retries it (after a
    backoff) instead of silently consuming the rejection. Returns the
    updated entry so the daemon can compare ``attempts`` against its
    retry budget; no-ops (with a warning, returning None) if no entry
    exists — e.g. a concurrent ``verify``/state edit removed it
    mid-iteration."""
    state = load_state(cfg)
    entry = state.issues.get(issue_num)
    if entry is None:
        print(
            f"  warning: record_failed_attempt(#{issue_num}) called but no "
            "state.json entry exists; skipping"
        )
        return None
    entry.attempts += 1
    save_state(cfg, state)
    # Daemon bookkeeping — mirror the row, no verdict event.
    _ledger_upsert(cfg, issue_num, entry)
    return entry


def record_stall(cfg: ReviewConfig, issue_num: int) -> Optional[IssueState]:
    """Flip ``issue_num`` to status="stalled" after the daemon exhausted
    its retry budget (``--max-attempts`` consecutive failed dispatches).

    A stalled entry keeps its notes, ``verdict_ts`` and ``attempts``
    for post-mortems, but ``needs_iteration`` is False for it, so the
    daemon stops picking it up. The daemon posts ONE notification
    comment on the GitHub issue when it stalls an entry; re-rejecting
    (:func:`record_rejection`) resets the counter and re-queues. No-ops
    (with a warning) if no entry exists."""
    state = load_state(cfg)
    entry = state.issues.get(issue_num)
    if entry is None:
        print(
            f"  warning: record_stall(#{issue_num}) called but no "
            "state.json entry exists; skipping"
        )
        return None
    entry.status = "stalled"
    save_state(cfg, state)
    # Daemon bookkeeping (the underlying human verdict is unchanged) —
    # mirror the row, no verdict event.
    _ledger_upsert(cfg, issue_num, entry)
    return entry


def pending_rejections(
    cfg: ReviewConfig, chapter: Optional[int] = None
) -> list[tuple[int, IssueState]]:
    """Return all currently-rejected issues (with their notes), filtered
    to ``chapter`` if given. Sorted by issue number.

    If ``chapter`` is supplied, only issues registered in that chapter's
    registry are returned. Unknown issues (not in any chapter registry)
    are returned only when ``chapter`` is ``None`` (project-wide query).

    Note: this returns ALL rejected issues, including ones the daemon
    has already iterated against once. "stalled" entries are NOT
    included — once the daemon gives up on a rejection, its notes also
    stop feeding the refine prompt context until the human re-rejects.
    For the daemon's queue-dispatch logic, use
    :func:`pending_rejections_needing_iteration` instead.
    """
    state = load_state(cfg)
    if chapter is None:
        candidates = state.issues.items()
    else:
        registry = cfg.chapter_registry(chapter)
        known = {num for num, _ in registry.entries}
        candidates = ((n, s) for n, s in state.issues.items() if n in known)
    out = [(num, st) for num, st in candidates if st.status == "rejected"]
    out.sort(key=lambda x: x[0])
    return out


def pending_rejections_needing_iteration(
    cfg: ReviewConfig, chapter: Optional[int] = None
) -> list[tuple[int, IssueState]]:
    """Subset of :func:`pending_rejections` that the daemon should still
    dispatch an iteration for: rejections that have never been iterated,
    or whose last iteration predates their current ``verdict_ts`` (e.g.
    the human re-rejected after a previous iteration).

    Sorted by ``verdict_ts`` ascending (oldest first), so the daemon
    can pop the head when picking the next focus issue — older
    rejections get attention first, which matches user intuition that
    a long-pending rejection is more urgent than a fresh one.
    """
    out = [
        (num, st)
        for num, st in pending_rejections(cfg, chapter)
        if st.needs_iteration()
    ]
    out.sort(key=lambda x: x[1].verdict_ts)
    return out


def render_pending_rejections_md(
    cfg: ReviewConfig,
    chapter: Optional[int] = None,
    focus_issue: Optional[int] = None,
) -> Optional[str]:
    """Render the pending rejections for ``chapter`` (or all chapters
    when None) as a Markdown block suitable for the Hermes/Claude
    prompt. Returns ``None`` if there are no pending rejections.

    Format mirrors what users wrote into referee.md before — top-level
    bullets, no auto-wrapper — so Hermes can read it identically.

    When ``focus_issue`` is supplied, only that one issue's rejection
    notes are rendered (or ``None`` if it isn't currently rejected).
    The daemon uses this for one-rejection-per-iteration dispatch:
    Hermes sees exactly the rejection the daemon picked, eliminating
    the prior pick-one-and-ignore-the-rest failure mode.
    """
    pending = pending_rejections(cfg, chapter)
    if focus_issue is not None:
        pending = [(n, s) for n, s in pending if n == focus_issue]
    if not pending:
        return None
    parts: list[str] = []
    for num, st in pending:
        # If the user's notes already begin with `- **`, keep them as-is.
        # Otherwise wrap with a stub header so Hermes always sees a
        # bullet boundary.
        notes = (st.notes or "").rstrip()
        if notes.lstrip().startswith("- **"):
            parts.append(notes)
        else:
            parts.append(f"- **Review #{num} REJECTED ({st.verdict_ts})**\n\n{notes}")
    return "\n\n".join(parts)


def hash_pending(cfg: ReviewConfig, chapter: Optional[int] = None) -> str:
    """SHA256 of the pending-rejections content for ``chapter`` (or
    project-wide when None). Used by the daemon to detect new rejections
    landing while it is in its poll loop.

    Returns ``''`` (empty string) when there are no pending rejections,
    matching the empty-file convention used previously."""
    rendered = render_pending_rejections_md(cfg, chapter)
    if not rendered:
        return ""
    return hashlib.sha256(rendered.encode()).hexdigest()

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

Every read-modify-write is serialized across both threads and processes.  The
lock lives at a stable path beside ``state.json`` (the data file itself is
atomically replaced, so locking that replaceable inode would be ineffective),
and combines an in-process :class:`threading.RLock` with ``flock``.  Human
verdict mirrors (``verdicts.jsonl`` + SQLite) are written while that same lock
is held, preserving verdict order across the three stores.

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
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from fcntl import LOCK_EX, LOCK_SH, LOCK_UN, flock
from pathlib import Path
from typing import Callable, Optional, TypeVar

from marathon.review.config import ReviewConfig


SCHEMA_VERSION = 1
STATE_RELPATH = Path(".marathon/review/state.json")
# Stable advisory-lock inode.  Never replace or unlink this file: state.json is
# replaced atomically on every write, so using state.json itself as the flock
# target would let two processes lock different inodes after a replace.
STATE_LOCK_RELPATH = Path(".marathon/review/runner-locks/state.lock")
# Tracked, append-only, merge-friendly verdict log (one JSON object per
# line, stable key order, never rewritten — the wall-time-v2-sidecar
# pattern). Exists for git provenance of human verdicts: the
# GeometricAnalysis operator hand-made PR #74 just to record a verify
# in git, which this file makes a one-line auto-appended diff instead.
VERDICTS_RELPATH = Path(".marathon/review/verdicts.jsonl")

# A parked rejection can be recovered in several materially different ways.
# Keep the choice beside the verdict generation so the conductor can apply it
# with the same compare-and-swap ownership guarantees as every other refine.
RECOVERY_RETRY_BUILD = "retry_build"
RECOVERY_CONTINUE_PR = "continue_pr"
RECOVERY_UPDATE_CONTINUE = "update_continue"
RECOVERY_START_OVER = "start_over"
RECOVERY_MODES = frozenset({
    RECOVERY_RETRY_BUILD,
    RECOVERY_CONTINUE_PR,
    RECOVERY_UPDATE_CONTINUE,
    RECOVERY_START_OVER,
})


def _validate_recovery_request(
    mode: Optional[str],
    recovery_id: Optional[str],
    pr: Optional[int],
    branch: Optional[str],
    head_sha: Optional[str],
    base: Optional[str],
) -> None:
    if mode is None:
        if recovery_id is not None:
            raise ValueError("recovery_id requires recovery_mode")
        return
    if mode not in RECOVERY_MODES:
        raise ValueError(f"unknown recovery mode {mode!r}")
    if not recovery_id:
        raise ValueError("recovery_id must be non-empty")
    supplied = (pr is not None, bool(branch), bool(head_sha))
    if mode != RECOVERY_START_OVER and not all(supplied):
        raise ValueError(f"{mode} requires PR, branch, and head SHA")
    if any(supplied) and not all(supplied):
        raise ValueError("recovery PR snapshot must be complete")
    if base and not all(supplied):
        raise ValueError("recovery_base requires a complete PR snapshot")
    if pr is not None and (isinstance(pr, bool) or pr <= 0):
        raise ValueError("recovery_pr must be positive")


class CorruptReviewStateError(RuntimeError):
    """A mutation refused to overwrite an unparseable review state file."""


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
    # A clean refine has finished, but the conductor could not determine
    # whether its branch has a PR because GitHub was temporarily unavailable.
    # While set, needs_iteration remains False and later conductor ticks retry
    # only the cheap PR check (never another Aristotle session).
    pending_pr_branch: Optional[str] = None
    # Branch produced by the clean iteration for this verdict. Unlike
    # pending_pr_branch (transient GitHub uncertainty), this remains until the
    # verdict changes/requeues and lets automerge reject stale/unowned PRs.
    iteration_branch: Optional[str] = None
    iteration_commit_sha: Optional[str] = None
    # Explicit recovery intent for a parked rejection.  ``None`` is the normal
    # first-dispatch path.  PR-preserving modes pin the exact remote head that
    # the human saw; publication later uses it as a force-with-lease token.
    recovery_mode: Optional[str] = None
    recovery_id: Optional[str] = None
    recovery_requested_ts: Optional[str] = None
    recovery_pr: Optional[int] = None
    recovery_branch: Optional[str] = None
    recovery_head_sha: Optional[str] = None
    recovery_base: Optional[str] = None

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
        try:
            last = datetime.fromisoformat(
                self.last_iteration_ts.replace("Z", "+00:00")
            )
            verdict = datetime.fromisoformat(self.verdict_ts.replace("Z", "+00:00"))
            return last < verdict
        except ValueError:
            # Malformed legacy timestamps retain the previous conservative
            # string behavior rather than making the entry unreadable.
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
                    **(
                        {"pending_pr_branch": st.pending_pr_branch}
                        if st.pending_pr_branch is not None
                        else {}
                    ),
                    **(
                        {"iteration_branch": st.iteration_branch}
                        if st.iteration_branch is not None
                        else {}
                    ),
                    **(
                        {"iteration_commit_sha": st.iteration_commit_sha}
                        if st.iteration_commit_sha is not None
                        else {}
                    ),
                    **(
                        {"recovery_mode": st.recovery_mode}
                        if st.recovery_mode is not None
                        else {}
                    ),
                    **(
                        {"recovery_id": st.recovery_id}
                        if st.recovery_id is not None
                        else {}
                    ),
                    **(
                        {"recovery_requested_ts": st.recovery_requested_ts}
                        if st.recovery_requested_ts is not None
                        else {}
                    ),
                    **(
                        {"recovery_pr": st.recovery_pr}
                        if st.recovery_pr is not None
                        else {}
                    ),
                    **(
                        {"recovery_branch": st.recovery_branch}
                        if st.recovery_branch is not None
                        else {}
                    ),
                    **(
                        {"recovery_head_sha": st.recovery_head_sha}
                        if st.recovery_head_sha is not None
                        else {}
                    ),
                    **(
                        {"recovery_base": st.recovery_base}
                        if st.recovery_base is not None
                        else {}
                    ),
                }
                for num, st in sorted(self.issues.items())
            },
        }


def state_path(cfg: ReviewConfig) -> Path:
    return cfg.repo_dir / STATE_RELPATH


def state_lock_path(cfg: ReviewConfig) -> Path:
    """Stable cross-process lock path for review state transactions."""
    return cfg.repo_dir / STATE_LOCK_RELPATH


# ``flock`` alone is process-scoped on some platforms and does not reliably
# serialize independent threads in one interpreter.  The RLock covers that
# layer and permits helpers in the same transaction to remain composable.
_STATE_RLOCK = threading.RLock()


@contextmanager
def _state_lock(cfg: ReviewConfig, *, exclusive: bool):
    """Hold the stable review-state lock until the surrounding operation ends."""
    lock_path = state_lock_path(cfg)
    with _STATE_RLOCK:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # a+ creates once but never truncates/replaces: the inode stays stable.
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            flock(lock_file.fileno(), LOCK_EX if exclusive else LOCK_SH)
            try:
                yield
            finally:
                flock(lock_file.fileno(), LOCK_UN)


def _load_state_unlocked(
    cfg: ReviewConfig, *, fail_on_corrupt: bool = False
) -> ReviewState:
    """Load state.json. Returns an empty state if the file doesn't exist
    or is unparseable (logged but non-fatal — callers can `record_*`
    against a fresh empty state). Caller must hold ``_state_lock``."""
    path = state_path(cfg)
    if not path.is_file():
        return ReviewState(issues={})
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"  warning: {path} unparseable ({e}); treating as empty state")
        if fail_on_corrupt:
            raise CorruptReviewStateError(
                f"refusing to overwrite corrupt review state at {path}: {e}"
            ) from e
        return ReviewState(issues={})
    if (
        not isinstance(data, dict)
        or data.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION
        or not isinstance(data.get("issues", {}), dict)
    ):
        message = f"{path} has an invalid/unsupported review-state schema"
        print(f"  warning: {message}; treating as empty state")
        if fail_on_corrupt:
            raise CorruptReviewStateError(f"refusing to overwrite: {message}")
        return ReviewState(issues={})
    raw_issues = data.get("issues", {})
    issues: dict[int, IssueState] = {}
    for key, val in raw_issues.items():
        try:
            issue_num = int(key)
            if not isinstance(val, dict):
                raise ValueError("entry is not an object")
            status = val["status"]
            verdict_ts = val["verdict_ts"]
            if status not in {"rejected", "verified", "stalled"}:
                raise ValueError(f"unknown status {status!r}")
            if not isinstance(verdict_ts, str) or not verdict_ts:
                raise ValueError("verdict_ts is not a non-empty string")
            pending_branch = val.get("pending_pr_branch")
            if pending_branch is not None and not isinstance(pending_branch, str):
                raise ValueError("pending_pr_branch is not a string")
            iteration_branch = val.get("iteration_branch")
            if iteration_branch is not None and not isinstance(iteration_branch, str):
                raise ValueError("iteration_branch is not a string")
            iteration_commit_sha = val.get("iteration_commit_sha")
            if iteration_commit_sha is not None and not isinstance(
                iteration_commit_sha, str
            ):
                raise ValueError("iteration_commit_sha is not a string")
            recovery_mode = val.get("recovery_mode")
            if recovery_mode is not None and recovery_mode not in RECOVERY_MODES:
                raise ValueError(f"unknown recovery_mode {recovery_mode!r}")
            recovery_id = val.get("recovery_id")
            if recovery_id is not None and not isinstance(recovery_id, str):
                raise ValueError("recovery_id is not a string")
            recovery_requested_ts = val.get("recovery_requested_ts")
            if recovery_requested_ts is not None and not isinstance(
                recovery_requested_ts, str
            ):
                raise ValueError("recovery_requested_ts is not a string")
            recovery_pr = val.get("recovery_pr")
            if recovery_pr is not None:
                recovery_pr = int(recovery_pr)
                if recovery_pr <= 0:
                    raise ValueError("recovery_pr is not positive")
            recovery_branch = val.get("recovery_branch")
            if recovery_branch is not None and not isinstance(recovery_branch, str):
                raise ValueError("recovery_branch is not a string")
            recovery_head_sha = val.get("recovery_head_sha")
            if recovery_head_sha is not None and not isinstance(
                recovery_head_sha, str
            ):
                raise ValueError("recovery_head_sha is not a string")
            recovery_base = val.get("recovery_base")
            if recovery_base is not None and (
                not isinstance(recovery_base, str) or not recovery_base.strip()
            ):
                raise ValueError("recovery_base is not a non-empty string")
            _validate_recovery_request(
                recovery_mode,
                recovery_id,
                recovery_pr,
                recovery_branch,
                recovery_head_sha,
                recovery_base,
            )
            issues[issue_num] = IssueState(
                status=status,
                verdict_ts=verdict_ts,
                notes=val.get("notes"),
                last_iteration_ts=val.get("last_iteration_ts"),
                # Absent in state files written before the daemon grew
                # retry tracking — default to 0, never an error.
                attempts=int(val.get("attempts", 0)),
                pending_pr_branch=pending_branch,
                iteration_branch=iteration_branch,
                iteration_commit_sha=iteration_commit_sha,
                recovery_mode=recovery_mode,
                recovery_id=recovery_id,
                recovery_requested_ts=recovery_requested_ts,
                recovery_pr=recovery_pr,
                recovery_branch=recovery_branch,
                recovery_head_sha=recovery_head_sha,
                recovery_base=recovery_base,
            )
        except (KeyError, TypeError, ValueError) as e:
            print(f"  warning: {path} issue {key!r} malformed ({e}); skipping")
            if fail_on_corrupt:
                raise CorruptReviewStateError(
                    f"refusing to overwrite malformed issue {key!r} in {path}: {e}"
                ) from e
    return ReviewState(issues=issues)


def load_state(cfg: ReviewConfig) -> ReviewState:
    """Load one consistent state snapshot under the stable shared lock."""
    with _state_lock(cfg, exclusive=False):
        return _load_state_unlocked(cfg)


def _save_state_unlocked(cfg: ReviewConfig, state: ReviewState) -> None:
    """Durably write then atomically replace state.json; caller holds lock."""
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(state.to_json(), indent=2) + "\n"
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Best-effort directory fsync makes the rename durable across a crash.
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def save_state(cfg: ReviewConfig, state: ReviewState) -> None:
    """Atomically replace state with a caller-provided complete snapshot.

    Production read-modify-write callers use :func:`_update_state` so their
    read is refreshed *inside* the same lock.  This lower-level API remains for
    migrations/tests that intentionally replace the whole document.
    """
    with _state_lock(cfg, exclusive=True):
        _save_state_unlocked(cfg, state)


_T = TypeVar("_T")


def _update_state(
    cfg: ReviewConfig,
    mutator: Callable[[ReviewState], tuple[_T, bool]],
    *,
    after_save: Optional[Callable[[_T], None]] = None,
) -> _T:
    """Fresh-load, mutate, and atomic-replace under one stable lock.

    ``mutator`` returns ``(result, changed)``.  It runs while the lock is held;
    ``after_save`` runs after the atomic JSON replacement but before releasing
    the lock.  Human-verdict callers use it for JSONL/SQLite mirrors, preserving
    the same total order across all stores.
    """
    with _state_lock(cfg, exclusive=True):
        state = _load_state_unlocked(cfg, fail_on_corrupt=True)
        result, changed = mutator(state)
        if changed:
            _save_state_unlocked(cfg, state)
            if after_save is not None:
                after_save(result)
        return result


def _now_iso() -> str:
    # Local-timezone ISO 8601 with offset, matching the prior verdict-ts
    # style seen in issue comments. astimezone() picks the local TZ.
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _next_verdict_ts(previous: Optional[str]) -> str:
    """A strictly-new verdict generation encoded as an ISO timestamp.

    Microseconds avoid the old same-second collision, while the explicit
    monotonic bump makes the token safe even under a frozen/coarse test clock.
    Completion callbacks compare this token exactly (CAS), not by wall time.
    """
    candidate = _now_iso()
    if not previous:
        return candidate
    try:
        current_dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        previous_dt = datetime.fromisoformat(previous.replace("Z", "+00:00"))
        if current_dt <= previous_dt:
            return (previous_dt + timedelta(microseconds=1)).isoformat(
                timespec="microseconds"
            )
    except ValueError:
        # A malformed legacy timestamp is still replaced with a fresh token.
        pass
    return candidate


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
    def mutate(state: ReviewState) -> tuple[IssueState, bool]:
        previous = state.issues.get(issue_num)
        entry = IssueState(
            status="rejected",
            verdict_ts=_next_verdict_ts(
                previous.verdict_ts if previous is not None else None
            ),
            notes=notes.strip(),
            last_iteration_ts=None,
            attempts=0,
            pending_pr_branch=None,
            iteration_branch=None,
            iteration_commit_sha=None,
        )
        state.issues[issue_num] = entry
        return entry, True

    def mirror(entry: IssueState) -> None:
        _append_verdict_jsonl(
            cfg, issue_num, "rejected", entry.notes, entry.verdict_ts
        )
        _ledger_upsert(cfg, issue_num, entry, verdict_event="rejected")

    return _update_state(cfg, mutate, after_save=mirror)


def record_rejection_if_current(
    cfg: ReviewConfig,
    issue_num: int,
    notes: str,
    *,
    expected_status: str,
    expected_verdict_ts: str,
) -> Optional[IssueState]:
    """CAS re-reject/requeue against one exact currently-observed verdict.

    Web/API flows should converge their external label transition first, then
    call this helper. A concurrent verify or newer re-reject returns ``None``
    instead of being overwritten. Successful transitions retain the same
    ordered state.json + verdict JSONL + ledger transaction as
    :func:`record_rejection`.
    """
    def mutate(state: ReviewState) -> tuple[Optional[IssueState], bool]:
        current = state.issues.get(issue_num)
        if (
            current is None
            or current.status != expected_status
            or current.verdict_ts != expected_verdict_ts
        ):
            return None, False
        entry = IssueState(
            status="rejected",
            verdict_ts=_next_verdict_ts(current.verdict_ts),
            notes=notes.strip(),
            last_iteration_ts=None,
            attempts=0,
            pending_pr_branch=None,
            iteration_branch=None,
            iteration_commit_sha=None,
        )
        state.issues[issue_num] = entry
        return entry, True

    def mirror(entry: Optional[IssueState]) -> None:
        if entry is None:
            return
        _append_verdict_jsonl(
            cfg, issue_num, "rejected", entry.notes, entry.verdict_ts
        )
        _ledger_upsert(cfg, issue_num, entry, verdict_event="rejected")

    return _update_state(cfg, mutate, after_save=mirror)


def record_rejection_if_absent(
    cfg: ReviewConfig,
    issue_num: int,
    notes: str,
    *,
    recovery_mode: Optional[str] = None,
    recovery_id: Optional[str] = None,
    recovery_pr: Optional[int] = None,
    recovery_branch: Optional[str] = None,
    recovery_head_sha: Optional[str] = None,
    recovery_base: Optional[str] = None,
) -> Optional[IssueState]:
    """Create a rejected verdict only if the issue still has no local row."""
    _validate_recovery_request(
        recovery_mode,
        recovery_id,
        recovery_pr,
        recovery_branch,
        recovery_head_sha,
        recovery_base,
    )

    def mutate(state: ReviewState) -> tuple[Optional[IssueState], bool]:
        if issue_num in state.issues:
            return None, False
        entry = IssueState(
            status="rejected",
            verdict_ts=_next_verdict_ts(None),
            notes=notes.strip(),
            last_iteration_ts=None,
            attempts=0,
            pending_pr_branch=None,
            iteration_branch=None,
            iteration_commit_sha=None,
            recovery_mode=recovery_mode,
            recovery_id=recovery_id,
            recovery_requested_ts=(_now_iso() if recovery_id else None),
            recovery_pr=recovery_pr,
            recovery_branch=recovery_branch,
            recovery_head_sha=recovery_head_sha,
            recovery_base=recovery_base,
        )
        state.issues[issue_num] = entry
        return entry, True

    def mirror(entry: Optional[IssueState]) -> None:
        if entry is None:
            return
        _append_verdict_jsonl(
            cfg, issue_num, "rejected", entry.notes, entry.verdict_ts
        )
        _ledger_upsert(cfg, issue_num, entry, verdict_event="rejected")

    return _update_state(cfg, mutate, after_save=mirror)


def request_recovery_if_current(
    cfg: ReviewConfig,
    issue_num: int,
    *,
    expected_status: str,
    expected_verdict_ts: str,
    expected_recovery_id: Optional[str] = None,
    recovery_id: str,
    recovery_mode: str,
    recovery_pr: Optional[int] = None,
    recovery_branch: Optional[str] = None,
    recovery_head_sha: Optional[str] = None,
    recovery_base: Optional[str] = None,
) -> Optional[IssueState]:
    """Queue an operational recovery without fabricating a human verdict.

    Unlike re-rejecting, this deliberately preserves ``verdict_ts``, the note,
    and the last published iteration identity.  It only makes the parked row
    dispatchable again and attaches an exact recovery operation.  Completion
    callbacks match ``recovery_id`` as well as the verdict generation, so an
    old worker can never consume a newer recovery request.
    """
    _validate_recovery_request(
        recovery_mode,
        recovery_id,
        recovery_pr,
        recovery_branch,
        recovery_head_sha,
        recovery_base,
    )

    def mutate(state: ReviewState) -> tuple[Optional[IssueState], bool]:
        current = state.issues.get(issue_num)
        if (
            current is None
            or current.status != expected_status
            or current.verdict_ts != expected_verdict_ts
            or current.recovery_id != expected_recovery_id
        ):
            return None, False
        current.status = "rejected"
        current.last_iteration_ts = None
        current.attempts = 0
        current.pending_pr_branch = None
        current.recovery_mode = recovery_mode
        current.recovery_id = recovery_id
        current.recovery_requested_ts = _now_iso()
        current.recovery_pr = recovery_pr
        current.recovery_branch = recovery_branch
        current.recovery_head_sha = recovery_head_sha
        current.recovery_base = recovery_base
        return current, True

    return _update_state(
        cfg,
        mutate,
        after_save=lambda entry: (
            _ledger_upsert(cfg, issue_num, entry) if entry is not None else None
        ),
    )


def advance_recovery_head_if_current(
    cfg: ReviewConfig,
    issue_num: int,
    *,
    expected_verdict_ts: Optional[str],
    expected_recovery_id: Optional[str],
    expected_head: str,
    new_head: str,
) -> Optional[IssueState]:
    """CAS-advance a recovery's pinned head to a revision we pushed ourselves.

    A refine attempt that publishes its checkpoint moves the remote branch
    past the head the recovery was requested at; every later attempt then
    refuses the "changed" remote even though the change is our own. The
    conductor calls this once it has *proved* the new remote head is the prior
    attempt's committed work, so the recovery continues instead of wedging.
    Fails (returns ``None``) whenever the row moved under us — a newer verdict
    or recovery choice always wins.
    """

    def mutate(state: ReviewState) -> tuple[Optional[IssueState], bool]:
        current = state.issues.get(issue_num)
        if (
            current is None
            or current.status != "rejected"
            or current.verdict_ts != expected_verdict_ts
            or current.recovery_id != expected_recovery_id
            or current.recovery_head_sha != expected_head
        ):
            return None, False
        current.recovery_head_sha = new_head
        return current, True

    return _update_state(
        cfg,
        mutate,
        after_save=lambda entry: (
            _ledger_upsert(cfg, issue_num, entry) if entry is not None else None
        ),
    )


def record_verification(cfg: ReviewConfig, issue_num: int) -> IssueState:
    """Record a verification: status=verified, notes cleared,
    ``last_iteration_ts`` cleared. This is the canonical way to clear a
    prior rejection's queue entry.

    Phase-1 dual-write: also mirrored into the ledger (issues row + one
    verdict_events row) and appended to the tracked ``verdicts.jsonl``;
    both are best-effort and never block the legacy write."""
    def mutate(state: ReviewState) -> tuple[IssueState, bool]:
        previous = state.issues.get(issue_num)
        entry = IssueState(
            status="verified",
            verdict_ts=_next_verdict_ts(
                previous.verdict_ts if previous is not None else None
            ),
            notes=None,
            last_iteration_ts=None,
            attempts=0,
            pending_pr_branch=None,
            iteration_branch=None,
            iteration_commit_sha=None,
        )
        state.issues[issue_num] = entry
        return entry, True

    def mirror(entry: IssueState) -> None:
        _append_verdict_jsonl(cfg, issue_num, "verified", None, entry.verdict_ts)
        _ledger_upsert(cfg, issue_num, entry, verdict_event="verified")

    entry = _update_state(cfg, mutate, after_save=mirror)
    # Freeze the verified statements into the approved-statements manifest so a
    # later fill/refine iteration can never change them (the fill guard reverts
    # any signature change). Best-effort — capture must never block a verify.
    try:
        from marathon.review.approved_statements import capture_on_verify

        capture_on_verify(cfg, issue_num)
    except Exception:  # noqa: BLE001
        pass
    return entry


_EXPECTED_RECOVERY_ID_UNSET = object()


def requeue_for_rereview(
    cfg: ReviewConfig,
    issue_num: int,
    *,
    expected_verdict_ts: Optional[str] = None,
    expected_status: str = "rejected",
    expected_recovery_id: object = _EXPECTED_RECOVERY_ID_UNSET,
) -> bool:
    """Drop an issue's local verdict so it RE-ENTERS the review queue — used
    after Aristotle's refine fix LANDS and the human must RE-REVIEW the result
    (reject -> refine -> merge -> re-review). Removes the ``state.json`` entry
    (the deck's read-side truth); paired with the caller removing the
    ``review:rejected`` GitHub label, the issue reads as unreviewed and becomes a
    ready card again. ``expected_recovery_id`` is a generation CAS; passing
    ``None`` explicitly requires that no newer operational recovery has been
    queued, while omitting it preserves compatibility for non-worker callers.
    Returns True iff an entry was removed. Best-effort — no ledger / GitHub /
    verdicts.jsonl write of its own."""
    def mutate(state: ReviewState) -> tuple[bool, bool]:
        entry = state.issues.get(issue_num)
        if entry is None:
            return False, False
        if expected_verdict_ts is not None and (
            entry.status != expected_status or entry.verdict_ts != expected_verdict_ts
        ):
            return False, False
        if (
            expected_recovery_id is not _EXPECTED_RECOVERY_ID_UNSET
            and entry.recovery_id != expected_recovery_id
        ):
            return False, False
        del state.issues[issue_num]
        return True, True

    return _update_state(cfg, mutate)


def _completion_matches(
    entry: Optional[IssueState],
    expected_verdict_ts: Optional[str],
    expected_recovery_id: Optional[str] = None,
) -> bool:
    """CAS predicate for daemon/conductor completion bookkeeping."""
    return (
        entry is not None
        and entry.status == "rejected"
        and (
            expected_verdict_ts is None
            or entry.verdict_ts == expected_verdict_ts
        )
        and entry.recovery_id == expected_recovery_id
    )


def record_iteration(
    cfg: ReviewConfig,
    issue_num: int,
    *,
    expected_verdict_ts: Optional[str] = None,
    expected_recovery_id: Optional[str] = None,
    pending_pr_branch: Optional[str] = None,
    iteration_branch: Optional[str] = None,
    iteration_commit_sha: Optional[str] = None,
) -> Optional[IssueState]:
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
    def mutate(state: ReviewState) -> tuple[Optional[IssueState], bool]:
        entry = state.issues.get(issue_num)
        if not _completion_matches(
            entry, expected_verdict_ts, expected_recovery_id
        ):
            print(
                f"  warning: stale record_iteration(#{issue_num}) completion "
                "does not own the current rejected verdict; skipping"
            )
            return None, False
        assert entry is not None
        # Record "at or AFTER the verdict": a GitHub instant can be ahead of
        # the local clock.  Compare datetimes rather than relying on offsets in
        # ISO strings, but preserve the historical safe fallback.
        now = _now_iso()
        try:
            now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
            verdict_dt = datetime.fromisoformat(
                entry.verdict_ts.replace("Z", "+00:00")
            )
            entry.last_iteration_ts = (
                max(now_dt, verdict_dt).isoformat(timespec="microseconds")
            )
        except ValueError:
            entry.last_iteration_ts = max(now, entry.verdict_ts or "")
        entry.pending_pr_branch = pending_pr_branch
        entry.iteration_branch = iteration_branch or pending_pr_branch
        entry.iteration_commit_sha = iteration_commit_sha
        entry.recovery_mode = None
        entry.recovery_id = None
        entry.recovery_requested_ts = None
        entry.recovery_pr = None
        entry.recovery_branch = None
        entry.recovery_head_sha = None
        entry.recovery_base = None
        return entry, True

    return _update_state(
        cfg,
        mutate,
        after_save=lambda entry: (
            _ledger_upsert(cfg, issue_num, entry) if entry is not None else None
        ),
    )


def record_failed_attempt(
    cfg: ReviewConfig,
    issue_num: int,
    *,
    expected_verdict_ts: Optional[str] = None,
    expected_recovery_id: Optional[str] = None,
) -> Optional[IssueState]:
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
    def mutate(state: ReviewState) -> tuple[Optional[IssueState], bool]:
        entry = state.issues.get(issue_num)
        if not _completion_matches(
            entry, expected_verdict_ts, expected_recovery_id
        ):
            print(
                f"  warning: stale record_failed_attempt(#{issue_num}) "
                "completion does not own the current rejected verdict; skipping"
            )
            return None, False
        assert entry is not None
        entry.attempts += 1
        return entry, True

    return _update_state(
        cfg,
        mutate,
        after_save=lambda entry: (
            _ledger_upsert(cfg, issue_num, entry) if entry is not None else None
        ),
    )


def record_stall(
    cfg: ReviewConfig,
    issue_num: int,
    *,
    expected_verdict_ts: Optional[str] = None,
    expected_recovery_id: Optional[str] = None,
) -> Optional[IssueState]:
    """Flip ``issue_num`` to status="stalled" after the daemon exhausted
    its retry budget (``--max-attempts`` consecutive failed dispatches).

    A stalled entry keeps its notes, ``verdict_ts`` and ``attempts``
    for post-mortems, but ``needs_iteration`` is False for it, so the
    daemon stops picking it up. The daemon posts ONE notification
    comment on the GitHub issue when it stalls an entry; re-rejecting
    (:func:`record_rejection`) resets the counter and re-queues. No-ops
    (with a warning) if no entry exists."""
    def mutate(state: ReviewState) -> tuple[Optional[IssueState], bool]:
        entry = state.issues.get(issue_num)
        if not _completion_matches(
            entry, expected_verdict_ts, expected_recovery_id
        ):
            print(
                f"  warning: stale record_stall(#{issue_num}) completion "
                "does not own the current rejected verdict; skipping"
            )
            return None, False
        assert entry is not None
        entry.status = "stalled"
        entry.pending_pr_branch = None
        entry.iteration_branch = None
        entry.iteration_commit_sha = None
        return entry, True

    return _update_state(
        cfg,
        mutate,
        after_save=lambda entry: (
            _ledger_upsert(cfg, issue_num, entry) if entry is not None else None
        ),
    )


def clear_pending_pr_check(
    cfg: ReviewConfig,
    issue_num: int,
    branch: str,
    *,
    expected_verdict_ts: str,
) -> bool:
    """Resolve a durable PR-presence check without touching a newer verdict."""
    def mutate(state: ReviewState) -> tuple[Optional[IssueState], bool]:
        entry = state.issues.get(issue_num)
        if (
            not _completion_matches(entry, expected_verdict_ts)
            or entry is None
            or entry.pending_pr_branch != branch
        ):
            return None, False
        entry.pending_pr_branch = None
        return entry, True

    entry = _update_state(
        cfg,
        mutate,
        after_save=lambda current: (
            _ledger_upsert(cfg, issue_num, current)
            if current is not None
            else None
        ),
    )
    return entry is not None


def pending_pr_checks(cfg: ReviewConfig) -> list[tuple[int, IssueState]]:
    """Rejected iterations whose GitHub PR-presence check must be retried."""
    state = load_state(cfg)
    return sorted(
        (
            (num, entry)
            for num, entry in state.issues.items()
            if entry.status == "rejected" and entry.pending_pr_branch
        ),
        key=lambda item: item[1].verdict_ts,
    )


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

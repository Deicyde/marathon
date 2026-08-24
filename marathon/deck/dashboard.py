"""marathon.deck.dashboard — read-only data assembly for the deck's operations
dashboard (the Status / Issues / History tabs).

EVERY function here is a PURE READ that DEGRADES to empty/None rather than
raising: a dashboard tab must never break the page. Sources are the same
runtime files the rest of marathon writes — the conductor jobs snapshot, the
review state + chapter registry, the verdicts log, the landing log — plus, for
the issues overview and PR history, one ``gh`` read each (graceful on failure).
Nothing here writes anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover — type-only
    from marathon.review.config import ReviewConfig

#: Aristotle progress lines the refine subprocess writes to its conductor log,
#: e.g. ``    status: IN_PROGRESS (15% complete)``. The LAST match is current.
_PROGRESS_RE = re.compile(r"(\d{1,3})%\s*complete", re.IGNORECASE)


def _read_tail(path: Path, max_bytes: int = 65536) -> str:
    """The last ``max_bytes`` of a text file (whole file when smaller), or ``""``
    on any error. Pure read — never raises."""
    try:
        with path.open("rb") as fh:
            try:
                fh.seek(-max_bytes, 2)
            except OSError:
                fh.seek(0)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _refine_progress_pct(workdir: Optional[str]) -> Optional[int]:
    """The latest Aristotle ``NN% complete`` the refine logged to
    ``<workdir>/conductor-refine.log``, or ``None``. Pure read."""
    if not workdir:
        return None
    text = _read_tail(Path(workdir) / "conductor-refine.log")
    matches = _PROGRESS_RE.findall(text)
    if not matches:
        return None
    try:
        return max(0, min(100, int(matches[-1])))
    except ValueError:
        return None


def _refine_iteration(workdir: Optional[str]) -> Optional[dict]:
    """``{completed, current, status, duration_seconds, agent_task_id}`` from the
    refine checkpoint ``<workdir>/marathon-refine-state.json``, or ``None``.
    Pure read; degrades to None on any error."""
    if not workdir:
        return None
    try:
        data = json.loads(
            (Path(workdir) / "marathon-refine-state.json").read_text()
        )
    except (OSError, ValueError):
        return None
    return {
        "completed": data.get("iterations_completed"),
        "current": data.get("current_iteration_idx"),
        "status": data.get("status"),
        "duration_seconds": data.get("duration_seconds"),
        "agent_task_id": data.get("agent_task_id"),
    }


#: Post-prover pipeline phases, recognised in the refine's conductor-refine.log.
#: Checked LATEST-line-first, so the most recent phase a running job reached wins.
_PHASE_MARKERS = [
    (re.compile(r"pull request|gh pr (create|edit|ready)|PR #|opening.*pr", re.I), "opening PR"),
    (re.compile(r"\bpush(ing|ed)?\b|git push", re.I), "pushing"),
    (re.compile(r"\bcommit(ted|ting)?\b|commit:", re.I), "committing"),
    (re.compile(r"auto-rate|rating:", re.I), "rating"),
    (re.compile(r"lake build|compiling|% complete|auto-build|building", re.I), "building"),
]


def _job_phase(
    workdir: Optional[str], status: Optional[str],
    aristotle_status: Optional[str], progress_pct: Optional[int],
) -> str:
    """A GRANULAR phase for a job — so a job whose Aristotle prover is done but
    is still building/PR-ing reads "building", not the misleading "Complete".

    Running jobs: ``proving NN%`` while the prover runs; once Aristotle is
    COMPLETE, the latest post-prover phase from the conductor-refine.log
    (building → rating → committing → pushing → opening PR). Finished jobs: their
    status (succeeded / failed / stalled). Pure read; degrades to the status."""
    st = status or ""
    if not st.startswith("running"):
        return st or "—"
    a = (aristotle_status or "").upper()
    if a == "QUEUED":
        return "queued (Aristotle)"
    if a in ("", "IN_PROGRESS", "RUNNING"):
        return "proving" + (f" {progress_pct}%" if progress_pct is not None else "")
    # Aristotle COMPLETE → the job is in its post-prover pipeline.
    text = _read_tail(Path(workdir) / "conductor-refine.log", 32768) if workdir else ""
    for line in reversed(text.splitlines()):
        for rx, label in _PHASE_MARKERS:
            if rx.search(line):
                return label
    return "building"  # prover done, in the build/finish pipeline


# ---------------------------------------------------------------------------
# Conductor / daemon status
# ---------------------------------------------------------------------------


def _timestamp_age_seconds(value: object) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        now = (
            datetime.now(stamp.tzinfo)
            if stamp.tzinfo is not None
            else datetime.now()
        )
        return max(0.0, (now - stamp).total_seconds())
    except ValueError:
        return None


def boot_digest(
    cfg: "ReviewConfig", *, verso_status: Optional[dict] = None
) -> dict:
    """The deck's landing view: a <1 second, LOCAL-ONLY answer to "what
    happened while I was away, and does anything need me?".

    No GitHub and no network — conductor liveness, running jobs, parked
    issues (with their last failure evidence), reconcile confirmations the
    conductor is waiting on, the prover toggle's provenance, audit coverage,
    and the most recent verdicts, all from files the runtime already
    maintains. An empty queue rendered from this digest can explain WHY it is
    empty instead of announcing "caught up". Every section degrades to
    empty/None rather than raising.
    """
    from marathon.conductor import (
        PENDING_HUMAN_RELPATH,
        load_jobs_snapshot,
        prover_control_path,
    )
    from marathon.review.state import load_state

    repo_dir = Path(cfg.repo_dir)
    status = conductor_status(cfg, include_landings=False)

    # Latest terminal evidence per issue from the raw jobs snapshot (the
    # dashboard job rows deliberately omit error text; the digest needs it).
    last_job: dict[int, dict] = {}
    try:
        for raw in (load_jobs_snapshot(repo_dir) or {}).get("jobs", []):
            num = raw.get("issue_num")
            if num is not None:
                last_job[num] = raw
    except Exception:  # noqa: BLE001
        last_job = {}

    needs_me: list[dict] = []
    try:
        state = load_state(cfg)
        for num, entry in sorted(state.issues.items()):
            if entry.status != "stalled":
                continue
            raw = last_job.get(num) or {}
            err = str(raw.get("error") or "").strip()
            needs_me.append({
                "kind": "stalled",
                "issue": num,
                "recovery_mode": entry.recovery_mode,
                "recovery_pr": entry.recovery_pr,
                "recovery_requested_ts": entry.recovery_requested_ts,
                "last_exit_code": raw.get("exit_code"),
                "last_finished_ts": raw.get("finished_ts"),
                "error_tail": err[-400:] if err else None,
            })
    except Exception:  # noqa: BLE001
        pass

    try:
        pending = json.loads((repo_dir / PENDING_HUMAN_RELPATH).read_text())
        for item in pending.get("reconcile", []):
            needs_me.append({
                "kind": "reconcile",
                "issue": item.get("issue"),
                "proposals": item.get("proposals"),
                "observed_ts": pending.get("updated_ts"),
            })
    except (OSError, ValueError):
        pass

    prover: dict = {"enabled": status.get("prover_enabled")}
    try:
        raw = json.loads(prover_control_path(repo_dir).read_text())
        if isinstance(raw.get("enabled"), bool):
            prover["override"] = raw.get("enabled")
        for key in ("updated_ts", "updated_by"):
            if raw.get(key) is not None:
                prover[key] = raw.get(key)
    except (OSError, ValueError, AttributeError):
        pass

    audit: Optional[dict] = None
    try:
        raw = json.loads(
            (repo_dir / ".marathon" / "audit" / "latest.json").read_text()
        )
        failures = [str(f) for f in (raw.get("failures") or [])]
        audit = {
            "created_at": raw.get("created_at"),
            "complete": not failures,
            "failures": failures[:5],
            "modules": len(raw.get("modules") or []),
        }
    except (OSError, ValueError):
        audit = None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "conductor": {
            key: status.get(key)
            for key in (
                "alive", "ready", "pid", "concurrency", "updated_ts",
                "degraded",
            )
        },
        "prover": prover,
        "jobs": status.get("jobs") or [],
        "needs_me": needs_me,
        "audit": audit,
        "verso": verso_status,
        "recent_verdicts": _verdict_events(cfg, 5),
    }


def conductor_status(
    cfg: "ReviewConfig", *, include_landings: bool = True
) -> dict:
    """The global orchestrator's live status: is a conductor alive (lock pid),
    its concurrency + last snapshot time, the per-job table (running/finished),
    and the recent landings. Pure read; everything degrades to empty."""
    from marathon.conductor import (
        CONDUCTOR_HEARTBEAT_STALE_SECONDS,
        CONDUCTOR_PROGRESS_STALE_SECONDS,
        conductor_lock_path,
        load_conductor_heartbeat,
        load_jobs_snapshot,
        snapshot_job_is_live,
    )
    from marathon.review import daemon

    # A conductor launched by the long-lived deck remains its child. Observe
    # the retained Popen before the PID-file probe so an early crash is reaped;
    # otherwise kill(pid, 0) reports the zombie as alive forever and hides the
    # manual restart control. This performs no launch, signal, or repo write.
    try:
        from marathon.review.review import refresh_pending_conductor_start

        refresh_pending_conductor_start(cfg)
    except Exception:  # noqa: BLE001 — status remains fail-soft
        pass

    # Liveness: the conductor lock holds the live pid (same check the conductor
    # itself uses to enforce the singleton).
    pid: Optional[int] = None
    alive = False
    try:
        lock = conductor_lock_path(cfg)
        if lock.is_file():
            pid = int(lock.read_text().strip())
            alive = daemon.process_alive(pid)
    except (ValueError, OSError):
        pid = None

    snap: Optional[dict] = None
    try:
        snap = load_jobs_snapshot(Path(cfg.repo_dir))
    except Exception:  # noqa: BLE001 — never let a bad snapshot break the tab
        snap = None
    snap = snap or {}

    heartbeat: dict = {}
    try:
        heartbeat = load_conductor_heartbeat(Path(cfg.repo_dir)) or {}
    except Exception:  # noqa: BLE001 — readiness remains fail-closed
        heartbeat = {}
    heartbeat_ts = heartbeat.get("updated_ts")
    heartbeat_age_seconds = _timestamp_age_seconds(heartbeat_ts)
    heartbeat_stale = (
        heartbeat_age_seconds is None
        or heartbeat_age_seconds > CONDUCTOR_HEARTBEAT_STALE_SECONDS
    )
    snapshot_ts = snap.get("updated_ts")
    snapshot_age_seconds = _timestamp_age_seconds(snapshot_ts)
    legacy_snapshot_fresh = bool(
        alive
        and not heartbeat
        and snap.get("conductor_pid") is None
        and snapshot_age_seconds is not None
        and snapshot_age_seconds <= (daemon.POLL_INTERVAL_SECONDS * 2 + 15)
    )
    snapshot_current = bool(
        pid is not None and snap.get("conductor_pid") == pid
    )
    ready = bool(
        legacy_snapshot_fresh
        or (
            alive
            and snapshot_current
            and heartbeat.get("pid") == pid
            and heartbeat.get("ready") is True
            and not heartbeat_stale
        )
    )
    # Jobs remain diagnostic independently of which conductor owns the lock.
    # During restart, the new conductor publishes its PID-bound snapshot only
    # after adopting live detached workers from this older one.  Hiding those
    # rows in that small window would make recovery_running false and could
    # enable a duplicate billed recovery.  snapshot_job_is_live() below keeps
    # stale/PID-reused rows fail-closed; only operational claims such as
    # concurrency, degradation, and prover state are PID-bound.
    jobs_snap = snap
    runtime_snap = (
        snap if snapshot_current or legacy_snapshot_fresh else {}
    )

    jobs: list[dict] = []
    for raw in jobs_snap.get("jobs", []):
        status = raw.get("status")
        jpid = raw.get("pid")
        # Mirror `conductor status`: a "running" job whose pid is dead is shown
        # as such (the state machine recorded nothing; next run re-dispatches).
        if status == "running" and not snapshot_job_is_live(raw):
            status = "running (pid dead)"
        workdir = raw.get("workdir")
        job = {
            "issue_num": raw.get("issue_num"),
            "chapter": raw.get("chapter"),
            "status": status,
            "pid": jpid,
            "project_id": raw.get("project_id"),
            "aristotle_status": raw.get("aristotle_status"),
            "started_ts": raw.get("started_ts"),
            "finished_ts": raw.get("finished_ts"),
            "branch": raw.get("branch"),
            "workdir": workdir,
            "recovery_mode": raw.get("recovery_mode"),
            "recovery_pr": raw.get("recovery_pr"),
            "recovery_head_sha": raw.get("recovery_head_sha"),
        }
        # Live Aristotle progress + iteration for a running job (cheap workdir
        # reads; None for a finished/reaped job).
        if status and status.startswith("running"):
            job["progress_pct"] = _refine_progress_pct(workdir)
            job["iteration"] = _refine_iteration(workdir)
        # The granular phase (proving NN% / building / committing / pushing /
        # opening PR) — so an Aristotle-COMPLETE-but-still-building job reads
        # "building", not a misleading "Complete".
        if status and status.startswith("running") and raw.get(
            "recovery_mode"
        ) == "retry_build":
            job["phase"] = "building exact PR head"
        elif (
            status
            and status.startswith("running")
            and raw.get("recovery_mode") == "update_continue"
            and not raw.get("aristotle_status")
        ):
            job["phase"] = "updating from main / building"
        else:
            job["phase"] = _job_phase(
                workdir,
                status,
                raw.get("aristotle_status"),
                job.get("progress_pct"),
            )
        jobs.append(job)

    # The prover toggle's state: the conductor publishes its EFFECTIVE proving
    # state into the snapshot each tick; fall back to the deck-written control
    # file (a conductor that predates this field, or none yet running).
    prover_enabled = runtime_snap.get("prover_enabled")
    if prover_enabled is None:
        try:
            from marathon.conductor import read_prover_control
            prover_enabled = read_prover_control(Path(cfg.repo_dir))
        except Exception:  # noqa: BLE001
            prover_enabled = None

    return {
        "alive": alive,
        "ready": ready,
        "pid": pid,
        "repo": getattr(cfg, "github_repo", None),
        "concurrency": runtime_snap.get("concurrency"),
        "updated_ts": jobs_snap.get("updated_ts"),
        "heartbeat_ts": heartbeat_ts,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "heartbeat_stale": heartbeat_stale,
        "legacy_ready": legacy_snapshot_fresh,
        "progress_age_seconds": snapshot_age_seconds,
        "progress_stale": bool(
            ready
            and snapshot_age_seconds is not None
            and snapshot_age_seconds > CONDUCTOR_PROGRESS_STALE_SECONDS
        ),
        "running": sum(1 for j in jobs if j["status"] == "running"),
        "prover_enabled": prover_enabled,
        # A dispatch-disabling environment problem the conductor published
        # (e.g. no Aristotle key) — the deck must NOT render a green
        # "conductor running" chip over a conductor that can't dispatch.
        "degraded": runtime_snap.get("degraded"),
        "jobs": jobs,
        "queued": _pending_queue(cfg, jobs, runtime_snap.get("concurrency")),
        "landings": _recent_landings(cfg, limit=8) if include_landings else [],
    }


def _pending_queue(
    cfg: "ReviewConfig", jobs: list[dict], concurrency: Optional[int]
) -> list[dict]:
    """Rejections that are queued for a refine but NOT yet dispatched — the gap
    between "I shipped a reject" and "it's a running job". Each carries WHY it is
    waiting (the conductor is single-flight + same-chapter-aware). Pure read;
    ``[]`` on any error."""
    try:
        from marathon.review.state import pending_rejections_needing_iteration
        pending = pending_rejections_needing_iteration(cfg, None)
    except Exception:  # noqa: BLE001 — never break the tab
        return []
    running = [j for j in jobs if (j.get("status") or "").startswith("running")]
    running_nums = {j["issue_num"] for j in running}
    running_chapters = {j["chapter"] for j in running if j["chapter"] is not None}
    out: list[dict] = []
    for num, st in pending:
        if num in running_nums:
            continue  # this one IS the running job
        try:
            chapter = cfg.chapter_of_issue(num)
        except Exception:  # noqa: BLE001
            chapter = None
        if chapter is not None and chapter in running_chapters:
            reason = f"waiting — chapter {chapter} is busy"
        elif concurrency and len(running) >= concurrency:
            reason = f"waiting — at concurrency limit ({concurrency})"
        else:
            reason = "queued — dispatches next cycle"
        out.append({
            "issue_num": num,
            "chapter": chapter,
            "reason": reason,
            "verdict_ts": getattr(st, "verdict_ts", None),
        })
    return out


def run_detail(cfg: "ReviewConfig", issue_num: int) -> dict:
    """Drill-down for ONE conductor run (clicking a job on the Status tab): its
    live progress + iteration, the per-iteration auto-rater RATINGS, and the
    refine-reviewer PROMPTS drafted for Aristotle — all read from the job's
    workdir. Pure read; everything degrades to empty/None.

    ``{found, issue_num, status, branch, progress_pct, iteration, ratings,
    prompts}`` where ``ratings`` is the parsed ``marathon-ratings.jsonl`` records
    (oldest→newest) and ``prompts`` is the ``marathon-refine-log.md`` text."""
    from marathon.conductor import load_jobs_snapshot

    out = {
        "found": False, "issue_num": issue_num, "status": None, "branch": None,
        "progress_pct": None, "iteration": None, "ratings": [], "prompts": "",
    }
    try:
        snap = load_jobs_snapshot(Path(cfg.repo_dir)) or {}
    except Exception:  # noqa: BLE001
        snap = {}
    raw = next(
        (j for j in snap.get("jobs", []) if j.get("issue_num") == issue_num), None
    )
    if raw is None:
        return out
    workdir = raw.get("workdir")
    out.update({
        "found": True,
        "status": raw.get("status"),
        "branch": raw.get("branch"),
        "workdir": workdir,
        "aristotle_status": raw.get("aristotle_status"),
        "progress_pct": _refine_progress_pct(workdir),
        "iteration": _refine_iteration(workdir),
        "ratings": _refine_ratings(workdir),
        "prompts": _refine_prompts(workdir),
    })
    return out


def _refine_ratings(workdir: Optional[str]) -> list[dict]:
    """The auto-rater records from ``<workdir>/marathon-ratings.jsonl`` (one JSON
    object per iteration, oldest→newest), skipping malformed lines. ``[]`` on any
    error. Pure read."""
    if not workdir:
        return []
    path = Path(workdir) / "marathon-ratings.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _refine_prompts(workdir: Optional[str], max_bytes: int = 200000) -> str:
    """The per-iteration refine-reviewer PROMPTS drafted for Aristotle, from
    ``<workdir>/marathon-refine-log.md`` (tailed to ``max_bytes``). ``""`` when
    absent. Pure read."""
    if not workdir:
        return ""
    return _read_tail(Path(workdir) / "marathon-refine-log.md", max_bytes)


#: A merged marathon refine branch → the issue whose Aristotle fix LANDED.
_REFINE_LAND_RE = re.compile(r"^marathon/refine-c\d+-i(\d+)$")
#: Per-repo cache of the (gh-derived) landings so the Status poll doesn't call
#: gh every tick. {repo_dir: (epoch, landings)}.
# Stale-while-revalidate cache for the deck's gh-backed reads. A slow `gh` call
# (e.g. when the laptop is busy compiling a refine alongside the hosted deck)
# must never block a request long enough to time out into a proxy 502: serve the
# last value immediately and refresh in the BACKGROUND. Only the first call for a
# key blocks (to seed it); a failed refresh keeps the last good value.
_SWR: dict = {}
_SWR_LOCK = threading.Lock()


def invalidate_swr() -> None:
    """Drop every cached dashboard view after a read-model-affecting mutation.

    The next reader re-seeds synchronously and therefore sees post-mutation
    state on its FIRST fetch — the deliberate trade against serving a stale
    Issues/landings view right after the operator's own verdict or ship.
    """
    with _SWR_LOCK:
        _SWR.clear()


def _swr(key: str, ttl: float, fetch):
    """Return the cached value for ``key`` immediately; if older than ``ttl``,
    kick a one-shot background refresh via ``fetch``. Seeds synchronously on the
    first call. Never raises (a failed refresh keeps the last good value)."""
    now = time.time()
    with _SWR_LOCK:
        ent = _SWR.get(key)
    if ent is None:
        try:
            val = fetch()
        except Exception:  # noqa: BLE001
            val = None
        with _SWR_LOCK:
            _SWR[key] = {"ts": time.time(), "data": val, "busy": False}
        return val
    if now - ent["ts"] >= ttl and not ent["busy"]:
        with _SWR_LOCK:
            ent["busy"] = True

        def _refresh() -> None:
            try:
                v = fetch()
                with _SWR_LOCK:
                    _SWR[key] = {"ts": time.time(), "data": v, "busy": False}
            except Exception:  # noqa: BLE001 — keep the last good value, back off
                with _SWR_LOCK:
                    ent["ts"] = time.time()
                    ent["busy"] = False

        threading.Thread(target=_refresh, name="marathon-deck-swr",
                         daemon=True).start()
    return ent["data"]


def _recent_landings(cfg: "ReviewConfig", limit: int = 8) -> list[dict]:
    """Recently LANDED issues — an issue's Aristotle fix MERGED onto main (a
    merged ``marathon/refine-c*-i*`` PR). Newest-first, capped.
    Stale-while-revalidate cached ~60s per repo, so a slow ``gh`` never blocks
    the Status poll into a 502. Pure read; ``[]`` on failure."""
    def _compute() -> list[dict]:
        out: list[dict] = []
        for pr in _merged_prs(cfg, max(limit * 3, 20)):
            m = _REFINE_LAND_RE.match(pr.get("head") or "")
            if m:
                out.append({"issue": int(m.group(1)), "pr": pr.get("number"),
                            "ts": pr.get("merged_at")})
        out.sort(key=lambda r: r.get("ts") or "", reverse=True)
        return out

    out = _swr("landings:" + str(getattr(cfg, "repo_dir", "")), 60, _compute) or []
    return out[:limit]


# ---------------------------------------------------------------------------
# Open issues + chapters overview
# ---------------------------------------------------------------------------

#: The display status of an issue, in PRECEDENCE order (first match wins).
_STATUS_ORDER = ("building", "verified", "rejected", "stalled", "ready",
                 "blocked", "pending")


def _issue_status(
    issue: int,
    building: "set[int]",
    labels: "set[str]",
    local_status: Optional[str],
    qcard: dict,
) -> str:
    """One issue's display status, combining (in precedence order) the live
    conductor jobs, the GitHub review labels, the local review state, and the
    deck queue's readiness."""
    if issue in building:
        return "building"
    if "review:verified" in labels or local_status == "verified":
        return "verified"
    if local_status in ("rejected", "stalled"):
        return local_status
    if "review:rejected" in labels:
        return "rejected"
    if qcard.get("ready"):
        return "ready"
    if qcard.get("blocked_reason"):
        return "blocked"
    return "pending"


def _open_refine_prs(cfg) -> "Optional[dict[int, dict]]":
    """Structured metadata for open per-issue refine PRs.

    ``{issue_num: {pr, branch, head_sha, base, build, mergeable,
    merge_state}}`` is the server-authoritative recovery input.  The browser
    receives actions derived from this snapshot and sends none of these refs
    back as trusted values.

    The legacy title build tag is only a fallback.  A SHA-bound
    ``marathon/lake-build`` commit status from a build-only recovery overrides
    it, matching automerge's gate.

    OPEN marathon
    refine PRs (one bounded paginated gh call; ``None`` on any failure). Lets the stuck
    surface say "fix PR #260 is open [build:FAIL held]" instead of the false
    "the fix never landed — the conductor can't reach them": a build-FAIL-held
    PR means the pipeline DID produce a fix and automerge is (correctly)
    refusing it — re-queuing spends a fresh billed run, so the reviewer should
    know one exists before clicking."""
    from marathon.review.automerge import (
        _REFINE_BRANCH_RE,
        trusted_build_status,
    )
    from marathon.review.github import gh

    try:
        cp = gh(
            "pr", "list", "--repo", cfg.github_repo, "--state", "open",
            "--json", (
                "number,headRefName,headRefOid,baseRefName,title,mergeable,"
                "mergeStateStatus,statusCheckRollup"
            ), "--limit", "1000",
            check=False, timeout=20,
        )
    except Exception:  # noqa: BLE001 — gh missing/hung → no annotation
        return None
    if cp.returncode != 0:
        return None
    out: "dict[int, dict]" = {}
    try:
        rows = json.loads(cp.stdout or "[]")
        if not isinstance(rows, list):
            return None
        # ``gh pr list --limit`` paginates up to the requested bound. Hitting
        # the bound means absence is not proven, so destructive recovery must
        # remain disabled instead of treating a truncated list as complete.
        if len(rows) >= 1000:
            return None
        for pr in rows:
            m = _REFINE_BRANCH_RE.match(pr.get("headRefName") or "")
            if not m:
                continue
            title = pr.get("title") or ""
            # NOTE: the title tag is what the conductor STAMPS; automerge
            # itself re-derives the tag from the head commit before merging (a
            # human can rename a title), so this is an informational hint for
            # the deck, not a merge gate. TIMEOUT is held like FAIL.
            build = ("OK" if "[build:OK]" in title
                     else "FAIL" if "[build:FAIL]" in title
                     else "TIMEOUT" if "[build:TIMEOUT]" in title else None)
            attested = trusted_build_status(pr.get("statusCheckRollup"))
            if attested == "OK":
                build = "OK"
            elif attested == "FAIL":
                build = "FAIL"
            elif attested == "PENDING":
                build = "PENDING"
            info = {
                "pr": pr.get("number"),
                "build": build,
            }
            # Keep the old compact shape when ``gh``/a test double does not
            # return the newer recovery fields.  Recovery actions then fail
            # closed (no exact head SHA => no PR-preserving action).
            optional = {
                "branch": pr.get("headRefName"),
                "head_sha": pr.get("headRefOid"),
                "base": pr.get("baseRefName"),
                "mergeable": (pr.get("mergeable") or "").upper() or None,
                "merge_state": (
                    (pr.get("mergeStateStatus") or "").upper() or None
                ),
            }
            info.update({key: value for key, value in optional.items() if value})
            issue_num = int(m.group(1))
            if issue_num in out:
                # Two open refine PRs for one issue make "the current work"
                # ambiguous. Preserve that fact without selecting either head.
                out[issue_num] = {"ambiguous": True}
            else:
                out[issue_num] = info
    except (ValueError, TypeError, KeyError):
        return None
    return out


def _recovery_token(row: dict) -> str:
    """Opaque compare-and-swap token for one rendered recovery snapshot."""
    bound = {
        key: row.get(key)
        for key in (
            "num", "status", "verdict_ts", "attempts", "orphaned",
            "recovery_id", "recovery_mode",
            "fix_pr", "fix_pr_branch", "fix_pr_head_sha", "fix_pr_base",
            "fix_pr_build", "fix_pr_mergeable", "fix_pr_merge_state",
            "pr_lookup_known", "fix_pr_ambiguous",
            "recovery_running",
            "local_recovery_available", "local_recovery_head",
        )
    }
    payload = json.dumps(bound, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _recovery_actions(row: dict) -> tuple[dict, Optional[str]]:
    """Server-derived recovery capability matrix + safe recommendation."""
    lookup_known = row.get("pr_lookup_known") is not False
    ambiguous = bool(row.get("fix_pr_ambiguous"))
    has_pr = bool(
        lookup_known and not ambiguous
        and row.get("fix_pr")
        and row.get("fix_pr_branch")
        and row.get("fix_pr_head_sha")
    )
    conflict = (
        row.get("fix_pr_mergeable") == "CONFLICTING"
        or row.get("fix_pr_merge_state") == "DIRTY"
    )
    needs_update = conflict or row.get("fix_pr_merge_state") == "BEHIND"
    build = row.get("fix_pr_build")
    recovery_running = bool(row.get("recovery_running"))
    local_progress = bool(row.get("local_recovery_available"))
    has_note = bool(row.get("has_note"))
    live_local_state = not bool(row.get("orphaned"))
    can_start_over = (
        lookup_known
        and not ambiguous
        and not recovery_running
        and has_note
        and (not row.get("fix_pr") or has_pr)
    )
    unavailable_reason = (
        "GitHub could not confirm the complete set of open PRs; retry after "
        "the PR lookup recovers."
        if not lookup_known
        else "Multiple open Marathon PRs exist for this issue; resolve the "
        "ambiguity before recovery."
        if ambiguous
        else "A recovery operation is already running for this issue."
        if recovery_running
        else None
    )
    retry_enabled = (
        has_pr
        and not needs_update
        and build != "OK"
        and not recovery_running
        and (has_note or live_local_state)
    )
    retry_reason = unavailable_reason or (
        "Runs build and gates on the exact PR head; no Aristotle call."
        if retry_enabled
        else "The orphaned rejection has no recoverable note to seed local state."
        if not has_note and not live_local_state
        else "Unavailable for a conflicted, actively building, or already-green PR."
    )
    continue_reason = unavailable_reason or (
        "A stored rejection note is required before Aristotle can continue."
        if not has_note
        else
        "Resumes the preserved local checkpoint and unpublished work."
        if has_pr and local_progress
        else "Keeps the existing PR diff and resumes its checkpoint when available."
        if has_pr
        else "No open Marathon PR was found to continue."
    )
    update_reason = unavailable_reason or (
        "A stored rejection note is required before Aristotle can continue."
        if not has_note
        else
        "Updates the existing PR from current main; Aristotle is used only "
        "when the preserved work still needs repair."
        if has_pr
        else "No open Marathon PR was found to update."
    )
    start_reason = unavailable_reason or (
        "Archives the current PR head, then regenerates from current main."
        if has_pr
        else "Regenerates from current main."
        if can_start_over
        else "A recoverable rejection note and complete PR lookup are required."
    )
    actions = {
        "retry_build": {
            "enabled": retry_enabled,
            "reason": retry_reason,
            "cost": "local build only",
            "destructive": False,
            "preserves_diff": True,
        },
        "continue_pr": {
            "enabled": has_pr and has_note and not recovery_running,
            "reason": continue_reason,
            "cost": "uses Aristotle",
            "destructive": False,
            "preserves_diff": True,
        },
        "update_continue": {
            "enabled": has_pr and has_note and not recovery_running,
            "reason": update_reason,
            "cost": "build first; Aristotle only if needed",
            "destructive": False,
            "preserves_diff": True,
        },
        "start_over": {
            "enabled": can_start_over,
            "reason": start_reason,
            "cost": "new Aristotle run",
            "destructive": True,
            "preserves_diff": False,
        },
    }
    if needs_update and actions["update_continue"]["enabled"]:
        recommended = "update_continue"
    elif local_progress and actions["continue_pr"]["enabled"]:
        recommended = "continue_pr"
    elif actions["retry_build"]["enabled"]:
        recommended = "retry_build"
    elif actions["continue_pr"]["enabled"]:
        recommended = "continue_pr"
    elif actions["start_over"]["enabled"]:
        recommended = "start_over"
    else:
        recommended = None
    return actions, recommended


def _local_recovery_progress(cfg, row: dict) -> dict:
    """Best-effort evidence of unpublished work/checkpoint for one PR.

    This is intentionally read-only and fail-quiet. A local tree is considered
    valuable when it is dirty, locally ahead/diverged from the rendered PR
    head, or has a saved Aristotle project. That evidence changes only the
    recommendation; every server-side action remains independently guarded.
    """
    branch = row.get("fix_pr_branch")
    expected_head = row.get("fix_pr_head_sha")
    if not branch or not expected_head:
        return {}
    candidates: list[tuple[Path, Optional[Path]]] = []
    try:
        from marathon.conductor import load_jobs_snapshot

        snap = load_jobs_snapshot(Path(cfg.repo_dir)) or {}
        for raw in reversed(list(snap.get("jobs") or [])):
            if raw.get("issue_num") != row.get("num") or raw.get("branch") != branch:
                continue
            worktree = Path(str(raw.get("worktree") or ""))
            workdir_raw = raw.get("workdir")
            workdir = Path(str(workdir_raw)) if workdir_raw else None
            if worktree.is_dir():
                candidates.append((worktree, workdir))
    except Exception:  # noqa: BLE001
        pass
    if not candidates:
        try:
            listed = subprocess.run(
                ["git", "-C", str(cfg.repo_dir), "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            current: Optional[Path] = None
            for line in (listed.stdout or "").splitlines():
                if line.startswith("worktree "):
                    current = Path(line[len("worktree "):].strip())
                elif line.strip() == f"branch refs/heads/{branch}" and current:
                    candidates.append((current, None))
                    break
        except Exception:  # noqa: BLE001
            pass
    for worktree, workdir in candidates:
        try:
            status = subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            local_head = (head.stdout or "").strip() if head.returncode == 0 else None
            dirty = status.returncode == 0 and bool((status.stdout or "").strip())
            project_id = None
            if workdir is not None:
                checkpoint = workdir / "marathon-refine-state.json"
                if checkpoint.is_file():
                    project_id = (json.loads(checkpoint.read_text()) or {}).get(
                        "project_id"
                    )
            if dirty or (local_head and local_head != expected_head) or project_id:
                return {
                    "local_recovery_available": True,
                    "local_recovery_dirty": dirty,
                    "local_recovery_head": local_head,
                    "local_recovery_session": bool(project_id),
                }
        except Exception:  # noqa: BLE001
            continue
    return {}


def _orphan_note_previews(cfg) -> "dict[int, str]":
    """Best-effort human preview for an ORPHANED rejection, from the LOCAL
    deck-subdecks store (``deck-subdecks.json`` — survives a git reset since it
    is untracked; no gh). Lists the declarations the human sent back to fix so a
    surfaced orphan is not a bare number. ``{}`` when nothing is recorded."""
    try:
        from marathon.deck.subdeck import _load_store

        store = _load_store(cfg)
    except Exception:  # noqa: BLE001 — no store → no previews
        return {}
    out: "dict[int, str]" = {}
    for num_s, decls in store.items():
        try:
            num = int(num_s)
        except (TypeError, ValueError):
            continue
        fix = sorted(
            d for d, rec in decls.items()
            # Skip reserved store keys (``__informal__`` / ``__missing__``) —
            # they are not Lean declarations and would read oddly in the preview.
            if not d.startswith("__") and (rec or {}).get("verdict") == "reject"
        )
        if fix:
            names = ", ".join(fix[:6]) + ("…" if len(fix) > 6 else "")
            out[num] = f"{len(fix)} declaration(s) sent back to fix: {names}"
    return out


def stuck_issues(cfg, *, conductor: Optional[dict] = None) -> "list[dict]":
    """Issues parked OUTSIDE both the review feed and the refine queue — the
    conductor won't retry them and they're not in the swipe queue, so a human
    can't reach them without the CLI. The STUCK set:

      * ``status == "stalled"`` — the conductor exhausted its retry budget; OR
      * ``status == "rejected"`` with ``needs_iteration() == False`` — already
        iterated, no fix landed (the silently-stranded case the #170 bug caused);
        OR
      * ORPHANED — carries the GitHub ``review:rejected`` label but has NO live
        ``state.json`` entry. This happens when the git-TRACKED ``state.json`` is
        reverted by a hard reset / checkout (deck-subdecks.json, being untracked,
        survives — so the split is real): the conductor keys off ``state.json``
        and never refines it, while the swipe queue reads the label and hides it.
        GitHub is the durable truth, so we surface it and let an explicit
        recovery request restore its local row (the note is recovered from the
        issue's reject comment). Degrades to state.json-only when gh is down.

    Each row carries a rejection-note preview plus a server-derived recovery
    capability matrix. Recovery is operational state, not a duplicate human
    rejection verdict."""
    from marathon.review.state import load_state

    state = load_state(cfg)
    out: "list[dict]" = []
    for num, s in sorted(state.issues.items()):
        stuck = s.status == "stalled" or (
            s.status == "rejected" and not s.needs_iteration())
        if not stuck:
            continue
        note = (s.notes or "").strip()
        out.append({
            "num": num,
            "status": s.status,
            "verdict_ts": s.verdict_ts,
            "attempts": getattr(s, "attempts", 0),
            # Internal recovery-generation fields are bound into the opaque
            # snapshot token and rechecked by the POST handler. The frontend
            # need not interpret them.
            "recovery_id": getattr(s, "recovery_id", None),
            "recovery_mode": getattr(s, "recovery_mode", None),
            "has_note": bool(note),
            "note_preview": (note[:240] + "…") if len(note) > 240 else note,
        })

    # Orphaned rejections: labeled review:rejected on GitHub but with no local
    # state entry at all. A live entry (any status — rejected/verified/stalled)
    # means the issue is already tracked, so we skip it; only a genuinely absent
    # entry is an orphan the conductor can't reach.
    from marathon.review.github import list_rejected_issues

    rejected = list_rejected_issues(cfg)  # one gh call; None on failure
    if rejected:
        previews = _orphan_note_previews(cfg)
        for num in sorted(rejected):
            if num in state.issues:
                continue  # already tracked (in `out` if stuck, else in-flight)
            out.append({
                "num": num,
                "status": "rejected",
                "verdict_ts": None,
                "attempts": 0,
                "recovery_id": None,
                "recovery_mode": None,
                # The note is recoverable from the GitHub reject comment at
                # recovery time even though state.json lost it.
                "has_note": True,
                "orphaned": True,
                "note_preview": previews.get(num, ""),
            })
    out.sort(key=lambda r: r["num"])

    # Annotate rows that ALREADY have an open fix PR (one bounded gh call,
    # only when something is stuck): "stalled" + an open [build:FAIL] PR means
    # the pipeline produced a fix and automerge is correctly holding it — the
    # deck must not claim "the fix never landed". Recovery distinguishes a
    # free rebuild from PR-preserving continuation and destructive start-over.
    if out:
        prs = _open_refine_prs(cfg)
        lookup_known = prs is not None
        for row in out:
            row["pr_lookup_known"] = lookup_known
            info = (prs or {}).get(row["num"])
            if info:
                if info.get("ambiguous"):
                    row["fix_pr_ambiguous"] = True
                    continue
                mapping = {
                    "fix_pr": "pr",
                    "fix_pr_build": "build",
                    "fix_pr_branch": "branch",
                    "fix_pr_head_sha": "head_sha",
                    "fix_pr_base": "base",
                    "fix_pr_mergeable": "mergeable",
                    "fix_pr_merge_state": "merge_state",
                }
                for row_key, info_key in mapping.items():
                    if info_key in info:
                        row[row_key] = info[info_key]
    conductor = conductor if conductor is not None else conductor_status(cfg)
    running_recoveries = {
        job.get("issue_num")
        for job in conductor.get("jobs", [])
        if job.get("status") == "running" and job.get("recovery_mode")
    }
    for row in out:
        row.update(_local_recovery_progress(cfg, row))
        row["conductor_online"] = bool(
            conductor.get("ready", conductor.get("alive"))
        )
        row["recovery_running"] = row["num"] in running_recoveries
        row["host_conductor_disabled"] = bool(
            os.environ.get("MARATHON_NO_HOST_CONDUCTOR")
        )
        row["actions"], row["recommended_action"] = _recovery_actions(row)
        row["recovery_token"] = _recovery_token(row)
    return out


def issues_overview(cfg: "ReviewConfig") -> dict:
    """All registered issues grouped by chapter, each with a combined status,
    tier, and (when available) readiness/blocked-reason + title.

    The whole result is stale-while-revalidate cached ~45s per repo: it runs
    several ``gh`` reads (bulk titles/labels + the queue build), so under heavy
    local load it must not block a request into a proxy 502 — it serves the last
    value and refreshes in the background."""
    repo = str(getattr(cfg, "github_repo", ""))
    cached = _swr("issues_overview:" + repo, 45,
                  lambda: _issues_overview_compute(cfg))
    if cached is not None:
        return cached
    return {  # cold-call failure: degrade to an empty overview, never raise
        "repo": getattr(cfg, "github_repo", None),
        "chapters": [], "counts": {s: 0 for s in _STATUS_ORDER}, "total": 0,
    }


def _issues_overview_compute(cfg: "ReviewConfig") -> dict:
    """The actual assembly behind :func:`issues_overview` (ONE ``gh`` bulk read
    for titles/labels; degrades to local-only on failure)."""
    chapters = getattr(cfg, "chapters", {}) or {}
    registry = []  # (chapter, issue, substring)
    for ch in sorted(chapters):
        for issue, substr in getattr(chapters[ch], "entries", []) or []:
            registry.append((ch, issue, substr))
    nums = [issue for _, issue, _ in registry]

    # GitHub titles + labels (one bulk GraphQL call; {} on failure).
    meta: dict = {}
    try:
        from marathon.review.github import fetch_issues_bulk

        meta = fetch_issues_bulk(nums, cfg.github_repo) or {}
    except Exception:  # noqa: BLE001
        meta = {}

    # Local review state (verified/rejected/stalled per issue).
    local: dict = {}
    try:
        from marathon.review.state import load_state

        st = load_state(cfg)
        local = {n: s.status for n, s in st.issues.items()}
    except Exception:  # noqa: BLE001
        local = {}

    # Live conductor jobs → which issues are building right now.
    building: set[int] = set()
    for j in conductor_status(cfg)["jobs"]:
        if j.get("status") == "running" and isinstance(j.get("issue_num"), int):
            building.add(j["issue_num"])

    # Deck queue → tier / ready / blocked_reason / title for queue issues.
    queue_by_id: dict = {}
    try:
        from marathon.deck.cards import build_queue

        for card in build_queue(cfg, None).cards:
            queue_by_id[card.id] = card.to_json()
    except Exception:  # noqa: BLE001
        queue_by_id = {}

    out_chapters = []
    counts: dict = {s: 0 for s in _STATUS_ORDER}
    for ch in sorted(chapters):
        rows = []
        for issue, substr in getattr(chapters[ch], "entries", []) or []:
            m = meta.get(issue) or {}
            labels = m.get("labels") or set()
            qcard = queue_by_id.get(issue) or {}
            status = _issue_status(issue, building, labels, local.get(issue), qcard)
            counts[status] = counts.get(status, 0) + 1
            rows.append({
                "id": issue,
                "title": m.get("title") or qcard.get("title") or substr,
                "status": status,
                "tier": qcard.get("tier"),
                "tier_qualifiers": qcard.get("tier_qualifiers") or [],
                "ready": bool(qcard.get("ready")),
                "blocked_reason": qcard.get("blocked_reason"),
                "github_state": m.get("state"),
            })
        out_chapters.append({"chapter": ch, "issues": rows})

    return {
        "repo": cfg.github_repo,
        "chapters": out_chapters,
        "counts": counts,
        "total": len(registry),
    }


# ---------------------------------------------------------------------------
# History: verdict (issue-status) changes + merged PRs
# ---------------------------------------------------------------------------


def history(cfg: "ReviewConfig", *, limit: int = 30) -> dict:
    """A recent-activity feed: issue STATUS changes (the tracked verdicts log)
    and MERGED PRs (one ``gh`` read). Each is newest-first, capped at
    ``limit``; either degrades to empty independently."""
    return {
        "repo": cfg.github_repo,
        "verdict_events": _verdict_events(cfg, limit),
        # The gh read rides the SWR memo so a History open costs local file
        # time, not a fresh network fetch per request.
        "merged_prs": _swr(
            f"merged_prs:{cfg.github_repo}:{limit}",
            60,
            lambda: _merged_prs(cfg, limit),
        ) or [],
    }


def _verdict_events(cfg: "ReviewConfig", limit: int) -> list[dict]:
    try:
        from marathon.review.state import VERDICTS_RELPATH

        path = Path(cfg.repo_dir) / VERDICTS_RELPATH
        if not path.is_file():
            return []
        rows: list[dict] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (ValueError, TypeError):
                continue
            rows.append({
                # The committed verdicts.jsonl field is "issue" (fall back to
                # "issue_num" for forward-compat); the UI key stays issue_num.
                "issue_num": ev.get("issue", ev.get("issue_num")),
                "verdict": ev.get("verdict"),
                "ts": ev.get("ts"),
                "notes": (ev.get("notes") or "")[:200],
            })
        # newest-first
        rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
        return rows[:limit]
    except Exception:  # noqa: BLE001
        return []


def _merged_prs(cfg: "ReviewConfig", limit: int) -> list[dict]:
    try:
        cp = subprocess.run(
            ["gh", "pr", "list",
             "--repo", cfg.github_repo,
             "--state", "merged",
             "--limit", str(limit),
             "--json", "number,title,mergedAt,headRefName,author"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if cp.returncode != 0:
            return []
        data = json.loads(cp.stdout or "[]")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for pr in data if isinstance(data, list) else []:
        author = pr.get("author") or {}
        out.append({
            "number": pr.get("number"),
            "title": pr.get("title"),
            "merged_at": pr.get("mergedAt"),
            "head": pr.get("headRefName"),
            "author": author.get("login") if isinstance(author, dict) else None,
        })
    return out

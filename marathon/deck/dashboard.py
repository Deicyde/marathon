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

import json
import re
import subprocess
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


# ---------------------------------------------------------------------------
# Conductor / daemon status
# ---------------------------------------------------------------------------


def conductor_status(cfg: "ReviewConfig") -> dict:
    """The global orchestrator's live status: is a conductor alive (lock pid),
    its concurrency + last snapshot time, the per-job table (running/finished),
    and the recent landings. Pure read; everything degrades to empty."""
    from marathon.conductor import conductor_lock_path, load_jobs_snapshot
    from marathon.review import daemon

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

    jobs: list[dict] = []
    for raw in snap.get("jobs", []):
        status = raw.get("status")
        jpid = raw.get("pid")
        # Mirror `conductor status`: a "running" job whose pid is dead is shown
        # as such (the state machine recorded nothing; next run re-dispatches).
        if status == "running" and jpid is not None:
            try:
                if not daemon.process_alive(int(jpid)):
                    status = "running (pid dead)"
            except (ValueError, TypeError):
                pass
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
        }
        # Live Aristotle progress + iteration for a running job (cheap workdir
        # reads; None for a finished/reaped job).
        if status and status.startswith("running"):
            job["progress_pct"] = _refine_progress_pct(workdir)
            job["iteration"] = _refine_iteration(workdir)
        jobs.append(job)

    return {
        "alive": alive,
        "pid": pid,
        "repo": getattr(cfg, "github_repo", None),
        "concurrency": snap.get("concurrency"),
        "updated_ts": snap.get("updated_ts"),
        "running": sum(1 for j in jobs if j["status"] == "running"),
        "jobs": jobs,
        "queued": _pending_queue(cfg, jobs, snap.get("concurrency")),
        "landings": _recent_landings(cfg, limit=8),
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


def _recent_landings(cfg: "ReviewConfig", limit: int = 8) -> list[dict]:
    try:
        from marathon.landing import LANDINGS_RELPATH

        path = Path(cfg.repo_dir) / LANDINGS_RELPATH
        if not path.is_file():
            return []
        rows: list[dict] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (ValueError, TypeError):
                continue
        return rows[-limit:]
    except Exception:  # noqa: BLE001
        return []


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


def issues_overview(cfg: "ReviewConfig") -> dict:
    """All registered issues grouped by chapter, each with a combined status,
    tier, and (when available) readiness/blocked-reason + title. ONE ``gh`` bulk
    read for titles/labels (degrades to local-only on failure)."""
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
        "merged_prs": _merged_prs(cfg, limit),
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
            capture_output=True, text=True, check=False,
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

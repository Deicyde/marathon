"""Per-run metric shards + central rollup aggregation.

Two metrics — the prompt log (``.marathon/PromptLog.md``, one
``timestamp  <project-uuid>`` line per Aristotle submission) and Aristotle
wall-time (``.marathon/wall-time.json``, the project-id-keyed v2 sidecar) —
used to be COMMITTED into every refine PR. Concurrent/sequential refine PRs
textually COLLIDE on these two shared files (this stranded PRs #159/#162/#165:
two PRs off the same base both edit the same lines of the same file → a merge
conflict no auto-merge can resolve).

The fix (Option B): each refine writes ONE per-run shard under
``.marathon/metrics/`` whose path is UNIQUE per run (keyed by the Aristotle
project UUID, so no two PRs ever touch the same path → zero textual
collision) and stages only that shard. The conductor then AGGREGATES every
shard centrally — in the primary checkout, on the single-writer
``marathon/metadata-regen`` branch — back into the two rollups
(``wall-time.json`` + ``PromptLog.md``), which remain the human-readable /
queryable projection. The shards are the durable git source; the rollups
and the ledger are derived and fully regenerable from them.

Shard schema (``.marathon/metrics/<chapter>-<issue>-<projectid>.json``)::

    {
      "project_id": "<uuid>",          # the merge key; "" for build-only
      "chapter": <int|null>,
      "issue": <int|null>,
      "wall_seconds": <int|null>,      # Aristotle compute + lake build for this run
      "build_only_seconds": <int|null>,# seconds not attributable to a project
      "submitted_at": "<iso|null>",    # the PromptLog submission timestamp
      "promptlog_line": "<str|null>",  # the verbatim PromptLog line (lossless)
      "added_at": "<iso>"
    }

A shard may carry only wall-time (a timed run with no PromptLog line), only a
PromptLog submission (``submitted_at``/``promptlog_line`` set, ``wall_seconds``
null), or both. The two rollups carry DIFFERENT populations — the PromptLog is
the full submission history, wall-time is the timed subset — so both shapes are
first-class.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

METRICS_DIR_RELPATH = Path(".marathon") / "metrics"

# Aristotle project UUIDs as they appear in PromptLog.md.
_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)
_ISO_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:.+\-]+)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_component(value: Optional[object]) -> str:
    """Render a shard-filename component: a missing chapter/issue becomes
    ``x`` so the path is always well-formed and stable."""
    if value is None:
        return "x"
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))
    return s or "x"


def shard_path(
    repo_dir: Path,
    *,
    project_id: Optional[str],
    chapter: Optional[object],
    issue: Optional[object],
) -> Path:
    """The unique shard path for one run. Keyed by project UUID, so two
    PRs racing off the same base never touch the same path (the whole
    point of the redesign). A run with no project_id (rare, non-Aristotle
    build-only path) gets a ``<chapter>-<issue>-buildonly`` name.

    Project UUID is the canonical key: if a shard for this project already
    exists under any ``<chapter>-<issue>-<pid>.json`` name (e.g. a
    submit-time shard written before the chapter/issue were known), reuse
    THAT exact path so the project never splits across two shards."""
    metrics_dir = repo_dir / METRICS_DIR_RELPATH
    if project_id:
        pid = _safe_component(project_id)
        existing = sorted(metrics_dir.glob(f"*-{pid}.json")) if metrics_dir.is_dir() else []
        if existing:
            return existing[0]
        name = f"{_safe_component(chapter)}-{_safe_component(issue)}-{pid}.json"
    else:
        name = f"{_safe_component(chapter)}-{_safe_component(issue)}-buildonly.json"
    return metrics_dir / name


def write_shard(
    repo_dir: Path,
    *,
    project_id: Optional[str],
    chapter: Optional[object] = None,
    issue: Optional[object] = None,
    wall_seconds: Optional[int] = None,
    build_only_seconds: Optional[int] = None,
    submitted_at: Optional[str] = None,
    promptlog_line: Optional[str] = None,
) -> Path:
    """Write this run's metric shard and return its path. Idempotent:
    re-running the same project overwrites its own shard (same path), so a
    retry/continuation never duplicates and never double-counts. Writes
    ``.marathon/metrics/.gitignore`` is NOT done here — shards are TRACKED
    (they are the durable git source); only the runtime dirs self-ignore."""
    path = shard_path(
        repo_dir, project_id=project_id, chapter=chapter, issue=issue
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "project_id": project_id or "",
        "chapter": _coerce_chapter(chapter),
        "issue": _coerce_int(issue),
        "wall_seconds": _coerce_int(wall_seconds),
        "build_only_seconds": _coerce_int(build_only_seconds),
        "submitted_at": submitted_at,
        "promptlog_line": promptlog_line,
        "added_at": _now_iso(),
    }
    # Merge into an existing shard for the same path rather than blindly
    # overwriting: the submit-time call (PromptLog line) and the
    # post-pipeline call (wall_seconds) for the SAME project write the same
    # shard at different moments, and neither should clobber the other's
    # field. Last-writer-wins per field, preserving prior non-null values.
    if path.is_file():
        try:
            prev = json.loads(path.read_text())
            if isinstance(prev, dict):
                for key in (
                    "wall_seconds", "build_only_seconds",
                    "submitted_at", "promptlog_line", "chapter", "issue",
                ):
                    if payload.get(key) is None and prev.get(key) is not None:
                        payload[key] = prev[key]
        except (OSError, ValueError):
            pass
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def _coerce_int(v: object) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_chapter(v: object) -> Optional[int]:
    """Chapter as an int: a plain int passes through; a label like
    ``"Chapter14"`` yields 14 (digits extracted). Non-numeric labels yield
    None — the field is descriptive (the project UUID is the shard key)."""
    n = _coerce_int(v)
    if n is not None:
        return n
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        if m:
            return int(m.group(0))
    return None


def read_shards(repo_dir: Path) -> list[dict]:
    """Every well-formed shard under ``.marathon/metrics/``, sorted by
    filename for deterministic aggregation. Malformed shards are skipped."""
    metrics_dir = repo_dir / METRICS_DIR_RELPATH
    if not metrics_dir.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(metrics_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


# --- aggregation: shards (+ legacy rollups) -> rollups ----------------------


def aggregate_rollups(repo_dir: Path) -> dict:
    """Fold every shard — plus whatever is ALREADY in the two legacy
    rollups — into the in-memory rollup shapes, WITHOUT writing. Returns
    ``{"wall_time": <v2 sidecar dict>, "promptlog_lines": [str, ...]}``.

    Merge semantics (no double-count):
      * wall-time: one entry per project UUID. The legacy sidecar's
        ``projects`` map is the starting point; a shard with the same
        project_id OVERWRITES its seconds (idempotent — a shard is the
        authoritative record of that run). ``build_only_seconds`` is the
        legacy value plus every shard's ``build_only_seconds``.
      * PromptLog: one line per project UUID. The legacy file's lines are
        the starting point (preserving their verbatim text, including the
        old hand-written non-ISO ones); a shard's ``promptlog_line``
        (or a synthesized ``submitted_at  project_id`` line) fills in any
        UUID not already present. Deduped by UUID, sorted for a stable
        committed projection.
    """
    from marathon.formalization import _load_sidecar, _WALL_TIME_SIDECAR

    shards = read_shards(repo_dir)

    # --- wall-time -----------------------------------------------------
    legacy = _load_sidecar(repo_dir / _WALL_TIME_SIDECAR)
    projects: dict[str, dict] = {
        str(k): dict(v)
        for k, v in (legacy.get("projects") or {}).items()
        if isinstance(v, dict)
    }
    legacy_build_only = int(legacy.get("build_only_seconds", 0) or 0)
    # build_only is the sum of every build-only SHARD when any exist (the
    # backfill turns the legacy lump into exactly one build-only shard, so
    # shards are then the complete record and the legacy lump must NOT be
    # re-added — that would double-count). Before backfill (no build-only
    # shard yet), fall back to the legacy lump so its seconds survive the
    # first aggregation that runs before a backfill.
    shard_build_only = 0
    saw_build_only_shard = False
    for s in shards:
        pid = (s.get("project_id") or "").strip()
        wall = _coerce_int(s.get("wall_seconds"))
        if pid and wall is not None:
            projects[pid] = {
                "seconds": int(wall),
                "added_at": s.get("added_at") or _now_iso(),
            }
        bo = _coerce_int(s.get("build_only_seconds"))
        if bo:
            saw_build_only_shard = True
            shard_build_only += int(bo)
    build_only = shard_build_only if saw_build_only_shard else legacy_build_only

    wall_time = {
        "version": 2,
        "projects": projects,
        "build_only_seconds": build_only,
        "last_updated": _now_iso(),
    }

    # --- PromptLog -----------------------------------------------------
    lines_by_uuid: dict[str, str] = {}
    legacy_log = _read_legacy_promptlog(repo_dir)
    for line in legacy_log:
        m = _UUID_RE.search(line)
        if m:
            lines_by_uuid.setdefault(m.group(1), line)
    for s in shards:
        pid = (s.get("project_id") or "").strip()
        if not pid or pid in lines_by_uuid:
            continue
        line = s.get("promptlog_line")
        if not line:
            ts = s.get("submitted_at")
            if not ts:
                continue  # a wall-only shard with no submission line
            line = f"{ts}  {pid}"
        lines_by_uuid[pid] = line

    promptlog_lines = sorted(lines_by_uuid.values())

    return {"wall_time": wall_time, "promptlog_lines": promptlog_lines}


def _read_legacy_promptlog(repo_dir: Path) -> list[str]:
    """The existing PromptLog.md's non-blank lines (preferring the
    ``.marathon/`` location over the legacy repo-root one)."""
    for rel in (Path(".marathon") / "PromptLog.md", Path("PromptLog.md")):
        path = repo_dir / rel
        if path.is_file():
            return [ln for ln in path.read_text().splitlines() if ln.strip()]
    return []


def write_rollups(repo_dir: Path, rollups: dict) -> list[Path]:
    """Write the aggregated rollups to disk. Returns the paths written.

    ``wall-time.json`` is written with ``sort_keys=True`` to match the live
    accumulator's dumper; ``PromptLog.md`` is written one line per UUID,
    sorted, preserving each line's verbatim original text."""
    from marathon.formalization import _WALL_TIME_SIDECAR

    written: list[Path] = []

    wt_path = repo_dir / _WALL_TIME_SIDECAR
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    wt_path.write_text(json.dumps(rollups["wall_time"], indent=2, sort_keys=True))
    written.append(wt_path)

    # Only (re)write PromptLog.md if it already exists (opt-in convention,
    # mirroring append_promptlog_url) OR there are lines to write.
    lines = rollups["promptlog_lines"]
    pl_path = repo_dir / Path(".marathon") / "PromptLog.md"
    legacy_pl = repo_dir / "PromptLog.md"
    target = pl_path if pl_path.is_file() or not legacy_pl.is_file() else legacy_pl
    if lines or target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(lines)
        target.write_text(body + ("\n" if body else ""))
        written.append(target)

    return written


def regenerate_rollups(repo_dir: Path) -> list[Path]:
    """Aggregate shards (+ legacy) into the rollups, write them, and run
    the ledger import so ``marathon.db`` reflects them. Returns the rollup
    paths written. The conductor calls this centrally after each success."""
    rollups = aggregate_rollups(repo_dir)
    written = write_rollups(repo_dir, rollups)
    # Refresh the queryable ledger from the just-written rollups. Best
    # effort — a partial repo (no ledger) just skips.
    try:
        from marathon.ledger import Ledger

        ledger = Ledger.for_repo(repo_dir)
        if ledger.db_path.is_file():
            from marathon.ledger import _import_promptlog, _import_wall_time

            with ledger._tx() as conn:
                _import_wall_time(conn, repo_dir)
                _import_promptlog(conn, repo_dir)
    except Exception:  # noqa: BLE001 — ledger refresh must never crash regen
        pass
    return written


# --- backfill: legacy rollups -> shards -------------------------------------


def backfill_shards(repo_dir: Path) -> dict:
    """One-time, lossless: emit one shard per project/run under
    ``.marathon/metrics/`` from the CURRENT ``wall-time.json`` (its
    ``projects`` + ``build_only_seconds``) and ``PromptLog.md`` (its UUID
    lines). After this, regenerating the rollups from (legacy + shards)
    reproduces today's content; nothing is lost.

    Idempotent: a shard's path is keyed by project UUID, so re-running
    overwrites the same shards rather than duplicating. Returns a summary
    ``{"wall_shards": N, "promptlog_shards": M, "build_only_seconds": S}``.
    """
    from marathon.formalization import _load_sidecar, _WALL_TIME_SIDECAR

    summary = {"wall_shards": 0, "promptlog_shards": 0, "build_only_seconds": 0}

    # PromptLog: a shard per UUID carrying the verbatim line + parsed ts.
    log_lines = _read_legacy_promptlog(repo_dir)
    seen_uuids: set[str] = set()
    for line in log_lines:
        m = _UUID_RE.search(line)
        if not m:
            continue
        pid = m.group(1)
        if pid in seen_uuids:
            continue
        seen_uuids.add(pid)
        ts_m = _ISO_TS_RE.match(line.strip())
        submitted_at = ts_m.group(1) if ts_m else None
        write_shard(
            repo_dir,
            project_id=pid,
            submitted_at=submitted_at,
            promptlog_line=line,
        )
        summary["promptlog_shards"] += 1

    # wall-time: fold each project's seconds into its shard (merging, so a
    # PromptLog-only shard written above gains its wall_seconds here).
    legacy = _load_sidecar(repo_dir / _WALL_TIME_SIDECAR)
    for pid, entry in (legacy.get("projects") or {}).items():
        if not isinstance(entry, dict):
            continue
        secs = _coerce_int(entry.get("seconds"))
        if secs is None:
            continue
        write_shard(repo_dir, project_id=str(pid), wall_seconds=secs)
        summary["wall_shards"] += 1

    # build_only_seconds: a single dedicated build-only shard (no project).
    build_only = int(legacy.get("build_only_seconds", 0) or 0)
    if build_only > 0:
        write_shard(
            repo_dir,
            project_id=None,
            chapter=None,
            issue=None,
            build_only_seconds=build_only,
        )
        summary["build_only_seconds"] = build_only

    return summary

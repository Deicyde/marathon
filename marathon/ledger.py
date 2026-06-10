"""Phase-1 ledger: SQLite at ``<repo>/.marathon/marathon.db``.

This is the first slice of marathon v2's "one ledger" ruling
(``docs/marathon-v2-plan.md`` §2 ruling 1): today's runtime truth is
smeared across **seven** drifting surfaces — ``marathon-state.json``,
``marathon-refine-state.json``, ``review/state.json``,
``wall-time.json``, ``PromptLog.md``, runner-locks, and the
``config.toml`` chapter registry — plus GitHub labels, with documented
drift between them (state.json carried 21 entries against 29
``review:verified`` labels; PR #74 exists solely to record one verify
in git). The ledger is the future single runtime truth.

**Phase-1 contract (binding):**

* DUAL-WRITE only. Writers (``marathon.review.state``'s ``record_*``
  helpers) mirror every legacy JSON write into the ledger; **reads stay
  on the legacy files**. Cutover is a later phase, so nothing here may
  ever be load-bearing yet: a missing or uninitializable ledger must
  degrade to legacy-only behavior with one printed warning, never an
  error.
* The db is **gitignored consumer-repo state** (like ``.lake/``):
  WAL mode means ``marathon.db-wal``/``-shm`` siblings appear at
  runtime, so consumer repos should ignore ``.marathon/marathon.db*``.
  Git provenance for human verdicts lives instead in a TRACKED,
  append-only JSONL (``.marathon/review/verdicts.jsonl``, written by
  ``marathon.review.state``) following the wall-time-v2-sidecar
  pattern: stable keys, one object per line, no rewrites — so parallel
  iteration branches merge cleanly.
* ``import_all`` is the one-shot ingest of whatever legacy surfaces
  exist, and is **idempotent**: re-running updates rows in place
  (upserts keyed on the surfaces' natural keys) rather than
  duplicating. Runner-locks are deliberately not imported — they are
  ephemeral PID files, not state worth preserving.

Schema v1 (a ``meta`` table records ``schema_version = 1``; a db with a
*newer* version makes every Phase-1 operation raise :class:`LedgerError`
so an old marathon checkout degrades to legacy-only instead of
corrupting a future schema):

* ``issues`` — mirror of ``review/state.py``'s ``IssueState`` (one row
  per sub-issue, latest verdict only), plus the issue's chapter from
  the config.toml registry.
* ``verdict_events`` — append-only verdict history. ``issues`` answers
  "what is the state now"; this answers "what happened" (the legacy
  state.json forgets every superseded verdict). A unique index over
  ``(issue_num, verdict, ts, source)`` makes re-imports no-ops.
* ``chapters`` — snapshot of the config.toml ``[[chapters]]`` registry
  (entries as JSON, in textbook order).
* ``wall_time`` — the project-id-keyed wall-time sidecar, one row per
  Aristotle project (the v1 lump sum lands under a sentinel id).
* ``prompt_log`` — one row per Aristotle project UUID ever recorded in
  ``PromptLog.md``.
* ``skeleton_chapters`` / ``refine_runs`` — import-only mirrors of the
  per-workdir ``marathon-state.json`` / ``marathon-refine-state.json``
  checkpoints. **No live writers in Phase 1**; they exist so the
  Phase-3 Conductor inherits a populated history instead of re-mining
  scattered workdirs.

Stdlib only (sqlite3 / json / tomllib), per the project ground rules.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

from marathon.state import load_refine_state, load_state

if TYPE_CHECKING:  # pragma: no cover — type-only; avoids review-package dep
    from marathon.review.config import ReviewConfig


SCHEMA_VERSION = 1
LEDGER_RELPATH = Path(".marathon") / "marathon.db"

# How long a writer waits on a locked db before giving up. Generous:
# the only contention is CLI verdicts racing a daemon's record_iteration,
# and both hold transactions for microseconds. WAL keeps readers (none
# in Phase 1, but eventually the deck) from blocking writers at all.
BUSY_TIMEOUT_MS = 5_000

# Sentinel ``wall_time.project_id`` for seconds not attributable to a
# specific Aristotle project (the v1 sidecar's ``total_seconds`` lump,
# carried by v2 as ``build_only_seconds``). Kept as a row — rather than
# dropped — so the ledger total matches the sidecar total exactly.
BUILD_ONLY_PROJECT_ID = "__build_only__"

# Ordered for `marathon ledger status` output: human-verdict tables
# first, accounting tables, then the import-only checkpoint mirrors.
TABLES = (
    "issues",
    "verdict_events",
    "chapters",
    "wall_time",
    "prompt_log",
    "skeleton_chapters",
    "refine_runs",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    issue_num         INTEGER PRIMARY KEY,
    chapter           INTEGER,
    status            TEXT NOT NULL
                      CHECK (status IN ('rejected', 'verified', 'stalled')),
    verdict_ts        TEXT NOT NULL,
    notes             TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_iteration_ts TEXT
);

CREATE TABLE IF NOT EXISTS verdict_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_num INTEGER NOT NULL,
    verdict   TEXT NOT NULL CHECK (verdict IN ('rejected', 'verified')),
    notes     TEXT,
    ts        TEXT NOT NULL,
    source    TEXT NOT NULL CHECK (source IN ('cli', 'import', 'sync'))
);

-- Dedup key for idempotent re-imports: the same verdict at the same
-- timestamp from the same source IS the same event. CLI-sourced events
-- carry second-precision wall-clock timestamps, so genuine repeat
-- verdicts (re-reject after an iteration) get distinct rows.
CREATE UNIQUE INDEX IF NOT EXISTS verdict_events_dedup
    ON verdict_events (issue_num, verdict, ts, source);

CREATE TABLE IF NOT EXISTS chapters (
    chapter      INTEGER PRIMARY KEY,
    target_path  TEXT,
    entries_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS wall_time (
    project_id  TEXT PRIMARY KEY,
    seconds     INTEGER NOT NULL,
    recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS prompt_log (
    project_id TEXT PRIMARY KEY,
    url        TEXT,
    first_seen TEXT
);

CREATE TABLE IF NOT EXISTS skeleton_chapters (
    workdir          TEXT NOT NULL,
    input_file       TEXT NOT NULL,
    output_folder    TEXT,
    project_id       TEXT,
    agent_task_id    TEXT,
    status           TEXT,
    started_at       TEXT,
    completed_at     TEXT,
    duration_seconds REAL,
    output_path      TEXT,
    note             TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workdir, input_file)
);

CREATE TABLE IF NOT EXISTS refine_runs (
    workdir               TEXT PRIMARY KEY,
    target_folder         TEXT,
    iterations_completed  INTEGER NOT NULL DEFAULT 0,
    current_iteration_idx INTEGER NOT NULL DEFAULT 0,
    project_id            TEXT,
    agent_task_id         TEXT,
    status                TEXT,
    started_at            TEXT,
    completed_at          TEXT,
    duration_seconds      REAL,
    attempts              INTEGER NOT NULL DEFAULT 0,
    output_path           TEXT,
    note                  TEXT
);
"""


class LedgerError(RuntimeError):
    """Raised for ledger-level invariant violations (e.g. a db written
    by a newer marathon). Callers in the dual-write path catch broadly
    and degrade to legacy-only; the CLI lets it surface."""


@dataclass(frozen=True)
class Ledger:
    """Handle on one consumer repo's ledger db.

    Holds only the path — every operation opens a short-lived
    connection, ensures the schema (lazy init: the first write from any
    entry point creates the db), runs one transaction, and closes.
    Connection-per-operation keeps the handle trivially safe to share
    and leans on WAL + ``busy_timeout`` for cross-process writers (CLI
    verdicts racing the daemon); the cost is negligible at CLI
    frequency.
    """

    db_path: Path

    @classmethod
    def for_repo(cls, repo_dir: Path) -> "Ledger":
        """Ledger for an explicit consumer-repo root."""
        return cls(db_path=repo_dir / LEDGER_RELPATH)

    @classmethod
    def for_review_config(cls, cfg: "ReviewConfig") -> "Ledger":
        """Derive the db path from a loaded ReviewConfig.

        ``cfg.review_dir`` is ``<repo>/.marathon/review``; the db lives
        beside it at ``<repo>/.marathon/marathon.db``. Derived from
        ``review_dir`` (not ``repo_dir``) so the db always lands next
        to the review state it mirrors, even if a future config grows a
        relocated review dir."""
        return cls(db_path=cfg.review_dir.parent / "marathon.db")

    # --- connection / schema -------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000)
        # WAL: writers never block the (future) readers, and crashed
        # processes can't leave a half-written main db. busy_timeout
        # makes concurrent CLI/daemon writers queue instead of raising
        # immediately.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One schema-ensured transaction. ``with conn:`` commits on
        success / rolls back on exception; the connection always
        closes."""
        conn = self._connect()
        try:
            _ensure_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self) -> Path:
        """Create the db + schema v1. Idempotent: every DDL statement
        is ``IF NOT EXISTS`` and the version row is insert-or-ignore,
        so calling against an existing v1 db is a no-op. Raises
        :class:`LedgerError` against a newer-versioned db."""
        with self._tx():
            pass
        return self.db_path

    # --- upsert helpers (the dual-write surface) ------------------------

    def upsert_issue(
        self,
        issue_num: int,
        *,
        status: str,
        verdict_ts: str,
        chapter: Optional[int] = None,
        notes: Optional[str] = None,
        attempts: int = 0,
        last_iteration_ts: Optional[str] = None,
    ) -> None:
        """Mirror one ``IssueState`` row (latest-verdict snapshot)."""
        with self._tx() as conn:
            _upsert_issue(
                conn,
                issue_num,
                chapter=chapter,
                status=status,
                verdict_ts=verdict_ts,
                notes=notes,
                attempts=attempts,
                last_iteration_ts=last_iteration_ts,
            )

    def append_verdict_event(
        self,
        issue_num: int,
        verdict: str,
        *,
        ts: str,
        notes: Optional[str] = None,
        source: str = "cli",
    ) -> bool:
        """Append one verdict to the history. Returns True if a row was
        inserted, False if the dedup index dropped an exact duplicate
        (which is what makes re-imports idempotent)."""
        with self._tx() as conn:
            return _append_verdict_event(
                conn, issue_num, verdict, notes=notes, ts=ts, source=source
            )

    def upsert_chapter(
        self,
        chapter: int,
        *,
        target_path: Optional[str],
        entries: list[list],
    ) -> None:
        """Snapshot one config.toml ``[[chapters]]`` registry entry."""
        with self._tx() as conn:
            _upsert_chapter(conn, chapter, target_path, json.dumps(entries))

    def upsert_wall_time(
        self, project_id: str, *, seconds: int, recorded_at: Optional[str] = None
    ) -> None:
        """Record a project's wall-clock seconds. Full overwrite on
        conflict — same idempotent semantics as the v2 sidecar
        (re-recording the same Aristotle project never double-counts)."""
        with self._tx() as conn:
            _upsert_wall_time(conn, project_id, seconds, recorded_at)

    def upsert_prompt_log(
        self,
        project_id: str,
        *,
        url: Optional[str] = None,
        first_seen: Optional[str] = None,
    ) -> None:
        """Record one PromptLog project UUID. ``first_seen`` is
        write-once (the earliest sighting wins across re-imports);
        ``url`` takes the latest non-NULL value."""
        with self._tx() as conn:
            _upsert_prompt_log(conn, project_id, url, first_seen)

    # --- introspection ---------------------------------------------------

    def status(self) -> dict:
        """Schema version + per-table row counts, for
        ``marathon ledger status``. Ensures the schema as a side effect
        (cheap, and means status never crashes on a fresh repo)."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            tables = {
                # Table names come from the trusted TABLES constant, not
                # user input — f-string interpolation is safe here.
                name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in TABLES
            }
        return {"schema_version": int(row[0]), "tables": tables}


# --- module-level pure helpers (one open transaction in, rows out) ---------
#
# Split out of the Ledger methods so ``import_all`` can batch hundreds
# of rows into ONE transaction instead of one commit per row.


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    found = int(row[0])
    if found != SCHEMA_VERSION:
        # A newer marathon wrote this db. Refuse to touch it — the
        # dual-write shim catches this and degrades to legacy-only, so
        # an old checkout warns instead of corrupting a future schema.
        raise LedgerError(
            f"ledger schema_version {found} is newer than this marathon's "
            f"{SCHEMA_VERSION}; refusing to write"
        )


def _upsert_issue(
    conn: sqlite3.Connection,
    issue_num: int,
    *,
    chapter: Optional[int],
    status: str,
    verdict_ts: str,
    notes: Optional[str],
    attempts: int,
    last_iteration_ts: Optional[str],
) -> None:
    # chapter is COALESCEd so a writer that can't resolve the chapter
    # (issue missing from the registry mid-bootstrap) doesn't NULL out
    # a previously-known value.
    conn.execute(
        """
        INSERT INTO issues
            (issue_num, chapter, status, verdict_ts, notes, attempts,
             last_iteration_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(issue_num) DO UPDATE SET
            chapter           = COALESCE(excluded.chapter, issues.chapter),
            status            = excluded.status,
            verdict_ts        = excluded.verdict_ts,
            notes             = excluded.notes,
            attempts          = excluded.attempts,
            last_iteration_ts = excluded.last_iteration_ts
        """,
        (issue_num, chapter, status, verdict_ts, notes, attempts,
         last_iteration_ts),
    )


def _append_verdict_event(
    conn: sqlite3.Connection,
    issue_num: int,
    verdict: str,
    *,
    notes: Optional[str],
    ts: str,
    source: str,
) -> bool:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO verdict_events (issue_num, verdict, notes, ts, source)
        VALUES (?, ?, ?, ?, ?)
        """,
        (issue_num, verdict, notes, ts, source),
    )
    return cur.rowcount > 0


def _upsert_chapter(
    conn: sqlite3.Connection,
    chapter: int,
    target_path: Optional[str],
    entries_json: str,
) -> None:
    conn.execute(
        """
        INSERT INTO chapters (chapter, target_path, entries_json)
        VALUES (?, ?, ?)
        ON CONFLICT(chapter) DO UPDATE SET
            target_path  = excluded.target_path,
            entries_json = excluded.entries_json
        """,
        (chapter, target_path, entries_json),
    )


def _upsert_wall_time(
    conn: sqlite3.Connection,
    project_id: str,
    seconds: int,
    recorded_at: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT INTO wall_time (project_id, seconds, recorded_at)
        VALUES (?, ?, ?)
        ON CONFLICT(project_id) DO UPDATE SET
            seconds     = excluded.seconds,
            recorded_at = excluded.recorded_at
        """,
        (project_id, int(seconds), recorded_at),
    )


def _upsert_prompt_log(
    conn: sqlite3.Connection,
    project_id: str,
    url: Optional[str],
    first_seen: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT INTO prompt_log (project_id, url, first_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(project_id) DO UPDATE SET
            url        = COALESCE(excluded.url, prompt_log.url),
            first_seen = COALESCE(prompt_log.first_seen, excluded.first_seen)
        """,
        (project_id, url, first_seen),
    )


def _upsert_skeleton_chapter(
    conn: sqlite3.Connection, workdir: str, fields: dict
) -> None:
    conn.execute(
        """
        INSERT INTO skeleton_chapters
            (workdir, input_file, output_folder, project_id, agent_task_id,
             status, started_at, completed_at, duration_seconds, output_path,
             note, attempts)
        VALUES (:workdir, :input_file, :output_folder, :project_id,
                :agent_task_id, :status, :started_at, :completed_at,
                :duration_seconds, :output_path, :note, :attempts)
        ON CONFLICT(workdir, input_file) DO UPDATE SET
            output_folder    = excluded.output_folder,
            project_id       = excluded.project_id,
            agent_task_id    = excluded.agent_task_id,
            status           = excluded.status,
            started_at       = excluded.started_at,
            completed_at     = excluded.completed_at,
            duration_seconds = excluded.duration_seconds,
            output_path      = excluded.output_path,
            note             = excluded.note,
            attempts         = excluded.attempts
        """,
        {"workdir": workdir, **fields},
    )


def _upsert_refine_run(
    conn: sqlite3.Connection, workdir: str, fields: dict
) -> None:
    conn.execute(
        """
        INSERT INTO refine_runs
            (workdir, target_folder, iterations_completed,
             current_iteration_idx, project_id, agent_task_id, status,
             started_at, completed_at, duration_seconds, attempts,
             output_path, note)
        VALUES (:workdir, :target_folder, :iterations_completed,
                :current_iteration_idx, :project_id, :agent_task_id, :status,
                :started_at, :completed_at, :duration_seconds, :attempts,
                :output_path, :note)
        ON CONFLICT(workdir) DO UPDATE SET
            target_folder         = excluded.target_folder,
            iterations_completed  = excluded.iterations_completed,
            current_iteration_idx = excluded.current_iteration_idx,
            project_id            = excluded.project_id,
            agent_task_id         = excluded.agent_task_id,
            status                = excluded.status,
            started_at            = excluded.started_at,
            completed_at          = excluded.completed_at,
            duration_seconds      = excluded.duration_seconds,
            attempts              = excluded.attempts,
            output_path           = excluded.output_path,
            note                  = excluded.note
        """,
        {"workdir": workdir, **fields},
    )


# --- one-shot legacy import -------------------------------------------------

# PromptLog.md entry shapes seen in the wild:
#   ``2026-06-09T21:02:13-04:00  307cb266-efec-4f84-8bb6-8fae535031e3``
# plus older hand-written lines that embed full dashboard URLs. The UUID
# regex matches both (same pattern as formalization._UUID_RE — duplicated
# rather than imported so the ledger never grows a dependency on that
# module's private surface).
_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)
_ISO_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:.+\-]+)")
_URL_RE = re.compile(r"(https?://\S+)")
_PROMPTLOG_RELPATHS = (Path(".marathon") / "PromptLog.md", Path("PromptLog.md"))
_DASHBOARD_URL_TEMPLATE = "https://aristotle.harmonic.fun/dashboard/requests/{}"


def import_all(
    repo_dir: Path, workdirs_parent: Optional[Path] = None
) -> dict[str, int]:
    """One-shot ingest of every legacy state surface present under
    ``repo_dir`` (plus, when ``workdirs_parent`` is given, the
    per-workdir skeleton/refine checkpoints under it).

    Idempotent by construction: every surface lands via keyed upserts
    (and the verdict-event dedup index), so re-running refreshes rows
    in place instead of duplicating. Each absent surface is silently
    skipped — partial repos (no review subsystem, no wall-time sidecar)
    import whatever they have.

    Returns ``{surface: rows processed}`` — *processed*, not
    newly-created, so the counts are stable across re-runs and the CLI
    can print them as a coverage report.

    Runs in ONE transaction: an import that dies halfway leaves the
    ledger untouched rather than half-ingested.
    """
    ledger = Ledger.for_repo(repo_dir)
    counts = {
        "chapters": 0,
        "issues": 0,
        "verdict_events": 0,
        "wall_time": 0,
        "prompt_log": 0,
        "skeleton_chapters": 0,
        "refine_runs": 0,
    }
    with ledger._tx() as conn:
        counts["chapters"], issue_to_chapter = _import_config_chapters(
            conn, repo_dir
        )
        counts["issues"], counts["verdict_events"] = _import_review_state(
            conn, repo_dir, issue_to_chapter
        )
        counts["wall_time"] = _import_wall_time(conn, repo_dir)
        counts["prompt_log"] = _import_promptlog(conn, repo_dir)
        if workdirs_parent is not None:
            counts["skeleton_chapters"], counts["refine_runs"] = (
                _import_workdirs(conn, workdirs_parent)
            )
    return counts


def _import_config_chapters(
    conn: sqlite3.Connection, repo_dir: Path
) -> tuple[int, dict[int, int]]:
    """Ingest the ``[[chapters]]`` registry from review/config.toml.

    Parsed with raw tomllib rather than ``review.config.load_config``
    because the loader ``sys.exit``\\ s on a missing/invalid file — the
    import must instead skip surfaces that don't exist. Returns
    ``(chapters ingested, issue_num → chapter map)``; the map gives the
    issues import its chapter column.
    """
    cfg_path = repo_dir / ".marathon" / "review" / "config.toml"
    if not cfg_path.is_file():
        return 0, {}
    try:
        with cfg_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"  warning: {cfg_path} unreadable ({e}); skipping chapters import")
        return 0, {}

    template = data.get("target_path_template", "") or ""
    issue_to_chapter: dict[int, int] = {}
    count = 0
    for entry in data.get("chapters", []):
        try:
            chap = int(entry["chapter"])
        except (KeyError, TypeError, ValueError):
            print(f"  warning: malformed [[chapters]] entry {entry!r}; skipping")
            continue
        normalized: list[list] = []
        for row in entry.get("entries", []) or []:
            try:
                num, pattern = int(row[0]), str(row[1])
            except (IndexError, TypeError, ValueError):
                print(
                    f"  warning: chapter {chap} registry row {row!r} "
                    "malformed; skipping"
                )
                continue
            normalized.append([num, pattern])
            issue_to_chapter[num] = chap
        target_path: Optional[str] = None
        if template:
            try:
                target_path = template.format(chapter=chap)
            except (KeyError, IndexError, ValueError):
                target_path = None
        _upsert_chapter(conn, chap, target_path, json.dumps(normalized))
        count += 1
    return count, issue_to_chapter


def _import_review_state(
    conn: sqlite3.Connection,
    repo_dir: Path,
    issue_to_chapter: dict[int, int],
) -> tuple[int, int]:
    """Ingest ``review/state.json`` into ``issues`` + one synthetic
    ``verdict_events`` row per issue (source='import', ts=verdict_ts —
    the dedup index makes re-runs no-ops).

    Parsed as raw JSON rather than via ``review.state.load_state``
    (which needs a full ReviewConfig); the schema is documented in that
    module and additive-only at schema_version 1. A "stalled" entry's
    event is recorded as 'rejected' — stalling is a daemon bookkeeping
    flip, and the entry's verdict_ts/notes belong to the underlying
    human rejection.
    """
    path = repo_dir / ".marathon" / "review" / "state.json"
    if not path.is_file():
        return 0, 0
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"  warning: {path} unreadable ({e}); skipping issues import")
        return 0, 0

    issues = 0
    events = 0
    for key, val in (data.get("issues") or {}).items():
        try:
            issue_num = int(key)
            status = str(val["status"])
            verdict_ts = str(val["verdict_ts"])
        except (KeyError, TypeError, ValueError) as e:
            print(f"  warning: {path} issue {key!r} malformed ({e}); skipping")
            continue
        if status not in ("rejected", "verified", "stalled"):
            print(
                f"  warning: {path} issue {key} has unknown status "
                f"{status!r}; skipping"
            )
            continue
        notes = val.get("notes")
        _upsert_issue(
            conn,
            issue_num,
            chapter=issue_to_chapter.get(issue_num),
            status=status,
            verdict_ts=verdict_ts,
            notes=notes,
            attempts=int(val.get("attempts", 0) or 0),
            last_iteration_ts=val.get("last_iteration_ts"),
        )
        issues += 1
        verdict = "rejected" if status == "stalled" else status
        # Count processed (not inserted): the dedup index turns re-runs
        # into no-op inserts, and import_all promises counts that are
        # stable across re-runs.
        _append_verdict_event(
            conn, issue_num, verdict, notes=notes, ts=verdict_ts, source="import"
        )
        events += 1
    return issues, events


def _import_wall_time(conn: sqlite3.Connection, repo_dir: Path) -> int:
    """Ingest the wall-time sidecar. Delegates v1/v2 shape handling to
    ``formalization._load_sidecar`` — the single battle-tested reader
    of that file (v1 ``total_seconds`` arrives pre-rolled into
    ``build_only_seconds``), so the ledger can't drift from the live
    accumulator's interpretation."""
    # Local import: keeps this module importable even if formalization
    # grows heavier dependencies later.
    from marathon.formalization import _WALL_TIME_SIDECAR, _load_sidecar

    path = repo_dir / _WALL_TIME_SIDECAR
    if not path.is_file():
        return 0
    data = _load_sidecar(path)
    count = 0
    for project_id, entry in (data.get("projects") or {}).items():
        if not isinstance(entry, dict):
            continue
        try:
            seconds = int(entry.get("seconds", 0) or 0)
        except (TypeError, ValueError):
            continue
        _upsert_wall_time(conn, str(project_id), seconds, entry.get("added_at"))
        count += 1
    build_only = int(data.get("build_only_seconds", 0) or 0)
    if build_only > 0:
        _upsert_wall_time(conn, BUILD_ONLY_PROJECT_ID, build_only, None)
        count += 1
    return count


def _import_promptlog(conn: sqlite3.Connection, repo_dir: Path) -> int:
    """Ingest PromptLog.md (preferring ``.marathon/PromptLog.md`` over
    the legacy repo-root location — same precedence as the live
    appender in ``post_pipeline``). One row per unique project UUID;
    ``first_seen`` comes from the leading timestamp of the line where
    the UUID first appears; the url is the line's explicit link if it
    has one, else the synthesized dashboard URL."""
    log_path: Optional[Path] = None
    for rel in _PROMPTLOG_RELPATHS:
        if (repo_dir / rel).is_file():
            log_path = repo_dir / rel
            break
    if log_path is None:
        return 0
    try:
        text = log_path.read_text()
    except OSError as e:
        print(f"  warning: {log_path} unreadable ({e}); skipping prompt_log import")
        return 0

    seen: set[str] = set()
    for line in text.splitlines():
        uuids = _UUID_RE.findall(line)
        if not uuids:
            continue
        ts_match = _ISO_TS_RE.match(line.strip())
        first_seen = ts_match.group(1) if ts_match else None
        url_match = _URL_RE.search(line)
        for project_id in uuids:
            url = (
                url_match.group(1)
                if url_match and project_id in url_match.group(1)
                else _DASHBOARD_URL_TEMPLATE.format(project_id)
            )
            _upsert_prompt_log(conn, project_id, url, first_seen)
            seen.add(project_id)
    return len(seen)


def _import_workdirs(
    conn: sqlite3.Connection, workdirs_parent: Path
) -> tuple[int, int]:
    """Ingest per-workdir ``marathon-state.json`` (skeleton checkpoints)
    and ``marathon-refine-state.json`` (refine checkpoints) from
    ``workdirs_parent`` itself and each immediate subdirectory — the
    same sibling-scan convention the referee uses. Parsed via
    ``marathon.state``'s loaders (single source of truth for unknown-key
    tolerance). Import-only in Phase 1: there are deliberately no live
    writers for these tables until the Phase-3 Conductor owns them."""
    if not workdirs_parent.is_dir():
        print(
            f"  warning: workdirs parent {workdirs_parent} is not a "
            "directory; skipping workdir import"
        )
        return 0, 0

    candidates = [workdirs_parent]
    candidates.extend(
        sorted(p for p in workdirs_parent.iterdir() if p.is_dir())
    )

    skeleton_rows = 0
    refine_rows = 0
    for workdir in candidates:
        wd_key = str(workdir.resolve())

        skel_path = workdir / "marathon-state.json"
        if skel_path.is_file():
            try:
                run = load_state(skel_path)
            except (OSError, ValueError, TypeError) as e:
                print(f"  warning: {skel_path} unreadable ({e}); skipping")
            else:
                for chapter_state in run.chapters:
                    _upsert_skeleton_chapter(conn, wd_key, asdict(chapter_state))
                    skeleton_rows += 1

        refine_path = workdir / "marathon-refine-state.json"
        if refine_path.is_file():
            try:
                refine = load_refine_state(refine_path)
            except (OSError, ValueError, TypeError) as e:
                print(f"  warning: {refine_path} unreadable ({e}); skipping")
            else:
                if refine is not None:
                    _upsert_refine_run(conn, wd_key, asdict(refine))
                    refine_rows += 1
    return skeleton_rows, refine_rows


def print_import_summary(db_path: Path, counts: dict[str, int]) -> None:
    """Human-readable `marathon ledger import` report."""
    print(f"imported legacy state into {db_path}:")
    for surface, n in counts.items():
        print(f"  {surface}: {n} row(s)")
    if all(n == 0 for n in counts.values()):
        print("  (no legacy surfaces found — is --repo-dir the consumer repo?)")

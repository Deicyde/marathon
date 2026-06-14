"""Tests for the Phase-1 ledger (marathon.ledger) and the dual-write
shim in marathon.review.state.

Phase-1 contract under test (docs/marathon-v2-plan.md §3 Phase 1):

* schema init is idempotent — `init()` twice is a no-op, and a db
  stamped with a NEWER schema_version is refused (so an old marathon
  degrades instead of corrupting it);
* every `record_*` write lands in BOTH stores (legacy state.json stays
  the read-side truth; the ledger mirrors it), and human verdicts
  additionally append to the tracked `verdicts.jsonl`;
* a broken ledger degrades to legacy-only with exactly ONE printed
  warning — never an exception, never a lost legacy write;
* `import_all` over fixture copies of the legacy surfaces is
  idempotent: re-running updates rows in place, duplicating nothing
  (the verdict_events dedup index is the interesting case).

No subprocesses, no network — everything runs against tmp_path.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

import marathon.review.state as state_mod
from marathon.ledger import (
    BUILD_ONLY_PROJECT_ID,
    LEDGER_RELPATH,
    SCHEMA_VERSION,
    TABLES,
    Ledger,
    LedgerError,
    import_all,
)
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels


# --- helpers -----------------------------------------------------------------


def make_cfg(tmp_path: Path) -> ReviewConfig:
    """A realistic ReviewConfig rooted at tmp_path (mirrors the shape
    `load_config` builds; same fixture style as test_review_plumbing)."""
    return ReviewConfig(
        repo_dir=tmp_path,
        config_path=tmp_path / ".marathon" / "review" / "config.toml",
        github_repo="someone/SomeProject",
        parent_issue=1,
        referee_path=tmp_path / ".marathon" / "referee.md",
        target_path_template="SomeProject/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(
                chapter=14,
                entries=[(14, "Lemma 14.7"), (15, "Proposition 14.8")],
            ),
        },
    )


def db_rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def db_one(db_path: Path, sql: str, params: tuple = ()):
    rows = db_rows(db_path, sql, params)
    assert len(rows) == 1, f"expected one row from {sql!r}, got {rows!r}"
    return rows[0]


@pytest.fixture(autouse=True)
def _reset_ledger_warn_flag(monkeypatch):
    """The dual-write shim warns once per *process*; tests share a
    process, so pin the flag to a known state per test (monkeypatch
    restores the original afterwards)."""
    monkeypatch.setattr(state_mod, "_ledger_warn_emitted", False)


# --- schema init -------------------------------------------------------------


def test_init_idempotent_and_status_empty(tmp_path):
    ledger = Ledger.for_repo(tmp_path)
    first = ledger.init()
    second = ledger.init()
    assert first == second == tmp_path / LEDGER_RELPATH
    assert first.is_file()

    info = ledger.status()
    assert info["schema_version"] == SCHEMA_VERSION == 3
    assert set(info["tables"]) == set(TABLES)
    assert all(count == 0 for count in info["tables"].values())


def test_for_review_config_derives_db_beside_review_dir(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = Ledger.for_review_config(cfg)
    assert ledger.db_path == tmp_path / ".marathon" / "marathon.db"


def test_newer_schema_version_is_refused(tmp_path):
    """A db written by a future marathon must not be touched: every op
    raises LedgerError, which the dual-write shim turns into the
    degrade-to-legacy warning."""
    ledger = Ledger.for_repo(tmp_path)
    ledger.init()
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(LedgerError):
        ledger.init()
    with pytest.raises(LedgerError):
        ledger.upsert_wall_time("p1", seconds=1)


# --- dual-write --------------------------------------------------------------


def test_record_rejection_writes_both_stores(tmp_path):
    cfg = make_cfg(tmp_path)
    entry = state_mod.record_rejection(cfg, 14, "- wrong binder order")

    # Legacy state.json: unchanged behavior (read-side truth).
    legacy = json.loads((tmp_path / state_mod.STATE_RELPATH).read_text())
    assert legacy["issues"]["14"]["status"] == "rejected"

    # Ledger mirror: latest-verdict row, chapter resolved from the registry.
    db = Ledger.for_review_config(cfg).db_path
    row = db_one(
        db,
        "SELECT chapter, status, verdict_ts, notes, attempts, "
        "last_iteration_ts FROM issues WHERE issue_num = 14",
    )
    assert row == (14, "rejected", entry.verdict_ts, "- wrong binder order", 0, None)

    # Append-only history: one cli-sourced event.
    event = db_one(
        db,
        "SELECT verdict, notes, ts, source FROM verdict_events "
        "WHERE issue_num = 14",
    )
    assert event == ("rejected", "- wrong binder order", entry.verdict_ts, "cli")


def test_record_verification_overwrites_row_and_appends_event(tmp_path):
    cfg = make_cfg(tmp_path)
    state_mod.record_rejection(cfg, 14, "- bad universe")
    entry = state_mod.record_verification(cfg, 14)

    db = Ledger.for_review_config(cfg).db_path
    row = db_one(
        db, "SELECT status, notes, attempts FROM issues WHERE issue_num = 14"
    )
    assert row == ("verified", None, 0)
    # History keeps BOTH verdicts (legacy state.json forgets the first).
    verdicts = [
        v for (v,) in db_rows(
            db, "SELECT verdict FROM verdict_events WHERE issue_num = 14 ORDER BY id"
        )
    ]
    assert verdicts == ["rejected", "verified"]
    assert entry.status == "verified"


def test_daemon_bookkeeping_mirrors_without_events(tmp_path):
    """record_iteration / record_failed_attempt / record_stall mirror
    the row but are NOT human verdicts — verdict_events stays at the
    single rejection event."""
    cfg = make_cfg(tmp_path)
    state_mod.record_rejection(cfg, 15, "- missing hypothesis")
    db = Ledger.for_review_config(cfg).db_path

    state_mod.record_iteration(cfg, 15)
    (last_iteration_ts,) = db_one(
        db, "SELECT last_iteration_ts FROM issues WHERE issue_num = 15"
    )
    assert last_iteration_ts is not None

    state_mod.record_failed_attempt(cfg, 15)
    (attempts,) = db_one(db, "SELECT attempts FROM issues WHERE issue_num = 15")
    assert attempts == 1

    state_mod.record_stall(cfg, 15)
    (status,) = db_one(db, "SELECT status FROM issues WHERE issue_num = 15")
    assert status == "stalled"

    (event_count,) = db_one(
        db, "SELECT COUNT(*) FROM verdict_events WHERE issue_num = 15"
    )
    assert event_count == 1  # just the original rejection


# --- graceful degradation ----------------------------------------------------


def test_ledger_failure_degrades_to_legacy_with_one_warning(
    tmp_path, monkeypatch, capsys
):
    import marathon.ledger as ledger_mod

    def boom(self):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ledger_mod.Ledger, "_connect", boom)
    cfg = make_cfg(tmp_path)

    entry = state_mod.record_rejection(cfg, 14, "- broken statement")
    assert entry.status == "rejected"

    # Legacy write survived the ledger failure.
    legacy = json.loads((tmp_path / state_mod.STATE_RELPATH).read_text())
    assert legacy["issues"]["14"]["status"] == "rejected"
    # The tracked JSONL is independent of the ledger and also survived.
    jsonl = (tmp_path / state_mod.VERDICTS_RELPATH).read_text().splitlines()
    assert len(jsonl) == 1

    out = capsys.readouterr().out
    assert out.count("ledger write failed") == 1

    # Second failing write: silent (one warning per process, not per call).
    state_mod.record_verification(cfg, 14)
    assert "ledger write failed" not in capsys.readouterr().out
    legacy = json.loads((tmp_path / state_mod.STATE_RELPATH).read_text())
    assert legacy["issues"]["14"]["status"] == "verified"

    # Nothing half-created on the ledger side.
    assert not (tmp_path / LEDGER_RELPATH).exists()


# --- verdicts.jsonl ----------------------------------------------------------


def test_verdicts_jsonl_appends_and_never_rewrites(tmp_path):
    cfg = make_cfg(tmp_path)
    path = tmp_path / state_mod.VERDICTS_RELPATH

    state_mod.record_rejection(cfg, 14, "- wrong binder")
    first_content = path.read_text()
    assert len(first_content.splitlines()) == 1

    state_mod.record_verification(cfg, 14)
    state_mod.record_rejection(cfg, 15, "- vacuous hypothesis")
    content = path.read_text()
    # Strictly appended: the earlier bytes are untouched.
    assert content.startswith(first_content)

    lines = [json.loads(line) for line in content.splitlines()]
    assert [
        (rec["issue"], rec["verdict"]) for rec in lines
    ] == [(14, "rejected"), (14, "verified"), (15, "rejected")]
    for rec in lines:
        assert set(rec) == {"issue", "verdict", "notes", "ts", "source"}
        assert rec["source"] == "cli"
    assert lines[0]["notes"] == "- wrong binder"
    assert lines[1]["notes"] is None

    # Daemon bookkeeping never touches the verdict log.
    state_mod.record_iteration(cfg, 15)
    assert len(path.read_text().splitlines()) == 3


# --- import_all --------------------------------------------------------------


PROJECT_A = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PROJECT_B = "11111111-2222-3333-4444-555555555555"


def write_fixture_repo(tmp_path: Path) -> Path:
    """Small but complete copies of the legacy surfaces. Returns the
    workdirs parent (outside the repo, like real refine workdirs)."""
    review_dir = tmp_path / ".marathon" / "review"
    review_dir.mkdir(parents=True)
    (review_dir / "config.toml").write_text(textwrap.dedent(
        """\
        github_repo = "someone/SomeProject"
        parent_issue = 1
        target_path_template = "SomeProject/Chapter{chapter}"

        [[chapters]]
        chapter = 14
        entries = [
          [14, "Lemma 14.7"],
          [15, "Proposition 14.8"],
        ]
        """
    ))
    (review_dir / "state.json").write_text(json.dumps({
        "schema_version": 1,
        "issues": {
            "14": {
                "status": "verified",
                "verdict_ts": "2026-06-01T10:00:00-04:00",
            },
            "15": {
                "status": "rejected",
                "verdict_ts": "2026-06-02T11:00:00-04:00",
                "notes": "- wrong binder",
                "attempts": 1,
            },
        },
    }))
    (tmp_path / ".marathon" / "wall-time.json").write_text(json.dumps({
        "version": 2,
        "projects": {
            PROJECT_A: {"seconds": 120, "added_at": "2026-06-01T10:30:00+00:00"},
        },
        "build_only_seconds": 30,
    }))
    (tmp_path / ".marathon" / "PromptLog.md").write_text(
        "# Prompt log\n\n"
        f"2026-06-01T10:00:00-04:00  {PROJECT_A}\n\n"
        f"2026-06-02T11:00:00-04:00  {PROJECT_B}\n\n"
        f"2026-06-03T12:00:00-04:00  {PROJECT_B}\n"  # duplicate UUID
    )

    workdirs_parent = tmp_path / "workdirs"
    workdir = workdirs_parent / "c14"
    workdir.mkdir(parents=True)
    (workdir / "marathon-state.json").write_text(json.dumps({
        "chapters": [{
            "input_file": "chap14.tex",
            "output_folder": "Chapter14",
            "project_id": PROJECT_A,
            "agent_task_id": "task-1",
            "status": "COMPLETE",
            "started_at": "2026-06-01T09:00:00-04:00",
            "attempts": 1,
        }],
    }))
    (workdir / "marathon-refine-state.json").write_text(json.dumps({
        "target_folder": "SomeProject/Chapter14",
        "iterations_completed": 3,
        "project_id": PROJECT_B,
        "status": "COMPLETE",
        "attempts": 2,
    }))
    return workdirs_parent


def test_import_all_ingests_every_surface(tmp_path):
    workdirs_parent = write_fixture_repo(tmp_path)
    counts = import_all(tmp_path, workdirs_parent=workdirs_parent)
    assert counts == {
        "chapters": 1,
        "issues": 2,
        "verdict_events": 2,
        "wall_time": 2,  # one project + the build-only sentinel
        "prompt_log": 2,  # PROJECT_B's duplicate line collapses
        "skeleton_chapters": 1,
        "refine_runs": 1,
    }

    db = Ledger.for_repo(tmp_path).db_path

    # issues get their chapter from the config.toml registry; the
    # rejection's import event carries the original verdict_ts.
    assert db_one(
        db, "SELECT chapter, status, attempts FROM issues WHERE issue_num = 15"
    ) == (14, "rejected", 1)
    assert db_one(
        db,
        "SELECT verdict, ts, source FROM verdict_events WHERE issue_num = 15",
    ) == ("rejected", "2026-06-02T11:00:00-04:00", "import")

    # chapters snapshot: target path rendered, entries as JSON.
    chapter, target_path, entries_json = db_one(
        db, "SELECT chapter, target_path, entries_json FROM chapters"
    )
    assert (chapter, target_path) == (14, "SomeProject/Chapter14")
    assert json.loads(entries_json) == [[14, "Lemma 14.7"], [15, "Proposition 14.8"]]

    # wall_time: project row + build-only sentinel preserve the total.
    assert db_one(
        db, "SELECT seconds FROM wall_time WHERE project_id = ?", (PROJECT_A,)
    ) == (120,)
    assert db_one(
        db,
        "SELECT seconds FROM wall_time WHERE project_id = ?",
        (BUILD_ONLY_PROJECT_ID,),
    ) == (30,)

    # prompt_log: first-seen timestamp wins for the duplicated UUID.
    assert db_one(
        db, "SELECT first_seen FROM prompt_log WHERE project_id = ?", (PROJECT_B,)
    ) == ("2026-06-02T11:00:00-04:00",)

    # workdir checkpoints.
    assert db_one(
        db,
        "SELECT input_file, project_id, status, attempts FROM skeleton_chapters",
    ) == ("chap14.tex", PROJECT_A, "COMPLETE", 1)
    assert db_one(
        db,
        "SELECT target_folder, iterations_completed, status FROM refine_runs",
    ) == ("SomeProject/Chapter14", 3, "COMPLETE")


def test_import_all_is_idempotent(tmp_path):
    workdirs_parent = write_fixture_repo(tmp_path)
    counts_first = import_all(tmp_path, workdirs_parent=workdirs_parent)
    counts_second = import_all(tmp_path, workdirs_parent=workdirs_parent)
    # Counts report rows *processed* — stable across re-runs.
    assert counts_first == counts_second

    # And the tables hold no duplicates (verdict_events is the one that
    # would silently grow without its dedup index).
    info = Ledger.for_repo(tmp_path).status()
    assert info["tables"] == {
        "issues": 2,
        "verdict_events": 2,
        "decl_verdicts": 0,  # v2 table; import_all never writes it
        "targets": 0,  # v3 tables; import_all never writes them
        "target_deps": 0,
        "chapters": 1,
        "wall_time": 2,
        "prompt_log": 2,
        "skeleton_chapters": 1,
        "refine_runs": 1,
    }


def test_import_all_skips_absent_surfaces(tmp_path):
    """A bare repo (no review subsystem, no sidecars) imports cleanly
    as all-zeros — absent surfaces must never be an error."""
    counts = import_all(tmp_path)
    assert all(n == 0 for n in counts.values())
    assert Ledger.for_repo(tmp_path).db_path.is_file()


def test_import_then_cli_verdict_coexist(tmp_path):
    """Import seeds history; a later CLI verdict on the same issue
    upserts the row and appends a second, distinct event — the
    dedup key (ts, source differ) must not swallow it."""
    write_fixture_repo(tmp_path)
    import_all(tmp_path)

    cfg = make_cfg(tmp_path)
    state_mod.record_verification(cfg, 15)

    db = Ledger.for_repo(tmp_path).db_path
    assert db_one(db, "SELECT status FROM issues WHERE issue_num = 15") == (
        "verified",
    )
    sources = [
        s for (s,) in db_rows(
            db,
            "SELECT source FROM verdict_events WHERE issue_num = 15 ORDER BY id",
        )
    ]
    assert sources == ["import", "cli"]

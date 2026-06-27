"""Tests for the per-run metric shards + central aggregation redesign.

Contract (the fix for the textual-collision outage that stranded
#159/#162/#165):

* Each refine writes ONE per-run shard under ``.marathon/metrics/`` whose
  path is UNIQUE per run (keyed by the Aristotle project UUID), so two PRs
  off the same base never touch the same path — zero textual collision.
* The conductor AGGREGATES every shard back into the two rollups
  (``.marathon/wall-time.json``, ``.marathon/PromptLog.md``) centrally; the
  rollups are derived, the shards are the durable git source.
* A one-time BACKFILL turns the existing rollups into shards losslessly:
  regenerating the rollups from (legacy + shards) reproduces today's
  numbers + UUIDs; re-running never duplicates (idempotent).

No test touches Aristotle, the network, or real git.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import marathon.metrics as metrics
import marathon.post_pipeline as pp


# --- shard write: unique path, correct content -----------------------------


def test_shard_path_keyed_by_project_uuid_is_unique(tmp_path):
    """The collision fix: two runs with DISTINCT project UUIDs get DISTINCT
    shard paths, so their PRs never touch the same file. Two runs of the
    SAME project (continuation/retry) reuse the SAME path (idempotent)."""
    p1 = metrics.shard_path(tmp_path, project_id="aaaa-1", chapter="Chapter14", issue=22)
    p2 = metrics.shard_path(tmp_path, project_id="bbbb-2", chapter="Chapter14", issue=22)
    assert p1 != p2
    p1_again = metrics.shard_path(tmp_path, project_id="aaaa-1", chapter="Chapter14", issue=99)
    # Different chapter/issue, same project → still the same path (UUID is
    # the canonical key; an existing shard for the pid is reused).
    metrics.write_shard(tmp_path, project_id="aaaa-1", chapter="Chapter14", issue=22)
    assert metrics.shard_path(
        tmp_path, project_id="aaaa-1", chapter="ChapterX", issue=1
    ) == p1


def test_write_shard_content_and_merge(tmp_path):
    """A shard carries the run's data; a later write for the SAME project
    MERGES fields (submit-time PromptLog line + post-pipeline wall_seconds)
    rather than clobbering."""
    # Submit-time: PromptLog line, no wall-clock yet.
    metrics.write_shard(
        tmp_path, project_id="proj-1", chapter="Chapter14", issue=22,
        submitted_at="2026-06-20T10:00:00-04:00",
        promptlog_line="2026-06-20T10:00:00-04:00  proj-1",
    )
    # Post-pipeline: wall_seconds for the same project (same shard).
    path = metrics.write_shard(
        tmp_path, project_id="proj-1", chapter="Chapter14", issue=22,
        wall_seconds=1234,
    )
    data = json.loads(path.read_text())
    assert data["project_id"] == "proj-1"
    assert data["wall_seconds"] == 1234
    assert data["submitted_at"] == "2026-06-20T10:00:00-04:00"  # preserved
    assert data["promptlog_line"] == "2026-06-20T10:00:00-04:00  proj-1"
    assert data["chapter"] == 14 and data["issue"] == 22
    # Exactly one shard on disk for this project (no split).
    assert len(list((tmp_path / ".marathon" / "metrics").glob("*.json"))) == 1


# --- aggregation: shards -> rollups ----------------------------------------


def _seed_rollup_dirs(repo: Path) -> None:
    (repo / ".marathon").mkdir(parents=True, exist_ok=True)


def test_aggregate_sums_wall_and_dedups_promptlog(tmp_path):
    repo = tmp_path
    _seed_rollup_dirs(repo)
    metrics.write_shard(
        repo, project_id="proj-1", chapter=14, issue=1,
        wall_seconds=100, submitted_at="2026-06-20T01:00:00-04:00",
        promptlog_line="2026-06-20T01:00:00-04:00  proj-1",
    )
    metrics.write_shard(
        repo, project_id="proj-2", chapter=15, issue=2,
        wall_seconds=250, submitted_at="2026-06-21T01:00:00-04:00",
        promptlog_line="2026-06-21T01:00:00-04:00  proj-2",
    )
    roll = metrics.aggregate_rollups(repo)
    wt = roll["wall_time"]
    assert wt["projects"]["proj-1"]["seconds"] == 100
    assert wt["projects"]["proj-2"]["seconds"] == 250
    # Total wall = 350 (no double-count).
    total = wt["build_only_seconds"] + sum(p["seconds"] for p in wt["projects"].values())
    assert total == 350
    # PromptLog: one line per UUID, sorted.
    assert roll["promptlog_lines"] == [
        "2026-06-20T01:00:00-04:00  proj-1",
        "2026-06-21T01:00:00-04:00  proj-2",
    ]


def test_aggregate_idempotent_no_double_count(tmp_path):
    """Re-aggregating (and re-writing the rollups) twice yields the same
    numbers — a project's seconds are OVERWRITTEN by its shard, never
    summed across re-runs."""
    repo = tmp_path
    _seed_rollup_dirs(repo)
    metrics.write_shard(repo, project_id="proj-1", chapter=14, issue=1, wall_seconds=100)
    metrics.write_rollups(repo, metrics.aggregate_rollups(repo))
    first = json.loads((repo / ".marathon" / "wall-time.json").read_text())
    # Aggregate again from (now-written legacy rollup + shards).
    metrics.write_rollups(repo, metrics.aggregate_rollups(repo))
    second = json.loads((repo / ".marathon" / "wall-time.json").read_text())
    assert first["projects"] == second["projects"]
    assert first["build_only_seconds"] == second["build_only_seconds"] == 0
    assert first["projects"]["proj-1"]["seconds"] == 100


def test_aggregate_merges_legacy_rollup_without_double_count(tmp_path):
    """A project already in the legacy wall-time.json plus a shard for the
    same project resolves to the shard's value once (overwrite, not sum)."""
    repo = tmp_path
    _seed_rollup_dirs(repo)
    (repo / ".marathon" / "wall-time.json").write_text(json.dumps({
        "version": 2,
        "projects": {"proj-1": {"seconds": 100, "added_at": "x"},
                     "proj-old": {"seconds": 50, "added_at": "y"}},
        "build_only_seconds": 7,
    }))
    # A shard updates proj-1 and adds proj-2.
    metrics.write_shard(repo, project_id="proj-1", chapter=14, issue=1, wall_seconds=999)
    metrics.write_shard(repo, project_id="proj-2", chapter=15, issue=2, wall_seconds=10)
    wt = metrics.aggregate_rollups(repo)["wall_time"]
    assert wt["projects"]["proj-1"]["seconds"] == 999   # shard overwrote legacy
    assert wt["projects"]["proj-old"]["seconds"] == 50  # legacy-only preserved
    assert wt["projects"]["proj-2"]["seconds"] == 10
    assert wt["build_only_seconds"] == 7  # legacy lump preserved (no build-only shard)


def test_aggregate_preserves_legacy_promptlog_lines_verbatim(tmp_path):
    """PromptLog lines already in the file — including old non-ISO
    hand-written ones — survive aggregation verbatim, deduped by UUID."""
    repo = tmp_path
    _seed_rollup_dirs(repo)
    (repo / ".marathon" / "PromptLog.md").write_text(
        "April 25th  aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n"
        "\n"
        "2026-06-20T01:00:00-04:00  bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n"
    )
    # A shard for a NEW uuid; and one re-stating an existing uuid (no dup).
    metrics.write_shard(
        repo, project_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        chapter=14, issue=1,
        promptlog_line="2026-06-22T01:00:00-04:00  cccccccc-cccc-cccc-cccc-cccccccccccc",
    )
    metrics.write_shard(
        repo, project_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        chapter=14, issue=2,
        promptlog_line="ZZZ should-not-replace  aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    lines = metrics.aggregate_rollups(repo)["promptlog_lines"]
    # Legacy hand-written line kept verbatim (shard did NOT replace it).
    assert "April 25th  aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in lines
    assert "2026-06-20T01:00:00-04:00  bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in lines
    assert "2026-06-22T01:00:00-04:00  cccccccc-cccc-cccc-cccc-cccccccccccc" in lines
    # Exactly 3 UUIDs (the re-stated 'aaaa' did not duplicate).
    assert len(lines) == 3


# --- backfill: legacy -> shards round-trips losslessly ----------------------


def _write_legacy(repo: Path, *, projects: dict, build_only: int, promptlog: str) -> None:
    (repo / ".marathon").mkdir(parents=True, exist_ok=True)
    (repo / ".marathon" / "wall-time.json").write_text(json.dumps({
        "version": 2, "projects": projects, "build_only_seconds": build_only,
    }))
    (repo / ".marathon" / "PromptLog.md").write_text(promptlog)


def test_backfill_then_aggregate_reproduces_legacy_content(tmp_path):
    """Lossless round-trip: backfill the legacy rollups to shards, blow
    away the legacy rollups, regenerate from shards alone — and the numbers
    + UUIDs match what we started with."""
    repo = tmp_path
    projects = {
        "11111111-1111-1111-1111-111111111111": {"seconds": 2397, "added_at": "t1"},
        "22222222-2222-2222-2222-222222222222": {"seconds": 1888, "added_at": "t2"},
    }
    promptlog = (
        "April 25th  99999999-9999-9999-9999-999999999999\n\n"
        "2026-06-20T01:00:00-04:00  11111111-1111-1111-1111-111111111111\n\n"
        "2026-06-27T00:36:29-04:00  22222222-2222-2222-2222-222222222222\n"
    )
    _write_legacy(repo, projects=projects, build_only=0, promptlog=promptlog)

    # Expected totals from the legacy files.
    expected_total = sum(p["seconds"] for p in projects.values())
    expected_uuids = {
        "99999999-9999-9999-9999-999999999999",
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    }

    summary = metrics.backfill_shards(repo)
    assert summary["wall_shards"] == 2
    assert summary["promptlog_shards"] == 3

    # Blow away the legacy rollups — shards must carry everything.
    (repo / ".marathon" / "wall-time.json").unlink()
    (repo / ".marathon" / "PromptLog.md").unlink()

    roll = metrics.aggregate_rollups(repo)
    wt = roll["wall_time"]
    got_total = wt["build_only_seconds"] + sum(p["seconds"] for p in wt["projects"].values())
    assert got_total == expected_total
    assert wt["projects"]["11111111-1111-1111-1111-111111111111"]["seconds"] == 2397
    assert wt["projects"]["22222222-2222-2222-2222-222222222222"]["seconds"] == 1888
    got_uuids = {ln.split()[-1] for ln in roll["promptlog_lines"]}
    assert got_uuids == expected_uuids
    # The non-ISO legacy line survived (its verbatim text is in a shard).
    assert "April 25th  99999999-9999-9999-9999-999999999999" in roll["promptlog_lines"]


def test_backfill_preserves_build_only_seconds(tmp_path):
    """build_only_seconds becomes a dedicated shard and round-trips."""
    repo = tmp_path
    _write_legacy(repo, projects={}, build_only=4321, promptlog="")
    metrics.backfill_shards(repo)
    (repo / ".marathon" / "wall-time.json").unlink()
    wt = metrics.aggregate_rollups(repo)["wall_time"]
    assert wt["build_only_seconds"] == 4321


def test_backfill_is_idempotent(tmp_path):
    """Re-running backfill overwrites the same shards (UUID-keyed) — the
    shard count and aggregated total are unchanged."""
    repo = tmp_path
    projects = {"11111111-1111-1111-1111-111111111111": {"seconds": 100, "added_at": "t"}}
    _write_legacy(
        repo, projects=projects, build_only=0,
        promptlog="2026-06-20T01:00:00-04:00  11111111-1111-1111-1111-111111111111\n",
    )
    metrics.backfill_shards(repo)
    n1 = len(list((repo / ".marathon" / "metrics").glob("*.json")))
    metrics.backfill_shards(repo)
    n2 = len(list((repo / ".marathon" / "metrics").glob("*.json")))
    assert n1 == n2 == 1
    wt = metrics.aggregate_rollups(repo)["wall_time"]
    assert wt["projects"]["11111111-1111-1111-1111-111111111111"]["seconds"] == 100


def test_write_rollups_promptlog_one_line_per_uuid(tmp_path):
    """The written PromptLog.md is one line per UUID, sorted, newline-
    terminated — the committed human projection."""
    repo = tmp_path
    _seed_rollup_dirs(repo)
    (repo / ".marathon" / "PromptLog.md").write_text("")  # opt-in marker
    metrics.write_shard(
        repo, project_id="bbbb", chapter=1, issue=1,
        promptlog_line="2026-06-21  bbbb",
    )
    metrics.write_shard(
        repo, project_id="aaaa", chapter=1, issue=2,
        promptlog_line="2026-06-20  aaaa",
    )
    metrics.write_rollups(repo, metrics.aggregate_rollups(repo))
    text = (repo / ".marathon" / "PromptLog.md").read_text()
    assert text == "2026-06-20  aaaa\n2026-06-21  bbbb\n"


# --- end-to-end through post_pipeline.append_promptlog_url ------------------


def test_append_promptlog_url_writes_shard_not_shared_file(tmp_path):
    """The submit-time call writes a PromptLog SHARD (never mutates the
    shared PromptLog.md, which is the collision the redesign removes)."""
    repo = tmp_path
    (repo / ".marathon").mkdir()
    (repo / ".marathon" / "PromptLog.md").write_text("# header\n")
    before = (repo / ".marathon" / "PromptLog.md").read_text()

    assert pp.append_promptlog_url(repo, "proj-xyz") is True

    # Shared file untouched (no append → no collision).
    assert (repo / ".marathon" / "PromptLog.md").read_text() == before
    # A shard was written for the project.
    shards = list((repo / ".marathon" / "metrics").glob("*proj-xyz.json"))
    assert len(shards) == 1
    data = json.loads(shards[0].read_text())
    assert data["project_id"] == "proj-xyz"
    assert data["promptlog_line"].endswith("proj-xyz")


def test_append_promptlog_url_empty_project_is_noop(tmp_path):
    repo = tmp_path
    (repo / ".marathon").mkdir()
    assert pp.append_promptlog_url(repo, "") is False
    assert not (repo / ".marathon" / "metrics").exists()

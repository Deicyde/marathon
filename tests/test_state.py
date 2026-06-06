"""Unit tests for marathon.state (skeleton + refine persistent state)."""

import json
from datetime import datetime

from marathon.state import (
    ChapterState,
    RefineState,
    RunState,
    compute_duration_seconds,
    format_duration,
    load_refine_state,
    load_state,
    now_iso,
    save_refine_state,
    save_state,
)


# --- RunState / ChapterState ------------------------------------------------


def test_load_state_missing_returns_empty(tmp_path):
    state = load_state(tmp_path / "nope.json")
    assert isinstance(state, RunState)
    assert state.chapters == []


def test_chapter_round_trip(tmp_path):
    path = tmp_path / "marathon-state.json"
    original = RunState(
        chapters=[
            ChapterState(
                input_file="chap01.tex",
                output_folder="Chapter01",
                project_id="proj-1",
                agent_task_id="task-1",
                status="COMPLETE",
                attempts=2,
            ),
            ChapterState(input_file="chap02.tex", output_folder="Chapter02"),
        ]
    )
    save_state(path, original)
    loaded = load_state(path)
    assert loaded == original


def test_save_state_is_atomic_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "marathon-state.json"
    save_state(path, RunState(chapters=[ChapterState("c.tex", "C")]))
    assert path.is_file()
    assert not (tmp_path / "marathon-state.json.tmp").exists()
    # Overwriting an existing file works (tmp + replace).
    save_state(path, RunState(chapters=[ChapterState("d.tex", "D")]))
    assert load_state(path).chapters[0].input_file == "d.tex"


def test_load_state_drops_unknown_keys(tmp_path):
    path = tmp_path / "marathon-state.json"
    path.write_text(
        json.dumps(
            {"chapters": [{"input_file": "c.tex", "output_folder": "C", "future_field": 99}]}
        )
    )
    state = load_state(path)  # must not raise on the unknown key
    assert state.chapters == [ChapterState("c.tex", "C")]


def test_runstate_find():
    state = RunState(chapters=[ChapterState("a.tex", "A"), ChapterState("b.tex", "B")])
    assert state.find("b.tex").output_folder == "B"
    assert state.find("missing.tex") is None


# --- RefineState ------------------------------------------------------------


def test_load_refine_state_missing_returns_none(tmp_path):
    assert load_refine_state(tmp_path / "nope.json") is None


def test_refine_round_trip(tmp_path):
    path = tmp_path / "marathon-refine-state.json"
    original = RefineState(
        target_folder="GeometricAnalysis/Chapter03",
        iterations_completed=3,
        current_iteration_idx=3,
        project_id="proj-9",
        status="COMPLETE",
        attempts=1,
    )
    save_refine_state(path, original)
    assert load_refine_state(path) == original


def test_load_refine_state_drops_unknown_keys(tmp_path):
    path = tmp_path / "marathon-refine-state.json"
    path.write_text(json.dumps({"target_folder": "T", "iterations_completed": 1, "legacy": True}))
    loaded = load_refine_state(path)
    assert loaded == RefineState(target_folder="T", iterations_completed=1)


# --- duration helpers -------------------------------------------------------


def test_compute_duration_seconds_none_when_either_missing():
    assert compute_duration_seconds(None, "2026-01-01T00:01:00+00:00") is None
    assert compute_duration_seconds("2026-01-01T00:00:00+00:00", None) is None


def test_compute_duration_seconds_value():
    secs = compute_duration_seconds(
        "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:30+00:00"
    )
    assert secs == 90.0


def test_format_duration():
    assert format_duration(None) == "?"
    assert format_duration(30) == "30s"
    assert format_duration(90) == "1.5m"
    assert format_duration(3700) == "1h 1m"


def test_now_iso_is_parseable_and_tz_aware():
    parsed = datetime.fromisoformat(now_iso())
    assert parsed.tzinfo is not None

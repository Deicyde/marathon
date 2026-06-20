"""The deck DASHBOARD data layer (marathon.deck.dashboard) — read-only assembly
for the Status / Issues / History tabs. Every source is monkeypatched; no real
conductor, gh, or git.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import marathon.deck.dashboard as dash


def _cfg(tmp_path, chapters=None):
    return SimpleNamespace(
        repo_dir=tmp_path,
        github_repo="o/r",
        chapters=chapters or {},
    )


# --- conductor_status ------------------------------------------------------

def test_conductor_status_alive_with_jobs(tmp_path, monkeypatch):
    lock = tmp_path / "conductor.lock"
    lock.write_text("4242")
    monkeypatch.setattr("marathon.conductor.conductor_lock_path", lambda cfg: lock)
    monkeypatch.setattr(
        "marathon.conductor.load_jobs_snapshot",
        lambda repo_dir: {
            "updated_ts": "2026-06-19T01:00:00", "concurrency": 2,
            "jobs": [
                {"issue_num": 93, "chapter": 12, "status": "running", "pid": 4242,
                 "project_id": "p1", "aristotle_status": "EDITING_FILE"},
                {"issue_num": 91, "chapter": 12, "status": "succeeded", "pid": None},
            ],
        },
    )
    monkeypatch.setattr("marathon.review.daemon.process_alive", lambda pid: True)

    d = dash.conductor_status(_cfg(tmp_path))

    assert d["alive"] is True and d["pid"] == 4242
    assert d["concurrency"] == 2
    assert d["running"] == 1
    assert {j["issue_num"] for j in d["jobs"]} == {93, 91}
    assert d["landings"] == []  # no landings file


def test_conductor_status_dead_pid_marks_running_job(tmp_path, monkeypatch):
    lock = tmp_path / "conductor.lock"
    lock.write_text("999")
    monkeypatch.setattr("marathon.conductor.conductor_lock_path", lambda cfg: lock)
    monkeypatch.setattr(
        "marathon.conductor.load_jobs_snapshot",
        lambda repo_dir: {"jobs": [
            {"issue_num": 93, "chapter": 12, "status": "running", "pid": 999},
        ]},
    )
    monkeypatch.setattr("marathon.review.daemon.process_alive", lambda pid: False)

    d = dash.conductor_status(_cfg(tmp_path))

    assert d["alive"] is False                       # lock pid not alive
    assert d["jobs"][0]["status"] == "running (pid dead)"
    assert d["running"] == 0                          # the dead one is not counted


def test_conductor_status_no_snapshot_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr("marathon.conductor.conductor_lock_path",
                        lambda cfg: tmp_path / "absent.lock")
    monkeypatch.setattr("marathon.conductor.load_jobs_snapshot", lambda repo_dir: None)
    d = dash.conductor_status(_cfg(tmp_path))
    assert d == {"alive": False, "pid": None, "concurrency": None,
                 "updated_ts": None, "running": 0, "jobs": [], "queued": [],
                 "landings": []}


# --- issues_overview -------------------------------------------------------

def _stub_issue_sources(monkeypatch, *, meta, local, building_jobs, queue_cards):
    monkeypatch.setattr("marathon.review.github.fetch_issues_bulk",
                        lambda nums, repo: meta)
    monkeypatch.setattr("marathon.review.state.load_state",
                        lambda cfg: SimpleNamespace(
                            issues={n: SimpleNamespace(status=s) for n, s in local.items()}))
    monkeypatch.setattr(dash, "conductor_status",
                        lambda cfg: {"jobs": building_jobs})
    monkeypatch.setattr("marathon.deck.cards.build_queue",
                        lambda cfg, ch: SimpleNamespace(
                            cards=[SimpleNamespace(id=c["id"], to_json=(lambda c=c: c))
                                   for c in queue_cards]))


def test_issues_overview_status_precedence(tmp_path, monkeypatch):
    chapters = {12: SimpleNamespace(entries=[
        (93, "building one"), (94, "verified by label"),
        (95, "rejected locally"), (96, "ready in queue"),
        (97, "blocked in queue"), (98, "pending"),
    ])}
    _stub_issue_sources(
        monkeypatch,
        meta={
            94: {"title": "T94", "state": "OPEN", "labels": {"review:verified"}},
            96: {"title": "T96", "state": "OPEN", "labels": set()},
        },
        local={95: "rejected"},
        building_jobs=[{"issue_num": 93, "status": "running"}],
        queue_cards=[
            {"id": 96, "title": "T96", "tier": "T1", "ready": True, "blocked_reason": None},
            {"id": 97, "title": "T97", "tier": "T2", "ready": False,
             "blocked_reason": "waiting on #96"},
        ],
    )

    d = dash.issues_overview(_cfg(tmp_path, chapters))

    by_id = {it["id"]: it["status"] for ch in d["chapters"] for it in ch["issues"]}
    assert by_id == {
        93: "building", 94: "verified", 95: "rejected",
        96: "ready", 97: "blocked", 98: "pending",
    }
    assert d["total"] == 6
    assert d["counts"]["building"] == 1 and d["counts"]["verified"] == 1
    # title falls back: GitHub > queue card > registry substring
    by_title = {it["id"]: it["title"] for ch in d["chapters"] for it in ch["issues"]}
    assert by_title[94] == "T94"          # from GitHub meta
    assert by_title[98] == "pending"      # falls back to the registry substring


def test_issues_overview_grouped_by_chapter(tmp_path, monkeypatch):
    chapters = {
        11: SimpleNamespace(entries=[(80, "a")]),
        12: SimpleNamespace(entries=[(93, "b"), (94, "c")]),
    }
    _stub_issue_sources(monkeypatch, meta={}, local={}, building_jobs=[], queue_cards=[])
    d = dash.issues_overview(_cfg(tmp_path, chapters))
    assert [c["chapter"] for c in d["chapters"]] == [11, 12]
    assert [len(c["issues"]) for c in d["chapters"]] == [1, 2]


def test_issues_overview_degrades_when_github_down(tmp_path, monkeypatch):
    chapters = {12: SimpleNamespace(entries=[(93, "Prop 12.19")])}

    def boom(nums, repo):
        raise RuntimeError("gh offline")

    monkeypatch.setattr("marathon.review.github.fetch_issues_bulk", boom)
    monkeypatch.setattr("marathon.review.state.load_state",
                        lambda cfg: SimpleNamespace(issues={}))
    monkeypatch.setattr(dash, "conductor_status", lambda cfg: {"jobs": []})
    monkeypatch.setattr("marathon.deck.cards.build_queue",
                        lambda cfg, ch: SimpleNamespace(cards=[]))

    d = dash.issues_overview(_cfg(tmp_path, chapters))  # must not raise
    issue = d["chapters"][0]["issues"][0]
    assert issue["id"] == 93 and issue["status"] == "pending"
    assert issue["title"] == "Prop 12.19"  # registry-substring fallback


# --- history ---------------------------------------------------------------

def test_history_reads_verdicts_and_merged_prs(tmp_path, monkeypatch):
    vpath = tmp_path / ".marathon" / "review" / "verdicts.jsonl"
    vpath.parent.mkdir(parents=True, exist_ok=True)
    vpath.write_text(
        json.dumps({"issue_num": 93, "verdict": "rejected", "ts": "2026-06-18T02:00:00", "notes": "fix it"}) + "\n"
        + json.dumps({"issue_num": 91, "verdict": "verified", "ts": "2026-06-19T01:00:00"}) + "\n"
    )

    def fake_run(cmd, **kw):
        assert cmd[:3] == ["gh", "pr", "list"]
        prs = [{"number": 109, "title": "iter for #93", "mergedAt": "2026-06-19T03:00:00",
                "headRefName": "marathon/refine-c12-i93", "author": {"login": "bot"}}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(prs), stderr="")

    monkeypatch.setattr(dash.subprocess, "run", fake_run)

    d = dash.history(_cfg(tmp_path), limit=10)

    # verdicts newest-first
    assert [e["issue_num"] for e in d["verdict_events"]] == [91, 93]
    assert d["verdict_events"][1]["notes"] == "fix it"
    assert d["merged_prs"][0]["number"] == 109
    assert d["merged_prs"][0]["author"] == "bot"


def test_history_degrades_when_gh_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(dash.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="no gh"))
    d = dash.history(_cfg(tmp_path))      # no verdicts file, gh fails
    assert d == {"verdict_events": [], "merged_prs": []}


# --- run observability: progress %, iteration, ratings, prompts -------------

def _make_run_workdir(tmp_path):
    wd = tmp_path / "wd-i93"
    wd.mkdir()
    (wd / "conductor-refine.log").write_text(
        "starting\n    status: IN_PROGRESS (15% complete)\n"
        "    status: IN_PROGRESS (42% complete)\n"
    )
    (wd / "marathon-refine-state.json").write_text(json.dumps({
        "iterations_completed": 1, "current_iteration_idx": 2,
        "status": "IN_PROGRESS", "duration_seconds": 120.0,
        "agent_task_id": "task-abc",
    }))
    (wd / "marathon-ratings.jsonl").write_text(
        json.dumps({"quality": 4, "math_correctness": 5}) + "\n"
        + json.dumps({"quality": 5, "math_correctness": 5}) + "\n"
    )
    (wd / "marathon-refine-log.md").write_text(
        "## iteration 1\nADD the CovariantTensorField smul lemma.\n"
    )
    return wd


def test_conductor_status_adds_progress_and_iteration(tmp_path, monkeypatch):
    wd = _make_run_workdir(tmp_path)
    lock = tmp_path / "conductor.lock"; lock.write_text("4242")
    monkeypatch.setattr("marathon.conductor.conductor_lock_path", lambda cfg: lock)
    monkeypatch.setattr(
        "marathon.conductor.load_jobs_snapshot",
        lambda repo_dir: {"jobs": [
            {"issue_num": 93, "chapter": 12, "status": "running", "pid": 4242,
             "workdir": str(wd)},
        ]},
    )
    monkeypatch.setattr("marathon.review.daemon.process_alive", lambda pid: True)

    d = dash.conductor_status(_cfg(tmp_path))
    j = d["jobs"][0]
    assert j["progress_pct"] == 42                  # the LATEST % in the log
    assert j["iteration"]["current"] == 2
    assert j["iteration"]["completed"] == 1
    assert j["workdir"] == str(wd)


def test_run_detail_reads_ratings_and_prompts(tmp_path, monkeypatch):
    wd = _make_run_workdir(tmp_path)
    monkeypatch.setattr(
        "marathon.conductor.load_jobs_snapshot",
        lambda repo_dir: {"jobs": [
            {"issue_num": 93, "chapter": 12, "status": "running",
             "branch": "marathon/refine-c12-i93", "workdir": str(wd)},
        ]},
    )
    d = dash.run_detail(_cfg(tmp_path), 93)
    assert d["found"] is True
    assert d["progress_pct"] == 42
    assert d["iteration"]["current"] == 2
    assert len(d["ratings"]) == 2 and d["ratings"][-1]["quality"] == 5
    assert "CovariantTensorField" in d["prompts"]
    assert d["branch"] == "marathon/refine-c12-i93"


def test_run_detail_not_found_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "marathon.conductor.load_jobs_snapshot",
        lambda repo_dir: {"jobs": []},
    )
    d = dash.run_detail(_cfg(tmp_path), 999)
    assert d["found"] is False
    assert d["ratings"] == [] and d["prompts"] == ""


def test_progress_pct_and_iteration_degrade_without_workdir():
    assert dash._refine_progress_pct(None) is None
    assert dash._refine_iteration(None) is None
    assert dash._refine_ratings(None) == []
    assert dash._refine_prompts(None) == ""


def test_conductor_status_shows_queued_pending_rejections(tmp_path, monkeypatch):
    """A rejection waiting behind a running same-chapter job appears in `queued`
    with the reason — the 'I shipped a reject, where is it?' gap."""
    from types import SimpleNamespace
    lock = tmp_path / "conductor.lock"; lock.write_text("4242")
    monkeypatch.setattr("marathon.conductor.conductor_lock_path", lambda cfg: lock)
    monkeypatch.setattr(
        "marathon.conductor.load_jobs_snapshot",
        lambda repo_dir: {"concurrency": 1, "jobs": [
            {"issue_num": 95, "chapter": 12, "status": "running", "pid": 4242}]},
    )
    monkeypatch.setattr("marathon.review.daemon.process_alive", lambda pid: True)
    monkeypatch.setattr(
        "marathon.review.state.pending_rejections_needing_iteration",
        lambda cfg, ch=None: [(95, SimpleNamespace(verdict_ts="t1")),
                              (98, SimpleNamespace(verdict_ts="t2"))],
    )
    cfg = SimpleNamespace(repo_dir=tmp_path, github_repo="o/r", chapters={},
                          chapter_of_issue=lambda n: 12)
    d = dash.conductor_status(cfg)
    q = d["queued"]
    assert [it["issue_num"] for it in q] == [98]   # #95 is the running job
    assert "chapter 12 is busy" in q[0]["reason"]
    assert q[0]["chapter"] == 12


def test_pending_queue_concurrency_and_cycle_reasons(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(
        "marathon.review.state.pending_rejections_needing_iteration",
        lambda cfg, ch=None: [(40, SimpleNamespace(verdict_ts="t"))],
    )
    cfg = SimpleNamespace(repo_dir=tmp_path, chapter_of_issue=lambda n: 99)
    # a running job in a DIFFERENT chapter, concurrency 1 → at the limit
    q = dash._pending_queue(cfg, [{"issue_num": 7, "chapter": 11, "status": "running"}], 1)
    assert "at concurrency limit" in q[0]["reason"]
    # no running jobs → dispatches next cycle
    q2 = dash._pending_queue(cfg, [], 1)
    assert "next cycle" in q2[0]["reason"]

"""Tests for the Phase-3 repo-level Conductor (marathon.conductor).

Contract under test (docs/marathon-v2-plan.md §2 ruling 2, §3 Phase 3):

* scheduling is deterministic Python — oldest verdict first ACROSS all
  chapters, concurrency-capped, same-chapter-folder collisions deferred
  (younger waits);
* each job runs in its own git worktree under a parent outside the
  repo, on the per-issue branch (double-dispatch guard via git's
  same-branch refusal — deferred, never crashed);
* failure handling is the Phase-0 retry/stall state machine REUSED
  (record_failed_attempt → backoff requeue; record_stall + exactly one
  gh notification; interrupted jobs record nothing);
* one conductor per repo (PID lock); jobs.json snapshot round-trips so
  `marathon conductor status` works without the daemon;
* orphan reconciliation is report-only.

Every subprocess / network / git boundary is monkeypatched: no test
invokes Aristotle, Claude, gh, or real git worktree commands.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

import marathon.conductor as conductor
import marathon.review.daemon as daemon
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels
from marathon.review.state import (
    load_state,
    pending_rejections_needing_iteration,
    state_path,
)

REPO_SLUG = "example/Demo"

# Registry: issues 22 + 23 share Chapter 14 (collision pair); 31 and 41
# live in their own chapters.
CHAPTERS = {
    14: [(22, "Lemma A"), (23, "Lemma B")],
    15: [(31, "Theorem C")],
    16: [(41, "Theorem D")],
}


def _conductor_lock_racer(cfg, start, release, results):
    """Spawn-safe helper for the cross-process singleton regression."""
    start.wait(10)
    acquired = conductor.acquire_conductor_lock(cfg)
    results.put(acquired)
    if acquired:
        release.wait(10)
        conductor.release_conductor_lock(cfg)


def make_cfg(tmp_path) -> ReviewConfig:
    """Minimal in-memory ReviewConfig rooted at tmp_path/repo (the repo
    must be a strict subdir so a worktree parent beside it counts as
    outside the repo)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo=REPO_SLUG,
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            chap: ChapterRegistry(chapter=chap, entries=list(entries))
            for chap, entries in CHAPTERS.items()
        },
    )


def seed_rejections(cfg: ReviewConfig, verdicts: dict[int, str]) -> None:
    """Write state.json directly (issue -> verdict_ts) for explicit
    cross-chapter ordering control — record_rejection() stamps now()
    with second precision, which ties under test speed."""
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issues": {
                    str(num): {
                        "status": "rejected",
                        "verdict_ts": ts,
                        "notes": f"fix #{num}",
                    }
                    for num, ts in verdicts.items()
                },
            }
        )
    )


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture(autouse=True)
def _restore_signals_and_stop_flag(monkeypatch):
    """run_conductor installs SIGTERM/SIGINT handlers and the stop flag
    is module-global; keep both from leaking across tests."""
    monkeypatch.setattr(conductor, "_STOP_REQUESTED", False)
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGINT, old_int)


class Clock:
    """Fake wall clock: _now() reads it, _sleep() advances it, so backoff
    not-before bookkeeping runs instantly and deterministically."""

    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds, **kwargs) -> None:
        self.t += max(float(seconds), 1.0)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(conductor, "_now", c.now)
    monkeypatch.setattr(conductor, "_sleep", c.sleep)
    return c


@pytest.fixture
def subprocess_calls(monkeypatch):
    """Record every subprocess.run invocation (conductor's git worktree
    calls AND the daemon's gh stall notifications — same stdlib module)
    and succeed each one."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(conductor.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def popen_factory(monkeypatch):
    """Fake subprocess.Popen for the refine jobs. Behavior is scripted
    per issue: {issue: {"rc": int, "polls": int}} — poll() returns None
    for polls-1 ticks then the rc. Tracks spawn order and the
    high-water mark of simultaneously-running fakes (the concurrency
    cap's observable)."""
    state = SimpleNamespace(spawned=[], script={}, active=0, high_water=0)

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.launch_cmd = list(cmd)
            split = self.launch_cmd.index("--")
            self.cmd = self.launch_cmd[split + 1:]
            self.kwargs = dict(kwargs)

            def _arg(flag):
                return (self.cmd[self.cmd.index(flag) + 1]
                        if flag in self.cmd else None)

            # refine → --review-rejection, fill → --issue, repair → neither.
            raw = _arg("--review-rejection") or _arg("--issue")
            self.issue = int(raw) if raw else 0
            worktree = _arg("--repo-dir")
            marker = (
                __import__("pathlib").Path(worktree) / conductor.WORKTREE_PID_MARKER
            )
            # The launch intent must be durable before the detached process is
            # created; otherwise a SIGKILL here leaves an invisible worker.
            self.launch_marker = json.loads(marker.read_text())
            self.pid = 4000 + len(state.spawned)
            ready_marker = dict(self.launch_marker)
            ready_marker.update(pid=self.pid, pgid=self.pid, status="ready")
            marker.write_text(json.dumps(ready_marker))
            conf = state.script.get(self.issue, {})
            self._polls_left = conf.get("polls", 1)
            self._rc = conf.get("rc", 0)
            self.returncode = None
            state.spawned.append(self)
            state.active += 1
            state.high_water = max(state.high_water, state.active)

        def poll(self):
            if self.returncode is not None:
                return self.returncode
            self._polls_left -= 1
            if self._polls_left <= 0:
                self.returncode = self._rc
                state.active -= 1
                return self.returncode
            return None

    monkeypatch.setattr(conductor.subprocess, "Popen", FakePopen)
    return state


@pytest.fixture
def e2e(cfg, clock, subprocess_calls, popen_factory, monkeypatch, tmp_path):
    """Everything a run_conductor end-to-end test needs: fake config
    loading, no orphan API calls, fake clock, recorded subprocesses."""
    monkeypatch.setattr(conductor, "load_config", lambda repo_dir=None: cfg)
    monkeypatch.setattr(conductor, "_report_orphans", lambda cfg_, ids: None)
    # Scheduling tests are isolated from the Mathlib-cache prime (it spawns its
    # own `lake exe cache get`, which the fake Popen would otherwise record);
    # _prime_mathlib_cache is exercised directly in test_conductor_cache.py.
    monkeypatch.setenv("MARATHON_SKIP_CACHE_GET", "1")
    # …and from the tracker safety-net (its own gh read), tested in
    # test_review_plumbing.py — so gh-call-count assertions stay exact.
    monkeypatch.setattr(
        "marathon.review.tracker.sync_tracker_to_verified",
        lambda cfg, **kw: (False, "skipped in tests"),
    )
    return SimpleNamespace(
        cfg=cfg,
        clock=clock,
        git=subprocess_calls,
        popen=popen_factory,
        runs_parent=tmp_path / "runs",
    )


def run_once(e2e, concurrency=1, max_attempts=3, prune=False, automerge=False,
             reconcile=False):
    # automerge AND reconcile default OFF here so the scheduling/dispatch tests
    # are isolated from the gh-pr-list features (the merge orchestrator and the
    # merged-but-rejected re-review reconcile); their dedicated tests opt in.
    return conductor.run_conductor(
        repo_dir=e2e.cfg.repo_dir,
        concurrency=concurrency,
        once=True,
        prune=prune,
        max_attempts=max_attempts,
        worktree_parent=e2e.runs_parent,
        automerge=automerge,
        reconcile=reconcile,
    )


# --- scheduler decision (unit) -------------------------------------------------


def test_pick_oldest_across_chapters(cfg):
    """#31 (chapter 15) has the older verdict; it must outrank #22
    (chapter 14) even though 14 < 15 — ordering is by verdict age,
    never by chapter number."""
    seed_rejections(cfg, {22: "2026-06-10T12:00:00-04:00",
                          31: "2026-06-09T08:00:00-04:00"})
    picks = conductor._pick_dispatchable(cfg, [], {}, now=0.0, slots=2)
    assert picks == [(31, 15), (22, 14)]
    # Concurrency cap respected at the picker level too.
    assert conductor._pick_dispatchable(cfg, [], {}, now=0.0, slots=1) == [(31, 15)]


def test_pick_respects_backoff_not_before(cfg):
    seed_rejections(cfg, {22: "2026-06-10T12:00:00-04:00",
                          31: "2026-06-09T08:00:00-04:00"})
    picks = conductor._pick_dispatchable(
        cfg, [], {31: 100.0}, now=50.0, slots=2
    )
    assert picks == [(22, 14)]  # 31 deferred by its backoff window


def test_pick_defers_same_chapter_collision(cfg):
    """Two rejections in chapter 14 never co-run: the younger (#23)
    defers both against a running job and within a single pick batch.
    (Chapter-folder granularity is the documented Phase-3 collision
    unit; decl-level overlap arrives with the Phase-5 audit engine.)"""
    seed_rejections(cfg, {22: "2026-06-09T08:00:00-04:00",
                          23: "2026-06-10T09:00:00-04:00",
                          31: "2026-06-11T10:00:00-04:00"})
    # Batch case: 22 and 23 share Demo/Chapter14 — only the older plus
    # the other chapter's issue are picked.
    picks = conductor._pick_dispatchable(cfg, [], {}, now=0.0, slots=3)
    assert picks == [(22, 14), (31, 15)]

    # Running-job case: a live job on chapter 14 blocks #23 (and #22,
    # which is also the same issue as the running job).
    running = conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree="/x/wt", workdir="/x/wd", branch="marathon/refine-c14-i22",
        status="running",
    )
    picks = conductor._pick_dispatchable(cfg, [running], {}, now=0.0, slots=3)
    assert picks == [(31, 15)]


# --- end-to-end: dispatch, worktrees, concurrency -------------------------------


def test_worktree_add_remove_and_refine_args(e2e):
    """Success path: worktree added on the per-issue branch from
    origin/main, refine dispatched with the daemon's exact flag set
    (imported, not forked), worktree removed after the clean exit, and
    the iteration recorded (Phase-0 clean-exit contract)."""
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    e2e.popen.script = {22: {"rc": 0, "polls": 1}}

    assert run_once(e2e) == 0

    adds = [c for c in e2e.git if c[3:5] == ["worktree", "add"]]
    assert len(adds) == 1
    add = adds[0]
    assert add[:3] == ["git", "-C", str(e2e.cfg.repo_dir)]
    assert add[5] == "-B"
    assert add[6] == "marathon/refine-c14-i22"  # post_pipeline's exact name
    wt_path = add[7]
    assert wt_path.startswith(str(e2e.runs_parent / "wt-i22-"))
    assert add[8] == "origin/main"

    removes = [c for c in e2e.git if c[3:5] == ["worktree", "remove"]]
    assert removes == [
        ["git", "-C", str(e2e.cfg.repo_dir), "worktree", "remove", "--force", wt_path]
    ]

    [proc] = e2e.popen.spawned
    cmd = proc.cmd
    assert proc.launch_marker["status"] == "launching"
    assert proc.launch_marker["pid"] is None
    assert proc.launch_cmd[1:3] == ["-m", "marathon.worker_launch"]
    assert proc.kwargs["stdin"] == subprocess.DEVNULL
    if os.name == "nt":
        assert proc.kwargs["creationflags"]
    else:
        assert proc.kwargs["start_new_session"] is True
    assert cmd[:4] == [sys.executable, "-m", "marathon", "refine"]
    assert cmd[4].endswith("Demo/Chapter14")
    assert cmd[4].startswith(wt_path)  # target lives inside the job worktree
    assert cmd[cmd.index("--repo-dir") + 1] == wt_path
    workdir = cmd[cmd.index("--workdir") + 1]
    assert workdir.startswith(str(e2e.runs_parent / "wd-i22-"))
    assert cmd[cmd.index("--review-rejection") + 1] == "22"
    # The per-issue flag set is the daemon's, verbatim.
    assert cmd[-len(daemon.DEFAULT_REFINE_ARGS):] == daemon.DEFAULT_REFINE_ARGS

    entry = load_state(e2e.cfg).issues[22]
    assert entry.last_iteration_ts is not None  # record_iteration ran
    assert entry.attempts == 0


def test_dispatch_primes_worktree_and_proceeds_when_prime_fails(e2e, monkeypatch):
    """The fresh worktree is primed (`_prime_mathlib_cache`) before the refine
    spawns — and a prime FAILURE never aborts the dispatch (the refine is still
    spawned; it just does a normal, slower build)."""
    primed: list[str] = []

    def fake_prime(worktree, **kwargs):
        primed.append(str(worktree))
        return False  # simulate a cache-get failure

    monkeypatch.setattr(conductor, "_prime_mathlib_cache", fake_prime)
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    e2e.popen.script = {22: {"rc": 0, "polls": 1}}

    assert run_once(e2e) == 0

    # The job worktree was primed…
    add = [c for c in e2e.git if c[3:5] == ["worktree", "add"]][0]
    wt_path = add[7]
    assert wt_path in primed
    # …and the dispatch proceeded despite the prime returning False.
    [proc] = e2e.popen.spawned
    assert proc.cmd[proc.cmd.index("--review-rejection") + 1] == "22"


def test_startup_warms_main_checkout_before_dispatch(e2e, monkeypatch):
    """The startup warm primes the MAIN checkout (cfg.repo_dir) before any
    worktree is primed/dispatched, so the shared store is warm first."""
    primed: list[str] = []
    monkeypatch.setattr(conductor, "_prime_mathlib_cache",
                        lambda w, **k: primed.append(str(w)) or False)
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    e2e.popen.script = {22: {"rc": 0, "polls": 1}}

    run_once(e2e)

    # The main checkout (tmp repo, not built) is warmed first…
    assert primed, "startup warm should fire on an unbuilt main checkout"
    assert primed[0] == str(e2e.cfg.repo_dir)
    # …and the worktree prime comes after it.
    assert any(p != str(e2e.cfg.repo_dir) for p in primed[1:])


def test_concurrency_cap_respected(e2e):
    """Three rejections in three chapters, concurrency 2: at most two
    fakes ever run simultaneously, all three run eventually, oldest
    verdicts dispatched first."""
    seed_rejections(e2e.cfg, {22: "2026-06-09T08:00:00-04:00",
                              31: "2026-06-09T09:00:00-04:00",
                              41: "2026-06-09T10:00:00-04:00"})
    e2e.popen.script = {n: {"rc": 0, "polls": 2} for n in (22, 31, 41)}

    assert run_once(e2e, concurrency=2) == 0

    assert [p.issue for p in e2e.popen.spawned] == [22, 31, 41]
    assert e2e.popen.high_water == 2


def test_same_chapter_jobs_serialize(e2e):
    """#22 and #23 both target Demo/Chapter14: even at concurrency 2
    they run one after the other (collision defers the younger)."""
    seed_rejections(e2e.cfg, {22: "2026-06-09T08:00:00-04:00",
                              23: "2026-06-09T09:00:00-04:00"})
    e2e.popen.script = {22: {"rc": 0, "polls": 1}, 23: {"rc": 0, "polls": 1}}

    assert run_once(e2e, concurrency=2) == 0

    assert [p.issue for p in e2e.popen.spawned] == [22, 23]
    assert e2e.popen.high_water == 1


# --- failure handling: the Phase-0 state machine, reused ------------------------


def test_failed_job_flows_through_record_failed_attempt(e2e, monkeypatch):
    """Non-zero exits must route through the daemon's state machine:
    record_failed_attempt per failure, backoff requeue (the not-before
    window — observed via the fake clock), stall at the budget."""
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    e2e.popen.script = {22: {"rc": 1, "polls": 1}}

    calls: list[int] = []
    orig = daemon.record_failed_attempt

    def spy(cfg_, issue_num, **kwargs):
        calls.append(issue_num)
        return orig(cfg_, issue_num, **kwargs)

    # _handle_refine_exit lives in the daemon module and resolves
    # record_failed_attempt from its own namespace — patch it there.
    monkeypatch.setattr(daemon, "record_failed_attempt", spy)

    assert run_once(e2e, max_attempts=2) == 0

    assert calls == [22, 22]  # one per failed dispatch, none after stall
    assert len(e2e.popen.spawned) == 2  # backoff requeue re-dispatched it
    # The retry continues the SAME Aristotle checkpoint/log.  Before this fix
    # only the worktree was reused and a fresh wd-i... forced a new paid session.
    retry_workdirs = [
        p.cmd[p.cmd.index("--workdir") + 1] for p in e2e.popen.spawned
    ]
    assert retry_workdirs[0] == retry_workdirs[1]
    # The retry waited out the Phase-0 exponential backoff (2^1 * 60s).
    assert e2e.clock.t >= 2 * daemon.BACKOFF_BASE_SECONDS
    entry = load_state(e2e.cfg).issues[22]
    assert entry.status == "stalled"
    assert entry.attempts == 2


def test_recovers_failed_retry_checkpoint_across_conductor_restart(
    cfg, tmp_path, monkeypatch,
):
    """A dead job preserved in jobs.json is paired with its workdir on the next
    conductor launch, provided the rejection still needs an iteration."""
    runs = tmp_path / "runs"
    wt = runs / "wt-i22-20260710-120000"
    wd = runs / "wd-i22-20260710-120000"
    wt.mkdir(parents=True)
    wd.mkdir(parents=True)
    seed_rejections(cfg, {22: "2026-07-10T12:00:00-04:00"})
    conductor.write_jobs_snapshot(cfg.repo_dir, [conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree=str(wt), workdir=str(wd),
        branch="marathon/refine-c14-i22", pid=999999, status="failed",
        exit_code=13,
    )], concurrency=1)
    monkeypatch.setattr(conductor, "_worktree_live_pid", lambda _p: None)

    got = conductor._recover_retry_worktrees(cfg, runs, busy_issues=set())
    assert got == {22: str(wt)}
    assert conductor._paired_retry_workdir(runs, 22, wt) == wd


def test_recovers_gate_blocked_fill_checkpoint_across_restart(
    cfg, tmp_path, monkeypatch,
):
    from marathon.refine import REFINE_STATE_FILENAME
    from marathon.state import RefineState, save_refine_state

    runs = tmp_path / "runs"
    wt = runs / "wt-i22-20260710-120000"
    wd = runs / "wd-i22-20260710-120000"
    wt.mkdir(parents=True)
    wd.mkdir(parents=True)
    save_refine_state(
        wd / REFINE_STATE_FILENAME,
        RefineState(
            target_folder="Demo/Chapter14",
            force_continuation=True,
            status="POST_PIPELINE_PR_BLOCKED",
        ),
    )
    conductor.write_jobs_snapshot(cfg.repo_dir, [conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree=str(wt), workdir=str(wd),
        branch="marathon/proof-c14-i22", pid=999999, status="failed",
        exit_code=13, fill=True,
    )], concurrency=1)
    monkeypatch.setattr(conductor, "_worktree_live_pid", lambda _p: None)

    got = conductor._recover_retry_worktrees(cfg, runs, busy_issues=set())
    assert got == {conductor.FILL_KEY_BASE + 22: str(wt)}


def test_recovers_stalled_refine_checkpoint_for_later_human_requeue(
    cfg, tmp_path, monkeypatch,
):
    from marathon.review.state import IssueState, ReviewState, save_state

    runs = tmp_path / "runs"
    wt = runs / "wt-i22-20260710-120000"
    wd = runs / "wd-i22-20260710-120000"
    wt.mkdir(parents=True)
    wd.mkdir(parents=True)
    save_state(
        cfg,
        ReviewState(
            issues={
                22: IssueState(
                    status="stalled",
                    verdict_ts="2026-07-10T12:00:00+00:00",
                    notes="keep trying",
                    attempts=3,
                )
            }
        ),
    )
    conductor.write_jobs_snapshot(cfg.repo_dir, [conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree=str(wt), workdir=str(wd),
        branch="marathon/refine-c14-i22", pid=999999, status="stalled",
        exit_code=13,
    )], concurrency=1)
    monkeypatch.setattr(conductor, "_worktree_live_pid", lambda _p: None)

    got = conductor._recover_retry_worktrees(cfg, runs, busy_issues=set())
    assert got == {22: str(wt)}


def test_stall_notifies_once_and_keeps_worktree(e2e):
    """At the retry budget the entry stalls, exactly ONE gh comment is
    posted (via the daemon's _notify_stall), the queue drains, and the
    failed job's worktree is kept for debugging."""
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    e2e.popen.script = {22: {"rc": 1, "polls": 1}}

    assert run_once(e2e, max_attempts=1) == 0

    gh_calls = [c for c in e2e.git if c[0] == "gh"]
    assert len(gh_calls) == 1
    assert gh_calls[0][:4] == ["gh", "issue", "comment", "22"]
    assert REPO_SLUG == gh_calls[0][gh_calls[0].index("--repo") + 1]

    assert load_state(e2e.cfg).issues[22].status == "stalled"
    assert pending_rejections_needing_iteration(e2e.cfg, None) == []
    # Worktree kept on failure: no `worktree remove` for this job.
    assert [c for c in e2e.git if c[3:5] == ["worktree", "remove"]] == []

    # A second pass dispatches nothing and posts nothing more.
    assert run_once(e2e, max_attempts=1) == 0
    assert len(e2e.popen.spawned) == 1
    assert len([c for c in e2e.git if c[0] == "gh"]) == 1


def test_interrupted_job_records_nothing(cfg, subprocess_calls, monkeypatch):
    """Stop-signal + non-zero exit = the kill, not a refine failure
    (daemon semantics): no iteration, no attempt, entry still queued
    for the next conductor launch."""
    seed_rejections(cfg, {22: "2026-06-10T12:00:00-04:00"})
    monkeypatch.setattr(conductor, "_STOP_REQUESTED", True)

    job = conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree=str(cfg.repo_dir.parent / "runs" / "wt-i22-x"),
        workdir=str(cfg.repo_dir.parent / "runs" / "wd-i22-x"),
        branch="marathon/refine-c14-i22", pid=4001, status="running",
        proc=SimpleNamespace(poll=lambda: 143),
    )
    reaped = conductor._reap_finished(cfg, [job], {}, {}, max_attempts=3)

    assert reaped == 1
    assert job.status == "interrupted"
    entry = load_state(cfg).issues[22]
    assert entry.attempts == 0
    assert entry.last_iteration_ts is None
    assert entry.needs_iteration()  # next launch re-dispatches
    assert subprocess_calls == []  # no gh, no worktree removal


# --- fill (prove) reap: never touches the verified issue's state -----------------


def _seed_verified(cfg, issue: int, ts: str = "2026-06-27T04:05:16-04:00") -> None:
    """Write state.json with a single VERIFIED issue (the prover only ever
    fills verified issues)."""
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "issues": {str(issue): {"status": "verified", "verdict_ts": ts}},
    }))


def test_fill_failure_never_mutates_verified_state(cfg, subprocess_calls, monkeypatch):
    """A FILL job that exits non-zero must NOT run the refine exit ladder:
    the verified issue's state is byte-identical, the worktree is kept, and
    it backs off — no record_failed_attempt / record_stall / gh call."""
    _seed_verified(cfg, 22)
    before = state_path(cfg).read_text()
    monkeypatch.setattr(conductor, "_now", lambda: 1000.0)
    job = conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree=str(cfg.repo_dir.parent / "runs" / "wt-i22-x"),
        workdir=str(cfg.repo_dir.parent / "runs" / "wd-i22-x"),
        branch="marathon/refine-c14-i22", pid=4001, status="running",
        fill=True, proc=SimpleNamespace(poll=lambda: 1),
    )
    not_before: dict[int, float] = {}
    kept: dict[int, str] = {}
    reaped = conductor._reap_finished(cfg, [job], not_before, kept, max_attempts=3)

    assert reaped == 1
    assert job.status == "failed"
    assert state_path(cfg).read_text() == before  # verified state untouched
    assert not_before[22] == 1000.0 + conductor.FILL_RETRY_BACKOFF_SECONDS
    assert kept[conductor.FILL_KEY_BASE + 22] == job.worktree
    assert subprocess_calls == []                 # no gh notification, no removal


def test_fill_reattach_transport_retry_does_not_consume_dispatch_budget(cfg):
    from marathon.refine import EXIT_REATTACH_TRANSIENT

    job = conductor.ConductorJob(
        issue_num=22,
        chapter=14,
        target="Demo/Chapter14",
        worktree=str(cfg.repo_dir.parent / "runs" / "wt-i22-x"),
        workdir=str(cfg.repo_dir.parent / "runs" / "wd-i22-x"),
        branch="marathon/proof-c14-i22",
        status="running",
        fill=True,
        proc=SimpleNamespace(poll=lambda: EXIT_REATTACH_TRANSIENT),
    )
    attempts = {22: 1}
    conductor._reap_finished(
        cfg,
        [job],
        {},
        {},
        max_attempts=3,
        fill_attempts=attempts,
    )
    assert attempts[22] == 0
    assert job.status == "failed"


def test_fill_success_no_requeue_no_iteration(cfg, subprocess_calls, monkeypatch):
    """A clean FILL job marks succeeded and lets automerge land the proof PR,
    but never requeues the verified issue for re-review or records an
    iteration against it (both are refine-only)."""
    _seed_verified(cfg, 22)
    before = state_path(cfg).read_text()
    monkeypatch.setattr(conductor, "_regenerate_metadata_after_success", lambda c: None)
    requeued: list[int] = []
    monkeypatch.setattr(
        conductor, "_requeue_if_no_pr_landed",
        lambda c, job: requeued.append(job.issue_num),
    )
    job = conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree=str(cfg.repo_dir.parent / "runs" / "wt-i22-x"),
        workdir=str(cfg.repo_dir.parent / "runs" / "wd-i22-x"),
        branch="marathon/refine-c14-i22", pid=4001, status="running",
        fill=True, proc=SimpleNamespace(poll=lambda: 0),
    )
    reaped = conductor._reap_finished(cfg, [job], {}, {}, max_attempts=3, reconcile=True)

    assert reaped == 1
    assert job.status == "succeeded"
    assert state_path(cfg).read_text() == before  # verified state untouched
    assert requeued == []                         # NOT bounced back to review


def test_prove_frontier_gates_on_skeleton_done(e2e, monkeypatch):
    """--prove-frontier makes every registered chapter a proving candidate, but
    the skeleton-done gate blocks all of them when nothing is verified — so a
    frontier run over an all-unverified project dispatches no fills and doesn't
    crash on the extra chapters."""
    monkeypatch.setattr(
        "marathon.review.verified_decls.verified_declarations",
        lambda cfg, chapter: {},  # nothing verified → no chapter is skeleton-done
    )
    rc = conductor.run_conductor(
        repo_dir=e2e.cfg.repo_dir, concurrency=1, once=True, prune=False,
        max_attempts=3, worktree_parent=e2e.runs_parent,
        automerge=False, reconcile=False, prove_frontier=True,
    )
    assert rc == 0
    assert e2e.popen.spawned == []  # no fills (and no refines) dispatched


def test_fill_is_gated_off_while_a_refine_is_pending(e2e, monkeypatch):
    """Proving is strictly lower priority than the human review/refine loop:
    with a rejection to refine, no fill dispatches this tick — the fill block
    is short-circuited before discovery even runs."""
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    monkeypatch.setattr(
        "marathon.review.verified_decls.verified_declarations",
        lambda cfg, chapter: {},
    )
    # If the priority gate ever leaked, discovery would run — make it explode
    # so the test fails loudly rather than silently dispatching a fill.
    def _boom(*a, **k):
        raise AssertionError("fill discovery ran while a refine was pending")

    monkeypatch.setattr(
        "marathon.review.fill_discovery.discover_fill_targets", _boom
    )
    rc = conductor.run_conductor(
        repo_dir=e2e.cfg.repo_dir, concurrency=1, once=True, prune=False,
        max_attempts=3, worktree_parent=e2e.runs_parent,
        automerge=False, reconcile=False, fill_chapters={14},
    )
    assert rc == 0
    # Exactly the refine ran — a refine cmd (`--review-rejection`), never a fill.
    assert len(e2e.popen.spawned) == 1
    assert "--review-rejection" in e2e.popen.spawned[0].cmd


def test_repair_never_preempts_a_pending_refine(e2e, monkeypatch):
    """Repair is idle-priority: a pending refine is dispatched BEFORE any repair
    — the human review loop is never preempted by an autonomous repair."""
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    monkeypatch.setattr(
        "marathon.repair.broken_modules",
        lambda cfg, ch: ["Demo.Chapter14.Broken"] if ch == 14 else [],
    )
    monkeypatch.setattr(conductor, "_regenerate_metadata_after_success", lambda c: None)
    rc = conductor.run_conductor(
        repo_dir=e2e.cfg.repo_dir, concurrency=1, once=True, prune=False,
        max_attempts=3, worktree_parent=e2e.runs_parent,
        automerge=False, reconcile=False, repair_chapters={14},
    )
    assert rc == 0
    cmds = [s.cmd for s in e2e.popen.spawned]
    first_refine = next(i for i, c in enumerate(cmds) if "--review-rejection" in c)
    repair_idxs = [i for i, c in enumerate(cmds) if "repair" in c]
    assert repair_idxs, "a repair should run once the refine queue drains"
    assert first_refine < repair_idxs[0]  # refine dispatched before any repair


def test_repair_dispatches_a_broken_chapter_when_idle(e2e, monkeypatch):
    """With the refine queue idle and a chapter carrying a broken module, the
    conductor dispatches `marathon repair` with the broken module(s) → main
    (only the broken chapter; 15/16 build, so they are skipped)."""
    monkeypatch.setattr(
        "marathon.repair.broken_modules",
        lambda cfg, ch: ["Demo.Chapter14.Broken"] if ch == 14 else [],
    )
    monkeypatch.setattr(conductor, "_regenerate_metadata_after_success", lambda c: None)
    rc = conductor.run_conductor(
        repo_dir=e2e.cfg.repo_dir, concurrency=1, once=True, prune=False,
        max_attempts=3, worktree_parent=e2e.runs_parent,
        automerge=False, reconcile=False, repair_chapters={14, 15, 16},
    )
    assert rc == 0
    repair_cmds = [
        s.cmd for s in e2e.popen.spawned
        if "repair" in s.cmd and "--module" in s.cmd
    ]
    assert repair_cmds, "the broken chapter 14 should be repaired"
    for cmd in repair_cmds:
        assert "Demo.Chapter14.Broken" in cmd          # only the broken chapter
        assert "--review-rejection" not in cmd and "--issue" not in cmd


# --- worktree guard rails --------------------------------------------------------


def test_branch_conflict_defers_not_crash(cfg, monkeypatch, tmp_path, capsys):
    """git's refusal to check out the per-issue branch in a second worktree: the
    conductor first reaps the (possibly orphaned) holder + retries; when that
    still fails (this mock fails every git call), it DEFERS — never raises."""
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=128, stdout="",
            stderr="fatal: 'marathon/refine-c14-i22' is already used by "
                   "worktree at '/elsewhere/wt'",
        )

    monkeypatch.setattr(conductor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        conductor,
        "_refine_branch_for_dispatch",
        lambda *a, **k: "marathon/refine-c14-i22",
    )
    monkeypatch.setattr(
        conductor.subprocess, "Popen",
        lambda *a, **kw: pytest.fail("must not spawn refine on a deferred dispatch"),
    )

    job = conductor._dispatch_job(cfg, 22, 14, tmp_path / "runs", {})
    assert job is None
    out = capsys.readouterr().out
    assert "reaping it and retrying" in out      # tries to clear the leftover first
    assert "deferring #22" in out                # then defers when it still can't


def test_dispatch_refuses_spawn_without_durable_launch_intent(
    cfg, monkeypatch, tmp_path, capsys,
):
    """A detached worker is never started if its recovery record cannot persist."""
    monkeypatch.setattr(
        conductor,
        "_refine_branch_for_dispatch",
        lambda *a, **k: "marathon/refine-c14-i22",
    )
    monkeypatch.setattr(
        conductor,
        "_git",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(conductor, "_prime_mathlib_cache", lambda *_: None)
    monkeypatch.setattr(conductor, "_write_worktree_pid_marker", lambda *a, **k: False)
    monkeypatch.setattr(
        conductor.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("must not spawn an unjournaled worker"),
    )

    kept = {}
    job = conductor._dispatch_job(cfg, 22, 14, tmp_path / "runs", kept)

    assert job is None
    assert 22 in kept
    assert "refusing to spawn" in capsys.readouterr().out


def test_worktree_parent_inside_repo_refused(cfg, monkeypatch):
    """Worktrees inside the repo leak into Aristotle bundles via
    `git ls-files --others` — refused at startup."""
    monkeypatch.setattr(conductor, "load_config", lambda repo_dir=None: cfg)
    with pytest.raises(SystemExit) as exc:
        conductor.run_conductor(
            repo_dir=cfg.repo_dir,
            worktree_parent=cfg.repo_dir / "inside",
        )
    assert "inside the repo" in str(exc.value)


def test_unsafe_startup_cache_prime_degrades_dispatch(e2e, monkeypatch):
    monkeypatch.setattr(
        conductor,
        "_mathlib_already_built",
        lambda _worktree: False,
    )
    monkeypatch.setattr(
        conductor,
        "_prime_mathlib_cache",
        lambda _worktree: conductor._CACHE_PRIME_UNSAFE,
    )

    assert run_once(e2e) == 0

    snapshot = conductor.load_jobs_snapshot(e2e.cfg.repo_dir)
    assert "cache preparation" in snapshot["degraded"]
    assert e2e.popen.spawned == []


def test_prune_fails_closed_when_leftover_publication_state_is_unreadable(
    cfg, subprocess_calls, tmp_path
):
    runs_parent = tmp_path / "runs"
    (runs_parent / "wt-i22-20260610-120000").mkdir(parents=True)
    (runs_parent / "wd-i22-20260610-120000").mkdir()  # workdir: untouched

    conductor.prune_worktrees(cfg.repo_dir, runs_parent)

    # The generic subprocess double returns no branch/HEAD.  Cleanup must now
    # treat that uncertainty as potentially unpublished work and preserve the
    # directory; only stale worktree metadata may be pruned.
    assert not any(call[3:5] == ["worktree", "remove"] for call in subprocess_calls)
    assert subprocess_calls[-1] == [
        "git", "-C", str(cfg.repo_dir), "worktree", "prune"
    ]


# --- single-conductor lock --------------------------------------------------------


def test_lock_prevents_second_conductor(e2e):
    """A live PID in conductor.lock means another conductor owns the
    repo: exit 0 immediately, dispatch nothing, leave the lock alone."""
    lock = conductor.conductor_lock_path(e2e.cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))  # provably-alive pid: our own
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})

    assert run_once(e2e) == 0

    assert e2e.popen.spawned == []
    assert e2e.git == []
    assert lock.read_text() == str(os.getpid())  # not clobbered


def test_lock_released_after_run(e2e):
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    e2e.popen.script = {22: {"rc": 0, "polls": 1}}
    assert run_once(e2e) == 0
    assert not conductor.conductor_lock_path(e2e.cfg).exists()


def test_concurrent_processes_cannot_both_acquire_conductor_lock(tmp_path):
    """The singleton decision is atomic, not a check-then-write PID race."""
    cfg = make_cfg(tmp_path)
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    release = ctx.Event()
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_conductor_lock_racer,
            args=(cfg, start, release, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    try:
        acquired = [results.get(timeout=15), results.get(timeout=15)]
        assert acquired.count(True) == 1
        assert acquired.count(False) == 1
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=15)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
    assert all(worker.exitcode == 0 for worker in workers)


# --- orphan reconciliation ---------------------------------------------------------


def test_orphan_report_prints_unknown_project_ids(cfg, monkeypatch, capsys):
    """In-flight Aristotle projects not referenced by current jobs/state
    are printed (report-only, never canceled); known ones are not."""
    import aristotlelib

    known_job = conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree="/x/wt", workdir="/x/wd", branch="marathon/refine-c14-i22",
        status="running", project_id="known-1",
    )
    conductor.write_jobs_snapshot(cfg.repo_dir, [known_job], 1)

    requested: dict = {}

    async def fake_list_projects(limit=100, status=None):
        requested["status"] = status
        return (
            [SimpleNamespace(project_id="known-1"),
             SimpleNamespace(project_id="orphan-9")],
            None,
        )

    monkeypatch.setattr(aristotlelib.Project, "list_projects", fake_list_projects)

    conductor._report_orphans(cfg, conductor._known_project_ids(cfg.repo_dir))

    out = capsys.readouterr().out
    assert "orphan-9" in out
    assert "known-1" not in out  # accounted for — not reported
    assert "never canceled" in out
    # Only in-flight statuses were requested from the API.
    assert requested["status"], "expected an in-flight status filter"


def test_orphan_report_failure_warns_and_continues(cfg, monkeypatch, capsys):
    import aristotlelib

    async def boom(limit=100, status=None):
        raise RuntimeError("api down")

    monkeypatch.setattr(aristotlelib.Project, "list_projects", boom)
    conductor._report_orphans(cfg, set())  # must not raise
    assert "orphan reconciliation skipped" in capsys.readouterr().out


# --- jobs.json snapshot -------------------------------------------------------------


def test_jobs_snapshot_round_trips(cfg):
    """to_json/from_json over a write/load cycle preserves every
    serialized field (proc/log_handle are runtime-only by design)."""
    jobs = [
        conductor.ConductorJob(
            issue_num=22, chapter=14, target="Demo/Chapter14",
            worktree="/runs/wt-i22-x", workdir="/runs/wd-i22-x",
            branch="marathon/refine-c14-i22", pid=4001,
            started_ts="2026-06-11T10:00:00-04:00", status="running",
            project_id="proj-abc", aristotle_status="IN_PROGRESS",
            proc=SimpleNamespace(poll=lambda: None),  # must not serialize
        ),
        conductor.ConductorJob(
            issue_num=31, chapter=15, target="Demo/Chapter15",
            worktree="/runs/wt-i31-x", workdir="/runs/wd-i31-x",
            branch="marathon/refine-c15-i31", pid=4002,
            started_ts="2026-06-11T09:00:00-04:00",
            finished_ts="2026-06-11T09:45:00-04:00",
            status="succeeded", exit_code=0,
        ),
    ]
    conductor.write_jobs_snapshot(cfg.repo_dir, jobs, concurrency=2)

    # The runtime dir self-ignores (like .marathon/review's runner
    # dirs): an unignored jobs.json would leak into Aristotle bundles
    # and permanently dirty the metadata hook's clean-tree check.
    snapshot_dir = conductor.jobs_snapshot_path(cfg.repo_dir).parent
    assert (snapshot_dir / ".gitignore").read_text() == "*\n"

    snap = conductor.load_jobs_snapshot(cfg.repo_dir)
    assert snap is not None
    assert snap["schema_version"] == conductor.SNAPSHOT_SCHEMA_VERSION
    assert snap["concurrency"] == 2
    loaded = [conductor.ConductorJob.from_json(raw) for raw in snap["jobs"]]
    assert [j.to_json() for j in loaded] == [j.to_json() for j in jobs]
    assert all(j.proc is None and j.log_handle is None for j in loaded)


def test_status_table_marks_dead_running_pid(cfg, capsys):
    job = conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree="/runs/wt-i22-x", workdir="/runs/wd-i22-x",
        branch="marathon/refine-c14-i22", pid=99999999,  # provably dead
        started_ts="2026-06-11T10:00:00-04:00", status="running",
    )
    conductor.write_jobs_snapshot(cfg.repo_dir, [job], 1)

    assert conductor.print_status(cfg.repo_dir) == 0
    out = capsys.readouterr().out
    assert "#22" in out
    assert "pid dead" in out


def test_status_without_snapshot_exits_nonzero(cfg, capsys):
    assert conductor.print_status(cfg.repo_dir) == 1
    assert "no conductor snapshot" in capsys.readouterr().out


# --- concurrency default ------------------------------------------------------------


def test_concurrency_default_and_env_fallback(monkeypatch):
    monkeypatch.delenv(conductor.CONCURRENCY_ENV_VAR, raising=False)
    assert conductor.resolve_concurrency(None) == 1  # BINDING: parity default
    assert conductor.resolve_concurrency(3) == 3
    monkeypatch.setenv(conductor.CONCURRENCY_ENV_VAR, "4")
    assert conductor.resolve_concurrency(None) == 4
    assert conductor.resolve_concurrency(2) == 2  # explicit flag beats env
    monkeypatch.setenv(conductor.CONCURRENCY_ENV_VAR, "lots")
    assert conductor.resolve_concurrency(None) == 1  # malformed env: warn + default


# --- automerge integration (the merge orchestrator runs in the conductor) -----


def test_conductor_runs_automerge_each_cycle(e2e):
    """With automerge on (default), the conductor lands eligible PRs each cycle
    — a `gh pr list` against the base appears among the subprocess calls."""
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    e2e.popen.script = {22: {"rc": 0, "polls": 1}}

    assert run_once(e2e, automerge=True) == 0

    assert any(c[:3] == ["gh", "pr", "list"] for c in e2e.git)


def test_conductor_no_automerge_skips_the_cycle(e2e):
    """`automerge=False` runs the dispatch loop with NO merge cycle."""
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    e2e.popen.script = {22: {"rc": 0, "polls": 1}}

    assert run_once(e2e, automerge=False) == 0

    assert not any(c[:3] == ["gh", "pr", "list"] for c in e2e.git)

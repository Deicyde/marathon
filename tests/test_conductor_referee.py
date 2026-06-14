"""Phase-8 tests: the conductor's deterministic scheduler RESPECTS referee
fix-task blocking, and the landings-count referee cadence.

Contract under test (docs/marathon-v2-plan.md §2 "Referee with teeth"):

* a target/rejection whose chapter is named by an unresolved BLOCKING
  referee fix-task (one carrying a non-NULL ``blocks_target``) is DEFERRED
  — never dispatched around — and prints a clear reason; once the task is
  resolved (or carries no teeth) the same target dispatches;
* with NO referee tasks (today's state) the scheduler decision is
  byte-identical to the pre-Phase-8 conductor (asserted against the exact
  same call without the gate);
* the referee fires on the landings-count cadence — once per crossed
  multiple of N — and never on a non-multiple; a trigger FAILURE does not
  advance the bookkeeping (and never fails the landing/dispatch path).

The scheduler stays PURE deterministic Python (no Claude). Everything that
would touch Claude / Aristotle / gh / real git is monkeypatched or absent;
the ledger + audit snapshot are written to disk directly (no Lean). Fully
offline.
"""

from __future__ import annotations

import json
import signal
import subprocess
from types import SimpleNamespace

import pytest

import marathon.conductor as conductor
import marathon.landing as landing
from marathon.audit.engine import save_snapshot
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.ledger import Ledger, RefereeTask
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels
from marathon.review.state import load_state, state_path

REPO_SLUG = "example/Demo"

# Issue 22 -> chapter 14 (folder Demo/Chapter14, module Demo.Chapter14.*);
# issue 31 -> chapter 15. The decl `coordinateCoframe` lives in chapter 14.
CHAPTERS = {
    14: [(22, "Lemma A")],
    15: [(31, "Theorem C")],
}


def make_cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)  # ledger/audit live under the repo
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo=REPO_SLUG,
        parent_issue=1,
        referee_path=repo / ".marathon/referee.md",
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            chap: ChapterRegistry(chapter=chap, entries=list(entries))
            for chap, entries in CHAPTERS.items()
        },
    )


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


def seed_rejections(cfg: ReviewConfig, verdicts: dict[int, str]) -> None:
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


def write_snapshot(cfg: ReviewConfig, decl_modules: dict[str, str]) -> None:
    """Persist a minimal audit snapshot mapping decl name -> module so the
    accessor can locate each decl in a chapter folder."""
    decls = [
        DeclAudit(
            name=name, kind="def", module=module, status="ok",
            type_pp="T", value_pp="v", cone=[], axioms=[], has_sorry=False,
            tags=[], reason=None,
        )
        for name, module in decl_modules.items()
    ]
    snap = AuditSnapshot(
        repo_dir=str(cfg.repo_dir), modules=sorted(set(decl_modules.values())),
        toolchain="lean4:v1", lean_version="v1", package_revs={},
        trusted_prefixes=["Mathlib"], created_at="2026-06-14T00:00:00+00:00",
        decls=decls, failures=[],
    )
    save_snapshot(snap, cfg.repo_dir)


def add_referee_task(
    cfg: ReviewConfig, *, dedup_key, title, target_decls,
    blocks_target, status="open", severity="high",
) -> int:
    ledger = Ledger.for_repo(cfg.repo_dir)
    ledger.init()
    return ledger.upsert_referee_task(
        RefereeTask(
            dedup_key=dedup_key, kind="dedup", title=title,
            target_decls=list(target_decls), severity=severity,
            status=status, blocks_target=blocks_target,
        )
    )


# --- referee_blocked_chapters accessor --------------------------------------


def test_no_referee_tasks_means_empty_block_map(cfg):
    """Today's state: no ledger tasks → no gate at all."""
    assert conductor.referee_blocked_chapters(cfg) == {}


def test_blocking_task_resolves_to_its_chapter(cfg):
    write_snapshot(cfg, {"coordinateCoframe": "Demo.Chapter14.Frames"})
    tid = add_referee_task(
        cfg, dedup_key="dedup:def:fp", title="unify coordinateCoframe",
        target_decls=["coordinateCoframe"], blocks_target="coordinateCoframe",
    )
    blocked = conductor.referee_blocked_chapters(cfg)
    assert blocked == {14: (tid, "unify coordinateCoframe")}


def test_advisory_task_has_no_teeth(cfg):
    """A task with blocks_target=None is advisory — it must NOT gate any
    chapter (the binding NULL=advisory rule)."""
    write_snapshot(cfg, {"coordinateCoframe": "Demo.Chapter14.Frames"})
    add_referee_task(
        cfg, dedup_key="naming:x", title="rename foo",
        target_decls=["coordinateCoframe"], blocks_target=None,
    )
    assert conductor.referee_blocked_chapters(cfg) == {}


def test_resolved_task_does_not_block(cfg):
    write_snapshot(cfg, {"coordinateCoframe": "Demo.Chapter14.Frames"})
    add_referee_task(
        cfg, dedup_key="dedup:def:fp", title="unify",
        target_decls=["coordinateCoframe"], blocks_target="coordinateCoframe",
        status="done",
    )
    assert conductor.referee_blocked_chapters(cfg) == {}


def test_blocking_task_with_no_snapshot_does_not_block(cfg, capsys):
    """A blocking task whose decls can't be located (no audit snapshot)
    schedules unblocked rather than guessing — and says so."""
    add_referee_task(
        cfg, dedup_key="dedup:def:fp", title="unify",
        target_decls=["coordinateCoframe"], blocks_target="coordinateCoframe",
    )
    assert conductor.referee_blocked_chapters(cfg) == {}
    assert "no audit snapshot" in capsys.readouterr().out


# --- scheduler defer (the teeth) --------------------------------------------


def test_scheduler_defers_blocked_chapter(cfg, capsys):
    """A blocked chapter's rejection is deferred (not picked); a rejection
    in an unblocked chapter is still dispatched."""
    seed_rejections(cfg, {22: "2026-06-10T12:00:00-04:00",
                          31: "2026-06-11T12:00:00-04:00"})
    blocked = {14: (7, "unify coordinateCoframe")}
    picks = conductor._pick_dispatchable(
        cfg, [], {}, now=0.0, slots=3, blocked_chapters=blocked
    )
    assert picks == [(31, 15)]  # 22's chapter 14 is gated; 31 flows
    out = capsys.readouterr().out
    assert "deferred #22" in out
    assert "referee task #7" in out
    assert "unify coordinateCoframe" in out


def test_no_block_is_byte_identical_to_today(cfg):
    """With no gate the Phase-8 picker returns EXACTLY what the pre-gate
    picker returned — the byte-identical guarantee."""
    seed_rejections(cfg, {22: "2026-06-10T12:00:00-04:00",
                          31: "2026-06-11T12:00:00-04:00"})
    # No blocked_chapters arg at all (default) and an explicit-empty arg
    # must both equal the original two-pick decision.
    expected = [(22, 14), (31, 15)]
    assert conductor._pick_dispatchable(cfg, [], {}, now=0.0, slots=3) == expected
    assert conductor._pick_dispatchable(
        cfg, [], {}, now=0.0, slots=3, blocked_chapters={}
    ) == expected


def test_blocked_then_dispatched_once_resolved(cfg):
    """The target blocked by an unresolved task is deferred; after the
    task resolves the same target dispatches — the end-to-end teeth."""
    seed_rejections(cfg, {22: "2026-06-10T12:00:00-04:00"})
    write_snapshot(cfg, {"coordinateCoframe": "Demo.Chapter14.Frames"})
    add_referee_task(
        cfg, dedup_key="dedup:def:fp", title="unify",
        target_decls=["coordinateCoframe"], blocks_target="coordinateCoframe",
    )
    blocked = conductor.referee_blocked_chapters(cfg)
    assert conductor._pick_dispatchable(
        cfg, [], {}, now=0.0, slots=3, blocked_chapters=blocked
    ) == []  # #22 deferred

    # Resolve the task (the self-accountability pass / a human close).
    Ledger.for_repo(cfg.repo_dir).resolve_referee_task("dedup:def:fp")
    blocked = conductor.referee_blocked_chapters(cfg)
    assert blocked == {}
    assert conductor._pick_dispatchable(
        cfg, [], {}, now=0.0, slots=3, blocked_chapters=blocked
    ) == [(22, 14)]


def test_deferral_reason_printed_once_per_issue(cfg, capsys):
    """The defer reason is printed at most once per issue per pass (no
    per-tick log spam) via the warned_blocked set."""
    seed_rejections(cfg, {22: "2026-06-10T12:00:00-04:00"})
    blocked = {14: (7, "unify")}
    warned: set[int] = set()
    for _ in range(3):
        conductor._pick_dispatchable(
            cfg, [], {}, now=0.0, slots=3,
            blocked_chapters=blocked, warned_blocked=warned,
        )
    assert capsys.readouterr().out.count("deferred #22") == 1


def test_ledger_unavailable_degrades(cfg, monkeypatch, capsys):
    """A ledger that won't open must not crash the scheduler — the gate
    degrades to empty (schedule without teeth)."""
    import marathon.ledger as ledger_mod

    def boom(self):
        raise ledger_mod.LedgerError("newer schema")

    monkeypatch.setattr(ledger_mod.Ledger, "init", boom)
    assert conductor.referee_blocked_chapters(cfg) == {}
    assert "ledger unavailable" in capsys.readouterr().out


# --- landings-count referee cadence -----------------------------------------


def write_landings(cfg: ReviewConfig, n: int) -> None:
    """Write n landing records to the repo-checkout landings.jsonl (the
    count_landings fallback path; no landing worktree in these tests)."""
    path = cfg.repo_dir / landing.LANDINGS_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps({"issue": i}) + "\n" for i in range(n))
    )


def test_count_landings(cfg, tmp_path):
    assert landing.count_landings(cfg.repo_dir) == 0
    write_landings(cfg, 3)
    # Point worktree_parent somewhere empty so the repo copy is read.
    assert landing.count_landings(cfg.repo_dir, tmp_path / "empty") == 3


def test_cadence_fires_on_multiple_only(cfg, tmp_path):
    fired: list[int] = []

    def trigger(repo_dir):
        fired.append(landing.count_landings(repo_dir, tmp_path / "empty"))
        return True

    parent = tmp_path / "empty"

    # 1 landing, every=2 → no fire (1 // 2 == 0).
    write_landings(cfg, 1)
    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=parent, trigger=trigger) is False
    assert fired == []

    # 2 landings → crosses the first multiple of 2 → fires once.
    write_landings(cfg, 2)
    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=parent, trigger=trigger) is True
    assert fired == [2]

    # 3 landings → still bucket 1 (3 // 2 == 1, already handled) → no fire.
    write_landings(cfg, 3)
    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=parent, trigger=trigger) is False
    assert fired == [2]

    # 4 landings → bucket 2 → fires again.
    write_landings(cfg, 4)
    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=parent, trigger=trigger) is True
    assert fired == [2, 4]


def test_cadence_off_by_default(cfg, tmp_path):
    fired: list[int] = []
    write_landings(cfg, 100)
    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 0, worktree_parent=tmp_path / "empty",
        trigger=lambda r: fired.append(1) or True) is False
    assert fired == []


def test_cadence_batch_crossing_fires_once(cfg, tmp_path):
    """Several multiples crossed between two checks fire exactly once
    (the latest count is what matters)."""
    fired: list[int] = []
    write_landings(cfg, 10)  # crosses 2,4,6,8,10 for every=2 at once
    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=tmp_path / "empty",
        trigger=lambda r: fired.append(1) or True) is True
    assert fired == [1]


def test_cadence_trigger_failure_does_not_advance(cfg, tmp_path):
    """A failed trigger returns False and does NOT advance the cadence
    bookkeeping — the next pass retries (the cadence is a freshness floor,
    not a fragile one-shot). The landing/dispatch path is never failed."""
    calls: list[int] = []
    parent = tmp_path / "empty"

    def failing(repo_dir):
        calls.append(1)
        return False  # referee exited non-zero / could not exec

    write_landings(cfg, 2)
    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=parent, trigger=failing) is False
    assert calls == [1]
    # Retried on the next pass because bookkeeping was not advanced.
    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=parent, trigger=failing) is False
    assert calls == [1, 1]

    # Once it succeeds, the bookkeeping advances and it stops re-firing.
    def ok(repo_dir):
        calls.append(2)
        return True

    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=parent, trigger=ok) is True
    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=parent, trigger=ok) is False
    assert calls == [1, 1, 2]


def test_cadence_state_file_self_gitignores(cfg, tmp_path):
    """The cadence state file is gitignored by NAME (not a bare ``*`` that
    would also hide the tracked landings.jsonl)."""
    write_landings(cfg, 2)
    landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=tmp_path / "empty",
        trigger=lambda r: True)
    state = landing.referee_cadence_state_path(cfg.repo_dir)
    assert state.is_file()
    gi = (state.parent / ".gitignore").read_text()
    assert landing.REFEREE_CADENCE_FILENAME in gi
    assert gi.strip() != "*"  # must not blanket-ignore landings.jsonl


def test_cadence_swallows_unexpected_error(cfg, tmp_path, capsys):
    """An unexpected trigger error is swallowed with a warning — the
    binding rule that a referee hiccup never fails the landing path."""
    write_landings(cfg, 2)

    def boom(repo_dir):
        raise RuntimeError("kaboom")

    assert landing.maybe_trigger_referee(
        cfg.repo_dir, 2, worktree_parent=tmp_path / "empty",
        trigger=boom) is False
    assert "referee cadence trigger failed" in capsys.readouterr().out


# --- end-to-end run_conductor wiring ----------------------------------------


@pytest.fixture(autouse=True)
def _restore_signals_and_stop_flag(monkeypatch):
    monkeypatch.setattr(conductor, "_STOP_REQUESTED", False)
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGINT, old_int)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds, **kwargs):
        self.t += max(float(seconds), 1.0)


@pytest.fixture
def e2e(cfg, monkeypatch, tmp_path):
    """A minimal run_conductor harness: fake config load, no orphan API,
    fake clock, recorded git subprocesses, scripted refine Popens."""
    clock = _Clock()
    monkeypatch.setattr(conductor, "load_config", lambda repo_dir=None: cfg)
    monkeypatch.setattr(conductor, "_report_orphans", lambda cfg_, ids: None)
    monkeypatch.setattr(conductor, "_now", clock.now)
    monkeypatch.setattr(conductor, "_sleep", clock.sleep)

    git_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        git_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(conductor.subprocess, "run", fake_run)

    spawned: list = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.cmd = list(cmd)
            self.issue = int(self.cmd[self.cmd.index("--review-rejection") + 1])
            self.pid = 5000 + len(spawned)
            self.returncode = None
            self._polls = 1
            spawned.append(self)

        def poll(self):
            if self.returncode is not None:
                return self.returncode
            self._polls -= 1
            if self._polls <= 0:
                self.returncode = 0
            return self.returncode

    monkeypatch.setattr(conductor.subprocess, "Popen", FakePopen)
    return SimpleNamespace(
        cfg=cfg, spawned=spawned, git=git_calls,
        runs_parent=tmp_path / "runs", clock=clock,
    )


def _run_once(e2e, **kwargs):
    return conductor.run_conductor(
        repo_dir=e2e.cfg.repo_dir, concurrency=1, once=True,
        worktree_parent=e2e.runs_parent, **kwargs,
    )


def test_run_conductor_skips_blocked_dispatches_blocked_chapter(e2e):
    """End-to-end: with a blocking referee task on chapter 14, run_conductor
    dispatches ONLY the unblocked chapter-15 rejection and drains (it does
    not spin to the safety cap on the blocked-only remainder)."""
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00",
                              31: "2026-06-11T12:00:00-04:00"})
    write_snapshot(e2e.cfg, {"coordinateCoframe": "Demo.Chapter14.Frames"})
    add_referee_task(
        e2e.cfg, dedup_key="dedup:def:fp", title="unify",
        target_decls=["coordinateCoframe"], blocks_target="coordinateCoframe",
    )

    assert _run_once(e2e) == 0
    # Only #31 ran; #22 stayed blocked (no iteration recorded for it).
    assert [p.issue for p in e2e.spawned] == [31]
    assert load_state(e2e.cfg).issues[31].last_iteration_ts is not None
    assert load_state(e2e.cfg).issues[22].last_iteration_ts is None


def test_run_conductor_dispatches_after_block_clears(e2e):
    """Once the blocking task resolves, the previously-deferred #22
    dispatches on a fresh run."""
    seed_rejections(e2e.cfg, {22: "2026-06-10T12:00:00-04:00"})
    write_snapshot(e2e.cfg, {"coordinateCoframe": "Demo.Chapter14.Frames"})
    add_referee_task(
        e2e.cfg, dedup_key="dedup:def:fp", title="unify",
        target_decls=["coordinateCoframe"], blocks_target="coordinateCoframe",
    )
    assert _run_once(e2e) == 0
    assert e2e.spawned == []  # blocked → nothing dispatched

    Ledger.for_repo(e2e.cfg.repo_dir).resolve_referee_task("dedup:def:fp")
    assert _run_once(e2e) == 0
    assert [p.issue for p in e2e.spawned] == [22]


def test_run_conductor_fires_cadence(e2e, monkeypatch):
    """--referee-every wires the landings-count cadence into the loop:
    with 2 landings on disk and referee_every=2 the cadence hook fires the
    referee exactly once."""
    seed_rejections(e2e.cfg, {31: "2026-06-11T12:00:00-04:00"})
    write_landings(e2e.cfg, 2)

    calls: list = []
    monkeypatch.setattr(
        landing, "_default_referee_trigger",
        lambda repo_dir: (calls.append(repo_dir) or True),
    )

    assert _run_once(e2e, referee_every=2) == 0
    assert len(calls) == 1


def test_run_conductor_no_cadence_when_zero(e2e, monkeypatch):
    """referee_every=0 (default) never fires the cadence."""
    seed_rejections(e2e.cfg, {31: "2026-06-11T12:00:00-04:00"})
    write_landings(e2e.cfg, 50)
    calls: list = []
    monkeypatch.setattr(
        landing, "_default_referee_trigger",
        lambda repo_dir: (calls.append(1) or True),
    )
    assert _run_once(e2e, referee_every=0) == 0
    assert calls == []

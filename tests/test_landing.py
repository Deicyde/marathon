"""Tests for the Phase-4 landing queue (marathon.landing).

Contract under test (docs/marathon-v2-plan.md §2 LANDING QUEUE box +
ruling 6, §3 Phase 4 row; critique shaping in
docs/v2-analysis/crit-feas-ux-first.md point 3):

* requests form a serial FIFO of JSON files in a self-gitignoring
  queue dir;
* a landing = fetch → cherry-pick onto marathon/next → lake build →
  gate (ENFORCE: fail blocks, no override) → plain push (never force)
  plus one tracked landings.jsonl record committed on the branch;
* any failure bounces (clean abort + worktree rollback + report file +
  ONE circuit-broken gh comment) — never blocks the queue, never
  resubmits to Aristotle;
* only push rejections re-queue, exactly once; conflict/build/gate
  failures wait for the next human/conductor action;
* promotion is explicit and fast-forward-only (divergence refused);
* in-repo worktree parents are refused; one landing runner per repo;
* the conductor enqueues on job success ONLY under ``--land next``.

Every git / lake / gate / gh boundary is monkeypatched: fully offline.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import marathon.landing as landing
from marathon.gate import CheckResult, GateReport
from marathon.post_pipeline import BuildResult
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels

REPO_SLUG = "example/Demo"
SRC_SHA = "cafe1111" * 5  # the job commit the landing cherry-picks
NEXT_SHA = "feed2222" * 5  # marathon/next tip after the cherry-pick

CHAPTERS = {
    14: [(22, "Lemma A")],
    15: [(31, "Theorem C")],
}


def make_cfg(tmp_path) -> ReviewConfig:
    """Minimal in-memory ReviewConfig rooted at tmp_path/repo (a strict
    subdir, so a worktree parent beside it counts as outside the repo)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
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


class FakeGit:
    """Scripted stand-in for ``landing._git``: records every call and
    returns canned results, with the failure points the tests script
    (cherry-pick conflict, push rejection, promote divergence)."""

    def __init__(self):
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.cherry_pick_rc = 0
        self.push_rc = 0
        self.next_exists = True       # refs/heads + refs/remotes for marathon/next
        self.commits_to_land = [SRC_SHA]
        self.divergence = "0\t1"      # rev-list --left-right --count output
        self.landings_tracked = True  # ls-files --error-unmatch landings.jsonl

    def args_of(self, subcommand: str) -> list[tuple[str, ...]]:
        return [a for _, a in self.calls if a[0] == subcommand]

    def __call__(self, repo_dir, *args):
        self.calls.append((Path(repo_dir), args))

        def cp(rc=0, out="", err=""):
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=rc, stdout=out, stderr=err
            )

        sub = args[0]
        if sub == "rev-parse":
            if "--abbrev-ref" in args:
                return cp(out=landing.LANDING_BRANCH + "\n")
            ref = args[-1]
            if ref in (
                f"refs/heads/{landing.LANDING_BRANCH}",
                f"refs/remotes/origin/{landing.LANDING_BRANCH}",
            ):
                return cp(0 if self.next_exists else 1, out=NEXT_SHA + "\n")
            if ref == "HEAD":
                return cp(out=NEXT_SHA + "\n")
            return cp(out=SRC_SHA + "\n")  # source-ref ^{commit} resolution
        if sub == "rev-list":
            if "--left-right" in args:
                return cp(out=self.divergence + "\n")
            return cp(out="".join(s + "\n" for s in self.commits_to_land))
        if sub == "cherry-pick":
            if "--abort" in args:
                return cp()
            return cp(
                self.cherry_pick_rc,
                err="CONFLICT (content): Demo/Chapter14/Basic.lean"
                if self.cherry_pick_rc else "",
            )
        if sub == "diff":
            return cp(out="+<<<<<<< HEAD conflict hunk")
        if sub == "push":
            return cp(
                self.push_rc,
                err="! [rejected] marathon/next -> marathon/next (fetch first)"
                if self.push_rc else "",
            )
        if sub == "ls-files":
            return cp(0 if self.landings_tracked else 1)
        # fetch / reset / worktree add / add / commit succeed silently.
        return cp()


@pytest.fixture
def gh_calls(monkeypatch):
    """Capture the gh bounce-comment boundary. landing._git is patched
    separately, so subprocess.run inside marathon.landing is only ever
    reached by `gh issue comment`."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(landing.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def fake_build(monkeypatch):
    state = SimpleNamespace(calls=[], result=BuildResult(ok=True, duration_seconds=42.0))

    def run(repo_dir, timeout):
        state.calls.append((Path(repo_dir), timeout))
        return state.result

    monkeypatch.setattr(landing, "run_lake_build", run)
    return state


def passing_report() -> GateReport:
    return GateReport(
        mode="skeleton", target="Demo/Chapter14",
        checks=[CheckResult(name="build", status="pass", summary="ok")],
    )


def failing_report() -> GateReport:
    return GateReport(
        mode="skeleton", target="Demo/Chapter14",
        checks=[CheckResult(
            name="axioms", status="fail", summary="non-whitelisted axiom",
        )],
    )


@pytest.fixture
def fake_gate(monkeypatch):
    state = SimpleNamespace(calls=[], report=passing_report())

    def run(repo_dir, target_folder, *, mode, build_ok, **kwargs):
        state.calls.append(SimpleNamespace(
            repo_dir=Path(repo_dir), target=Path(target_folder),
            mode=mode, build_ok=build_ok,
        ))
        return state.report

    monkeypatch.setattr(landing, "run_gate", run)
    return state


@pytest.fixture
def env(cfg, monkeypatch, tmp_path, gh_calls, fake_build, fake_gate):
    """Everything a run_landing end-to-end test needs: fake config
    loading, scripted git, recorded gh/build/gate boundaries."""
    git = FakeGit()
    monkeypatch.setattr(landing, "_git", git)
    monkeypatch.setattr(landing, "load_config", lambda repo_dir=None: cfg)
    parent = tmp_path / "land-runs"
    return SimpleNamespace(
        cfg=cfg, repo=cfg.repo_dir, git=git, gh=gh_calls,
        build=fake_build, gate=fake_gate,
        parent=parent, worktree=parent / landing.LANDING_WORKTREE_NAME,
    )


def enqueue(env, issue=22, chapter=14, source_ref=SRC_SHA, **kwargs):
    return landing.enqueue_landing(
        env.repo, issue_num=issue, chapter=chapter,
        source_ref=source_ref, workdir="/x/wd", **kwargs,
    )


def run_once(env, **kwargs):
    return landing.run_landing(
        repo_dir=env.repo, once=True, worktree_parent=env.parent, **kwargs
    )


def make_req(issue=22, **kwargs) -> landing.LandingRequest:
    return landing.LandingRequest(
        issue_num=issue, chapter=14, source_ref=SRC_SHA, workdir="/x/wd",
        enqueued_ts="2026-06-12T10:00:00-04:00", **kwargs,
    )


def bounce_reports(repo: Path, issue: int) -> list[Path]:
    bdir = repo / landing.BOUNCES_RELPATH
    return sorted(bdir.glob(f"{issue}-*.md")) if bdir.is_dir() else []


# --- queue ------------------------------------------------------------------------


def test_queue_fifo_and_self_gitignore(cfg):
    """Enqueue order = pop order (filename-sortable timestamps), the
    queue dir self-gitignores on first write (conductor jobs.json
    convention), and popped files are removed."""
    p1 = landing.enqueue_landing(
        cfg.repo_dir, issue_num=22, chapter=14, source_ref="aaa", workdir="/w1"
    )
    p2 = landing.enqueue_landing(
        cfg.repo_dir, issue_num=31, chapter=15, source_ref="bbb", workdir="/w2"
    )
    qdir = cfg.repo_dir / landing.QUEUE_RELPATH
    assert (qdir / ".gitignore").read_text() == "*\n"
    assert p1.name < p2.name  # FIFO is plain filename order

    r1 = landing.pop_oldest_request(cfg.repo_dir)
    assert (r1.issue_num, r1.chapter, r1.source_ref) == (22, 14, "aaa")
    assert (r1.attempts, r1.mode) == (0, "skeleton")
    datetime.fromisoformat(r1.enqueued_ts)  # stamps a parseable timestamp
    assert not p1.exists()

    r2 = landing.pop_oldest_request(cfg.repo_dir)
    assert r2.issue_num == 31
    assert landing.pop_oldest_request(cfg.repo_dir) is None


def test_enqueue_rejects_unknown_mode(cfg):
    with pytest.raises(ValueError, match="unknown landing mode"):
        landing.enqueue_landing(
            cfg.repo_dir, issue_num=22, chapter=14, source_ref="aaa",
            workdir="/w", mode="vibes",
        )


def test_enqueue_is_atomic_no_tmp_left_behind(cfg):
    """Requests are written tmp-then-rename: the polling runner can
    never read (and quarantine) a torn half-written request."""
    landing.enqueue_landing(
        cfg.repo_dir, issue_num=22, chapter=14, source_ref="aaa", workdir="/w"
    )
    qdir = cfg.repo_dir / landing.QUEUE_RELPATH
    assert not list(qdir.glob("*.tmp"))
    assert landing.pop_oldest_request(cfg.repo_dir).issue_num == 22


def test_pathlike_issue_num_quarantined(cfg):
    """issue_num flows into bounce-report FILENAMES and gh argv: a queue
    file carrying a path-shaped issue_num must quarantine as .corrupt,
    never parse into a request."""
    qdir = cfg.repo_dir / landing.QUEUE_RELPATH
    qdir.mkdir(parents=True)
    bad = dict(make_req().to_json(), issue_num="../../../tmp/evil")
    (qdir / "00000000-000000-000000-i0.json").write_text(json.dumps(bad))

    assert landing.pop_oldest_request(cfg.repo_dir) is None
    assert list(qdir.glob("*.corrupt"))


def test_corrupt_queue_file_never_wedges_fifo(cfg, capsys):
    qdir = cfg.repo_dir / landing.QUEUE_RELPATH
    qdir.mkdir(parents=True)
    (qdir / "00000000-000000-000000-i9.json").write_text("not json")
    landing.enqueue_landing(
        cfg.repo_dir, issue_num=22, chapter=14, source_ref="aaa", workdir="/w"
    )
    req = landing.pop_oldest_request(cfg.repo_dir)
    assert req is not None and req.issue_num == 22
    assert "unreadable landing request" in capsys.readouterr().out
    assert list(qdir.glob("*.corrupt"))  # quarantined, not deleted


# --- happy path -------------------------------------------------------------------


def test_happy_path_landing(env):
    """cherry-pick → build ok → gate pass → plain push + one tracked
    landings.jsonl record committed on marathon/next."""
    enqueue(env)
    assert run_once(env, build_timeout=1234) == 0

    # Queue drained; the build ran in the landing worktree with the flag
    # timeout; the gate ran in skeleton mode on the chapter folder.
    assert landing.pop_oldest_request(env.repo) is None
    assert env.build.calls == [(env.worktree, 1234)]
    [g] = env.gate.calls
    assert g.repo_dir == env.worktree
    assert g.target == env.worktree / "Demo/Chapter14"
    assert g.mode == "skeleton"
    assert g.build_ok is True

    # The job's commit was cherry-picked, then ONE plain push — never
    # any force flag anywhere in the git stream.
    assert ("cherry-pick", SRC_SHA) in [a for _, a in env.git.calls]
    assert env.git.args_of("push") == [("push", "origin", landing.LANDING_BRANCH)]
    assert all(
        "--force" not in a and "-f" not in a for _, a in env.git.calls
    )

    # The keyed landings record was appended in the worktree and staged
    # + committed (it rides marathon/next with the landing itself).
    [line] = (env.worktree / landing.LANDINGS_RELPATH).read_text().splitlines()
    rec = json.loads(line)
    assert rec["issue"] == 22
    assert rec["sha_landed"] == SRC_SHA
    assert rec["next_sha"] == NEXT_SHA
    assert rec["build_secs"] == 42.0
    datetime.fromisoformat(rec["ts"])
    assert ("add", "--", landing.LANDINGS_RELPATH.as_posix()) in [
        a for _, a in env.git.calls
    ]
    assert env.git.args_of("commit")  # the record commit happened

    # No bounce artifacts on success.
    assert env.gh == []
    assert bounce_reports(env.repo, 22) == []


def test_first_landing_creates_branch_from_base(env):
    """marathon/next absent everywhere: created with -b (never -B) from
    origin/<base> and published with --set-upstream — no reset, no force."""
    env.git.next_exists = False
    enqueue(env)
    assert run_once(env, base="main") == 0

    [add] = [a for _, a in env.git.calls if a[:2] == ("worktree", "add")]
    assert add[2] == "-b"
    assert add[3] == landing.LANDING_BRANCH
    assert add[-1] == "origin/main"
    assert ("push", "--set-upstream", "origin", landing.LANDING_BRANCH) in [
        a for _, a in env.git.calls
    ]
    assert env.git.args_of("reset") == []  # nothing to align against yet


# --- bounce paths ------------------------------------------------------------------


def test_conflict_bounce(env):
    """Cherry-pick conflict: abort + rollback, report with the conflict
    diff, exactly one gh comment, never re-queued, nothing pushed."""
    env.git.cherry_pick_rc = 1
    enqueue(env)
    assert run_once(env) == 0

    calls = [a for _, a in env.git.calls]
    abort_idx = calls.index(("cherry-pick", "--abort"))
    rollback = ("reset", "--hard", f"origin/{landing.LANDING_BRANCH}")
    assert rollback in calls[abort_idx:]  # rolled back AFTER the abort

    [report] = bounce_reports(env.repo, 22)
    text = report.read_text()
    assert landing.CLASS_CONFLICT in text
    assert "conflict hunk" in text  # the conflict diff is the evidence

    [gh] = env.gh
    assert gh[:4] == ["gh", "issue", "comment", "22"]
    assert gh[gh.index("--repo") + 1] == REPO_SLUG
    body = gh[gh.index("--body") + 1]
    assert landing.CLASS_CONFLICT in body
    assert "NOT re-queued" in body

    # Bounce, not block: queue drained, no retry, no push, no build/gate
    # spend, no landings record.
    assert landing.pop_oldest_request(env.repo) is None
    assert env.git.args_of("push") == []
    assert env.build.calls == []
    assert env.gate.calls == []
    assert not (env.worktree / landing.LANDINGS_RELPATH).exists()
    # The bounces dir self-gitignores (runtime evidence, never tracked).
    assert (env.repo / landing.BOUNCES_RELPATH / ".gitignore").read_text() == "*\n"


def test_build_fail_bounce_never_requeued(env):
    env.build.result = BuildResult(
        ok=False, duration_seconds=9.0, log_tail="error: unknown identifier 'foo'"
    )
    enqueue(env)
    assert run_once(env) == 0

    [report] = bounce_reports(env.repo, 22)
    assert "unknown identifier 'foo'" in report.read_text()
    assert env.git.args_of("push") == []
    assert env.gate.calls == []  # gate is pointless on a red build
    assert landing.pop_oldest_request(env.repo) is None
    assert len(env.gh) == 1


def test_gate_fail_bounce_never_requeued(env):
    """ENFORCE semantics: a fail verdict blocks the landing — no
    override path, no auto-retry; the bounce report carries the gate's
    rendered findings."""
    env.gate.report = failing_report()
    enqueue(env)
    assert run_once(env) == 0

    [report] = bounce_reports(env.repo, 22)
    text = report.read_text()
    assert landing.CLASS_GATE in text
    assert "FAIL" in text  # the rendered gate report is the evidence
    assert env.git.args_of("push") == []
    assert landing.pop_oldest_request(env.repo) is None
    assert len(env.gh) == 1
    # Rolled back: the cherry-picked-but-rejected state never survives.
    assert ("reset", "--hard", f"origin/{landing.LANDING_BRANCH}") in [
        a for _, a in env.git.calls
    ]


def test_transient_push_rejection_retried_exactly_once(env):
    """Push rejection = the remote moved: re-queued once (fresh fetch +
    cherry-pick), then it waits for human action. The identical failure
    signature collapses both bounces into ONE gh comment."""
    env.git.push_rc = 1
    enqueue(env)
    assert run_once(env) == 0

    # Two full attempts (original + the single re-queue), then drained.
    assert len(env.git.args_of("push")) == 2
    picks = [a for _, a in env.git.calls
             if a[0] == "cherry-pick" and "--abort" not in a]
    assert len(picks) == 2
    assert landing.pop_oldest_request(env.repo) is None

    # Both attempts wrote reports; the breaker collapsed the comments.
    assert len(bounce_reports(env.repo, 22)) == 2
    assert len(env.gh) == 1
    body = env.gh[0][env.gh[0].index("--body") + 1]
    assert "re-queued once" in body


def test_rollback_discards_untracked_landings_record(env):
    """``reset --hard`` leaves untracked files: a landings.jsonl row
    appended but never committed (first-ever landing whose record
    commit failed, or a crash before it) must not survive the rollback
    and be folded into the NEXT landing's record commit."""
    path = env.worktree / landing.LANDINGS_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text('{"issue": 99}\n')

    env.git.landings_tracked = True
    landing._rollback(env.worktree)
    assert path.exists()  # tracked: reset --hard already restored it

    env.git.landings_tracked = False
    landing._rollback(env.worktree)
    assert not path.exists()  # untracked leftover: dropped


# --- bounce-comment circuit breaker ---------------------------------------------------


def test_bounce_comment_dedup_same_signature(cfg, gh_calls, capsys):
    """The same failure signature is never posted twice — but the report
    file is always written (the silent local record)."""
    landing._bounce(cfg, make_req(), landing.CLASS_GATE, "gate verdict FAIL: axioms")
    landing._bounce(cfg, make_req(), landing.CLASS_GATE, "gate verdict FAIL: axioms")

    assert len(gh_calls) == 1
    assert len(bounce_reports(cfg.repo_dir, 22)) == 2
    assert "suppressed (identical failure signature" in capsys.readouterr().out


def test_bounce_comment_daily_cap(cfg, gh_calls, capsys):
    """Distinct signatures still hit the hard cap of 3 comments per
    issue per day; other issues keep their own budget."""
    for i in range(5):
        landing._bounce(cfg, make_req(), landing.CLASS_GATE, f"distinct failure {i}")

    assert len(gh_calls) == landing.BOUNCE_COMMENT_DAILY_CAP
    assert len(bounce_reports(cfg.repo_dir, 22)) == 5  # reports never capped
    assert "suppressed (daily cap" in capsys.readouterr().out

    landing._bounce(cfg, make_req(issue=31), landing.CLASS_GATE, "other failure")
    assert len(gh_calls) == landing.BOUNCE_COMMENT_DAILY_CAP + 1


def test_gh_failure_is_best_effort_and_not_counted(cfg, monkeypatch, capsys):
    """A gh failure warns, never raises, and is NOT recorded against the
    breaker — the next bounce retries the notification."""
    def failing_run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="auth")

    monkeypatch.setattr(landing.subprocess, "run", failing_run)
    landing._bounce(cfg, make_req(), landing.CLASS_GATE, "detail")
    assert "bounce comment for #22 failed" in capsys.readouterr().out
    assert landing._load_breaker(landing._breaker_path(cfg.repo_dir))["posted"] == {}


# --- guard rails -----------------------------------------------------------------------


def test_in_repo_worktree_parent_refused(cfg, monkeypatch):
    monkeypatch.setattr(landing, "load_config", lambda repo_dir=None: cfg)
    with pytest.raises(SystemExit) as exc:
        landing.run_landing(
            repo_dir=cfg.repo_dir, worktree_parent=cfg.repo_dir / "inside"
        )
    assert "inside the repo" in str(exc.value)


def test_lock_exclusivity(env):
    """A live PID in landing.lock means another runner owns the queue:
    exit 0, touch nothing. A stale (dead-PID) lock is reclaimed."""
    lock = landing.landing_lock_path(env.repo)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))  # provably-alive pid: our own
    enqueue(env)

    assert run_once(env) == 0
    assert len(list((env.repo / landing.QUEUE_RELPATH).glob("*.json"))) == 1
    assert env.git.calls == []
    assert lock.read_text() == str(os.getpid())  # not clobbered

    lock.write_text("999999999")  # provably-dead pid: reclaimed
    assert run_once(env) == 0
    assert landing.pop_oldest_request(env.repo) is None  # processed
    assert not lock.exists()  # released on exit


# --- promotion ---------------------------------------------------------------------------


def test_promote_fast_forwards_when_clean(cfg, monkeypatch, capsys):
    """left=0/right>0: a server-side ff push of the remote next tip to
    the base ref — no force flag exists on this path."""
    git = FakeGit()
    git.divergence = "0\t3"
    monkeypatch.setattr(landing, "_git", git)

    assert landing.promote(cfg.repo_dir, base="main") == 0
    assert git.args_of("push") == [
        ("push", "origin", f"{NEXT_SHA}:refs/heads/main")
    ]
    assert "promoted" in capsys.readouterr().out


def test_promote_refuses_divergence(cfg, monkeypatch, capsys):
    git = FakeGit()
    git.divergence = "2\t3"
    monkeypatch.setattr(landing, "_git", git)

    assert landing.promote(cfg.repo_dir, base="main") == 2
    assert git.args_of("push") == []
    out = capsys.readouterr().out
    assert "not fast-forwardable" in out
    assert "2 commit(s)" in out and "3 commit(s)" in out  # the summary


def test_promote_nothing_to_promote(cfg, monkeypatch, capsys):
    git = FakeGit()
    git.divergence = "0\t0"
    monkeypatch.setattr(landing, "_git", git)

    assert landing.promote(cfg.repo_dir, base="main") == 0
    assert git.args_of("push") == []
    assert "nothing to promote" in capsys.readouterr().out


def test_promote_requires_remote_next(cfg, monkeypatch, capsys):
    git = FakeGit()
    git.next_exists = False
    monkeypatch.setattr(landing, "_git", git)

    assert landing.promote(cfg.repo_dir, base="main") == 1
    assert git.args_of("push") == []
    assert "does not exist" in capsys.readouterr().out


# --- conductor integration ------------------------------------------------------------------


def _succeeded_job_reap(cfg, monkeypatch, land, git_rc=0):
    """Drive conductor._reap_finished over one cleanly-exited job with
    every side boundary stubbed except the landing enqueue itself."""
    import marathon.conductor as conductor
    import marathon.review.daemon as daemon

    monkeypatch.setattr(daemon, "_handle_refine_exit", lambda *a, **k: 0)
    monkeypatch.setattr(conductor, "_remove_worktree", lambda *a: None)
    monkeypatch.setattr(conductor, "_regenerate_metadata_after_success", lambda c: None)

    def fake_git(repo_dir, *args):
        # The enqueue hook resolves the per-issue branch to a SHA.
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=git_rc,
            stdout="" if git_rc else SRC_SHA + "\n",
            stderr="fatal: unknown revision" if git_rc else "",
        )

    monkeypatch.setattr(conductor, "_git", fake_git)
    job = conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree="/x/wt", workdir="/x/wd", branch="marathon/refine-c14-i22",
        pid=4001, status="running", proc=SimpleNamespace(poll=lambda: 0),
    )
    reaped = conductor._reap_finished(cfg, [job], {}, {}, max_attempts=3, land=land)
    assert reaped == 1
    assert job.status == "succeeded"


def test_conductor_enqueues_only_under_land_next(cfg, monkeypatch):
    """Default (no --land): a successful job leaves no landing request —
    today's per-issue PR flow is unchanged. Under --land next the job is
    enqueued with the branch resolved to its commit SHA and the daemon
    flag set's gate mode."""
    qdir = cfg.repo_dir / landing.QUEUE_RELPATH

    _succeeded_job_reap(cfg, monkeypatch, land=None)
    assert not qdir.exists()

    _succeeded_job_reap(cfg, monkeypatch, land="next")
    [path] = sorted(qdir.glob("*.json"))
    data = json.loads(path.read_text())
    assert data["issue_num"] == 22
    assert data["chapter"] == 14
    assert data["source_ref"] == SRC_SHA  # SHA, not the resettable branch name
    assert data["workdir"] == "/x/wd"
    assert data["mode"] == "skeleton"  # daemon refine args are --skeleton
    assert data["attempts"] == 0


def test_conductor_skips_enqueue_when_branch_unresolvable(cfg, monkeypatch, capsys):
    """A branch that no longer resolves is SKIPPED, never enqueued by
    name: by pop time origin/<branch> may have been hard-reset to a
    different iteration, and the queue would land commits this job
    never produced."""
    _succeeded_job_reap(cfg, monkeypatch, land="next", git_rc=128)
    assert not (cfg.repo_dir / landing.QUEUE_RELPATH).exists()
    assert "skipped" in capsys.readouterr().out


def test_conductor_enqueue_failure_never_kills_the_loop(cfg, monkeypatch, capsys):
    monkeypatch.setattr(
        "marathon.landing.enqueue_landing",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    _succeeded_job_reap(cfg, monkeypatch, land="next")  # must not raise
    assert "landing enqueue for #22 failed" in capsys.readouterr().out


def test_cli_wires_landing_and_land_flag():
    from marathon.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["landing", "run", "--once", "--build-timeout", "900"])
    assert args.command == "landing" and callable(args.func)
    assert args.once and args.build_timeout == 900

    args = parser.parse_args(["landing", "promote", "--base", "develop"])
    assert args.base == "develop" and callable(args.func)

    args = parser.parse_args(["landing", "status"])
    assert callable(args.func)

    args = parser.parse_args(["conductor", "run", "--land", "next"])
    assert args.land == "next"
    args = parser.parse_args(["conductor", "run"])
    assert args.land is None  # opt-in: default off

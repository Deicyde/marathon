"""Conductor fill-dispatch: which verified-but-`sorry` issues get a fill this tick.
Excludes in-flight, backed-off, attempt-capped, and open-PR (pending-merge) issues."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import marathon.conductor as cond
from marathon.review.fill_discovery import FillTarget


@pytest.fixture(autouse=True)
def _complete_fake_worker_handshake(monkeypatch):
    monkeypatch.setattr(
        cond,
        "_await_worker_handshake",
        lambda _wt, proc, **_kw: {
            "pid": proc.pid, "pgid": proc.pid, "status": "running",
        },
    )


def _ft(issue, chapter=12):
    return FillTarget(issue=issue, chapter=chapter, decls=["d"])


def test_pick_fill_excludes_running_backoff_capped_and_open_pr():
    cfg = SimpleNamespace(github_repo="o/r")
    targets = {12: [_ft(101), _ft(102), _ft(103), _ft(104), _ft(105)]}
    picks = cond._pick_fill_dispatchable(
        cfg, {12},
        running_issues={101},               # 101 already running
        not_before={102: 2000.0},           # 102 backed off (future)
        fill_attempts={103: 3},             # 103 hit the attempt cap
        max_fill_attempts=3,
        _now_fn=lambda: 1000.0,
        _discover=lambda c, ch: targets.get(ch, []),
        _open_pr=lambda c, n, ch, **kw: n == 104,  # 104 has a fill PR pending merge
        _skeleton_done=lambda c, ch: True,
    )
    assert picks == [(105, 12)]


def test_pick_fill_skips_chapters_whose_skeleton_is_not_done():
    """A chapter is not proved until every registered statement is verified —
    even one still-churning issue blocks the whole chapter's fills."""
    cfg = SimpleNamespace(github_repo="o/r")
    targets = {11: [_ft(201, 11)], 12: [_ft(202, 12)]}
    picks = cond._pick_fill_dispatchable(
        cfg, {11, 12}, set(), {}, {}, 3,
        _discover=lambda c, ch: targets.get(ch, []),
        _open_pr=lambda c, n, ch, **kw: False,
        _skeleton_done=lambda c, ch: ch == 12,  # only 12 is skeleton-done
    )
    assert picks == [(202, 12)]  # chapter 11 skipped entirely


def test_pick_fill_empty_when_no_chapters_requested():
    cfg = SimpleNamespace(github_repo="o/r")
    assert cond._pick_fill_dispatchable(cfg, set(), set(), {}, {}, 3) == []


class _FakeDecl:
    def __init__(self, name, cone):
        self.name, self.kind, self.cone, self.axioms, self.has_sorry = \
            name, "theorem", cone, [], True


class _FakeSnap:
    def __init__(self, decls):
        self._b = {d.name: d for d in decls}

    def by_name(self):
        return self._b


def test_auto_audit_gates_on_throttle_and_head_move(tmp_path, monkeypatch):
    """Auto re-audit fires only when main MOVED since the last audit AND the
    throttle window has passed — so a downstream break surfaces, without
    re-auditing on every tick."""
    import marathon.deck.refresh as refresh_mod
    cfg = SimpleNamespace(repo_dir=tmp_path,
                          target_path_template="GA/LeeSM/Chapter{chapter}")
    calls = []
    monkeypatch.setattr(cond.subprocess, "Popen",
                        lambda *a, **k: calls.append(list(a[0])))
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda r: False)
    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: None)
    monkeypatch.setattr(refresh_mod, "head_sha", lambda r: "newhead123")
    I = cond.AUTO_AUDIT_MIN_INTERVAL_SECONDS

    # throttled (within the interval of last_time) → no audit
    h, t = cond._maybe_auto_audit(cfg, "old", last_time=100.0, _now_fn=lambda: 100.0 + I / 2)
    assert (h, t) == ("old", 100.0) and not calls

    # HEAD unmoved → no audit even past the throttle
    h, t = cond._maybe_auto_audit(cfg, "newhead123", last_time=0.0, _now_fn=lambda: 1e12)
    assert h == "newhead123" and not calls

    # HEAD moved + clean + throttle ok → spawn `marathon audit run --target …`
    h, t = cond._maybe_auto_audit(cfg, "old", last_time=0.0, _now_fn=lambda: 1e12)
    # Launch is not success: the durable cursor stays at the old revision.
    assert h == "old" and t == 1e12
    assert len(calls) == 1
    assert "audit" in calls[0] and "run" in calls[0] and "GA/LeeSM" in calls[0]


def test_auto_audit_skips_while_operator_editing(tmp_path, monkeypatch):
    """Never race the operator: skip the auto-audit while the checkout has
    uncommitted Lean work (but still reset the throttle)."""
    import marathon.deck.refresh as refresh_mod
    cfg = SimpleNamespace(repo_dir=tmp_path,
                          target_path_template="GA/LeeSM/Chapter{chapter}")
    calls = []
    monkeypatch.setattr(cond.subprocess, "Popen", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(refresh_mod, "head_sha", lambda r: "newhead")
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda r: True)  # mid-edit
    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: None)
    h, t = cond._maybe_auto_audit(cfg, "old", last_time=0.0, _now_fn=lambda: 1e12)
    assert h == "old"        # not audited
    assert t == 1e12         # throttle reset so we retry next window
    assert not calls         # no subprocess spawned


def test_auto_audit_advances_only_after_persisted_success(tmp_path, monkeypatch):
    """A completed+saved child advances the cursor on the next tick; merely
    spawning it never does (the merge/restart race from issue #10)."""
    import marathon.deck.refresh as refresh_mod

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    persisted = {"revision": None}
    calls = []
    monkeypatch.setattr(
        cond, "_persisted_audit_revision", lambda cfg: persisted["revision"]
    )
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda r: False)
    monkeypatch.setattr(refresh_mod, "head_sha", lambda r: "newhead")
    monkeypatch.setattr(
        cond.subprocess, "Popen", lambda *a, **k: calls.append(a)
    )

    head, last_time = cond._maybe_auto_audit(
        cfg, "oldhead", 0.0, _now_fn=lambda: 1e12
    )
    assert head == "oldhead"
    assert len(calls) == 1

    # Simulate the child's successful atomic save.  Recovery happens even
    # inside the throttle window and does not launch a second child.
    persisted["revision"] = "newhead"
    head, last_time_2 = cond._maybe_auto_audit(
        cfg, head, last_time, _now_fn=lambda: 1e12 + 1
    )
    assert (head, last_time_2) == ("newhead", last_time)
    assert len(calls) == 1


def test_auto_audit_failure_keeps_cursor_and_retries(tmp_path, monkeypatch):
    import marathon.deck.refresh as refresh_mod

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    calls = []
    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: "old")
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda r: False)
    monkeypatch.setattr(refresh_mod, "head_sha", lambda r: "new")
    monkeypatch.setattr(
        cond.subprocess, "Popen", lambda *a, **k: calls.append(a)
    )
    interval = cond.AUTO_AUDIT_MIN_INTERVAL_SECONDS

    head, when = cond._maybe_auto_audit(
        cfg, "old", 0.0, _now_fn=lambda: interval + 1
    )
    assert head == "old" and len(calls) == 1
    # No persisted success appeared.  Once the throttle expires, retry.
    head, _ = cond._maybe_auto_audit(
        cfg, head, when, _now_fn=lambda: when + interval + 1
    )
    assert head == "old" and len(calls) == 2


def test_persisted_audit_revision_requires_complete_same_revision_coverage(
    tmp_path, monkeypatch
):
    import marathon.audit.engine as audit_engine

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    monkeypatch.setattr(
        audit_engine, "derive_modules", lambda repo, target: (["A", "B"], [])
    )
    snapshot = SimpleNamespace(
        module_revisions={"A": "old", "B": "old"}
    )
    monkeypatch.setattr(
        audit_engine, "load_snapshot", lambda *a, **k: snapshot
    )
    assert cond._persisted_audit_revision(cfg) == "old"

    # A partial new audit must not become the all-project cursor.
    snapshot.module_revisions = {"A": "new", "B": "old"}
    assert cond._persisted_audit_revision(cfg) is None
    snapshot.module_revisions = {"A": "new"}
    assert cond._persisted_audit_revision(cfg) is None


def test_prover_control_file_roundtrip(tmp_path):
    """The deck's prover toggle: absent → no override (None); written → the
    boolean; stored in the self-ignoring runtime dir so it never leaks."""
    assert cond.read_prover_control(tmp_path) is None      # absent → honor default
    cond.set_prover_enabled(tmp_path, True)
    assert cond.read_prover_control(tmp_path) is True
    cond.set_prover_enabled(tmp_path, False)
    assert cond.read_prover_control(tmp_path) is False
    assert (tmp_path / ".marathon" / "conductor" / ".gitignore").read_text() == "*\n"


def test_read_prover_control_tolerates_malformed(tmp_path):
    import json
    p = cond.prover_control_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("not json at all")
    assert cond.read_prover_control(tmp_path) is None       # unparseable → no override
    p.write_text(json.dumps({"enabled": "yes"}))            # wrong type
    assert cond.read_prover_control(tmp_path) is None
    p.write_text(json.dumps({"other": 1}))                  # missing key
    assert cond.read_prover_control(tmp_path) is None


def test_pick_fill_orders_leaves_first_over_dependency_dag():
    """With an audit snapshot the picker returns fills LEAVES-FIRST over the
    dependency DAG — even when that inverts issue-number order. Here Root(#101)
    cones onto Leaf(#202), so Leaf must dispatch first despite its higher number."""
    cfg = SimpleNamespace(github_repo="o/r")
    snap = _FakeSnap([_FakeDecl("Leaf", []), _FakeDecl("Root", ["Leaf"])])
    targets = {12: [FillTarget(101, 12, ["Root"]), FillTarget(202, 12, ["Leaf"])]}
    common = dict(
        _discover=lambda c, ch: targets.get(ch, []),
        _open_pr=lambda c, n, ch, **kw: False,
        _skeleton_done=lambda c, ch: True,
    )
    picks = cond._pick_fill_dispatchable(cfg, {12}, set(), {}, {}, 3, _snapshot=snap, **common)
    assert picks == [(202, 12), (101, 12)]   # Leaf before Root — DAG order

    # Without a snapshot, it falls back to discovery's issue-number order.
    fallback = cond._pick_fill_dispatchable(cfg, {12}, set(), {}, {}, 3, _snapshot=None, **common)
    assert fallback == [(101, 12), (202, 12)]


def test_pick_fill_spans_chapters_sorted_by_issue():
    cfg = SimpleNamespace(github_repo="o/r")
    targets = {11: [_ft(201, 11)], 12: [_ft(202, 12)]}
    picks = cond._pick_fill_dispatchable(
        cfg, {11, 12}, set(), {}, {}, 3,
        _discover=lambda c, ch: targets.get(ch, []),
        _open_pr=lambda c, n, ch, **kw: False,
        _skeleton_done=lambda c, ch: True,
    )
    assert picks == [(201, 11), (202, 12)]


def test_pick_fill_expired_backoff_is_dispatchable():
    cfg = SimpleNamespace(github_repo="o/r")
    picks = cond._pick_fill_dispatchable(
        cfg, {12}, set(),
        not_before={108: 500.0},  # backoff in the past → eligible again
        fill_attempts={}, max_fill_attempts=3,
        _now_fn=lambda: 1000.0,
        _discover=lambda c, ch: [_ft(108)],
        _open_pr=lambda c, n, ch, **kw: False,
        _skeleton_done=lambda c, ch: True,
    )
    assert picks == [(108, 12)]


# --- proofs land on the prover branch, never main ---------------------------------


from marathon.post_pipeline import _branch_name_for_issue  # noqa: E402
from marathon.review.config import ReviewConfig, ReviewLabels  # noqa: E402


def test_proof_branch_name_distinct_from_refine():
    assert _branch_name_for_issue("Chapter14", 170, kind="proof") == \
        "marathon/proof-c14-i170"
    assert _branch_name_for_issue("Chapter14", 170) == "marathon/refine-c14-i170"


def _reviewcfg(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return ReviewConfig(
        repo_dir=repo, config_path=repo / ".marathon/review/config.toml",
        github_repo="o/r", parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(), chapters={},
    )


def test_dispatch_fill_routes_to_prover_branch_never_main(tmp_path, monkeypatch):
    """A fill branches from origin/<prover>, uses a proof- branch, and targets
    its PR at the prover branch — proofs never touch main."""
    cfg = _reviewcfg(tmp_path)
    git_calls = []

    def fake_git(repo_dir, *args):
        git_calls.append(list(args))
        if args[:2] == ("rev-parse", "--verify"):
            # No open repair branch on the remote — a fresh cycle.
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cond, "_git", fake_git)
    monkeypatch.setattr(cond, "_prime_mathlib_cache", lambda w: None)
    monkeypatch.setattr(cond, "_mathlib_already_built", lambda w: True)
    cmds = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            self.cmd = list(cmd)
            self.pid = 123
            cmds.append(self.cmd)

    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    job = cond._dispatch_job(
        cfg, 170, 14, tmp_path / "runs", {},
        fill=True, prover_branch="marathon/proving",
    )
    assert job is not None and job.fill is True
    assert job.branch == "marathon/proof-c14-i170"

    add = [c for c in git_calls if c[:2] == ["worktree", "add"]][0]
    assert add[-1] == "origin/marathon/proving"   # branched from the prover branch
    assert "marathon/proof-c14-i170" in add
    assert "origin/main" not in add               # NEVER main

    cmd = cmds[0]
    i = cmd.index("--auto-pr-base")
    assert cmd[i + 1] == "marathon/proving"        # PR targets the prover branch
    assert "--gate" in cmd and cmd[cmd.index("--gate") + 1] == "enforce"
    assert "--issue" in cmd and "170" in cmd


# --- repairs branch from main under a repair- name --------------------------------


def test_dispatch_repair_routes_to_main_repair_branch(tmp_path, monkeypatch):
    """A repair job branches from origin/main under `marathon/repair-c<N>`, runs
    `marathon repair` with the broken modules, and is flagged repair (not fill)."""
    cfg = _reviewcfg(tmp_path)
    git_calls = []

    def fake_git(repo_dir, *args):
        git_calls.append(list(args))
        if args[:2] == ("rev-parse", "--verify"):
            # No open repair branch on the remote — a fresh cycle.
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cond, "_git", fake_git)
    monkeypatch.setattr(cond, "_prime_mathlib_cache", lambda w: None)
    monkeypatch.setattr(cond, "_mathlib_already_built", lambda w: True)
    cmds = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            self.cmd = list(cmd)
            self.pid = 7
            cmds.append(self.cmd)

    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    job = cond._dispatch_job(
        cfg, cond.REPAIR_KEY_BASE + 16, 16, tmp_path / "runs", {},
        repair=True,
        repair_modules=["A.B.Chapter16.IntegrationManifold", "A.B.Chapter16.StokesTheorem"],
    )
    assert job is not None and job.repair is True and job.fill is False
    assert job.branch == "marathon/repair-c16"

    add = [c for c in git_calls if c[:2] == ["worktree", "add"]][0]
    assert add[-1] == "origin/main"           # repairs fork from main
    assert "marathon/repair-c16" in add

    cmd = cmds[0]
    assert "repair" in cmd
    assert cmd.count("--module") == 2
    assert "A.B.Chapter16.IntegrationManifold" in cmd
    assert "--auto-pr-base" not in cmd        # defaults to main (skeleton)


# --- informal-source (--tex) threading ---------------------------------------


def test_dispatch_passes_tex_when_configured(tmp_path, monkeypatch):
    """With tex_dir configured and the chapter's source present, every
    dispatched job carries --tex <path> (the Aristotle bundle tracks the
    informal textbook; Claude never sees it)."""
    texdir = tmp_path / "LeeSM-LaTeX"
    texdir.mkdir()
    (texdir / "c16.tex").write_text("\\chapter{Integration on Manifolds}")

    import dataclasses

    cfg = dataclasses.replace(_reviewcfg(tmp_path), tex_dir=texdir)  # real field

    monkeypatch.setattr(cond, "_git", lambda repo, *a: SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(cond, "_prime_mathlib_cache", lambda w: None)
    monkeypatch.setattr(cond, "_mathlib_already_built", lambda w: True)
    cmds = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            self.cmd = list(cmd)
            self.pid = 8
            cmds.append(self.cmd)

    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    job = cond._dispatch_job(
        cfg, cond.REPAIR_KEY_BASE + 16, 16, tmp_path / "runs", {},
        repair=True, repair_modules=["A.Chapter16.M"],
    )
    assert job is not None
    cmd = cmds[0]
    assert "--tex" in cmd
    assert cmd[cmd.index("--tex") + 1] == str(texdir / "c16.tex")

    # Chapter without a source file → no --tex (pre-tex behavior).
    job2 = cond._dispatch_job(
        cfg, cond.REPAIR_KEY_BASE + 15, 15, tmp_path / "runs", {},
        repair=True, repair_modules=["A.Chapter15.M"],
    )
    assert job2 is not None and "--tex" not in cmds[1]


def test_tex_args_tolerates_old_config():
    """A cfg without tex_path (older ReviewConfig) yields [] — never a
    blocked dispatch."""
    assert cond._tex_args(SimpleNamespace(), 16) == []


def test_repair_retry_continues_open_branch(tmp_path, monkeypatch):
    """A repair retry stacks on the branch's remote head when it exists (an
    open [build:FAIL] PR holds prior partial progress); a fresh cycle forks
    origin/main."""
    cfg = _reviewcfg(tmp_path)
    git_calls = []

    def fake_git(repo_dir, *args):
        git_calls.append(list(args))
        if args[:2] == ("rev-parse", "--verify"):
            return SimpleNamespace(returncode=0, stdout="abc\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cond, "_git", fake_git)
    monkeypatch.setattr(cond, "_prime_mathlib_cache", lambda w: None)
    monkeypatch.setattr(cond, "_mathlib_already_built", lambda w: True)

    class FakePopen:
        def __init__(self, cmd, **kw):
            self.cmd = list(cmd)
            self.pid = 9

    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    job = cond._dispatch_job(
        cfg, cond.REPAIR_KEY_BASE + 16, 16, tmp_path / "runs", {},
        repair=True, repair_modules=["A.Chapter16.M"],
    )
    assert job is not None
    add = [c for c in git_calls if c[:2] == ["worktree", "add"]][0]
    assert add[-1] == "origin/marathon/repair-c16"   # stacked, not origin/main


def test_repair_fresh_cycle_forks_main(tmp_path, monkeypatch):
    cfg = _reviewcfg(tmp_path)
    git_calls = []

    def fake_git(repo_dir, *args):
        git_calls.append(list(args))
        if args[:2] == ("rev-parse", "--verify"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # no branch
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cond, "_git", fake_git)
    monkeypatch.setattr(cond, "_prime_mathlib_cache", lambda w: None)
    monkeypatch.setattr(cond, "_mathlib_already_built", lambda w: True)

    class FakePopen:
        def __init__(self, cmd, **kw):
            self.cmd = list(cmd)
            self.pid = 9

    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    job = cond._dispatch_job(
        cfg, cond.REPAIR_KEY_BASE + 16, 16, tmp_path / "runs", {},
        repair=True, repair_modules=["A.Chapter16.M"],
    )
    assert job is not None
    add = [c for c in git_calls if c[:2] == ["worktree", "add"]][0]
    assert add[-1] == "origin/main"

"""Conductor fill-dispatch: which verified-but-`sorry` issues get a fill this tick.
Excludes in-flight, backed-off, attempt-capped, and open-PR (pending-merge) issues."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import marathon.conductor as cond
from marathon.review.fill_discovery import FillTarget


@pytest.fixture(autouse=True)
def _complete_fake_worker_handshake(monkeypatch):
    cond._AUTO_AUDIT_RECENT_TIMED_HEADS.clear()
    monkeypatch.setattr(
        cond,
        "_await_worker_handshake",
        lambda _wt, proc, **_kw: {
            "pid": proc.pid, "pgid": proc.pid, "status": "running",
        },
    )
    yield
    cond._AUTO_AUDIT_RECENT_TIMED_HEADS.clear()


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


def _auto_audit_guard_payload(
    repo, *, head="new", target="GA/LeeSM", launched_at_epoch=1_800_000_000.0,
    schema=None,
):
    schema = cond.AUTO_AUDIT_GUARD_SCHEMA_VERSION if schema is None else schema
    payload = {
        "schema_version": schema,
        "status": "running",
        "launcher_pid": os.getpid(),
        "pid": 9876,
        "head": head,
        "target": target,
        "repo_dir": str(repo.resolve()),
        "launched_at": datetime.fromtimestamp(
            launched_at_epoch, timezone.utc
        ).isoformat(),
        "command": cond._auto_audit_command(repo, target),
    }
    if schema >= 2:
        payload["launched_at_epoch"] = launched_at_epoch
    return payload


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
    monkeypatch.setattr(cond, "_live_legacy_auto_audit_pids", lambda *a: [])
    monkeypatch.setattr(refresh_mod, "head_sha", lambda r: "newhead123")
    I = cond.AUTO_AUDIT_MIN_INTERVAL_SECONDS

    # A retry clock known to belong to this exact HEAD throttles it.
    identity = cond._auto_audit_identity(tmp_path, "GA/LeeSM")
    cond._AUTO_AUDIT_RECENT_TIMED_HEADS[identity] = "newhead123"
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
    monkeypatch.setattr(cond, "_live_legacy_auto_audit_pids", lambda *a: [])
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


def test_auto_audit_failure_retries_only_after_durable_cooldown(
    tmp_path, monkeypatch,
):
    import marathon.deck.refresh as refresh_mod

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    calls = []
    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: "old")
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda r: False)
    monkeypatch.setattr(cond, "_live_legacy_auto_audit_pids", lambda *a: [])
    monkeypatch.setattr(refresh_mod, "head_sha", lambda r: "new")
    class FakePopen:
        pid = 9876

        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)
    interval = cond.AUTO_AUDIT_MIN_INTERVAL_SECONDS
    started = 1_800_000_000.0

    head, when = cond._maybe_auto_audit(
        cfg, "old", 0.0, _now_fn=lambda: started
    )
    assert head == "old" and len(calls) == 1
    # No persisted success appeared. Even a restarted conductor whose memory
    # throttle is empty must not repeat the same expensive HEAD after 15m.
    head, deferred_at = cond._maybe_auto_audit(
        cfg, head, float("-inf"), _now_fn=lambda: when + interval + 1
    )
    assert head == "old" and deferred_at == when + interval + 1
    assert len(calls) == 1

    # The same metadata is fail-open once its bounded 6h cooldown expires.
    head, _ = cond._maybe_auto_audit(
        cfg,
        head,
        float("-inf"),
        _now_fn=lambda: when
        + cond.AUTO_AUDIT_FAILED_ATTEMPT_COOLDOWN_SECONDS
        + 1,
    )
    assert head == "old" and len(calls) == 2


@pytest.mark.parametrize("schema", [1, 2])
def test_auto_audit_restart_suppresses_recent_same_head_attempt(
    tmp_path, monkeypatch, schema,
):
    """The on-disk attempt clock survives a conductor's empty restart state.

    Schema 1 covers the guard already present on the live host at upgrade time;
    schema 2 is what this conductor writes on subsequent launches.
    """
    import marathon.deck.refresh as refresh_mod

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    now = cond._now()
    marker = cond.auto_audit_guard_path(tmp_path)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps(_auto_audit_guard_payload(
        tmp_path, head="new", launched_at_epoch=now, schema=schema,
    )))
    calls = []
    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: "old")
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda repo: False)
    monkeypatch.setattr(
        cond, "_live_legacy_auto_audit_pids",
        lambda *args: pytest.fail("cooldown should precede legacy scanning"),
    )
    monkeypatch.setattr(refresh_mod, "head_sha", lambda repo: "new")
    monkeypatch.setattr(
        cond.subprocess, "Popen", lambda *a, **k: calls.append(a)
    )

    head, when = cond._maybe_auto_audit(
        cfg, "old", float("-inf"), _now_fn=lambda: now + 60
    )
    assert (head, when) == ("old", now + 60)
    assert calls == []


def test_auto_audit_changed_head_bypasses_recent_attempt_cooldown(
    tmp_path, monkeypatch,
):
    import marathon.deck.refresh as refresh_mod

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    now = cond._now()
    marker = cond.auto_audit_guard_path(tmp_path)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps(_auto_audit_guard_payload(
        tmp_path, head="prior-head", launched_at_epoch=now,
    )))
    calls = []

    class FakePopen:
        pid = 9877

        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: "old")
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda repo: False)
    monkeypatch.setattr(cond, "_live_legacy_auto_audit_pids", lambda *args: [])
    monkeypatch.setattr(refresh_mod, "head_sha", lambda repo: "new-head")
    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    head, when = cond._maybe_auto_audit(
        cfg, "old", float("-inf"), _now_fn=lambda: now + 60
    )
    assert (head, when) == ("old", now + 60)
    assert len(calls) == 1
    assert json.loads(marker.read_text())["head"] == "new-head"


def test_auto_audit_new_head_bypasses_throttle_set_by_cooldown(
    tmp_path, monkeypatch,
):
    """A same-HEAD deferral must not make a later revision wait 15 minutes."""
    import marathon.deck.refresh as refresh_mod

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    now = cond._now()
    marker = cond.auto_audit_guard_path(tmp_path)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps(_auto_audit_guard_payload(
        tmp_path, head="head-a", launched_at_epoch=now,
    )))
    current = {"head": "head-a"}
    calls = []

    class FakePopen:
        pid = 9879

        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: "old")
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda repo: False)
    monkeypatch.setattr(cond, "_live_legacy_auto_audit_pids", lambda *args: [])
    monkeypatch.setattr(refresh_mod, "head_sha", lambda repo: current["head"])
    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    head, deferred_at = cond._maybe_auto_audit(
        cfg, "old", float("-inf"), _now_fn=lambda: now + 60
    )
    assert head == "old" and calls == []

    current["head"] = "head-b"
    head, launched_at = cond._maybe_auto_audit(
        cfg, head, deferred_at, _now_fn=lambda: now + 61
    )
    assert (head, launched_at) == ("old", now + 61)
    assert len(calls) == 1
    assert json.loads(marker.read_text())["head"] == "head-b"


@pytest.mark.parametrize(
    "case",
    [
        "expired", "malformed", "oversized", "wrong-schema",
        "foreign-repo", "foreign-target", "unknown-field",
        "contradictory-time", "duplicate-option", "extra-option",
        "foreign-interpreter",
    ],
)
def test_auto_audit_untrusted_guard_metadata_never_suppresses(
    tmp_path, monkeypatch, case,
):
    import marathon.deck.refresh as refresh_mod

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    now = cond._now()
    marker = cond.auto_audit_guard_path(tmp_path)
    marker.parent.mkdir(parents=True)
    payload = _auto_audit_guard_payload(
        tmp_path, head="new", launched_at_epoch=now,
    )
    if case == "expired":
        payload["launched_at_epoch"] = (
            now - cond.AUTO_AUDIT_FAILED_ATTEMPT_COOLDOWN_SECONDS - 1
        )
        raw = json.dumps(payload).encode()
    elif case == "malformed":
        raw = b"{not-json"
    elif case == "oversized":
        raw = b"x" * (cond.AUTO_AUDIT_GUARD_MAX_BYTES + 1)
    elif case == "wrong-schema":
        payload["schema_version"] = 999
        raw = json.dumps(payload).encode()
    elif case == "foreign-repo":
        payload["repo_dir"] = str((tmp_path / "foreign").resolve())
        raw = json.dumps(payload).encode()
    elif case == "foreign-target":
        payload["target"] = "GA/Elsewhere"
        raw = json.dumps(payload).encode()
    elif case == "contradictory-time":
        payload["launched_at"] = "2020-01-01T00:00:00+00:00"
        raw = json.dumps(payload).encode()
    elif case == "duplicate-option":
        payload["command"].extend(["--repo-dir", str(tmp_path / "foreign")])
        raw = json.dumps(payload).encode()
    elif case == "extra-option":
        payload["command"].extend(["--timeout", "1"])
        raw = json.dumps(payload).encode()
    elif case == "foreign-interpreter":
        payload["command"][0] = str(tmp_path / "python-foreign")
        raw = json.dumps(payload).encode()
    else:
        payload["unexpected"] = True
        raw = json.dumps(payload).encode()
    marker.write_bytes(raw)
    calls = []

    class FakePopen:
        pid = 9878

        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: "old")
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda repo: False)
    monkeypatch.setattr(cond, "_live_legacy_auto_audit_pids", lambda *args: [])
    monkeypatch.setattr(refresh_mod, "head_sha", lambda repo: "new")
    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    head, when = cond._maybe_auto_audit(
        cfg, "old", float("-inf"), _now_fn=lambda: now + 60
    )
    assert (head, when) == ("old", now + 60)
    assert len(calls) == 1


def test_malformed_guard_throttles_same_head_but_not_changed_head(
    tmp_path, monkeypatch,
):
    """The in-process clock remembers its revision independently of the
    durable diagnostics: corrupting those diagnostics cannot cause a hot retry
    of the same HEAD or delay a different HEAD that lands moments later."""
    import marathon.deck.refresh as refresh_mod

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    current = {"head": "head-a"}
    calls = []

    class FakePopen:
        pid = 9880

        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: "old")
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda repo: False)
    monkeypatch.setattr(cond, "_live_legacy_auto_audit_pids", lambda *args: [])
    monkeypatch.setattr(refresh_mod, "head_sha", lambda repo: current["head"])
    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    head, launched_at = cond._maybe_auto_audit(
        cfg, "old", float("-inf"), _now_fn=lambda: 1_800_000_000.0
    )
    assert head == "old" and len(calls) == 1
    marker = cond.auto_audit_guard_path(tmp_path)
    marker.write_text("{broken")

    # The same revision remains protected even though its durable record was
    # corrupted after launch.
    head, same_time = cond._maybe_auto_audit(
        cfg, head, launched_at, _now_fn=lambda: launched_at + 1
    )
    assert (head, same_time) == ("old", launched_at)
    assert len(calls) == 1

    # A new revision does not inherit head-a's retry clock merely because the
    # durable record is unreadable.
    current["head"] = "head-b"
    head, changed_time = cond._maybe_auto_audit(
        cfg, head, same_time, _now_fn=lambda: launched_at + 2
    )
    assert (head, changed_time) == ("old", launched_at + 2)
    assert len(calls) == 2


@pytest.mark.skipif(os.name == "nt", reason="auto-audit lease uses POSIX flock")
def test_auto_audit_busy_guard_is_restart_safe_and_does_not_move_throttle(
    tmp_path, monkeypatch
):
    """A child inherited lease survives its launcher's fd close.  A replacement
    conductor cannot launch beside it; normal exit releases the kernel state
    even though the stable guard file and its stale metadata remain."""
    guard, status = cond._try_acquire_auto_audit_guard(tmp_path)
    assert status == "acquired" and guard is not None
    # Even perfectly matching recent metadata cannot replace the authoritative
    # kernel liveness check: while the child owns the flock this stays busy.
    cond._write_auto_audit_guard_metadata(
        guard,
        _auto_audit_guard_payload(
            tmp_path, head="new", launched_at_epoch=cond._now(),
        ),
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        pass_fds=(guard.fileno(),),
        start_new_session=True,
    )
    guard.close()  # simulate the conductor exiting immediately after spawn
    try:
        replacement, status = cond._try_acquire_auto_audit_guard(tmp_path)
        assert replacement is None and status == "busy"

        import marathon.deck.refresh as refresh_mod

        cfg = SimpleNamespace(
            repo_dir=tmp_path,
            target_path_template="GA/LeeSM/Chapter{chapter}",
        )
        calls = []
        monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: "old")
        monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda repo: False)
        monkeypatch.setattr(refresh_mod, "head_sha", lambda repo: "new")
        monkeypatch.setattr(cond.subprocess, "Popen", lambda *a, **k: calls.append(a))
        head, when = cond._maybe_auto_audit(
            cfg, "old", 123.0, _now_fn=lambda: 1e12
        )
        assert (head, when) == ("old", 123.0)
        assert calls == []
    finally:
        child.terminate()
        child.wait(timeout=10)

    recovered, status = cond._try_acquire_auto_audit_guard(tmp_path)
    assert status == "acquired" and recovered is not None
    recovered.close()


def test_auto_audit_adopts_exact_live_legacy_process(tmp_path, monkeypatch):
    """The first upgraded conductor waits for an old pre-flock audit rather
    than creating a duplicate.  Waiting does not consume the retry throttle."""
    import marathon.deck.refresh as refresh_mod

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    calls = []
    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: "old")
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda repo: False)
    monkeypatch.setattr(refresh_mod, "head_sha", lambda repo: "new")
    monkeypatch.setattr(cond, "_live_legacy_auto_audit_pids", lambda *a: [4242])
    monkeypatch.setattr(cond.subprocess, "Popen", lambda *a, **k: calls.append(a))

    head, when = cond._maybe_auto_audit(
        cfg, "old", 123.0, _now_fn=lambda: 1e12
    )
    assert (head, when) == ("old", 123.0)
    assert calls == []


def test_live_legacy_auto_audit_scan_requires_exact_identity(tmp_path, monkeypatch):
    repo = tmp_path.resolve()
    command = (
        f"/usr/bin/python3 -m marathon audit run --target GA/LeeSM "
        f"--repo-dir {repo}"
    )
    monkeypatch.setattr(
        cond.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=(
                f"101 S {command}\n"                    # exact live worker
                f"102 Z {command}\n"                    # completed zombie
                f"103 S {command}Elsewhere\n"           # wrong repo
                f"104 S /usr/bin/python3 -m marathon fill --repo-dir {repo}\n"
            ),
        ),
    )
    assert cond._live_legacy_auto_audit_pids(repo, "GA/LeeSM") == [101]


def test_auto_audit_child_inherits_guard_and_records_diagnostics(
    tmp_path, monkeypatch
):
    import marathon.deck.refresh as refresh_mod

    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        target_path_template="GA/LeeSM/Chapter{chapter}",
    )
    captured = {}

    class FakePopen:
        pid = 9876

        def __init__(self, command, **kwargs):
            captured["command"] = list(command)
            captured["kwargs"] = kwargs

    monkeypatch.setattr(cond, "_persisted_audit_revision", lambda cfg: "old")
    monkeypatch.setattr(cond, "_has_uncommitted_lean", lambda repo: False)
    monkeypatch.setattr(cond, "_live_legacy_auto_audit_pids", lambda *a: [])
    monkeypatch.setattr(refresh_mod, "head_sha", lambda repo: "new")
    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    head, when = cond._maybe_auto_audit(
        cfg, "old", 0.0, _now_fn=lambda: 1e12
    )
    assert head == "old" and when == 1e12
    inherited = captured["kwargs"]["pass_fds"]
    assert len(inherited) == 1 and isinstance(inherited[0], int)

    marker = cond.auto_audit_guard_path(tmp_path)
    payload = json.loads(marker.read_text())
    assert payload["status"] == "running"
    assert payload["pid"] == 9876
    assert payload["head"] == "new"

    # The fake child inherited nothing, so stale diagnostic contents alone do
    # not block the next launch.
    recovered, status = cond._try_acquire_auto_audit_guard(tmp_path)
    assert status == "acquired" and recovered is not None
    recovered.close()


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

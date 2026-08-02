"""The hosted-deck repo sync loop (marathon.deck.sync): fast-forward git pull,
fail-soft, audit-age note. All git calls are monkeypatched."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import marathon.deck.sync as sync


def _fake_git(seq):
    """Return a fake _git that pops (returncode, stdout, stderr) per call."""
    calls = list(seq)

    def _g(repo_dir, *args, timeout=90):
        rc, out, err = calls.pop(0)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    return _g


def _on_main(*rest):
    """Prefix the branch-guard calls (origin/HEAD → main, current → main) onto a
    pull_once sequence so the checkout reads as ON the review branch."""
    return [(0, "origin/main\n", ""), (0, "main\n", "")] + list(rest)


def test_pull_once_up_to_date(monkeypatch, tmp_path):
    # branch guard (on main) → rev-parse before/after same; pull --ff-only ok
    monkeypatch.setattr(sync, "_git", _fake_git(_on_main(
        (0, "abc123\n", ""),     # before HEAD
        (0, "", ""),             # pull --ff-only
        (0, "abc123\n", ""),     # after HEAD
    )))
    assert sync.pull_once(tmp_path) == "up to date"


def test_pull_once_updated(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_git", _fake_git(_on_main(
        (0, "aaaaaaa\n", ""),    # before
        (0, "", ""),             # pull
        (0, "bbbbbbb\n", ""),    # after
    )))
    assert sync.pull_once(tmp_path) == "updated aaaaaaa → bbbbbbb"


def test_pull_once_non_ff_operator_commit_is_not_reset(monkeypatch, tmp_path):
    # a non-ff divergence whose local-ahead commit is NOT a marathon metadata
    # commit is reported and left ALONE — operator work is never reset away.
    monkeypatch.setattr(sync, "_git", _fake_git(_on_main(
        (0, "aaaaaaa\n", ""),                            # before (rev-parse HEAD)
        (1, "", "fatal: Not possible to fast-forward"),  # pull --ff-only fails
        (0, "", ""),                                     # recovery: fetch origin
        (0, "origin/main\n", ""),                        # recovery: rev-parse @{u}
        (0, "operator: hand-written work\n", ""),        # recovery: log @{u}..HEAD
    )))
    assert "NOT resetting" in sync.pull_once(tmp_path)


# --- the review-branch guard (off-branch → loud warn; warn-only, never switch) -

def test_review_branch_from_origin_head(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_git", _fake_git([(0, "origin/main\n", "")]))
    assert sync._review_branch(tmp_path) == "main"


def test_review_branch_fallback_when_origin_head_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_git", _fake_git([(1, "", "no origin/HEAD")]))
    assert sync._review_branch(tmp_path) == "main"


def test_current_branch_detached_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_git", _fake_git([(0, "HEAD\n", "")]))
    assert sync._current_branch(tmp_path) == ""


def test_ensure_on_base_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_git", _fake_git([(0, "main\n", "")]))  # current == base
    assert sync._ensure_on_review_branch(tmp_path, "main") is None


def test_ensure_off_branch_warns_loudly(monkeypatch, tmp_path):
    # warn-only: off the review branch → a loud ⚠ STALE line + the fix command;
    # the guard NEVER switches the branch (ONE _git call — no fetch/stash/checkout).
    monkeypatch.setattr(sync, "_git", _fake_git([(0, "fix/ch15-pullback\n", "")]))
    out = sync._ensure_on_review_branch(tmp_path, "main")
    assert "STALE" in out and "fix/ch15-pullback" in out and "checkout main" in out


def test_ensure_detached_head_warns(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_git", _fake_git([(0, "HEAD\n", "")]))
    out = sync._ensure_on_review_branch(tmp_path, "main")
    assert "STALE" in out and "detached" in out and "checkout main" in out


def test_pull_once_off_branch_warns_not_switch(monkeypatch, tmp_path):
    # end to end: origin/HEAD → main, current → feature → STALE warn, no pull.
    monkeypatch.setattr(sync, "_git", _fake_git([
        (0, "origin/main\n", ""),      # _review_branch
        (0, "feature\n", ""),          # _current_branch (OFF the review branch)
    ]))
    out = sync.pull_once(tmp_path)
    assert "STALE" in out and "feature" in out


# --- loss-free stash-pop conflict resolution (verdicts.jsonl never discarded) --

def test_union_lines_preserves_all_dedup_in_order():
    # upstream {1,2} + stashed-local {1,3} → {1,2,3}; every verdict survives.
    assert sync._union_lines('{"v":1}\n{"v":2}\n', '{"v":1}\n{"v":3}\n') == \
        '{"v":1}\n{"v":2}\n{"v":3}\n'


def test_union_lines_empty():
    assert sync._union_lines("", "") == ""


def test_resolve_pop_conflicts_unions_verdicts_no_loss(monkeypatch, tmp_path):
    # THE data-loss scenario the review caught: verdicts.jsonl conflicts on a
    # stash pop; the local "C" verdict must NOT be discarded.
    monkeypatch.setattr(sync, "_git", _fake_git([
        (0, ".marathon/review/verdicts.jsonl\n", ""),   # diff --diff-filter=U
        (0, '{"v":"A"}\n{"v":"B"}\n', ""),              # show :2 (upstream)
        (0, '{"v":"A"}\n{"v":"C"}\n', ""),              # show :3 (stashed local)
        (0, "", ""),                                    # add
    ]))
    (tmp_path / ".marathon" / "review").mkdir(parents=True)
    assert sync._resolve_pop_conflicts(tmp_path) == (1, 0)
    written = (tmp_path / ".marathon" / "review" / "verdicts.jsonl").read_text()
    assert written == '{"v":"A"}\n{"v":"B"}\n{"v":"C"}\n'   # local "C" preserved


def test_resolve_pop_conflicts_keeps_upstream_for_state(monkeypatch, tmp_path):
    calls = []

    def g(repo_dir, *args, timeout=90):
        calls.append(args)
        if args[:1] == ("diff",):
            return SimpleNamespace(
                returncode=0, stdout=".marathon/review/state.json\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sync, "_git", g)
    assert sync._resolve_pop_conflicts(tmp_path) == (0, 1)   # rederivable → upstream
    assert any(a[:2] == ("checkout", "HEAD") for a in calls)


def test_resolve_pop_conflicts_no_conflicts_resets(monkeypatch, tmp_path):
    calls = []

    def g(repo_dir, *args, timeout=90):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sync, "_git", g)
    assert sync._resolve_pop_conflicts(tmp_path) == (0, 0)
    assert any(a[:2] == ("reset", "--hard") for a in calls)


def test_resolve_pop_conflicts_real_git_unions_no_verdict_lost(tmp_path):
    """Real git, the actual data-loss scenario: a genuine stash-pop CONFLICT on
    verdicts.jsonl from divergent appends. Both the local and upstream verdict
    must survive and the conflict must be fully resolved (no markers)."""
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    vdir = repo / ".marathon" / "review"
    vdir.mkdir(parents=True)
    f = vdir / "verdicts.jsonl"
    f.write_text('{"v":"base"}\n')
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    # local (stashed) append, then a DIVERGENT upstream append → real conflict
    f.write_text('{"v":"base"}\n{"v":"LOCAL"}\n')
    git("stash", "push", "-q")
    f.write_text('{"v":"base"}\n{"v":"UPSTREAM"}\n')
    git("add", "-A")
    git("commit", "-q", "-m", "upstream")
    assert git("stash", "pop").returncode != 0          # genuinely conflicted

    n_union, n_keep = sync._resolve_pop_conflicts(repo)
    git("stash", "drop")

    assert n_union == 1
    text = f.read_text()
    assert '{"v":"LOCAL"}' in text                       # the local verdict NOT lost
    assert '{"v":"UPSTREAM"}' in text and '{"v":"base"}' in text
    assert "<<<<<<<" not in text and ">>>>>>>" not in text   # conflict resolved


def test_pull_once_failsoft_on_exception(monkeypatch, tmp_path):
    def _boom(repo_dir, *a, **k):
        raise OSError("git missing")

    monkeypatch.setattr(sync, "_git", _boom)
    assert sync.pull_once(tmp_path).startswith("sync error:")   # never raises


def test_audit_age_note_none_when_absent(tmp_path):
    assert "none" in sync.audit_age_note(tmp_path)              # no snapshot file


def test_audit_age_note_reports_age(tmp_path):
    p = tmp_path / ".marathon" / "audit" / "latest.json"
    p.parent.mkdir(parents=True)
    p.write_text("{}")
    assert "old" in sync.audit_age_note(tmp_path)               # has a snapshot


def test_start_repo_sync_noop_when_disabled(tmp_path):
    assert sync.start_repo_sync(tmp_path, 0) is None
    assert sync.start_repo_sync(tmp_path, -5) is None


def test_repo_sync_notifies_derived_artifacts_after_source_advance(
    monkeypatch, tmp_path,
):
    changed = threading.Event()
    slept = threading.Event()
    halt = threading.Event()
    monkeypatch.setattr(sync, "pull_once", lambda _repo: "updated abc1234 → def5678")

    def one_long_sleep(_interval):
        slept.set()
        halt.wait()

    monkeypatch.setattr(sync.time, "sleep", one_long_sleep)
    thread = sync.start_repo_sync(
        tmp_path, 60, log=lambda _line: None, on_change=changed.set
    )
    assert thread is not None
    assert changed.wait(1)
    assert slept.wait(1)


def test_repo_sync_survives_a_failing_change_callback(monkeypatch, tmp_path):
    slept = threading.Event()
    hold = threading.Event()
    monkeypatch.setattr(sync, "pull_once", lambda _repo: "updated abc1234 → def5678")

    def broken_callback():
        raise RuntimeError("semantic prewarm failed")

    def observe_sleep(_interval):
        slept.set()
        hold.wait()

    monkeypatch.setattr(sync.time, "sleep", observe_sleep)
    thread = sync.start_repo_sync(
        tmp_path, 60, log=lambda _line: None, on_change=broken_callback
    )
    assert thread is not None
    # Reaching sleep proves the callback exception stayed inside the live loop.
    assert slept.wait(1)


def test_refresh_audit_from_local_path(tmp_path):
    src = tmp_path / "published.json"
    src.write_text('{"schema":1,"decls":[]}')
    repo = tmp_path / "repo"
    repo.mkdir()
    assert sync.refresh_audit(repo, str(src)) == "audit refreshed"
    dest = repo / ".marathon" / "audit" / "latest.json"
    assert dest.read_text() == '{"schema":1,"decls":[]}'    # atomic copy landed


def test_refresh_audit_rejects_non_json(tmp_path):
    src = tmp_path / "garbage.json"
    src.write_text("not json {{{")
    repo = tmp_path / "repo"
    repo.mkdir()
    out = sync.refresh_audit(repo, str(src))
    assert out.startswith("audit refresh failed")           # validation rejected it
    assert not (repo / ".marathon" / "audit" / "latest.json").exists()  # live file untouched


def test_refresh_audit_failsoft_missing_source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = sync.refresh_audit(repo, str(tmp_path / "nope.json"))
    assert out.startswith("audit refresh failed")           # never raises


def test_refresh_audit_keeps_existing_on_failure(tmp_path):
    repo = tmp_path / "repo"
    dest = repo / ".marathon" / "audit" / "latest.json"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"good":"prior"}')
    sync.refresh_audit(repo, str(tmp_path / "missing.json"))
    assert dest.read_text() == '{"good":"prior"}'           # prior snapshot preserved

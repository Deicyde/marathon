"""The hosted-deck repo sync loop (marathon.deck.sync): fast-forward git pull,
fail-soft, audit-age note. All git calls are monkeypatched."""
from __future__ import annotations

from types import SimpleNamespace

import marathon.deck.sync as sync


def _fake_git(seq):
    """Return a fake _git that pops (returncode, stdout, stderr) per call."""
    calls = list(seq)

    def _g(repo_dir, *args, timeout=90):
        rc, out, err = calls.pop(0)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    return _g


def test_pull_once_up_to_date(monkeypatch, tmp_path):
    # rev-parse HEAD (before) → same sha; pull --ff-only ok; rev-parse (after) → same
    monkeypatch.setattr(sync, "_git", _fake_git([
        (0, "abc123\n", ""),     # before HEAD
        (0, "", ""),             # pull --ff-only
        (0, "abc123\n", ""),     # after HEAD
    ]))
    assert sync.pull_once(tmp_path) == "up to date"


def test_pull_once_updated(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_git", _fake_git([
        (0, "aaaaaaa\n", ""),    # before
        (0, "", ""),             # pull
        (0, "bbbbbbb\n", ""),    # after
    ]))
    assert sync.pull_once(tmp_path) == "updated aaaaaaa → bbbbbbb"


def test_pull_once_non_ff_is_reported_not_forced(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_git", _fake_git([
        (0, "aaaaaaa\n", ""),                          # before
        (1, "", "fatal: Not possible to fast-forward"),  # pull fails
    ]))
    out = sync.pull_once(tmp_path)
    assert out.startswith("pull failed (ff-only):") and "fast-forward" in out


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

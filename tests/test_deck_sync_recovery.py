"""Deck sync self-heals a marathon-metadata divergence — the stale-checkout bug
that left the deck frozen behind origin/main and made a reviewer re-reject an
already-fixed decl. It must NOT touch operator commits, and must preserve tracked
review state across the reset."""
from __future__ import annotations

import subprocess

from marathon.deck.sync import pull_once


def _git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)


def _setup(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git(repo, "config", k, v)
    (repo / "code.lean").write_text("theorem a : True := by sorry\n")
    (repo / "config.toml").write_text("entries = []\n")  # tracked "review state"
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "branch", "--set-upstream-to=origin/main", "main")
    return origin, repo


def _advance_origin(origin, tmp_path, content):
    """A second clone pushes a new commit to origin (simulating an automerge)."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    for k, v in (("user.email", "o@o"), ("user.name", "o")):
        _git(other, "config", k, v)
    (other / "code.lean").write_text(content)
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "upstream advance")
    _git(other, "push", "-q", "origin", "main")


def test_recovers_from_marathon_metadata_divergence(tmp_path):
    origin, repo = _setup(tmp_path)
    _advance_origin(origin, tmp_path, "theorem a : True := by exact trivial\n")  # the fix lands
    # local makes a marathon metadata commit (diverges) + an uncommitted review edit
    (repo / "formalization.yaml").write_text("x: 1\n")
    _git(repo, "add", "formalization.yaml")
    _git(repo, "commit", "-q", "-m", "chore(marathon): regenerate formalization.yaml")
    (repo / "config.toml").write_text("entries = [15]\n")  # uncommitted tracked review state

    msg = pull_once(repo)

    assert "recovered" in msg, msg
    assert "by exact trivial" in (repo / "code.lean").read_text()  # tree now shows the fix
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    upstream = _git(repo, "rev-parse", "origin/main").stdout.strip()
    assert head == upstream                                        # HEAD == origin/main
    assert (repo / "config.toml").read_text() == "entries = [15]\n"  # review edit preserved


def test_does_not_reset_operator_commits(tmp_path):
    origin, repo = _setup(tmp_path)
    _advance_origin(origin, tmp_path, "theorem a : True := by exact trivial\n")
    # a NON-marathon (operator) commit must never be reset away
    (repo / "operator.txt").write_text("important\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "operator: my work")

    msg = pull_once(repo)

    assert "NOT resetting" in msg, msg
    assert (repo / "operator.txt").exists()                       # operator work kept
    assert "by sorry" in (repo / "code.lean").read_text()         # stays stale (operator resolves)

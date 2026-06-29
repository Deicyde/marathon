"""Restate-after-refine: the diff-window reconcile that handles RENAME / SPLIT /
DELETE / ADD when a refine changes an issue's declaration set.

The leaf-match reconciler (test_deck_reconcile_leafmatch.py) caught 1:1 renames
that PRESERVE the leaf; it could not handle a SPLIT (one iff → two directional
lemmas, the #166 shape) — the stale parent stayed cited and the new sublemmas
never appeared. This drives the reconcile from the refine's git diff window
(removed/added decls) instead, classifying:

* RENAME — one added decl replaces the stale one (shared leaf, or the sole
  removed/added pair);
* SPLIT — two+ added decls replace it (only once EVERY target is live);
* DELETE — removed with no replacement (left STALE, never auto-dropped on a guess
  — the deck footnotes it);
* ADD — leftover window-added decls the issue should also cite.

Fully offline: classify_diff_window is pure; the diff helper runs a real tmp git
repo; the e2e monkeypatches the oracle + gh, no network/lake.
"""

from __future__ import annotations

import subprocess

from marathon.deck.refresh import classify_diff_window, _leaves_related
from marathon.review.verified_decls import declarations_added_removed_in_diff


# ---------------------------------------------------------------------------
# _leaves_related — the conservative split/rename token heuristic
# ---------------------------------------------------------------------------


def test_leaves_related():
    # equal leaf (a namespace-only rename keeps the leaf)
    assert _leaves_related("id", "id")
    # the #166 split shape: share the result root + one direction, drop `iff`
    assert _leaves_related("a_iff_c", "a_imp_c")
    assert _leaves_related("a_iff_c", "c_imp_a")
    # unrelated: a single shared connective word is not enough
    assert not _leaves_related("a_iff_c", "helper")
    assert not _leaves_related("id", "comp")


# ---------------------------------------------------------------------------
# classify_diff_window — pure classification
# ---------------------------------------------------------------------------


def test_classify_rename_same_leaf():
    cls = classify_diff_window(
        ["A.foo"], {"foo"}, {"A.foo"},
        live_names=["B.foo"], orphaned=["B.foo"], cited_set=set(),
    )
    assert cls["renames"] == [{"old": "A.foo", "new": "B.foo"}]
    assert cls["splits"] == [] and cls["deletes"] == [] and cls["kept_stale"] == []


def test_classify_split_166():
    cls = classify_diff_window(
        ["Exercise_15_13_a_iff_c"],
        {"Exercise_15_13_a_imp_c", "Exercise_15_13_c_imp_a"},
        {"Exercise_15_13_a_iff_c"},
        live_names=["Exercise_15_13_a_imp_c", "Exercise_15_13_c_imp_a"],
        orphaned=["Exercise_15_13_a_imp_c", "Exercise_15_13_c_imp_a"],
        cited_set=set(),
    )
    assert len(cls["splits"]) == 1
    sg = cls["splits"][0]
    assert sg["old"] == "Exercise_15_13_a_iff_c"
    assert set(sg["targets"]) == {
        "Exercise_15_13_a_imp_c", "Exercise_15_13_c_imp_a"}
    assert cls["renames"] == [] and cls["deletes"] == []


def test_classify_delete_confirmed():
    cls = classify_diff_window(
        ["Gone"], set(), {"Gone"},
        live_names=[], orphaned=[], cited_set=set(),
    )
    assert cls["deletes"] == ["Gone"]
    assert cls["renames"] == [] and cls["splits"] == []


def test_classify_split_missing_target_kept_stale():
    # Only one of the two products is live -> NOT a confident split -> keep the
    # parent (never drop a citation without ALL replacements present).
    cls = classify_diff_window(
        ["Foo_a_iff_c"],
        {"Foo_a_imp_c", "Foo_c_imp_a"},
        {"Foo_a_iff_c"},
        live_names=["Foo_a_imp_c"],
        orphaned=["Foo_a_imp_c"],
        cited_set=set(),
    )
    assert cls["splits"] == []
    assert "Foo_a_iff_c" in cls["kept_stale"]


def test_classify_stale_not_in_window_kept():
    # A stale decl the window did NOT touch -> unexplained -> kept, not deleted.
    cls = classify_diff_window(
        ["Unrelated"], {"newthing"}, {"other"},
        live_names=["X.newthing"], orphaned=["X.newthing"], cited_set=set(),
    )
    assert "Unrelated" in cls["kept_stale"]
    assert cls["deletes"] == []


def test_classify_sole_pair_rename_leaf_changed():
    # Leaf changed entirely, but exactly one decl out + one in this window -> the
    # addition is the rename target by provenance.
    cls = classify_diff_window(
        ["A.old_name"], {"brand_new"}, {"A.old_name"},
        live_names=["A.brand_new"], orphaned=["A.brand_new"], cited_set=set(),
    )
    assert cls["renames"] == [{"old": "A.old_name", "new": "A.brand_new"}]


# ---------------------------------------------------------------------------
# declarations_added_removed_in_diff — real tmp git repo
# ---------------------------------------------------------------------------


def _git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a],
                          capture_output=True, text=True)


def test_diff_added_removed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    f = repo / "Foo.lean"
    f.write_text("theorem old_iff : True := trivial\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    old = _git(repo, "rev-parse", "HEAD").stdout.strip()
    f.write_text("theorem new_mp : True := trivial\n"
                 "theorem new_mpr : True := trivial\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")
    new = _git(repo, "rev-parse", "HEAD").stdout.strip()

    pair = declarations_added_removed_in_diff(repo, old, new, ["Foo.lean"])
    assert pair is not None
    added, removed = pair
    assert "old_iff" in removed
    assert {"new_mp", "new_mpr"} <= added


def test_diff_git_failure_is_none(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    # Bogus shas -> git diff exits nonzero -> None (NOT an empty diff), so the
    # caller retries next HEAD move instead of acting on phantom-empty.
    assert declarations_added_removed_in_diff(
        repo, "deadbeef", "cafebabe", ["*.lean"]) is None


# ---------------------------------------------------------------------------
# auto_reconcile_issue — the #166 split, end to end (### Lean declarations)
# ---------------------------------------------------------------------------

from marathon.deck import refresh as refresh_mod  # noqa: E402
from marathon.deck.refresh import auto_reconcile_issue  # noqa: E402
from marathon.review.config import ChapterRegistry  # noqa: E402
from tests.test_deck_source_fallback import (  # noqa: E402
    make_cfg, make_snapshot, install_snapshot, patch_bulk, _decl,
)

_SPLIT_SRC = """\
import Mathlib

namespace Demo.Chapter15.Tf

theorem Exercise_15_13_a_imp_c : True := trivial
theorem Exercise_15_13_c_imp_a : True := trivial

end Demo.Chapter15.Tf
"""


def _setup_split(tmp_path, monkeypatch, *, labels=None):
    cfg = make_cfg(tmp_path)
    cfg.chapters[15] = ChapterRegistry(chapter=15, entries=[(166, "Ex 15.13")])
    (cfg.repo_dir / "Demo" / "Chapter15").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter15" / "Tf.lean").write_text(_SPLIT_SRC)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter15.Tf.Exercise_15_13_a_imp_c",
              module="Demo.Chapter15.Tf"),
        _decl("Demo.Chapter15.Tf.Exercise_15_13_c_imp_a",
              module="Demo.Chapter15.Tf"),
    ], cfg.repo_dir))
    cited = "Demo.Chapter15.Tf.Exercise_15_13_a_iff_c"
    body = (
        "## Context\n\nProse with an unrelated `token`.\n\n"
        "### Lean declarations\n\n"
        f"- [Tf.lean:1-2](https://github.com/x/y/blob/OLD/Demo/Chapter15/"
        f"Tf.lean#L1-L2) — `{cited}`\n\n"
        "### Informal Statement\n\ntext\n"
    )
    patch_bulk(monkeypatch, {166: {"title": "Ex 15.13", "state": "OPEN",
                                   "labels": labels or set(), "body": body}})
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEWSHA")
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: body)
    # The refine's diff window: a_iff_c removed, the two directions added.
    import marathon.review.verified_decls as vd
    monkeypatch.setattr(
        vd, "declarations_added_removed_in_diff",
        lambda repo, old, new, paths: (
            {"Exercise_15_13_a_imp_c", "Exercise_15_13_c_imp_a"},
            {"Exercise_15_13_a_iff_c"}),
    )
    return cfg, body


def test_split_166_end_to_end(tmp_path, monkeypatch):
    cfg, _body = _setup_split(tmp_path, monkeypatch)
    r = auto_reconcile_issue(cfg, 166, dry_run=True, diff_window=("OLD", "NEW"))
    assert len(r["splits"]) == 1
    nb = r["new_body"]
    # both directional sublemmas now cited
    assert "Exercise_15_13_a_imp_c`" in nb
    assert "Exercise_15_13_c_imp_a`" in nb
    # the stale iff parent is dropped
    assert "Exercise_15_13_a_iff_c`" not in nb
    # every other section preserved verbatim
    assert "### Informal Statement\n\ntext\n" in nb
    assert "Prose with an unrelated `token`." in nb


def test_split_166_verified_no_write(tmp_path, monkeypatch):
    """A verified/frozen issue is NEVER rewritten: the split is flagged,
    updated=False, and _gh_edit_body is never called."""
    from marathon.review.state import ReviewState, IssueState, save_state

    cfg, _body = _setup_split(tmp_path, monkeypatch)
    save_state(cfg, ReviewState(issues={
        166: IssueState(status="verified",
                        verdict_ts="2026-06-20T00:00:00-04:00"),
    }))

    def _boom(*a, **k):
        raise AssertionError("a verified issue body must NEVER be edited")

    monkeypatch.setattr(refresh_mod, "_gh_edit_body", _boom)
    r = auto_reconcile_issue(cfg, 166, diff_window=("OLD", "NEW"))
    assert r["updated"] is False
    assert len(r["flagged_splits"]) == 1
    assert "verified/frozen" in r["message"]

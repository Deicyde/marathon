"""Regression tests for the review verify/merge plumbing and the bulk
(GraphQL) issue fetch.

History these tests guard against (see docs/marathon-v2-plan.md §1):

* ``_maybe_merge_marathon_pr`` iterated ``cfg.chapters`` — a
  ``dict[int, ChapterRegistry]`` — yielding int keys, then called
  ``.chapter`` on them. The resulting AttributeError was swallowed by a
  bare ``except Exception: return``, so NO verify ever auto-merged a PR
  or flipped the tracker emoji. Fixing the lookup also exposed a latent
  NameError (``num`` vs ``issue_num``) in the tracker-update call,
  which lived inside the PR helper (so it died with it, and never ran
  for issues without a PR).
* ``cmd_list`` / ``cmd_next`` / ``verified_declarations`` issued one
  ``gh issue view`` per issue, serially (N+1) — now one
  ``gh api graphql`` call with a per-issue fallback on failure.

All tests monkeypatch at the gh-runner boundary (the ``gh`` name in the
module under test) — no subprocesses, no network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import marathon.review.github as github_mod
import marathon.review.review as review_mod
import marathon.review.verified_decls as vd_mod
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels
from marathon.review.github import fetch_issues_bulk


# --- helpers -----------------------------------------------------------------


def make_cfg(tmp_path: Path) -> ReviewConfig:
    """A realistic ReviewConfig: ``chapters`` is a dict[int, ChapterRegistry]
    exactly as ``load_config`` builds it — the shape that broke the old
    ``for ch in cfg.chapters: ch.chapter`` iteration."""
    return ReviewConfig(
        repo_dir=tmp_path,
        config_path=tmp_path / ".marathon" / "review" / "config.toml",
        github_repo="someone/SomeProject",
        parent_issue=1,
        referee_path=tmp_path / ".marathon" / "referee.md",
        target_path_template="SomeProject/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(
                chapter=14,
                entries=[(14, "Lemma 14.7"), (15, "Proposition 14.8")],
            ),
        },
    )


def completed(args=("gh",), returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=list(args), returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeGh:
    """Records every gh(...) call; replies via a handler keyed on the
    leading args. Unmatched calls succeed with empty stdout."""

    def __init__(self, handler=None):
        self.calls: list[tuple[str, ...]] = []
        self.handler = handler

    def __call__(self, *args, check=True, capture=True):
        self.calls.append(args)
        if self.handler is not None:
            result = self.handler(args)
            if result is not None:
                if check and result.returncode != 0:
                    raise RuntimeError(f"gh {' '.join(args)} failed")
                return result
        return completed(args=("gh", *args))

    def calls_starting_with(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == prefix]


# --- _maybe_merge_marathon_pr --------------------------------------------------


def test_maybe_merge_reaches_merge_attempt(tmp_path, monkeypatch, capsys):
    """THE regression test for the dict-iteration AttributeError: with a
    real chapters dict, the helper must resolve issue→chapter and reach
    the `gh pr list` + `gh pr merge` calls. The old code raised
    AttributeError on the first int key, swallowed it, and returned
    before any gh call."""
    cfg = make_cfg(tmp_path)

    def handler(args):
        if args[:2] == ("pr", "list"):
            return completed(stdout="55\n")
        if args[:2] == ("pr", "merge"):
            return completed()
        return None

    fake = FakeGh(handler)
    monkeypatch.setattr(review_mod, "gh", fake)

    review_mod._maybe_merge_marathon_pr(cfg, 15)

    # The pr-list lookup happened, against the correct derived branch
    # (issue 15 lives in chapter 14).
    lists = fake.calls_starting_with("pr", "list")
    assert lists, "never queried for an open PR (chapter lookup broken again?)"
    assert "marathon/refine-c14-i15" in lists[0]
    # And the merge attempt happened for the PR number gh returned.
    merges = fake.calls_starting_with("pr", "merge")
    assert merges and merges[0][2] == "55"
    assert "merged #55" in capsys.readouterr().out


def test_maybe_merge_no_open_pr_is_silent_skip(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    fake = FakeGh(lambda args: completed(stdout=""))  # no PR for the branch
    monkeypatch.setattr(review_mod, "gh", fake)

    review_mod._maybe_merge_marathon_pr(cfg, 14)

    assert fake.calls_starting_with("pr", "list")
    assert not fake.calls_starting_with("pr", "merge")
    assert "WARN" not in capsys.readouterr().out  # legitimate no-op


def test_maybe_merge_failures_are_visible(tmp_path, monkeypatch, capsys):
    """Failures must print a WARN, never pass silently (the old bare
    ``except Exception: return`` hid this path for months)."""
    cfg = make_cfg(tmp_path)

    # (a) unregistered issue → WARN, no gh calls.
    fake = FakeGh()
    monkeypatch.setattr(review_mod, "gh", fake)
    review_mod._maybe_merge_marathon_pr(cfg, 999)
    assert not fake.calls
    assert "WARN" in capsys.readouterr().out

    # (b) merge fails (e.g. conflicts) → WARN with the gh error text,
    # and no exception propagates.
    def handler(args):
        if args[:2] == ("pr", "list"):
            return completed(stdout="55\n")
        if args[:2] == ("pr", "merge"):
            return completed(returncode=1, stderr="merge conflict with main")
        return None

    monkeypatch.setattr(review_mod, "gh", FakeGh(handler))
    review_mod._maybe_merge_marathon_pr(cfg, 15)
    out = capsys.readouterr().out
    assert "WARN" in out and "merge conflict" in out


# --- cmd_verify: tracker flip is independent of the PR merge ------------------


def test_cmd_verify_updates_tracker_even_without_pr(tmp_path, monkeypatch, capsys):
    """The tracker 🟠→🟡 flip used to live inside _maybe_merge_marathon_pr
    and died with it. It must now run from cmd_verify even when there is
    no PR to merge at all."""
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(review_mod, "gh", FakeGh(lambda args: completed(stdout="")))

    recorded_verifications: list[int] = []
    monkeypatch.setattr(
        review_mod, "record_verification",
        lambda cfg_, num: recorded_verifications.append(num),
    )
    tracker_calls: list[tuple[int, str]] = []

    def fake_tracker(cfg_, num, emoji, **kw):
        tracker_calls.append((num, emoji))
        return True, f"'Proposition 14.8' line updated 🟠 → {emoji}"

    monkeypatch.setattr(review_mod, "update_tracker_emoji", fake_tracker)

    args = SimpleNamespace(issue_num=15, close=False, comment=None)
    review_mod.cmd_verify(args)

    assert recorded_verifications == [15]
    assert tracker_calls == [(15, "🟡")]
    assert "tracker:" in capsys.readouterr().out


def test_cmd_verify_survives_tracker_failure(tmp_path, monkeypatch, capsys):
    """update_tracker_emoji raising (gh failure inside, check=True →
    RuntimeError) must downgrade to a WARN, not abort the verify."""
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(review_mod, "gh", FakeGh(lambda args: completed(stdout="")))
    monkeypatch.setattr(review_mod, "record_verification", lambda cfg_, num: None)

    def boom(cfg_, num, emoji, **kw):
        raise RuntimeError("gh issue edit 1 failed: rate limited")

    monkeypatch.setattr(review_mod, "update_tracker_emoji", boom)

    review_mod.cmd_verify(SimpleNamespace(issue_num=15, close=False, comment=None))
    out = capsys.readouterr().out
    assert "tracker: WARN" in out and "rate limited" in out


# --- fetch_issues_bulk ---------------------------------------------------------


def _graphql_payload():
    return json.dumps({
        "data": {
            "repository": {
                "i14": {
                    "number": 14,
                    "title": "Lemma 14.7 (alt tensors)",
                    "state": "OPEN",
                    "body": "```lean\ntheorem foo : True := trivial\n```",
                    "labels": {"nodes": [{"name": "review:verified"}]},
                },
                # Issue 15 unresolvable (deleted/transferred): null node.
                "i15": None,
            }
        }
    })


def test_fetch_issues_bulk_parses_one_graphql_call(monkeypatch):
    fake = FakeGh(lambda args: completed(stdout=_graphql_payload())
                  if args[:2] == ("api", "graphql") else None)
    monkeypatch.setattr(github_mod, "gh", fake)

    out = fetch_issues_bulk([14, 15, 14], "someone/SomeProject")  # dup deduped

    assert len(fake.calls) == 1, "must be exactly ONE gh call for N issues"
    query = fake.calls[0][3]
    assert "i14: issue(number: 14)" in query
    assert query.count("i14:") == 1  # duplicate input didn't duplicate alias
    assert out is not None
    assert out[14]["title"] == "Lemma 14.7 (alt tensors)"
    assert out[14]["labels"] == {"review:verified"}
    assert "theorem foo" in out[14]["body"]
    assert 15 not in out  # null node omitted → caller falls back per-issue


def test_fetch_issues_bulk_failure_returns_none(monkeypatch):
    fake = FakeGh(lambda args: completed(returncode=1, stderr="HTTP 502"))
    monkeypatch.setattr(github_mod, "gh", fake)
    assert fetch_issues_bulk([14], "someone/SomeProject") is None

    # Malformed JSON is also a whole-call failure.
    monkeypatch.setattr(
        github_mod, "gh", FakeGh(lambda args: completed(stdout="not json"))
    )
    assert fetch_issues_bulk([14], "someone/SomeProject") is None


def test_fetch_issues_bulk_empty_and_bad_repo():
    # No subprocess needed for these edge cases.
    assert fetch_issues_bulk([], "someone/SomeProject") == {}
    assert fetch_issues_bulk([14], "not-a-repo-slug") is None


# --- verified_declarations: bulk path + fallback -------------------------------

LEAN_BODY = "intro\n```lean\ntheorem foo : True := trivial\n```\n"


def test_verified_declarations_uses_bulk(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    meta = {
        14: {"title": "t", "state": "OPEN", "body": LEAN_BODY,
             "labels": {"review:verified"}},
        15: {"title": "t", "state": "OPEN", "body": LEAN_BODY,
             "labels": set()},  # not verified → skipped
    }
    monkeypatch.setattr(vd_mod, "fetch_issues_bulk", lambda nums, repo: meta)

    def no_per_issue(*a, **kw):  # the N+1 path must stay cold
        raise AssertionError("per-issue gh call made despite bulk success")

    monkeypatch.setattr(vd_mod, "issue_labels", no_per_issue)
    monkeypatch.setattr(vd_mod, "_fetch_body", no_per_issue)

    assert vd_mod.verified_declarations(cfg, 14) == {14: {"foo"}}


def test_verified_declarations_graphql_fallback(tmp_path, monkeypatch, capsys):
    """Bulk fetch fails → printed warning + per-issue path still produces
    the right answer."""
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(vd_mod, "fetch_issues_bulk", lambda nums, repo: None)
    monkeypatch.setattr(
        vd_mod, "issue_labels",
        lambda num, repo: {"review:verified"} if num == 14 else set(),
    )
    monkeypatch.setattr(
        vd_mod, "_fetch_body",
        lambda num, repo: LEAN_BODY if num == 14 else None,
    )

    out = vd_mod.verified_declarations(cfg, 14)

    assert out == {14: {"foo"}}
    captured = capsys.readouterr().out
    assert "bulk GraphQL issue fetch failed" in captured


# --- cmd_list: one bulk call instead of N --------------------------------------


def test_cmd_list_uses_single_bulk_fetch(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review_mod, "load_config", lambda: cfg)

    bulk_calls: list[list[int]] = []

    def fake_bulk(nums, repo):
        bulk_calls.append(list(nums))
        return {
            14: {"title": "Lemma 14.7", "state": "CLOSED", "body": "",
                 "labels": {"review:verified"}},
            15: {"title": "Proposition 14.8", "state": "OPEN", "body": "",
                 "labels": set()},
        }

    monkeypatch.setattr(review_mod, "fetch_issues_bulk", fake_bulk)

    def no_per_issue(*a, **kw):
        raise AssertionError("per-issue gh call made despite bulk success")

    monkeypatch.setattr(review_mod, "issue_title", no_per_issue)
    monkeypatch.setattr(review_mod, "issue_labels", no_per_issue)

    review_mod.cmd_list(SimpleNamespace(chapter=14))

    assert bulk_calls == [[14, 15]]
    out = capsys.readouterr().out
    assert "verified" in out and "Lemma 14.7" in out
    assert "unreviewed" in out and "Proposition 14.8" in out


def test_cmd_list_falls_back_per_issue_on_bulk_failure(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(review_mod, "fetch_issues_bulk", lambda nums, repo: None)
    monkeypatch.setattr(
        review_mod, "issue_labels",
        lambda num, repo: {"review:verified"} if num == 14 else set(),
    )
    monkeypatch.setattr(review_mod, "issue_title", lambda num, repo: f"title-{num}")

    review_mod.cmd_list(SimpleNamespace(chapter=14))

    captured = capsys.readouterr()
    assert "bulk GraphQL issue fetch failed" in captured.err
    assert "title-14" in captured.out and "title-15" in captured.out
    assert "verified" in captured.out and "unreviewed" in captured.out

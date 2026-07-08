"""The merge orchestrator (marathon.review.automerge): the per-cycle auto-merge
the Aristotle daemon/conductor runs to land eligible PRs on main.

Fully offline — `gh` is monkeypatched; no network, no real merges.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import marathon.review.automerge as am


def _cfg(base="main"):
    return SimpleNamespace(github_repo="o/r", auto_pr_base=base)


def _pr(num, *, mergeable="MERGEABLE", state="CLEAN", draft=False,
        checks=None, head=None, base="main", title="t", commits=None, build=None):
    # `build="OK"/"FAIL"/"TIMEOUT"` synthesizes a head commit whose subject
    # carries the pipeline's [build:*] verdict tag; `commits` overrides outright.
    if commits is None:
        commits = (
            [{"messageHeadline": f"marathon: c1 iter for #{num} [build:{build}]",
              "oid": f"sha{num}"}]
            if build is not None else []
        )
    return {
        "number": num,
        "title": title,
        "headRefName": head or f"branch-{num}",
        "baseRefName": base,
        "isDraft": draft,
        "mergeable": mergeable,
        "mergeStateStatus": state,
        "statusCheckRollup": checks or [],
        "commits": commits,
    }


def _cp(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


# --- eligibility() (pure gate) ---------------------------------------------

@pytest.mark.parametrize("pr, ok, reason_has", [
    (_pr(1), True, "clean"),
    (_pr(1, state="BEHIND"), True, "behind"),                  # mergeable, behind base
    (_pr(1, state="UNSTABLE"), True, "unstable"),              # mergeable, checks gate decides
    (_pr(1, draft=True), False, "draft"),
    (_pr(1, mergeable="CONFLICTING"), False, "conflict"),
    (_pr(1, state="DIRTY"), False, "conflict"),
    (_pr(1, mergeable="UNKNOWN"), False, "retry"),            # not computed → fail safe
    (_pr(1, state="UNKNOWN"), False, "retry"),                # fail safe
    (_pr(1, state="BLOCKED"), False, "branch protection"),
    # a future / unrecognized mergeStateStatus must FAIL SAFE, never merge
    (_pr(1, state="SOME_NEW_STATE"), False, "unrecognized"),
    # wrong base — never merged even though gh --base should have filtered it
    (_pr(1, base="develop"), False, "targets develop"),
])
def test_eligibility_core_gates(pr, ok, reason_has):
    got_ok, reason = am.eligibility(pr, base="main")
    assert got_ok is ok
    assert reason_has in reason.lower()


def test_eligibility_excludes_in_flight_refine_branches():
    pr = _pr(1, head="marathon/refine-c12-i93", build="OK")
    # normally mergeable…
    assert am.eligibility(pr, base="main")[0] is True
    # …but excluded while a refine is live on that branch
    ok, reason = am.eligibility(
        pr, base="main", exclude_heads=frozenset({"marathon/refine-c12-i93"})
    )
    assert ok is False and "running refine" in reason


@pytest.mark.parametrize("check, blocks", [
    ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}, True),
    ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "TIMED_OUT"}, True),
    ({"__typename": "StatusContext", "state": "FAILURE"}, True),
    ({"__typename": "StatusContext", "state": "ERROR"}, True),
    ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}, False),
    ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "NEUTRAL"}, False),
    ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"}, False),
    ({"__typename": "StatusContext", "state": "SUCCESS"}, False),
])
def test_eligibility_failing_checks_block_passing_checks_ok(check, blocks):
    ok = am.eligibility(_pr(1, checks=[check]), base="main")[0]
    assert ok is (not blocks)


@pytest.mark.parametrize("check", [
    {"__typename": "CheckRun", "status": "IN_PROGRESS"},      # conclusion null while running
    {"__typename": "CheckRun", "status": "QUEUED"},
    {"__typename": "StatusContext", "state": "PENDING"},
    {"__typename": "StatusContext", "state": "EXPECTED"},
])
def test_eligibility_pending_check_gated_by_require_checks(check):
    pr = _pr(1, checks=[check])
    assert am.eligibility(pr, base="main", require_checks=True)[0] is False
    # loosened: a conflict-free PR merges despite PENDING checks (never failing)
    assert am.eligibility(pr, base="main", require_checks=False)[0] is True


def test_eligibility_mixed_checks_one_failure_blocks():
    pr = _pr(1, checks=[
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
    ])
    assert am.eligibility(pr, base="main")[0] is False


# --- eligibility() build-verdict gate (the [build:FAIL] → main gap) ---------

def test_eligibility_build_fail_head_refused():
    # the #212 shape: head commit tagged [build:FAIL], no CI checks → REFUSE.
    ok, reason = am.eligibility(
        _pr(1, head="marathon/refine-c15-i168", build="FAIL"), base="main")
    assert ok is False and "build fail" in reason.lower()


def test_eligibility_build_timeout_head_refused():
    ok, reason = am.eligibility(_pr(1, build="TIMEOUT"), base="main")
    assert ok is False and "timeout" in reason.lower()


def test_eligibility_build_ok_head_merges():
    # the legit refine / sorry-only skeleton case: [build:OK] still clears.
    ok, reason = am.eligibility(
        _pr(1, head="marathon/refine-c15-i168", build="OK"), base="main")
    assert ok is True and reason == "clean"


def test_eligibility_untagged_refine_head_skipped():
    # a refine head with no [build:*] tag → unknown build → fail safe + retry.
    ok, reason = am.eligibility(
        _pr(1, head="marathon/refine-c1-i9",
            commits=[{"messageHeadline": "wip", "oid": "x"}]), base="main")
    assert ok is False and "build" in reason.lower()


def test_eligibility_untagged_human_pr_not_overblocked():
    # a human/infra PR (NOT a refine branch) legitimately has no tag → passes.
    ok, _ = am.eligibility(
        _pr(1, head="chore/register-ch12",
            commits=[{"messageHeadline": "register ch12", "oid": "y"}]),
        base="main")
    assert ok is True


def test_eligibility_uses_head_commit_for_verdict():
    # the HEAD (last) commit governs — an earlier [build:FAIL] is superseded.
    pr = _pr(1, head="marathon/refine-c1-i9", commits=[
        {"messageHeadline": "wip [build:FAIL]", "oid": "a"},
        {"messageHeadline": "marathon: c1 [build:OK]", "oid": "b"},
    ])
    assert am.eligibility(pr, base="main")[0] is True


def test_eligibility_require_build_ok_false_allows_untagged_but_not_red():
    # the escape hatch: require_build_ok=False lets an UNTAGGED refine head
    # through, but an explicit [build:FAIL] is STILL refused.
    untagged = _pr(1, head="marathon/refine-c1-i9",
                   commits=[{"messageHeadline": "wip", "oid": "x"}])
    assert am.eligibility(untagged, base="main", require_build_ok=False)[0] is True
    red = _pr(2, head="marathon/refine-c1-i9", build="FAIL")
    assert am.eligibility(red, base="main", require_build_ok=False)[0] is False


# --- automerge() (one cycle, gh mocked) ------------------------------------

def _install_gh(monkeypatch, prs, *, merge_rc=0, methods=None):
    # default repo: squash-only (mirrors GeometricAnalysis, which disallows
    # merge commits — the bug that made the live merge fail).
    methods = methods or {
        "squashMergeAllowed": True,
        "mergeCommitAllowed": False,
        "rebaseMergeAllowed": False,
    }
    calls = {"merged": [], "list": 0, "method_flags": [], "unlabeled": [],
             "comments": []}

    def fake_gh(*args, check=True, capture=True):
        if args[:2] == ("repo", "view"):
            return _cp(stdout=json.dumps(methods))
        if args[:2] == ("pr", "list"):
            calls["list"] += 1
            return _cp(stdout=json.dumps(prs))
        if args[:2] == ("pr", "merge"):
            calls["merged"].append(int(args[2]))
            for flag in ("--squash", "--merge", "--rebase"):
                if flag in args:
                    calls["method_flags"].append(flag)
            return _cp(returncode=merge_rc, stderr="boom" if merge_rc else "")
        if args[:2] == ("issue", "edit"):
            # capture the re-queue relabel: (issue, --remove-label, <label>)
            calls["unlabeled"].append(int(args[2]))
            return _cp()
        if args[:2] == ("issue", "comment"):
            # capture the recovery comment: (issue, ..., --body, <body>)
            calls["comments"].append(
                (int(args[2]), args[list(args).index("--body") + 1]))
            return _cp()
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(am, "gh", fake_gh)
    return calls


def test_automerge_merges_only_eligible(monkeypatch):
    prs = [
        _pr(10),                                   # clean → merge
        _pr(11, mergeable="CONFLICTING"),          # conflict → skip
        _pr(12, draft=True),                       # draft → skip
        _pr(13, checks=[{"__typename": "CheckRun", "status": "COMPLETED",
                         "conclusion": "FAILURE"}]),  # red → skip
        _pr(14, state="BEHIND"),                   # behind but mergeable → merge
    ]
    calls = _install_gh(monkeypatch, prs)
    results = am.automerge(_cfg())

    assert calls["merged"] == [10, 14]
    merged = {r.pr for r in results if r.action == "merged"}
    skipped = {r.pr for r in results if r.action == "skipped"}
    assert merged == {10, 14}
    assert skipped == {11, 12, 13}


def test_automerge_refuses_build_fail_head(monkeypatch):
    # End-to-end analogue of the #212 regression: a [build:FAIL] refine head
    # reaches the gate and is SKIPPED; a [build:OK] head still lands.
    prs = [
        _pr(10, head="marathon/refine-c15-i166", build="OK"),
        _pr(11, head="marathon/refine-c15-i168", build="FAIL"),
    ]
    calls = _install_gh(monkeypatch, prs)
    results = am.automerge(_cfg())
    assert calls["merged"] == [10]
    assert any(r.pr == 11 and r.action == "skipped"
               and "build fail" in r.reason.lower() for r in results)


def test_pr_fields_include_commits():
    # The build-verdict signal must ride in the single `gh pr list` call.
    assert "commits" in am._PR_FIELDS


def test_automerge_uses_the_repos_allowed_merge_method(monkeypatch):
    # GeometricAnalysis disallows merge commits — automerge must squash, not
    # fail every PR with "Merge commits are not allowed".
    calls = _install_gh(monkeypatch, [_pr(10)], methods={
        "squashMergeAllowed": True, "mergeCommitAllowed": False,
        "rebaseMergeAllowed": False,
    })
    am.automerge(_cfg())
    assert calls["merged"] == [10]
    assert calls["method_flags"] == ["--squash"]


def test_automerge_method_preference_squash_then_merge_then_rebase(monkeypatch):
    # all allowed → squash (cleanest history for single-purpose PRs)
    calls = _install_gh(monkeypatch, [_pr(10)], methods={
        "squashMergeAllowed": True, "mergeCommitAllowed": True,
        "rebaseMergeAllowed": True,
    })
    am.automerge(_cfg())
    assert calls["method_flags"] == ["--squash"]
    # only rebase allowed → rebase
    calls = _install_gh(monkeypatch, [_pr(11)], methods={
        "squashMergeAllowed": False, "mergeCommitAllowed": False,
        "rebaseMergeAllowed": True,
    })
    am.automerge(_cfg())
    assert calls["method_flags"] == ["--rebase"]


def test_automerge_dry_run_merges_nothing(monkeypatch):
    calls = _install_gh(monkeypatch, [_pr(10), _pr(11)])
    results = am.automerge(_cfg(), dry_run=True)
    assert calls["merged"] == []                       # NEVER calls gh pr merge
    assert {r.pr for r in results if r.action == "would-merge"} == {10, 11}


def test_automerge_reports_merge_failure(monkeypatch):
    calls = _install_gh(monkeypatch, [_pr(10)], merge_rc=1)
    results = am.automerge(_cfg())
    assert calls["merged"] == [10]                     # attempted
    assert [r.action for r in results] == ["failed"]


def test_automerge_targets_the_configured_base(monkeypatch):
    seen = {}

    def fake_gh(*args, check=True, capture=True):
        if args[:2] == ("pr", "list"):
            # capture the --base value passed to gh pr list
            seen["base"] = args[args.index("--base") + 1]
            return _cp(stdout="[]")
        raise AssertionError("no merge expected")

    monkeypatch.setattr(am, "gh", fake_gh)
    am.automerge(_cfg(base="develop"))
    assert seen["base"] == "develop"
    # explicit base arg overrides the config
    am.automerge(_cfg(base="develop"), base="release")
    assert seen["base"] == "release"


def test_automerge_requeues_a_refined_issue(monkeypatch):
    # Merging a marathon/refine-cN-iM PR re-queues issue M for re-review:
    # removes review:rejected AND clears local state.
    requeued = []
    import marathon.review.state as state_mod
    monkeypatch.setattr(state_mod, "requeue_for_rereview",
                        lambda cfg, issue: requeued.append(issue) or True)
    cfg = SimpleNamespace(github_repo="o/r", auto_pr_base="main",
                          labels=SimpleNamespace(rejected="review:rejected"))
    calls = _install_gh(monkeypatch, [_pr(109, head="marathon/refine-c12-i93",
                                          build="OK")])

    res = am.automerge(cfg)

    assert calls["merged"] == [109]
    assert calls["unlabeled"] == [93]      # review:rejected removed from #93
    assert requeued == [93]                # local state cleared for #93
    assert "re-queued #93 for re-review" in res[0].reason
    # The RECOVERY comment closes the notification loop: names the merged PR
    # and supersedes any dangling "Auto-refine stalled" comment (2026-07-07).
    assert len(calls["comments"]) == 1
    n, body = calls["comments"][0]
    assert n == 93
    assert "recovered" in body.lower() and "PR #109" in body


def test_requeue_landed_false_posts_no_recovery_comment(monkeypatch):
    # The no-PR-landed caller (a refine that produced no diff → nothing merged)
    # must NOT post the "Aristotle's fix ... landed" recovery comment — that
    # would be a public lie. It still relabels + clears state.
    import marathon.review.state as state_mod
    monkeypatch.setattr(state_mod, "requeue_for_rereview", lambda cfg, i: True)
    cfg = SimpleNamespace(github_repo="o/r",
                          labels=SimpleNamespace(rejected="review:rejected"))
    calls = _install_gh(monkeypatch, [])
    n = am._requeue_refined_issue(cfg, "marathon/refine-c12-i93", landed=False)
    assert n == 93
    assert calls["unlabeled"] == [93]        # still re-queued
    assert calls["comments"] == []           # but NO false "fix landed" comment


def test_automerge_non_refine_pr_does_not_requeue(monkeypatch):
    requeued = []
    import marathon.review.state as state_mod
    monkeypatch.setattr(state_mod, "requeue_for_rereview",
                        lambda cfg, issue: requeued.append(issue) or True)
    # a config/registration PR head — NOT a refine branch
    calls = _install_gh(monkeypatch, [_pr(108, head="chore/register-ch12")])

    am.automerge(_cfg())

    assert calls["merged"] == [108]
    assert calls["unlabeled"] == [] and requeued == []   # nothing re-queued


def test_automerge_handles_gh_list_failure(monkeypatch):
    monkeypatch.setattr(am, "gh", lambda *a, **k: _cp(returncode=1, stderr="nope"))
    assert am.automerge(_cfg()) == []                  # no crash, nothing merged


def test_automerge_skips_pr_with_no_number(monkeypatch):
    bad = _pr(None)
    bad["number"] = None                               # GitHub omitted it
    calls = _install_gh(monkeypatch, [bad, _pr(10)])
    results = am.automerge(_cfg())
    assert calls["merged"] == [10]                     # no int(None) crash
    assert any(r.action == "skipped" and "number" in r.reason for r in results)


def test_automerge_excludes_in_flight_branch(monkeypatch):
    prs = [_pr(10, head="marathon/refine-c12-i93"), _pr(11)]
    calls = _install_gh(monkeypatch, prs)
    results = am.automerge(
        _cfg(), exclude_heads=frozenset({"marathon/refine-c12-i93"})
    )
    assert calls["merged"] == [11]                      # 10 protected (live refine)
    assert any(r.pr == 10 and "running refine" in r.reason for r in results)


def test_automerge_defends_against_wrong_base_in_list(monkeypatch):
    # gh --base should filter, but if a develop-targeted PR leaks into the list
    # it must NOT be merged onto develop while we asked for main.
    prs = [_pr(10), _pr(11, base="develop")]
    calls = _install_gh(monkeypatch, prs)
    am.automerge(_cfg(base="main"))
    assert calls["merged"] == [10]


def test_automerge_infra_scope_merges_only_marathon_config(monkeypatch):
    prs = [_pr(10), _pr(11)]
    calls = _install_gh(monkeypatch, prs)
    files = {
        10: [".marathon/review/config.toml"],          # infra → merge
        11: ["GeometricAnalysis/LeeSM/Chapter12/TensorField.lean"],  # proof → skip
    }
    monkeypatch.setattr(am, "_pr_files", lambda repo, num: files[num])
    results = am.automerge(_cfg(), scope="infra")
    assert calls["merged"] == [10]
    assert any(r.pr == 11 and "non-infra" in r.reason for r in results)


def test_automerge_infra_scope_skips_unreadable_files(monkeypatch):
    calls = _install_gh(monkeypatch, [_pr(10)])
    monkeypatch.setattr(am, "_pr_files", lambda repo, num: None)  # gh files failed
    results = am.automerge(_cfg(), scope="infra")
    assert calls["merged"] == []                        # never merge on unknown files
    assert results[0].action == "skipped"

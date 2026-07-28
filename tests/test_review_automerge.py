"""The merge orchestrator (marathon.review.automerge): the per-cycle auto-merge
the Aristotle daemon/conductor runs to land eligible PRs on main.

Fully offline — `gh` is monkeypatched; no network, no real merges.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import marathon.review.automerge as am
import marathon.review.state as review_state


def _cfg(base="main"):
    return SimpleNamespace(github_repo="o/r", auto_pr_base=base)


def _pr(num, *, mergeable="MERGEABLE", state="CLEAN", draft=False,
        checks=None, head=None, base="main", title="t", commits=None, build=None):
    # `build="OK"/"FAIL"/"TIMEOUT"` synthesizes a head commit whose subject
    # carries the pipeline's [build:*] verdict tag; `commits` overrides outright.
    if commits is None:
        commits = [{
            "messageHeadline": (
                f"marathon: c1 iter for #{num} [build:{build}]"
                if build is not None else f"ordinary change for #{num}"
            ),
            "oid": f"sha{num}",
        }]
    return {
        "number": num,
        "title": title,
        "headRefName": head or f"branch-{num}",
        "headRefOid": commits[-1]["oid"] if commits else None,
        "baseRefName": base,
        "baseRefOid": f"base-sha-{base}",
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
    (_pr(1, state="BEHIND"), True, "behind"),                  # non-refine human PR
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


def test_eligibility_refine_head_behind_base_requires_update():
    ok, reason = am.eligibility(
        _pr(
            1,
            head="marathon/refine-c15-i168",
            state="BEHIND",
            build="OK",
        ),
        base="main",
    )

    assert ok is False
    assert "base moved" in reason.lower()


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


def test_trusted_sha_bound_build_status_overrides_stale_failure_tag():
    pr = _pr(
        1,
        head="marathon/refine-c1-i9",
        build="FAIL",
        checks=[{
            "__typename": "StatusContext",
            "context": am.TRUSTED_BUILD_CONTEXT,
            "state": "SUCCESS",
        }],
    )

    ok, reason = am.eligibility(pr, base="main")

    assert ok is True
    assert reason == "clean"


def test_unrelated_green_status_cannot_override_stale_failure_tag():
    pr = _pr(
        1,
        head="marathon/refine-c1-i9",
        build="FAIL",
        checks=[{
            "__typename": "StatusContext",
            "context": "ci/looks-similar-but-is-not-marathon",
            "state": "SUCCESS",
        }],
    )

    ok, reason = am.eligibility(pr, base="main")

    assert ok is False
    assert "build fail" in reason.lower()


@pytest.mark.parametrize("conclusion", ["SUCCESS", "NEUTRAL", "SKIPPED"])
def test_same_named_check_run_cannot_impersonate_build_status(conclusion):
    pr = _pr(
        1,
        head="marathon/refine-c1-i9",
        build="FAIL",
        checks=[{
            "__typename": "CheckRun",
            "name": am.TRUSTED_BUILD_CONTEXT,
            "status": "COMPLETED",
            "conclusion": conclusion,
        }],
    )

    ok, reason = am.eligibility(pr, base="main")

    assert ok is False
    assert "build fail" in reason.lower()


# --- automerge() (one cycle, gh mocked) ------------------------------------

def _install_gh(
    monkeypatch,
    prs,
    *,
    merge_rc=0,
    methods=None,
    label_remove_ok=True,
    live_head_overrides=None,
    live_base_name_overrides=None,
    live_base_overrides=None,
    live_state_overrides=None,
    on_merge=None,
):
    # default repo: squash-only (mirrors GeometricAnalysis, which disallows
    # merge commits — the bug that made the live merge fail).
    methods = methods or {
        "squashMergeAllowed": True,
        "mergeCommitAllowed": False,
        "rebaseMergeAllowed": False,
    }
    calls = {
        "merged": [],
        "list": 0,
        "method_flags": [],
        "matched_heads": [],
        "unlabeled": [],
        "comments": [],
    }
    issue_label_state = {}
    live_head_overrides = live_head_overrides or {}
    live_base_name_overrides = live_base_name_overrides or {}
    live_base_overrides = live_base_overrides or {}
    live_state_overrides = live_state_overrides or {}

    def fake_gh(*args, check=True, capture=True):
        if args[:2] == ("repo", "view"):
            return _cp(stdout=json.dumps(methods))
        if args[:2] == ("pr", "list"):
            calls["list"] += 1
            return _cp(stdout=json.dumps(prs))
        if args[:1] == ("api",) and "/pulls/" in args[1]:
            number = int(args[1].rsplit("/", 1)[-1])
            pr = next(row for row in prs if row["number"] == number)
            return _cp(stdout=json.dumps({
                "state": live_state_overrides.get(number, "open"),
                "head": {"sha": live_head_overrides.get(
                    number, pr["headRefOid"]
                )},
                "base": {
                    "ref": live_base_name_overrides.get(
                        number, pr["baseRefName"]
                    ),
                    "sha": live_base_overrides.get(
                        number, pr["baseRefOid"]
                    ),
                },
            }))
        if args[:2] == ("pr", "merge"):
            calls["merged"].append(int(args[2]))
            calls["matched_heads"].append(
                args[list(args).index("--match-head-commit") + 1]
            )
            for flag in ("--squash", "--merge", "--rebase"):
                if flag in args:
                    calls["method_flags"].append(flag)
            if on_merge is not None:
                on_merge()
            return _cp(returncode=merge_rc, stderr="boom" if merge_rc else "")
        if args[:2] == ("issue", "edit"):
            # capture the re-queue relabel: (issue, --remove-label, <label>)
            issue = int(args[2])
            calls["unlabeled"].append(issue)
            labels = issue_label_state.setdefault(issue, {"review:rejected"})
            if label_remove_ok and "--remove-label" in args:
                labels.discard(args[list(args).index("--remove-label") + 1])
            return _cp()
        if args[:2] == ("issue", "view"):
            issue = int(args[2])
            labels = issue_label_state.setdefault(issue, {"review:rejected"})
            return _cp(stdout=json.dumps({
                "labels": [{"name": name} for name in sorted(labels)]
            }))
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
    assert calls["matched_heads"] == ["sha10", "sha14"]
    merged = {r.pr for r in results if r.action == "merged"}
    skipped = {r.pr for r in results if r.action == "skipped"}
    assert merged == {10, 14}
    assert skipped == {11, 12, 13}


@pytest.mark.parametrize(
    "override, expected_reason",
    [("head", "head moved"), ("base", "base moved")],
)
def test_automerge_refuses_revision_race_before_merge(
    monkeypatch, override, expected_reason
):
    pr = _pr(10)
    kwargs = (
        {"live_head_overrides": {10: "different-head"}}
        if override == "head"
        else {"live_base_overrides": {10: "different-base"}}
    )
    calls = _install_gh(monkeypatch, [pr], **kwargs)

    result = am.automerge(_cfg())

    assert calls["merged"] == []
    assert result[0].action == "failed"
    assert expected_reason in result[0].reason.lower()


def test_automerge_refuses_same_sha_base_retarget_before_merge(monkeypatch):
    """A retarget can preserve ``base.sha`` when both branches share a tip."""
    pr = _pr(10, base="main")
    calls = _install_gh(
        monkeypatch,
        [pr],
        live_base_name_overrides={10: "release"},
        # Deliberately leave the live base SHA unchanged.
    )

    result = am.automerge(_cfg(base="main"))

    assert calls["merged"] == []
    assert result[0].action == "failed"
    assert "base branch changed from main to release" in result[0].reason.lower()


def test_automerge_refuses_pr_closed_after_eligibility_check(monkeypatch):
    pr = _pr(10)
    calls = _install_gh(
        monkeypatch,
        [pr],
        live_state_overrides={10: "closed"},
    )

    result = am.automerge(_cfg())

    assert calls["merged"] == []
    assert result[0].action == "failed"
    assert "no longer open" in result[0].reason.lower()


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


def test_automerge_refuses_refine_pr_owned_by_older_verdict(monkeypatch, tmp_path):
    head = "marathon/refine-c15-i166"
    cfg = SimpleNamespace(
        repo_dir=tmp_path,
        github_repo="o/r",
        auto_pr_base="main",
        labels=SimpleNamespace(rejected="review:rejected"),
    )
    # Per-issue branch names are reused. The current verdict owns the same
    # branch but a DIFFERENT commit; the old remote PR must not be mistaken for
    # the new no-diff iteration and merged before cleanup CAS gets a say.
    review_state.save_state(
        cfg,
        review_state.ReviewState(
            issues={
                166: review_state.IssueState(
                    status="rejected",
                    verdict_ts="2026-07-10T12:00:00+00:00",
                    notes="new verdict",
                    iteration_branch=head,
                    iteration_commit_sha="new-current-commit",
                )
            }
        ),
    )
    calls = _install_gh(monkeypatch, [_pr(10, head=head, build="OK")])

    [result] = am.automerge(cfg)

    assert calls["merged"] == []
    assert result.action == "skipped"
    assert "stale/unowned" in result.reason


def test_automerge_heals_branchless_exact_sha_retry_completion(
    monkeypatch, tmp_path
):
    """A legacy no-reconcile reap kept the SHA but lost the branch.

    The PR head + issue-bearing branch make that completed verdict generation
    unambiguous, so automerge may land it and clear the parked rejection.
    """
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    head = "marathon/refine-c15-i166"
    pr = _pr(110, head=head, build="OK")
    rejected = review_state.record_rejection(cfg, 166, "delete the helper")
    review_state.record_iteration(
        cfg,
        166,
        expected_verdict_ts=rejected.verdict_ts,
        iteration_commit_sha=pr["headRefOid"],
    )
    poisoned = review_state.load_state(cfg).issues[166]
    assert poisoned.iteration_branch is None
    assert poisoned.last_iteration_ts is not None
    calls = _install_gh(monkeypatch, [pr])

    [result] = am.automerge(cfg)

    assert calls["merged"] == [110]
    assert result.action == "merged"
    assert 166 not in review_state.load_state(cfg).issues


@pytest.mark.parametrize("completed", [False, True])
def test_automerge_branchless_self_heal_requires_exact_completed_iteration(
    monkeypatch, tmp_path, completed
):
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    head = "marathon/refine-c15-i166"
    pr = _pr(110, head=head, build="OK")
    rejected = review_state.record_rejection(cfg, 166, "current verdict")
    if completed:
        # A completed iteration at a different SHA is an older/unrelated head.
        review_state.record_iteration(
            cfg,
            166,
            expected_verdict_ts=rejected.verdict_ts,
            iteration_commit_sha="different-head",
        )
    calls = _install_gh(monkeypatch, [pr])

    [result] = am.automerge(cfg)

    assert calls["merged"] == []
    assert result.action == "skipped"
    assert "stale/unowned" in result.reason


def test_automerge_holds_old_pr_while_start_over_recovery_is_active(
    monkeypatch, tmp_path
):
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    head = "marathon/refine-c12-i93"
    pr = _pr(109, head=head, build="OK")
    rejected = review_state.record_rejection(cfg, 93, "replace the old work")
    review_state.record_iteration(
        cfg,
        93,
        expected_verdict_ts=rejected.verdict_ts,
        iteration_branch=head,
        iteration_commit_sha=pr["headRefOid"],
    )
    queued = review_state.request_recovery_if_current(
        cfg,
        93,
        expected_status="rejected",
        expected_verdict_ts=rejected.verdict_ts,
        expected_recovery_id=None,
        recovery_id="start-over-b",
        recovery_mode=review_state.RECOVERY_START_OVER,
        recovery_pr=109,
        recovery_branch=head,
        recovery_head_sha=pr["headRefOid"],
        recovery_base="main",
    )
    assert queued is not None
    calls = _install_gh(monkeypatch, [pr])

    [result] = am.automerge(cfg)

    assert calls["merged"] == []
    assert result.action == "skipped"
    assert "recovery start_over is active" in result.reason
    current = review_state.load_state(cfg).issues[93]
    assert current.recovery_id == "start-over-b"


def test_recovery_queued_inside_merge_cannot_be_consumed_by_old_completion(
    monkeypatch, tmp_path
):
    """The pre-merge owner A must not adopt recovery B after GitHub merges."""
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    head = "marathon/refine-c12-i93"
    pr = _pr(109, head=head, build="OK")
    rejected = review_state.record_rejection(cfg, 93, "repair the declaration")
    review_state.record_iteration(
        cfg,
        93,
        expected_verdict_ts=rejected.verdict_ts,
        iteration_branch=head,
        iteration_commit_sha=pr["headRefOid"],
    )

    def queue_recovery_b():
        queued = review_state.request_recovery_if_current(
            cfg,
            93,
            expected_status="rejected",
            expected_verdict_ts=rejected.verdict_ts,
            expected_recovery_id=None,
            recovery_id="recovery-b",
            recovery_mode=review_state.RECOVERY_START_OVER,
            recovery_pr=109,
            recovery_branch=head,
            recovery_head_sha=pr["headRefOid"],
            recovery_base="main",
        )
        assert queued is not None

    calls = _install_gh(monkeypatch, [pr], on_merge=queue_recovery_b)

    [result] = am.automerge(cfg)

    assert calls["merged"] == [109]
    # The operation-generation mismatch is detected before label removal.
    assert calls["unlabeled"] == []
    assert result.action == "merged"
    assert "requeue not confirmed" in result.reason
    current = review_state.load_state(cfg).issues[93]
    assert current.recovery_id == "recovery-b"
    assert current.recovery_mode == review_state.RECOVERY_START_OVER


@pytest.mark.parametrize(
    "mergeable, merge_state, reason",
    [
        ("CONFLICTING", "DIRTY", "merge conflicts"),
        (
            "MERGEABLE",
            "BEHIND",
            "base moved after build",
        ),
    ],
)
def test_automerge_queues_non_destructive_update_for_stale_owned_refine(
    monkeypatch, tmp_path, mergeable, merge_state, reason
):
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    head = "marathon/refine-c16-i240"
    pr = _pr(
        279,
        head=head,
        build="OK",
        mergeable=mergeable,
        state=merge_state,
    )
    rejected = review_state.record_rejection(
        cfg, 240, "keep the existing PR and repair its integration"
    )
    review_state.record_iteration(
        cfg,
        240,
        expected_verdict_ts=rejected.verdict_ts,
        iteration_branch=head,
        iteration_commit_sha=pr["headRefOid"],
    )
    calls = _install_gh(monkeypatch, [pr])

    [result] = am.automerge(cfg)

    assert calls["merged"] == []
    assert result.action == "recovery-queued"
    assert reason in result.reason
    current = review_state.load_state(cfg).issues[240]
    assert current.recovery_mode == review_state.RECOVERY_UPDATE_CONTINUE
    assert current.recovery_id
    assert current.recovery_pr == 279
    assert current.recovery_branch == head
    assert current.recovery_head_sha == pr["headRefOid"]
    assert current.recovery_base == "main"
    assert current.needs_iteration() is True

    # A second merge pass must not replace the operation token or enqueue a
    # duplicate worker while the first recovery owns the branch.
    operation_id = current.recovery_id
    [second] = am.automerge(cfg)
    assert second.action == "skipped"
    assert review_state.load_state(cfg).issues[240].recovery_id == operation_id


def test_automerge_does_not_auto_recover_unowned_or_scope_excluded_refine(
    monkeypatch, tmp_path
):
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    head = "marathon/refine-c16-i240"
    pr = _pr(
        279,
        head=head,
        build="OK",
        mergeable="CONFLICTING",
        state="DIRTY",
    )
    rejected = review_state.record_rejection(cfg, 240, "repair it")
    review_state.record_iteration(
        cfg,
        240,
        expected_verdict_ts=rejected.verdict_ts,
        iteration_branch=head,
        iteration_commit_sha="a-different-head",
    )
    _install_gh(monkeypatch, [pr])

    [unowned] = am.automerge(cfg)
    assert unowned.action == "skipped"
    assert review_state.load_state(cfg).issues[240].recovery_id is None

    review_state.record_iteration(
        cfg,
        240,
        expected_verdict_ts=rejected.verdict_ts,
        iteration_branch=head,
        iteration_commit_sha=pr["headRefOid"],
    )
    [excluded] = am.automerge(cfg, scope="infra")
    assert excluded.action == "skipped"
    assert review_state.load_state(cfg).issues[240].recovery_id is None

    [dry_run] = am.automerge(cfg, dry_run=True)
    assert dry_run.action == "skipped"
    assert review_state.load_state(cfg).issues[240].recovery_id is None


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


def test_no_pr_requeue_cannot_erase_recovery_queued_during_label_roundtrip(
    monkeypatch, tmp_path
):
    """Old operation A must lose to recovery B queued after label removal."""
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    rejected = review_state.record_rejection(cfg, 93, "repair the declaration")
    review_state.record_iteration(
        cfg,
        93,
        expected_verdict_ts=rejected.verdict_ts,
        iteration_branch="marathon/refine-c12-i93",
        iteration_commit_sha="a" * 40,
    )
    calls = {"removed": 0, "restored": 0}

    def fake_gh(*args, **kwargs):
        if args[:2] == ("issue", "edit") and "--remove-label" in args:
            calls["removed"] += 1
            return _cp()
        if args[:2] == ("issue", "view"):
            queued = review_state.request_recovery_if_current(
                cfg,
                93,
                expected_status="rejected",
                expected_verdict_ts=rejected.verdict_ts,
                expected_recovery_id=None,
                recovery_id="recovery-b",
                recovery_mode=review_state.RECOVERY_START_OVER,
            )
            assert queued is not None
            return _cp(stdout='{"labels": []}')
        if args[:2] == ("issue", "edit") and "--add-label" in args:
            calls["restored"] += 1
            return _cp()
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(am, "gh", fake_gh)

    result = am._requeue_refined_issue(
        cfg,
        "marathon/refine-c12-i93",
        landed=False,
        expected_verdict_ts=rejected.verdict_ts,
        expected_recovery_id=None,
    )

    assert result is None
    current = review_state.load_state(cfg).issues[93]
    assert current.recovery_id == "recovery-b"
    assert current.recovery_mode == review_state.RECOVERY_START_OVER
    assert calls == {"removed": 1, "restored": 1}


def test_requeue_keeps_local_anchor_when_label_removal_silently_fails(
    monkeypatch, capsys,
):
    """The old ordering cleared state.json even when GitHub kept Reject, so no
    automatic pass had enough information to heal the split-brain."""
    import marathon.review.state as state_mod

    requeued = []
    monkeypatch.setattr(
        state_mod, "requeue_for_rereview",
        lambda cfg, issue: requeued.append(issue) or True,
    )
    cfg = SimpleNamespace(
        github_repo="o/r", auto_pr_base="main",
        labels=SimpleNamespace(rejected="review:rejected"),
    )
    calls = _install_gh(
        monkeypatch,
        [_pr(109, head="marathon/refine-c12-i93", build="OK")],
        label_remove_ok=False,
    )

    [result] = am.automerge(cfg)

    assert calls["merged"] == [109]           # merge itself still lands
    assert calls["unlabeled"] == [93, 93, 93]  # bounded confirmation retries
    assert requeued == []                      # local rejected state PRESERVED
    assert calls["comments"] == []             # no false recovery claim
    assert "requeue not confirmed" in result.reason
    assert "keeping local rejection state" in capsys.readouterr().out


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

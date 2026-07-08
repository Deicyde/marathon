"""Auto-merge orchestrator — lands clean PRs on the base branch with no human
in the loop.

Runs inside the auto-refine daemon (and the conductor) at the top of each poll:
every OPEN, NON-DRAFT, conflict-free PR targeting the base branch is merged with
``gh pr merge --merge --delete-branch``. This is what keeps the mechanical infra
PRs (chapter registration → ``.marathon/review/config.toml``) and green refine
PRs landed on ``main`` so the human never has to babysit a merge — the failure
mode that left the deck reading a branch with an empty chapter registry.

DETERMINISTIC — plain code, no Claude (matches the daemon/conductor rulings).
The gates are HARD and fail-safe:

* **conflict-free** (``mergeable == "MERGEABLE"``, ``mergeStateStatus != DIRTY``)
  — a conflicted PR is reported and SKIPPED, never force-merged;
* **not a draft**;
* **base is the configured base** (default ``main``) — a PR onto any other
  branch is never touched;
* **checks not failing**, and (unless ``require_checks=False``) not still
  pending — so an unattended merge cannot land a red build on ``main``;
* **build verdict not red** — the head commit's ``[build:OK|FAIL|TIMEOUT]`` tag
  (stamped by the refine pipeline) must not be FAIL/TIMEOUT, and a refine head
  must AFFIRMATIVELY carry ``[build:OK]``; on a CI-less repo this commit tag is
  the ONLY build signal (``statusCheckRollup`` is empty), so it is what stops a
  red refine from landing unattended — the gap that let a ``[build:FAIL]`` head
  reach ``main``;
* **not blocked by branch protection** (required review/approval) — left for
  the human; ``gh`` would refuse anyway.

A PR whose mergeability GitHub has not finished computing (``UNKNOWN``) is
skipped THIS cycle and retried the next — never merged on a guess. Every merge,
skip, and failure is logged and returned (so the loop can print a summary and
tests can assert without a real ``gh``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from marathon.review.github import gh

if TYPE_CHECKING:  # pragma: no cover — type-only
    from marathon.review.config import ReviewConfig

#: A merged PR on this branch is Aristotle's REFINE of a rejected issue; merging
#: it re-queues issue ``M`` for the human to RE-REVIEW (reject -> refine ->
#: merge -> re-review). Matches ``post_pipeline._branch_name_for_issue``.
_REFINE_BRANCH_RE = re.compile(r"^marathon/refine-c\d+-i(\d+)$")

#: PR JSON fields the gate needs. ``mergeable``/``mergeStateStatus`` are the
#: conflict + protection signals; ``statusCheckRollup`` is the CI signal;
#: ``commits`` carries the head commit's ``[build:*]`` verdict tag (the only
#: build signal on a CI-less repo) — it rides in this one ``gh pr list`` call.
_PR_FIELDS = (
    "number,title,headRefName,baseRefName,isDraft,mergeable,"
    "mergeStateStatus,statusCheckRollup,commits"
)

#: ``conclusion`` (CheckRun) / ``state`` (StatusContext) values that mean a
#: check has FAILED — any one of these blocks the merge.
_FAILED_CHECK = frozenset({
    "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
    "STARTUP_FAILURE", "ERROR", "STALE",
})
#: Values that mean a check has not finished (green-pending).
_PENDING_CHECK = frozenset({"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING"})

#: ``mergeStateStatus`` values we KNOW are safe to merge once ``mergeable`` is
#: MERGEABLE and the checks pass. Anything ELSE — BLOCKED, DIRTY, UNKNOWN, or a
#: future enum value GitHub adds — is a fail-safe SKIP (never merged on a guess).
#: CLEAN = green+approved; BEHIND = mergeable, base moved (gh makes a merge
#: commit); UNSTABLE = mergeable, the check gate below decides; "" tolerates an
#: older gh that omits the field.
_MERGEABLE_STATES = frozenset({"CLEAN", "BEHIND", "UNSTABLE", ""})

#: ``scope="infra"`` only merges PRs whose every changed file is under this
#: prefix (chapter registration / referee / review config) and is not Lean —
#: the boundary that keeps proof PRs out of an unattended merge.
SAFE_INFRA_PREFIX = ".marathon/"


@dataclass(frozen=True)
class AutoMergeResult:
    """One PR's outcome this cycle. ``action`` ∈ {merged, would-merge, skipped,
    failed}; ``reason`` is the human-readable why."""

    pr: int
    head: str
    title: str
    action: str
    reason: str

    def line(self) -> str:
        verb = {
            "merged": "merged",
            "would-merge": "WOULD merge",
            "skipped": "skip",
            "failed": "FAILED to merge",
        }.get(self.action, self.action)
        return f"  automerge: {verb} #{self.pr} ({self.head}) — {self.reason}"


def _check_summary(rollup: Optional[list]) -> tuple[int, int]:
    """``(failing, pending)`` counts over a PR's ``statusCheckRollup``. A check
    is failing if its conclusion/state is in :data:`_FAILED_CHECK`; pending if
    it has not COMPLETED (CheckRun) or its state is green-pending. SUCCESS /
    NEUTRAL / SKIPPED count as neither."""
    failing = pending = 0
    for c in rollup or []:
        conclusion = (c.get("conclusion") or "").upper()
        status = (c.get("status") or "").upper()
        state = (c.get("state") or "").upper()
        if conclusion in _FAILED_CHECK or state in _FAILED_CHECK:
            failing += 1
        elif state in _PENDING_CHECK or (status and status != "COMPLETED"):
            pending += 1
    return failing, pending


#: The refine pipeline stamps each commit SUBJECT (and the PR title) with the
#: lake build verdict — ``[build:OK]`` / ``[build:FAIL]`` / ``[build:TIMEOUT]``
#: (see ``post_pipeline.run_post_pipeline``, the ``[build:{tag}]`` writer — the
#: SINGLE source of this format; keep them in sync). On a CI-less repo the
#: ``statusCheckRollup`` is empty, so this commit tag is the ONLY machine-readable
#: build signal the gate has. Read from the commit (immutable, travels on the PR
#: head), NOT the title (a human can rename it).
_BUILD_TAG_RE = re.compile(r"\[build:(OK|FAIL|TIMEOUT)\]", re.IGNORECASE)


def _head_build_verdict(commits: Optional[list]) -> Optional[str]:
    """The lake build verdict — ``"OK"`` / ``"FAIL"`` / ``"TIMEOUT"`` — parsed
    from the HEAD commit's subject of a PR (the LAST element of ``gh``'s
    ``commits`` array, which is chronological — newest last). None when there are
    no commits or the head subject carries no ``[build:*]`` tag. The head commit
    governs: an earlier commit's stale verdict is ignored (a later fixing commit
    is what counts)."""
    if not commits:
        return None
    head = commits[-1] if isinstance(commits[-1], dict) else {}
    m = _BUILD_TAG_RE.search(head.get("messageHeadline") or "")
    return m.group(1).upper() if m else None


def eligibility(
    pr: dict,
    *,
    base: str,
    exclude_heads: "frozenset[str]" = frozenset(),
    require_checks: bool = True,
    require_build_ok: bool = True,
) -> tuple[bool, str]:
    """Decide whether ``pr`` (a parsed ``gh`` JSON object) may be auto-merged
    onto ``base``. Returns ``(ok, reason)``. PURE — fully unit-testable.

    Fail-safe gates, cheapest first. EVERY gate defaults to SKIP: a PR is merged
    only when it AFFIRMATIVELY clears all of them. ``exclude_heads`` holds the
    head branches of in-flight refines that must never be merged+deleted out
    from under a running job."""
    # Defense in depth beyond `gh pr list --base`: never merge a PR whose ACTUAL
    # base is not the one we were asked to land (a stale/buggy list, or a PR
    # retargeted between the list and the merge, must not slip through).
    if (pr.get("baseRefName") or "") != base:
        return False, f"targets {pr.get('baseRefName') or '?'}, not {base}"
    head = pr.get("headRefName") or ""
    if head in exclude_heads:
        return False, "branch in use by a running refine"
    if pr.get("isDraft"):
        return False, "draft"
    mergeable = (pr.get("mergeable") or "").upper()
    state = (pr.get("mergeStateStatus") or "").upper()
    if mergeable == "CONFLICTING" or state == "DIRTY":
        return False, "merge conflicts"
    if mergeable != "MERGEABLE":
        # UNKNOWN / empty / anything GitHub has not resolved to MERGEABLE — never
        # guess; the next cycle re-checks once it settles.
        return False, f"not mergeable ({mergeable or '?'}/{state or '?'} — retry)"
    if state == "BLOCKED":
        return False, "blocked by branch protection (needs review/approval)"
    if state not in _MERGEABLE_STATES:
        # An unrecognized / future mergeStateStatus — fail safe, retry next cycle.
        return False, f"unrecognized merge state {state} (retry next cycle)"
    failing, pending = _check_summary(pr.get("statusCheckRollup"))
    if failing:
        return False, f"{failing} check(s) failing"
    if require_checks and pending:
        return False, f"{pending} check(s) still pending"
    # Build verdict from the head commit's [build:*] tag — on a CI-less repo this
    # is the ONLY build signal (statusCheckRollup is empty, so the check gate
    # above is a no-op). The contract: "an unattended merge cannot land a red
    # build on main". A red tag is refused on ANY head; a refine head must
    # AFFIRMATIVELY carry [build:OK] (its absence = unknown build → fail safe and
    # retry), while a human/infra PR — legitimately untagged and NOT on a refine
    # branch — is left untouched.
    verdict = _head_build_verdict(pr.get("commits"))
    if verdict in ("FAIL", "TIMEOUT"):
        return False, f"build {verdict.lower()} (head tagged [build:{verdict}])"
    if require_build_ok and verdict is None and _REFINE_BRANCH_RE.match(head):
        return False, "no [build:*] verdict on refine head (retry/needs build)"
    return True, "clean" if state in ("", "CLEAN") else state.lower()


def _pr_files(repo: str, pr: int) -> Optional[list[str]]:
    """The repo-relative paths a PR changes, or None on a ``gh`` failure."""
    cp = gh(
        "pr", "view", str(pr),
        "--repo", repo,
        "--json", "files",
        "--jq", ".files[].path",
        check=False,
    )
    if cp.returncode != 0:
        return None
    return [ln for ln in cp.stdout.splitlines() if ln.strip()]


def _infra_only(repo: str, pr: int, reason: str) -> tuple[bool, str]:
    """``scope="infra"`` gate: True only when EVERY changed file is under
    :data:`SAFE_INFRA_PREFIX` and not a ``.lean`` file (so a proof PR is never
    auto-merged). A PR whose file list cannot be read is SKIPPED (retry)."""
    files = _pr_files(repo, pr)
    if files is None:
        return False, "could not list PR files (retry next cycle)"
    if files and all(
        f.startswith(SAFE_INFRA_PREFIX) and not f.endswith(".lean") for f in files
    ):
        return True, f"{reason} (infra-only)"
    return False, "touches non-infra files (left for deck review)"


def _list_open_prs(repo: str, base: str) -> Optional[list[dict]]:
    """Open PRs targeting ``base`` with the gate fields, or None on a ``gh``
    failure (the caller treats that as "nothing to do this cycle")."""
    cp = gh(
        "pr", "list",
        "--repo", repo,
        "--base", base,
        "--state", "open",
        "--json", _PR_FIELDS,
        check=False,
    )
    if cp.returncode != 0:
        print(
            "  automerge: WARN — `gh pr list` failed: "
            f"{(cp.stderr or cp.stdout).strip()[:200]}"
        )
        return None
    try:
        data = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        print("  automerge: WARN — could not parse `gh pr list` JSON")
        return None
    return data if isinstance(data, list) else None


#: Preference order for the merge method — the FIRST one the repo allows is
#: used. Squash first (cleanest history for these single-purpose PRs), then a
#: merge commit, then rebase. A repo that disallows merge commits (common) thus
#: lands a squash instead of failing.
_MERGE_METHOD_FLAGS = (
    ("--squash", "squashMergeAllowed"),
    ("--merge", "mergeCommitAllowed"),
    ("--rebase", "rebaseMergeAllowed"),
)


def _repo_merge_method(repo: str) -> Optional[str]:
    """The gh merge-method flag the repo ALLOWS (``--squash`` / ``--merge`` /
    ``--rebase``), preferring :data:`_MERGE_METHOD_FLAGS` order. None when the
    repo settings cannot be read (the caller falls back to a plain merge)."""
    cp = gh(
        "repo", "view", repo,
        "--json", "squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed",
        check=False,
    )
    if cp.returncode != 0:
        return None
    try:
        allowed = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return None
    for flag, key in _MERGE_METHOD_FLAGS:
        if allowed.get(key):
            return flag
    return None


def _requeue_refined_issue(
    cfg: "ReviewConfig", head: Optional[str], pr_num: Optional[int] = None,
    *, landed: bool = True,
) -> Optional[int]:
    """If ``head`` is a refine branch (``marathon/refine-c<N>-i<M>``), re-queue
    issue ``M`` for re-review: remove the ``review:rejected`` GitHub label AND
    clear the local verdict state, so the issue reads as unreviewed and the
    deck surfaces it as a ready card again. Returns the issue number it
    re-queued, or None. Best-effort — a failure here never fails the merge.

    ``landed`` (default True — Aristotle's fix actually MERGED) posts a
    RECOVERY comment that closes the notification loop and supersedes any
    dangling 'Auto-refine stalled' comment (the 2026-07-07 perceived outage:
    every recovery step was GitHub-silent while the stall comment stayed the
    newest signal for hours). Pass ``landed=False`` from the no-PR-landed
    caller (a refine that exited clean with no diff → nothing merged): a
    'fix landed' comment there would be a public LIE — that path re-queues for
    re-review of UNCHANGED code, so it posts nothing (the deck surfaces it)."""
    m = _REFINE_BRANCH_RE.match(head or "")
    if not m:
        return None
    issue = int(m.group(1))
    rejected_label = getattr(getattr(cfg, "labels", None), "rejected", "review:rejected")
    try:
        gh("issue", "edit", str(issue), "--repo", cfg.github_repo,
           "--remove-label", rejected_label, check=False)
    except Exception:  # noqa: BLE001
        pass
    try:
        from marathon.review.state import requeue_for_rereview

        requeue_for_rereview(cfg, issue)
    except Exception:  # noqa: BLE001
        pass
    if landed:
        try:
            merged_in = f" (merged in PR #{pr_num})" if pr_num is not None else ""
            gh(
                "issue", "comment", str(issue), "--repo", cfg.github_repo,
                "--body",
                (
                    f"♻️ **Auto-refine recovered** — Aristotle's fix for this "
                    f"rejection landed{merged_in}. The issue is back in the "
                    f"deck's review queue for re-review. (Any earlier "
                    f"\"Auto-refine stalled\" comment above is superseded.)"
                ),
                check=False,
            )
        except Exception:  # noqa: BLE001 — the comment is best-effort, never gates
            pass
    return issue


def _merge_pr(repo: str, pr: int, method: str) -> tuple[bool, str]:
    """Merge one PR with the repo's allowed ``method`` + delete the source
    branch. Returns ``(ok, detail)``."""
    cp = gh(
        "pr", "merge", str(pr),
        "--repo", repo,
        method,
        "--delete-branch",
        check=False,
    )
    if cp.returncode == 0:
        return True, f"merged ({method.lstrip('-')}) + deleted branch"
    return False, (cp.stderr or cp.stdout).strip()[:200]


def automerge(
    cfg: "ReviewConfig",
    *,
    base: Optional[str] = None,
    exclude_heads: "frozenset[str]" = frozenset(),
    scope: str = "any",
    require_checks: bool = True,
    require_build_ok: bool = True,
    dry_run: bool = False,
) -> list[AutoMergeResult]:
    """Merge every eligible open PR targeting ``base`` (default the config's
    auto-PR base, else ``main``). ONE cycle — the daemon/conductor calls it on
    each poll. Returns a per-PR :class:`AutoMergeResult` list (also logged).

    * ``scope="any"`` (default): any eligible PR; ``scope="infra"``: only PRs
      whose every changed file is under ``.marathon/`` (proof PRs stay
      deck-gated — see :func:`_infra_only`).
    * ``exclude_heads``: head branches of in-flight refines, never merged
      (deleting the branch would break a running job's push).
    * ``dry_run`` reports what WOULD merge but calls no ``gh pr merge``.
    * ``require_checks=False`` merges a conflict-free PR with PENDING checks
      (still never a FAILING one).
    * ``require_build_ok=True`` (default) refuses any head tagged ``[build:FAIL]``
      / ``[build:TIMEOUT]`` and requires an affirmative ``[build:OK]`` on a refine
      head — the only build gate on a CI-less repo. Set False to merge an untagged
      refine head (an explicit red tag is STILL refused)."""
    base = base or getattr(cfg, "auto_pr_base", None) or "main"
    repo = cfg.github_repo
    prs = _list_open_prs(repo, base)
    if prs is None:
        return []

    # The repo's allowed merge method (squash/merge/rebase) — resolved LAZILY on
    # the first real merge (so empty / dry-run / all-skipped cycles never query
    # repo settings) and reused. A repo that disallows merge commits lands a
    # squash instead of failing; unreadable settings fall back to a merge commit.
    method: Optional[str] = None

    results: list[AutoMergeResult] = []
    for pr in prs:
        num = pr.get("number")
        head = pr.get("headRefName") or "?"
        title = (pr.get("title") or "")[:80]
        # A PR with no usable integer number can never be safely merged — skip
        # it loudly rather than crash the whole cycle on int(None).
        if not isinstance(num, int):
            results.append(AutoMergeResult(
                -1, head, title, "skipped", f"PR has no usable number ({num!r})"
            ))
            continue
        ok, reason = eligibility(
            pr, base=base, exclude_heads=exclude_heads,
            require_checks=require_checks, require_build_ok=require_build_ok,
        )
        if ok and scope == "infra":
            ok, reason = _infra_only(repo, num, reason)
        if not ok:
            results.append(AutoMergeResult(num, head, title, "skipped", reason))
            continue
        if dry_run:
            results.append(AutoMergeResult(num, head, title, "would-merge", reason))
            continue
        if method is None:
            method = _repo_merge_method(repo) or "--merge"
        merged, detail = _merge_pr(repo, num, method)
        if merged:
            requeued = _requeue_refined_issue(cfg, head, pr_num=num)
            if requeued is not None:
                detail += f"; re-queued #{requeued} for re-review"
        results.append(AutoMergeResult(
            num, head, title, "merged" if merged else "failed", detail
        ))

    merged_n = sum(1 for r in results if r.action == "merged")
    would_n = sum(1 for r in results if r.action == "would-merge")
    if results and (merged_n or would_n or any(r.action == "failed" for r in results)):
        verb = "would merge" if dry_run else "merged"
        print(
            f"  automerge: {len(prs)} open PR(s) onto {base}; "
            f"{verb} {would_n or merged_n}",
            flush=True,
        )
        for r in results:
            if r.action != "skipped":
                print(r.line(), flush=True)
    return results

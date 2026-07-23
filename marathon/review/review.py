"""Review review-loop command handlers.

These are the per-sub-issue review commands: ``list`` / ``next`` /
``show`` / ``verify`` / ``reject`` / ``refine-status`` / ``refine-start``
/ ``refine-stop``. Each takes a parsed ``argparse`` namespace and
operates against a ``ReviewConfig`` loaded from the current repo.

The command wiring lives in ``marathon.review.cli`` (which is
invoked by ``marathon review ...``); this module holds the logic so
it can also be tested / scripted directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional

from marathon.review.automerge import _head_build_verdict
from marathon.review.config import ReviewConfig, load_config
from marathon.review.github import (
    converge_issue_labels_rest,
    fetch_issues_bulk,
    gh,
    issue_labels,
    issue_title,
    post_issue_comment_rest,
)
from marathon.review.state import record_rejection, record_verification
from marathon.review.tracker import update_tracker_emoji


# --- Status reporting --------------------------------------------------------


def _status_from_labels(cfg: ReviewConfig, labels: set[str]) -> Optional[str]:
    """Map a label set → 'verified' / 'inflight' / 'rejected' / None."""
    if cfg.labels.verified in labels:
        return "verified"
    if cfg.labels.inflight in labels:
        return "inflight"
    if cfg.labels.rejected in labels:
        return "rejected"
    return None


def get_issue_status(cfg: ReviewConfig, num: int) -> Optional[str]:
    """Returns 'verified', 'rejected', 'inflight', or None (unreviewed)."""
    labels = issue_labels(num, cfg.github_repo)
    if labels is None:
        return None
    return _status_from_labels(cfg, labels)


# --- list / next / show ------------------------------------------------------


def _bulk_registry_meta(
    cfg: ReviewConfig, registry
) -> Optional[dict[int, dict]]:
    """Fetch metadata for every issue in ``registry`` in one GraphQL call.

    Returns the ``fetch_issues_bulk`` dict, or None when the bulk call
    failed — in which case a warning is printed (degraded performance
    must be visible, not silent) and the caller should fall back to the
    per-issue ``gh issue view`` helpers.
    """
    nums = [num for num, _ in registry.entries]
    meta = fetch_issues_bulk(nums, cfg.github_repo)
    if meta is None:
        print(
            "  warn: bulk GraphQL issue fetch failed; "
            "falling back to one `gh issue view` per issue (slower)",
            file=sys.stderr,
        )
    return meta


# --- tier integration (Phase 5b — additive; guarded by new flags) ------------
#
# The tier column (`list --tiers`) and the floor filter (`next
# --min-tier`) are the ONLY tier-aware behavior in this module, and both
# are fully guarded: without the flag the commands run byte-for-byte as
# before (Ch.12's seven first-reviews must never stall behind tooling
# that has not run yet — crit-feas §6).


def _load_tier_index(cfg: ReviewConfig):
    """``(tiers_by_decl, note)``: ``{decl_name: TierResult}`` from the
    latest audit snapshot + ledger, or ``({}, note)`` when no snapshot
    exists. The note is a single line the caller prints so a missing
    snapshot degrades visibly (a '-' column with one explanation), never
    silently and never as an error — the audit engine may simply not
    have run for this repo yet."""
    # Local imports: keep the review CLI importable on a checkout whose
    # audit/ledger deps are lazy, and mirror the trust core's own local
    # imports across the audit↔review boundary.
    from marathon.audit.engine import load_snapshot
    from marathon.audit.trust import compute_tiers
    from marathon.ledger import Ledger

    snapshot = load_snapshot(cfg.repo_dir)
    if snapshot is None:
        return {}, (
            "  note: no audit snapshot (`marathon audit run` has not run "
            "for this repo) — tier column is '-'"
        )
    ledger = Ledger.for_repo(cfg.repo_dir)
    return {r.decl_name: r for r in compute_tiers(snapshot, ledger)}, None


def _issue_tier(
    cfg: ReviewConfig,
    num: int,
    body: Optional[str],
    tiers_by_decl: dict,
) -> tuple[Optional[str], list[str]]:
    """The tier of issue ``num`` = the WORST computed tier over the Lean
    declarations its body cites (a sub-issue is only as trustworthy as
    its least-trusted decl), with the names whose tier produced it.

    Reuses ``verified_decls.extract_declarations_from_issue_body`` (the
    single parser of the card format — never forked). Returns
    ``(tier or None, decl_names)``; None when the body is unavailable or
    cites no decl present in the snapshot — the caller renders '-' and
    does not gate on an unknown."""
    from marathon.audit.trust import TIER_ORDER
    from marathon.review.verified_decls import (
        extract_declarations_from_issue_body,
    )

    if body is None or not tiers_by_decl:
        return None, []
    cited = extract_declarations_from_issue_body(body)
    matched: list[tuple[str, str]] = []  # (decl_name, tier)
    for raw in sorted(cited):
        result = tiers_by_decl.get(raw)
        if result is None:
            # Issue bodies cite names as written (often unqualified);
            # the snapshot is fully qualified. Unique dotted-suffix
            # match, same deterministic rule as `audit show` / backfill.
            suffix = [
                r for name, r in tiers_by_decl.items()
                if name.endswith("." + raw)
            ]
            if len(suffix) == 1:
                result = suffix[0]
        if result is not None:
            matched.append((result.decl_name, result.tier))
    if not matched:
        return None, []
    worst = min(matched, key=lambda m: TIER_ORDER.index(m[1]))
    return worst[1], [name for name, _ in matched]


def cmd_list(args) -> None:
    cfg = load_config()
    registry = cfg.chapter_registry(args.chapter)
    meta = _bulk_registry_meta(cfg, registry)
    show_tiers = getattr(args, "tiers", False)
    tiers_by_decl: dict = {}
    if show_tiers:
        tiers_by_decl, note = _load_tier_index(cfg)
        if note is not None:
            print(note)
    print(
        f"Sub-issues of #{cfg.parent_issue} in review order "
        f"(chapter {args.chapter}):\n"
    )
    tier_hdr = f"  {'tier':<7}" if show_tiers else ""
    tier_rule = f"  {'----':<7}" if show_tiers else ""
    print(f"  {'idx':>3}  {'issue':>6}  {'status':<10}{tier_hdr}  title")
    print(f"  {'---':>3}  {'-----':>6}  {'------':<10}{tier_rule}  -----")
    for idx, (num, _) in enumerate(registry.entries, 1):
        if meta is not None and num in meta:
            status = _status_from_labels(cfg, meta[num]["labels"]) or "unreviewed"
            title = meta[num]["title"]
            body = meta[num].get("body")
        else:
            # Bulk call failed, or this one issue was absent from the
            # bulk response — per-issue fallback.
            status = get_issue_status(cfg, num) or "unreviewed"
            title = issue_title(num, cfg.github_repo)
            body = None
        tier_col = ""
        if show_tiers:
            tier, _ = _issue_tier(cfg, num, body, tiers_by_decl)
            tier_col = f"  {(tier or '-'):<7}"
        marker = "→ " if status == "unreviewed" else "  "
        print(f"  {marker}{idx:>2}  #{num:>4}  {status:<10}{tier_col}  {title}")
    print()


def cmd_next(args) -> None:
    cfg = load_config()
    registry = cfg.chapter_registry(args.chapter)
    meta = _bulk_registry_meta(cfg, registry)
    min_tier = getattr(args, "min_tier", None)
    tiers_by_decl: dict = {}
    if min_tier is not None:
        from marathon.audit.trust import TIER_ORDER

        tiers_by_decl, note = _load_tier_index(cfg)
        if note is not None:
            print(note)
        floor_rank = TIER_ORDER.index(min_tier)
    for num, _ in registry.entries:
        if meta is not None and num in meta:
            status = _status_from_labels(cfg, meta[num]["labels"])
            body = meta[num].get("body")
        else:
            status = get_issue_status(cfg, num)
            body = None
        if status is not None:
            continue  # already reviewed — unchanged behavior
        if min_tier is not None:
            from marathon.audit.trust import TIER_ORDER

            tier, names = _issue_tier(cfg, num, body, tiers_by_decl)
            if tier is not None and TIER_ORDER.index(tier) < floor_rank:
                joined = ", ".join(names)
                print(
                    f"  skipping #{num}: tier {tier} below floor "
                    f"{min_tier} (decls: {joined})"
                )
                continue
        print(f"Next unreviewed sub-issue: #{num}\n")
        _show_issue(cfg, num)
        return
    suffix = f" at or above {min_tier}" if min_tier is not None else ""
    print(f"All chapter {args.chapter} sub-issues are reviewed{suffix}. 🎉")


def _show_issue(cfg: ReviewConfig, num: int) -> None:
    cp = gh("issue", "view", str(num), "--repo", cfg.github_repo)
    print(cp.stdout)


def cmd_show(args) -> None:
    cfg = load_config()
    _show_issue(cfg, args.issue_num)


# --- open (interactive Claude Code session in VS Code) -----------------------


def cmd_open(args) -> None:
    """``marathon review open <issue_num>`` — spawn an interactive Claude
    Code chat in the user's VS Code via the URI handler, pre-populated
    with the issue's context + chapter queue + `@`-mention list."""
    from marathon.review.open_session import open_session_for_issue

    cfg = load_config()
    num = args.issue_num
    try:
        result = open_session_for_issue(
            cfg, num,
            include_file_mentions=not args.no_attach,
            dry_run=args.dry_run,
        )
    except RuntimeError as e:
        sys.exit(str(e))

    if args.dry_run:
        print(f"# dry-run for `marathon review open {num}`")
        print(f"# prompt_chars = {result.prompt_chars} / 5000")
        print(f"# files listed = {result.files_listed!r}")
        if result.truncated:
            print(
                "# note: prompt was trimmed to fit the 5000-char "
                "VS Code URI ceiling"
            )
        print()
        print(result.uri)
        return

    print(f"opened interactive Claude Code session for #{num}")
    print(f"  prompt_chars = {result.prompt_chars} / 5000")
    if result.truncated:
        print(
            "  note: the issue body and/or pending-queue section was "
            "trimmed (with a `…[trimmed]…` marker) to fit the 5000-char "
            "VS Code URI ceiling. Use `gh issue view {num}` once the chat "
            "opens to inspect the full body if needed."
        )
    if result.files_listed:
        print("  files to @-mention once the chat opens:")
        for m in result.files_listed:
            print(f"    {m}")


# --- bootstrap-chapter / audit-chapter (chapter-scale sessions) ---------------


def cmd_bootstrap_chapter(args) -> None:
    """``marathon review bootstrap-chapter --chapter N [--informal-statements F]``

    One-time setup pass: opens a chapter-bootstrap coreviewer session in
    VS Code that drafts a `Chapter{N}.md` drafts file by pairing every
    Lean declaration in the chapter folder with sections of the
    user-supplied informal-statements file (or LLM-rendered statements
    if none is provided). The agent proposes the full sub-issue list
    and waits for human go-ahead before creating any GitHub issues."""
    from marathon.review.chapter_sessions import open_chapter_session
    cfg = load_config()
    informal: Optional[Path] = None
    if args.informal_statements:
        informal = Path(args.informal_statements)
        if not informal.is_file():
            sys.exit(f"informal-statements file not found: {informal}")
    try:
        result = open_chapter_session(
            cfg, args.chapter, "bootstrap",
            informal_statements_path=informal,
            dry_run=args.dry_run,
        )
    except RuntimeError as e:
        sys.exit(str(e))

    if args.dry_run:
        print(f"# dry-run for `marathon review bootstrap-chapter --chapter {args.chapter}`")
        print(f"# briefing written to: {result.briefing_path}")
        print(f"# pointer prompt_chars = {result.prompt_chars} / 5000")
        print()
        print(result.uri)
        return

    print(f"opened chapter-bootstrap session for chapter {args.chapter}")
    print(f"  briefing: {result.briefing_path}")
    print(f"  pointer prompt_chars: {result.prompt_chars} / 5000")
    if informal is None:
        print(
            "  note: no --informal-statements file was provided; the "
            "coreviewer will LLM-render Informal Statements with the "
            "`⚠️ verification pending` marker. Pass --informal-statements "
            "<file> next time to skip that."
        )


def cmd_audit_chapter(args) -> None:
    """``marathon review audit-chapter --chapter N``

    Maintenance pass: opens a chapter-audit coreviewer session that
    cross-references every existing sub-issue body against current code,
    identifies drift / coverage gaps / readability passes, proposes a
    unified edit set, and waits for human go-ahead."""
    from marathon.review.chapter_sessions import open_chapter_session
    cfg = load_config()
    try:
        result = open_chapter_session(
            cfg, args.chapter, "audit",
            dry_run=args.dry_run,
        )
    except RuntimeError as e:
        sys.exit(str(e))

    if args.dry_run:
        print(f"# dry-run for `marathon review audit-chapter --chapter {args.chapter}`")
        print(f"# briefing written to: {result.briefing_path}")
        print(f"# pointer prompt_chars = {result.prompt_chars} / 5000")
        print()
        print(result.uri)
        return

    print(f"opened chapter-audit session for chapter {args.chapter}")
    print(f"  briefing: {result.briefing_path}")
    print(f"  pointer prompt_chars: {result.prompt_chars} / 5000")


# --- lock (pre-proving statement freeze) -------------------------------------


def cmd_lock(args) -> None:
    """Hard-freeze the STATEMENTS of declarations into the approved-statements
    manifest, so no subsequent fill/refine/repair can change a signature — the
    guarantee to establish BEFORE the proving / sorry-filling stage (Aristotle
    fills bodies, never re-types a locked statement).

    Targets: ``--issue N`` (repeatable), ``--chapter N`` (all its verified
    sub-issues, repeatable), and/or ``--all-verified`` (every registered
    chapter's verified sub-issues). Reuses ``capture_on_verify`` per issue."""
    from marathon.review.approved_statements import capture_on_verify
    from marathon.review.verified_decls import verified_declarations

    cfg = load_config()
    issues: set[int] = set(getattr(args, "issue", None) or [])
    chapters = (
        list(cfg.chapters or {}) if getattr(args, "all_verified", False)
        else list(getattr(args, "chapter", None) or [])
    )
    for ch in chapters:
        try:
            issues |= {int(k) for k in verified_declarations(cfg, ch)}
        except Exception as e:  # noqa: BLE001
            print(f"  chapter {ch}: could not list verified sub-issues ({e})")
    if not issues:
        print("nothing to lock — pass --issue N, --chapter N, or --all-verified")
        return
    total = 0
    for num in sorted(issues):
        try:
            frozen = capture_on_verify(cfg, num)
        except Exception as e:  # noqa: BLE001
            print(f"  #{num}: lock failed ({e})")
            continue
        total += len(frozen)
        tail = f" — {', '.join(frozen)}" if frozen else " (no cited decls resolved)"
        print(f"  #{num}: froze {len(frozen)} declaration(s){tail}")
    print(
        f"\nLOCKED {total} declaration(s) across {len(issues)} issue(s) into "
        f"{cfg.repo_dir / '.marathon/review/approved_statements.json'}. A "
        "fill/refine/repair that changes a frozen signature is now reverted "
        "before commit."
    )


# --- verify ------------------------------------------------------------------


def _ensure_verified_label(cfg: "ReviewConfig", num: int) -> bool:
    """Add ``review:verified`` (and drop ``review:rejected``), CONFIRMING the
    *entire final label state* landed on GitHub. Retries once and prints a loud
    warning unless verified is present AND rejected is absent. A mixed label
    state is not success: queue status gives rejection precedence, so recording
    a local verification while ``review:rejected`` remains would strand the
    issue in a split brain."""
    if converge_issue_labels_rest(
        num,
        cfg.github_repo,
        required=(cfg.labels.verified,),
        forbidden=(cfg.labels.rejected,),
    ):
        return True
    print(
        f"  ⚠ label: could NOT confirm the final verified-only label state on "
        f"#{num} after 2 attempts — no local verification was recorded. "
        "Retry Ship after GitHub connectivity recovers."
    )
    return False


def cmd_verify(args) -> None:
    cfg = load_config()
    num = args.issue_num
    if args.close:
        default_comment = (
            "✅ VERIFIED — declaration matches the textbook reference and is "
            "fully implemented. See body for findings."
        )
    else:
        default_comment = (
            "✅ VERIFIED — declaration matches the textbook reference. "
            "See body for findings. Sub-issue stays open to track remaining "
            "`sorry` bodies; pass `--close` to verify if the declaration is "
            "fully implemented."
        )
    comment = args.comment or default_comment

    print(f"Marking #{num} as VERIFIED...")
    operation_id = getattr(args, "operation_id", None)
    if operation_id:
        post_issue_comment_rest(
            num, cfg.github_repo, comment, operation_id=operation_id
        )
    else:
        post_issue_comment_rest(num, cfg.github_repo, comment)
    # Add the verified label + drop any rejected one, CONFIRMING it landed. The
    # deck's "already verified" status keys off this label, so a SILENT failure
    # here (the old `check=False` that swallowed a transient gh error) leaves the
    # issue verified-locally (T2 ledger, tracker flipped) but UNLABELED → the
    # deck re-surfaces it for review with no explanation, and the human
    # re-verifies into the same silent failure (the #23/#24 re-surfacing bug).
    if not _ensure_verified_label(cfg, num):
        # This exception intentionally crosses the deck/apply_verdict boundary:
        # SHIP remains retryable and its local subdeck is not cleared.  The
        # previous behavior continued into record_verification despite a failed
        # GitHub transition, making local state say verified while the queue's
        # rejected label kept the issue permanently rejected.
        raise RuntimeError(
            f"verification of #{num} was not recorded because GitHub labels "
            "did not converge to verified-only; retry after connectivity "
            "recovers"
        )
    # Clear any prior rejection queue entry for this issue. Idempotent
    # if there was no prior rejection. This also fires capture_on_verify
    # (freezes the verified statements into the approved-statements
    # manifest), so by the time we reach the T2 step below the verify
    # has already fully landed.
    record_verification(cfg, num)

    # Promote the verified issue's cited declarations T1 → T2 in the
    # trust ladder: a human spec-verdict pinned to the current type +
    # cone fingerprints. Always-on (the intended default), but fully
    # best-effort — a missing snapshot, a ledger error, or any other
    # failure must NOT roll back or fail the verify (the label flip,
    # state.json write, and capture_on_verify already succeeded). Mirrors
    # capture_on_verify's never-block-a-verify contract.
    _grant_t2_on_verify(cfg, num)

    # If --auto-pr was used, the iteration's commits sit in a
    # marathon-owned branch with an open PR. Verifying the issue
    # should also merge that PR so the iteration lands on main —
    # otherwise the labels say "verified" but the code is still in
    # a pending PR. Best-effort: skips when no matching PR exists
    # (e.g., older issues that pre-date --auto-pr), and a merge
    # failure must never abort the verification (the label flip
    # already happened) — but it must be SEEN, so failures print a
    # WARN rather than being swallowed.
    try:
        _maybe_merge_marathon_pr(cfg, num)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"  pr: WARN — auto-merge step failed for #{num}: {e}")

    # Tracker flip lives here, NOT inside _maybe_merge_marathon_pr: the
    # parent-issue emoji must go 🟠→🟡 whether or not there is a PR to
    # merge (issues that pre-date --auto-pr have no PR at all).
    # update_tracker_emoji uses gh(check=True), which raises
    # RuntimeError on gh failure — downgrade to a visible WARN.
    try:
        ok, msg = update_tracker_emoji(cfg, num, "🟡")
    except (RuntimeError, OSError) as e:
        ok, msg = False, f"update failed: {e}"
    if ok:
        print(f"  tracker: {msg}")
    else:
        print(f"  tracker: WARN — {msg}")

    if args.close:
        gh("issue", "close", str(num), "--repo", cfg.github_repo)
        print(f"✅ #{num} verified + closed (fully implemented), tracker → 🟡.")
    else:
        gh("issue", "reopen", str(num), "--repo", cfg.github_repo, check=False)
        print(
            f"✅ #{num} verified (statements accepted; sorrys remain — "
            "issue kept OPEN), tracker → 🟡."
        )


def _grant_t2_on_verify(cfg: ReviewConfig, issue_num: int) -> None:
    """Best-effort: promote issue ``issue_num``'s cited declarations
    T1 → T2 by appending a human spec-verdict pinned to the CURRENT
    snapshot fingerprints.

    Wires the LIVE verify flow into the trust ladder. Without this, only
    the one-time ``--attest`` backfill ever wrote ``decl_verdicts``
    events, so freshly-verified declarations stayed stuck at T1 forever.

    Contract (mirrors ``capture_on_verify``): this NEVER fails or rolls
    back the verify. The verify has already fully landed (label flip +
    state.json + frozen-statements manifest) by the time we are called;
    the entire body is wrapped in a broad ``try/except`` that downgrades
    any failure — absent snapshot, ledger error, parser error — to a
    printed warning and returns normally.

    Per-decl, it reuses the SAME machinery as the backfill: the same
    issue-body decl extractor ``capture_on_verify`` uses, the same
    ``_resolve_decl`` (handles namespace-unqualified citations), the
    same ``_cone_pins`` / ``_already_pinned`` idempotency check, and the
    same ``record_spec_verdict`` writer. A decl that is absent / unknown
    / unpinnable in the snapshot is skipped with a short reason
    (``record_spec_verdict`` would raise ``TrustError`` for it); a decl
    already pinned to the current fingerprints appends NO duplicate event
    (re-verify is idempotent).

    No reject-side revocation is needed: a reject leads to a refine that
    changes the decl, and any type/cone-fingerprint change auto-degrades
    the T2 on read (the pin no longer matches the live snapshot), so an
    explicit revocation would be redundant.
    """
    try:
        from marathon.audit.engine import load_snapshot
        from marathon.audit.trust import (
            TrustError,
            _already_pinned,
            _cone_pins,
            _resolve_decl,
            record_spec_verdict,
        )
        from marathon.review.github import issue_body
        from marathon.review.verified_decls import (
            extract_declarations_from_issue_body,
        )

        body = issue_body(issue_num, cfg.github_repo)
        if not body:
            return
        cited = extract_declarations_from_issue_body(body)
        if not cited:
            return

        snap = load_snapshot(cfg.repo_dir)
        if snap is None:
            print(
                "  trust: skipped (no audit snapshot — run `marathon audit "
                "run` to enable T2 promotion on verify)"
            )
            return

        ledger = _ledger(cfg)
        by_name = snap.by_name()

        promoted = 0
        already = 0
        skipped: list[str] = []
        for raw in sorted(cited):
            decl, why = _resolve_decl(raw, by_name)
            if decl is None or decl.is_unknown or decl.fingerprint_type is None:
                skipped.append(raw)
                continue
            pins, unpinnable = _cone_pins(decl, by_name)
            if unpinnable:
                skipped.append(decl.name)
                continue
            if _already_pinned(
                ledger.decl_verdict_events(decl.name),
                decl,
                pins,
                snap.toolchain,
            ):
                already += 1
                continue
            try:
                record_spec_verdict(
                    ledger,
                    decl.name,
                    snap,
                    tier="T2",
                    issue_num=issue_num,
                    source="deck-verify",
                )
            except TrustError:
                skipped.append(decl.name)
                continue
            promoted += 1

        summary = f"  trust: {promoted} decl(s) → T2"
        if already:
            summary += f", {already} already T2"
        if skipped:
            summary += (
                f"; skipped {len(skipped)} (unaudited/unpinnable): "
                + ", ".join(sorted(skipped))
            )
        print(summary)
    except Exception as e:  # noqa: BLE001 — never fail/rollback a verify
        print(f"  trust: WARN — T2 promotion step failed for #{issue_num}: {e}")


def _ledger(cfg: ReviewConfig):
    """Open the canonical ledger for ``cfg`` — the same path used by the
    deck (``marathon.deck.cards._ledger``), the state.json dual-write
    mirror, and ``Ledger.for_review_config`` everywhere else."""
    from marathon.ledger import Ledger

    return Ledger.for_review_config(cfg)


def _maybe_merge_marathon_pr(cfg: ReviewConfig, issue_num: int) -> None:
    """Find the open marathon PR for ``issue_num`` and merge it.

    PR is identified by head branch ``marathon/refine-c<N>-i<issue_num>``
    where N is derived from the chapter the issue lives in. Skips
    silently ONLY when no matching open PR exists (the legitimate
    no-op: the iteration didn't go through --auto-pr, or the PR was
    already merged). Every failure mode — unregistered issue, gh
    error, unmergeable PR — prints a WARN so it can't go unnoticed;
    historically a bare ``except Exception: return`` here meant no
    verify ever merged a PR and nobody knew.

    The tracker-emoji update deliberately does NOT live here — it
    belongs to ``cmd_verify`` so it runs even when there is no PR.
    """
    chapter = cfg.chapter_of_issue(issue_num)
    if chapter is None:
        print(
            f"  pr: WARN — #{issue_num} not in any registered chapter; "
            "cannot derive the marathon branch name to look for a PR"
        )
        return

    branch = f"marathon/refine-c{chapter}-i{issue_num}"
    # Look for an open PR with this head branch — fetch its head-commit build tag
    # in the SAME call so the gate below reads it without a second round-trip.
    proc = gh(
        "pr", "list",
        "--repo", cfg.github_repo,
        "--head", branch,
        "--state", "open",
        "--json", "number,commits",
        check=False,
    )
    if proc.returncode != 0:
        print(
            f"  pr: WARN — `gh pr list` failed for head {branch}: "
            f"{(proc.stderr or proc.stdout).strip()[:200]}"
        )
        return
    try:
        prs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        print(f"  pr: WARN — could not parse `gh pr list` JSON for head {branch}")
        return
    if not prs:
        return  # no open PR — the normal silent skip
    pr = prs[0] if isinstance(prs, list) else None
    pr_num = pr.get("number") if isinstance(pr, dict) else None
    if not isinstance(pr_num, int):
        print(
            f"  pr: WARN — unexpected `gh pr list` output for {branch}: "
            f"{str(pr)[:200]!r}"
        )
        return
    # Build-verdict gate — the verify-path twin of the automerge gate
    # (:func:`marathon.review.automerge._head_build_verdict`). NEVER merge a
    # refine PR whose head build is RED: verifying an issue's STATEMENT does not
    # certify its BUILD, and merging a ``[build:FAIL]``/``[build:TIMEOUT]`` head
    # would put a red build on main (the #212 class — a non-compiling pullback
    # once reached main this way). Human-initiated, so an UNTAGGED head is allowed
    # through (the human is watching); only an EXPLICIT red verdict is refused,
    # and the human can still merge by hand if they truly intend to land it red.
    verdict = _head_build_verdict(pr.get("commits"))
    if verdict in ("FAIL", "TIMEOUT"):
        print(
            f"  pr: WARN — NOT merging #{pr_num} ({branch}): head build is "
            f"[build:{verdict}]. #{issue_num} is verified, but the build is red — "
            f"fix forward with a new refine iteration, or `gh pr merge {pr_num}` "
            f"by hand to land it red."
        )
        return
    # Merge with --delete-branch so the marathon branch goes away on
    # success — the iteration's content is on main, no need to keep
    # the source branch around.
    merge = gh(
        "pr", "merge", str(pr_num),
        "--repo", cfg.github_repo,
        "--merge",
        "--delete-branch",
        check=False,
    )
    if merge.returncode == 0:
        print(f"  pr: merged #{pr_num} ({branch}) + deleted branch")
    else:
        # Common case: PR has merge conflicts because main moved.
        # Surface the error but don't block the verification — the
        # label flip already happened.
        print(
            f"  pr: WARN — could not merge #{pr_num} ({branch}); "
            f"{(merge.stderr or merge.stdout).strip()[:200]}"
        )


# --- reject ------------------------------------------------------------------


def _ensure_rejected_label(cfg: "ReviewConfig", num: int) -> bool:
    """Converge GitHub to rejected-present/verified-absent and confirm it."""
    if converge_issue_labels_rest(
        num,
        cfg.github_repo,
        required=(cfg.labels.rejected,),
        forbidden=(cfg.labels.verified,),
    ):
        return True
    print(
        f"  ⚠ label: could NOT confirm the final rejected-only label state "
        f"on #{num}; no local rejection was recorded or dispatched. Retry "
        "after GitHub connectivity recovers."
    )
    return False


def _ensure_unreviewed_labels(cfg: "ReviewConfig", num: int) -> bool:
    """Converge GitHub to neither verdict label and confirm it.

    This is the compensation path when a requeue's label transition wins but
    its local CAS loses to a concurrent merge/review that removed the state
    entry. Without compensation the issue would look rejected on GitHub while
    being unreviewed locally.
    """
    if converge_issue_labels_rest(
        num,
        cfg.github_repo,
        forbidden=(cfg.labels.rejected, cfg.labels.verified),
    ):
        return True
    print(
        f"  ⚠ label: could NOT confirm unreviewed labels on #{num}; "
        "repair the GitHub labels before reviewing it again"
    )
    return False


def cmd_reject(args) -> None:
    cfg = load_config()
    num = args.issue_num
    notes_path = Path(args.notes)
    if notes_path.is_file():
        notes_text = notes_path.read_text()
    else:
        notes_text = args.notes
    notes_text = notes_text.strip()
    if not notes_text:
        sys.exit("rejection notes are empty; supply --notes <file-or-text>")

    print(f"Marking #{num} as REJECTED...")
    comment = args.comment or (
        "❌ REJECTED — see body for findings."
    )
    # Mirror the verbatim note into the comment inside an HTML-comment marker
    # (invisible in render) so a collaborator's conductor that discovers its
    # queue from GitHub can recover the exact note. Backward-compatible: old
    # readers ignore it; a comment override still gets the note appended.
    from marathon.review.reject_note import embed_reject_note

    comment = f"{comment}\n\n{embed_reject_note(notes_text)}"
    try:
        operation_id = getattr(args, "operation_id", None)
        if operation_id:
            post_issue_comment_rest(
                num, cfg.github_repo, comment, operation_id=operation_id
            )
        else:
            post_issue_comment_rest(num, cfg.github_repo, comment)
    except RuntimeError as exc:
        raise RuntimeError(
            f"GitHub projection failed for rejection #{num}; no rejection "
            "was queued. The deck's saved declaration decisions remain safe "
            f"to retry: {exc}"
        ) from exc
    if not _ensure_rejected_label(cfg, num):
        raise RuntimeError(
            f"rejection of #{num} was not recorded because GitHub labels did "
            "not converge to rejected-only; retry after connectivity recovers"
        )
    record_rejection(cfg, num, notes_text)
    print(f"  recorded rejection at {cfg.state_path}")
    print(f"❌ #{num} rejected and queued for refinement.")

    if not args.no_refine:
        # The single repo-level CONDUCTOR (the global orchestrator) dispatches
        # this rejection across ALL chapters and auto-merges eligible PRs — it
        # replaces the old per-chapter refine daemons. Ensure it is running;
        # the rejection is already queued in state.json for it to pick up.
        _ensure_conductor(cfg)


# --- refine daemon control ---------------------------------------------------


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _live_conductor_pid(cfg: ReviewConfig) -> Optional[int]:
    """PID of a live repo-level conductor, or None. The lock location is
    the conductor's own (imported, not re-derived) and the liveness
    semantics mirror its stale-lock handling — a dead PID reads as
    'no conductor', so the next rejection/start can relaunch the one global
    orchestrator."""
    from marathon.conductor import conductor_lock_path
    lock = conductor_lock_path(cfg)
    if not lock.is_file():
        return None
    try:
        pid = int(lock.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if _process_alive(pid) else None


@dataclass(frozen=True)
class ConductorStartResult:
    """Outcome of an idempotent conductor start request.

    ``started`` is true only for the caller that actually spawned a child.
    Other callers receive the already-live (or still-starting) PID so a
    double-click cannot create a second launch attempt.
    """

    pid: int
    started: bool
    log_path: Optional[Path] = None


_conductor_start_lock = Lock()
_pending_conductor_starts: dict[str, tuple[subprocess.Popen, Path]] = {}


def _pending_conductor_pid_locked(repo_key: str) -> Optional[int]:
    """Return a still-starting child PID, reaping an exited child first.

    Caller holds ``_conductor_start_lock``. Retaining and polling the Popen is
    essential for a long-lived deck process: ``kill(pid, 0)`` reports a zombie
    as alive until its parent reaps it.
    """

    pending = _pending_conductor_starts.get(repo_key)
    if pending is None:
        return None
    pending_proc, _pending_log = pending
    if pending_proc.poll() is None:
        return pending_proc.pid
    _pending_conductor_starts.pop(repo_key, None)
    return None


def refresh_pending_conductor_start(cfg: ReviewConfig) -> Optional[int]:
    """Observe/reap a deck-launched child; return its PID while still running.

    This never launches, signals, or removes a lock. The dashboard calls it
    before reading ``conductor.lock`` so a crashed child cannot remain a zombie
    that is permanently displayed as a live conductor.
    """

    repo_key = str(Path(cfg.repo_dir).resolve())
    with _conductor_start_lock:
        return _pending_conductor_pid_locked(repo_key)


def start_conductor(cfg: ReviewConfig) -> ConductorStartResult:
    """Idempotently start the repo-level conductor, detached from this process.

    This is the explicit lifecycle primitive used by both the CLI/rejection
    wrapper and the deck's opt-in manual recovery control.  It deliberately
    does not inspect ``MARATHON_NO_HOST_CONDUCTOR``: that variable governs
    *automatic* reject-triggered launches in :func:`_ensure_conductor`, while a
    server must separately opt in before exposing the authenticated manual
    control.
    """

    with _conductor_start_lock:
        repo_dir = getattr(cfg, "repo_dir", None)
        repo_key = str(Path(repo_dir).resolve()) if repo_dir is not None else None
        pending_pid = (
            _pending_conductor_pid_locked(repo_key)
            if repo_key is not None
            else None
        )

        # Poll/reap our retained child *before* consulting the PID lock:
        # kill(pid, 0) reports an unreaped zombie as alive.
        pid = _live_conductor_pid(cfg)
        if pid is not None:
            return ConductorStartResult(pid=pid, started=False)
        if pending_pid is not None:
            pending_log = _pending_conductor_starts[repo_key][1]
            return ConductorStartResult(
                pid=pending_pid, started=False, log_path=pending_log
            )

        if repo_key is None:
            # A real ReviewConfig always has repo_dir; keeping this lookup here
            # preserves the cheap already-live probe used by focused callers.
            repo_key = str(Path(cfg.repo_dir).resolve())

        cfg.runner_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = (
            cfg.runner_log_dir
            / f"conductor-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.log"
        )
        with log_path.open("w") as log_fh:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "marathon",
                    "conductor",
                    "run",
                    "--repo-dir",
                    str(cfg.repo_dir),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(cfg.repo_dir),
            )
        _pending_conductor_starts[repo_key] = (proc, log_path)
        return ConductorStartResult(
            pid=proc.pid, started=True, log_path=log_path
        )


def _ensure_conductor(cfg: ReviewConfig) -> None:
    """Ensure the single repo-level CONDUCTOR — the global orchestrator that
    replaces the old per-chapter refine daemons — is running.

    The conductor polls ``pending_rejections_needing_iteration`` across ALL
    chapters (oldest verdict first), dispatches Aristotle refines in isolated
    worktrees, AND auto-merges eligible PRs each cycle. Singleton via the
    conductor lock: if one is already alive this is a no-op note; otherwise
    spawn ``marathon conductor run`` detached (it survives this process). The
    rejection is already recorded in state.json for it to pick up.

    If ``MARATHON_NO_HOST_CONDUCTOR`` is set (a SHARED deck, where the expensive
    Aristotle compute should be billed to a collaborator's OWN key, not the
    host's), this spawns NOTHING: the rejection is still recorded in state.json
    for that collaborator's own conductor — running on their machine with their
    ARISTOTLE_API_KEY — to pick up and refine.

    PRECONDITION for that guarantee: do NOT also run a host conductor on the
    shared deck host. This flag only prevents THIS process from SPAWNING one; an
    already-running host conductor polls the same shared state.json and would
    drain the queue under the host's credentials regardless."""
    if os.environ.get("MARATHON_NO_HOST_CONDUCTOR"):
        print(
            "  host-side conductor disabled (MARATHON_NO_HOST_CONDUCTOR); this "
            "rejection is recorded in state.json for a collaborator's own "
            "conductor to refine under their own key"
        )
        return
    result = start_conductor(cfg)
    if not result.started:
        print(
            f"  repo-level conductor already active (pid {result.pid}); this rejection "
            "is queued in state.json — it dispatches on its next poll"
        )
        return
    print(
        f"  repo-level conductor launched (pid {result.pid}); it dispatches "
        "refines + auto-merges PRs across all chapters. "
        f"log: {result.log_path}"
    )


def cmd_refine_status(args) -> None:
    """Report the GLOBAL conductor started by :func:`cmd_refine_start`.

    ``--chapter`` is only a display filter for jobs.  The previous
    implementation still inspected the retired ``refine-cN.lock``, so a
    successful ``refine-start`` was immediately reported as "no daemon active".
    """
    cfg = load_config()
    chapter = getattr(args, "chapter", None)
    from marathon.conductor import conductor_lock_path, load_jobs_snapshot

    lock = conductor_lock_path(cfg)
    pid = _live_conductor_pid(cfg)
    if pid is None:
        if lock.is_file():
            print("global conductor: stale/unreadable lock; removing")
            lock.unlink(missing_ok=True)
        else:
            print("global conductor: not active")
        return

    scope = f" (showing chapter {chapter})" if chapter is not None else ""
    print(f"global conductor active (pid {pid}){scope}")
    snapshot = load_jobs_snapshot(cfg.repo_dir) or {}
    jobs = snapshot.get("jobs") or []
    if chapter is not None:
        jobs = [j for j in jobs if j.get("chapter") == chapter]
    running = [j for j in jobs if j.get("status") == "running"]
    if running:
        for job in running:
            print(
                f"  #{job.get('issue_num')} chapter {job.get('chapter')}: "
                f"{job.get('aristotle_status') or 'running'} "
                f"(pid {job.get('pid') or '?'})"
            )
    else:
        print("  no running refine jobs in this view")

    logs = sorted(cfg.runner_log_dir.glob("conductor-*.log"))
    if logs:
        latest = logs[-1]
        print(f"  log: {latest}")
        print(f"  --- tail -20 {latest.name} ---")
        tail = subprocess.run(["tail", "-20", str(latest)], capture_output=True, text=True)
        print(tail.stdout)


def cmd_refine_stop(args) -> None:
    """Stop the same GLOBAL conductor that ``refine-start`` launches."""
    import signal as _sig
    cfg = load_config()
    chapter = getattr(args, "chapter", None)
    from marathon.conductor import conductor_lock_path

    lock = conductor_lock_path(cfg)
    pid = _live_conductor_pid(cfg)
    if pid is None:
        if lock.is_file():
            print("global conductor: stale/unreadable lock; removing")
            lock.unlink(missing_ok=True)
        else:
            print("global conductor: not active")
        return
    try:
        os.kill(pid, _sig.SIGTERM)
        suffix = (
            f" (--chapter {chapter} is a compatibility argument; the global "
            "conductor spans all chapters)"
            if chapter is not None else ""
        )
        print(f"global conductor: sent SIGTERM to pid {pid}{suffix}")
        print(
            "  no Aristotle jobs are canceled; the conductor stops new "
            "dispatches and waits for running jobs to finish"
        )
    except (OSError, ProcessLookupError) as e:
        print(f"global conductor: could not signal pid {pid}: {e}")


def cmd_refine_start(args) -> None:
    # The per-chapter daemon is retired: "start refining" now ensures the single
    # repo-level conductor (the global orchestrator) is up. ``--chapter`` is
    # accepted for backward compatibility but ignored (the conductor spans all).
    cfg = load_config()
    _ensure_conductor(cfg)


# --- subissues bulk commands -------------------------------------------------


def cmd_subissues_create(args) -> None:
    """``marathon review subissues create <drafts.md> [--skip N,M]``."""
    from marathon.review.subissues import create_subissues_from_drafts
    cfg = load_config()
    drafts_path = Path(args.drafts_file)
    if not drafts_path.is_file():
        sys.exit(f"drafts file not found: {drafts_path}")
    skip = {int(x) for x in (args.skip or "").split(",") if x.strip()}
    create_subissues_from_drafts(cfg, drafts_path, skip=skip)


def cmd_subissues_refresh(args) -> None:
    """``marathon review subissues refresh <drafts.md> [--only N,M]``."""
    from marathon.review.subissues import refresh_subissue_bodies
    cfg = load_config()
    drafts_path = Path(args.drafts_file)
    if not drafts_path.is_file():
        sys.exit(f"drafts file not found: {drafts_path}")
    only = {int(x) for x in (args.only or "").split(",") if x.strip()}
    refresh_subissue_bodies(cfg, drafts_path, only=only or None)

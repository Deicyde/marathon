"""Auto-un-verify a verified review issue when its reviewable content changes.

A human ``marathon review verify N`` freezes each cited declaration's
normalized signature into the approved-statements manifest
(:func:`marathon.review.approved_statements.capture_on_verify`) and records
``status='verified'`` in ``state.json``. That snapshot is taken once, at
verify time. But the reviewable *set* an issue cites can drift afterwards —
the issue body is edited to add a new carded decl, a frozen decl's statement
is changed in code, or a decl is renamed/removed — without anyone re-reviewing
the new shape. The deck then shows the issue as verified while the thing it
verified no longer exists in that form.

This module closes that gap:

* :func:`verified_staleness` — diff an issue's CURRENT reviewable set against
  the manifest record frozen at verify time. CONSERVATIVE by construction: any
  source-read failure for a decl is treated as UNCHANGED (never a false flag),
  and an empty manifest-for-issue (nothing was ever frozen for it) is never
  stale.

* :func:`unverify_for_rereview` — re-open a stale verified issue for
  re-review: drop the ``review:verified`` GitHub label, delete the issue's
  ``state.json`` entry (so the deck reads it as unreviewed), and post ONE
  summary comment. The approved-statements manifest is deliberately LEFT
  FROZEN — a later statement change still diffs against the original frozen
  signature, and the per-card verdicts in deck-subdecks.json carry on their
  own (they survive a state.json deletion, see ``subdeck._effective``).

The conductor calls :func:`unverify_for_rereview` from a wall-clock-throttled
pass (a new card can arrive via an issue-body edit that does NOT move HEAD, so
the pass is NOT HEAD-gated like signature reconcile).
"""

from __future__ import annotations

from typing import Optional

from marathon.review.config import ReviewConfig


def verified_staleness(cfg: ReviewConfig, issue_num: int) -> tuple[bool, list[str]]:
    """Diff issue ``issue_num``'s CURRENT reviewable set against the manifest
    record frozen for it at verify time. Returns ``(is_stale, reasons)``.

    ``manifest-for-issue`` is the subset of the approved-statements manifest
    whose ``ApprovedStatement.issue == issue_num`` (the decls THIS verify
    froze). ``current`` is the carded decl set parsed from the live issue body
    (:func:`extract_declarations_from_issue_body`). We classify:

    * **new card** — a current carded decl NOT in manifest-for-issue, AND
      FREEZABLE the way :func:`capture_on_verify` resolves a signature
      (present in chapter source OR defined by a ```lean block in the body).
      A name-only ``### Lean declarations`` citation that capture could never
      have frozen is NOT a new card — flagging it would re-open an unchanged
      verified issue forever.
    * **changed statement** — a manifest decl still present in code whose
      current normalized signature differs from the frozen one.
    * **removed/renamed** — a manifest decl no longer in the current carded
      set, OR no longer found in code.

    CONSERVATIVE — never false-flag:

    * If manifest-for-issue is empty (nothing was ever frozen for this issue —
      e.g. capture failed at verify, or a pre-manifest verify), return
      ``(False, [])``: there is no frozen baseline to diff against.
    * If the issue body can't be fetched, return ``(False, [])``.
    * For a "changed statement" check, a decl whose CURRENT signature can't be
      read from source (decl absent → ``extract_signature`` returns None) is
      NOT flagged as changed here — absence is handled by the removed/renamed
      branch, which requires the decl to ALSO have dropped from the carded set
      OR be genuinely gone from code. A transient empty/unreadable source
      therefore yields no signature for ANY decl, so we additionally guard:
      when the whole chapter source comes back empty — OR a PARTIAL read failed
      (one ``.lean`` file unreadable while siblings succeed) — we treat every
      frozen decl as still-present-unchanged (no source-derived flags), because
      such a source is indistinguishable from a read failure and must never
      re-open everything.
    """
    from marathon.review.approved_statements import (
        _chapter_source_read,
        extract_signature,
        load_manifest,
    )

    manifest = load_manifest(cfg.repo_dir)
    frozen = {
        name: st for name, st in manifest.items() if st.issue == issue_num
    }
    if not frozen:
        # No frozen baseline for this issue — nothing to diff, never stale.
        return (False, [])

    body = _issue_body_safe(cfg, issue_num)
    if body is None:
        # Couldn't read the issue body — conservatively not stale.
        return (False, [])

    current = _current_carded_decls(body)
    # The issue body's own ```lean text (fences stripped) — the SAME fallback
    # signature source capture_on_verify reads from. A decl freezable here
    # (extract_signature finds a block defining it) is one capture WOULD have
    # frozen even when it is absent from chapter source.
    body_lean = _issue_body_lean(body)

    chapter = None
    try:
        chapter = cfg.chapter_of_issue(issue_num)
    except Exception:  # noqa: BLE001 — registry lookup must never crash the pass
        chapter = None
    src, src_read_failed = (
        _chapter_source_read(cfg, chapter) if chapter is not None else ("", False)
    )
    # An empty source string is indistinguishable from a read failure (the
    # folder is gone, a checkout is mid-swap, every file failed to read). A
    # PARTIAL read (one .lean file unreadable while siblings succeed) is just as
    # untrustworthy: a frozen decl living in the unread file reads as
    # removed/changed though the source is fine. In either case we have no
    # reliable evidence ANY frozen decl changed or vanished, so we suppress the
    # source-derived checks (changed / removed-from-code) and only run the
    # body-only "new card" + "dropped-from-cards" checks below.
    have_src = bool(src.strip()) and not src_read_failed

    reasons: list[str] = []

    # new card: a current carded decl that THIS issue never froze. Only count it
    # if capture COULD have frozen it — i.e. it is FREEZABLE exactly as
    # capture_on_verify resolves a signature: present in chapter source
    # (extract_signature(src, name) is not None) OR defined by a ```lean
    # signature block in the body (extract_signature(body_lean, name) is not
    # None). A name-only `### Lean declarations` citation that is neither is
    # carded but was never freezable → absent from the manifest by design, NOT
    # a re-open trigger. (When the source read is untrustworthy we cannot rule
    # out in-source freezability, so we treat any carded-but-unfrozen decl with
    # a body signature OR fall back to body-only freezability.)
    for name in sorted(current):
        if name in frozen:
            continue
        in_source = have_src and extract_signature(src, name) is not None
        in_body = extract_signature(body_lean, name) is not None
        if in_source or in_body:
            reasons.append(f"new card: {name}")

    for name in sorted(frozen):
        in_cards = name in current
        cur_sig = extract_signature(src, name) if have_src else None
        if not in_cards:
            # No longer carded → removed/renamed regardless of source state.
            reasons.append(f"removed/renamed: {name}")
            continue
        # Still carded. Only consult source if we have a trustworthy source.
        if not have_src:
            continue
        if cur_sig is None:
            # Carded but absent from code → genuinely gone (removed/renamed).
            reasons.append(f"removed/renamed: {name}")
            continue
        if cur_sig != frozen[name].signature:
            reasons.append(f"changed statement: {name}")

    return (bool(reasons), reasons)


def unverify_for_rereview(
    cfg: ReviewConfig, issue_num: int, reasons: list[str]
) -> bool:
    """Re-open a verified issue for re-review because its reviewable content
    changed. Returns True on success.

    Steps (each best-effort, none aborts the others):

    1. Remove the ``review:verified`` GitHub label (``gh issue edit
       --remove-label``).
    2. Delete the issue's ``state.json`` entry — shares
       :func:`marathon.review.state.requeue_for_rereview`'s deletion so the
       deck reads the issue as unreviewed and it becomes a ready card again.
    3. Post ONE summary comment listing ``reasons``.

    The approved-statements manifest is deliberately NOT touched: leaving the
    frozen signatures in place means a LATER statement change still diffs
    against the original baseline (re-verifying re-captures the new shape). The
    per-card verdicts in deck-subdecks.json are also untouched — they survive
    the state.json deletion and carry via ``subdeck._effective``, so cards that
    did NOT change keep their verdicts; only the new/changed cards read as
    unreviewed.
    """
    from marathon.review.github import gh
    from marathon.review.state import requeue_for_rereview

    # 1. Drop the verified label (best-effort: a label-edit failure must not
    #    block the state deletion — the deck's read-side truth is state.json).
    try:
        gh(
            "issue", "edit", str(issue_num),
            "--repo", cfg.github_repo,
            "--remove-label", cfg.labels.verified,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  reverify: WARN — could not remove verified label from "
              f"#{issue_num} ({e})")

    # 2. Delete the state.json entry so the deck re-surfaces the issue.
    removed = requeue_for_rereview(cfg, issue_num)

    # 3. Post one summary comment.
    try:
        gh(
            "issue", "comment", str(issue_num),
            "--repo", cfg.github_repo,
            "--body", _reopen_comment(reasons),
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  reverify: WARN — could not comment on #{issue_num} ({e})")

    return removed


# --- helpers ---------------------------------------------------------------


def _reopen_comment(reasons: list[str]) -> str:
    """One-line-per-bucket re-open comment summarizing ``reasons``.

    Buckets the raw ``"<kind>: <name>"`` reason strings back into new-card /
    changed-statement / removed-renamed groups so the comment reads cleanly
    even with many decls."""
    new_cards = [r.split(": ", 1)[1] for r in reasons if r.startswith("new card:")]
    changed = [
        r.split(": ", 1)[1] for r in reasons if r.startswith("changed statement:")
    ]
    removed = [
        r.split(": ", 1)[1] for r in reasons if r.startswith("removed/renamed:")
    ]
    parts: list[str] = []
    if new_cards:
        parts.append(f"new card(s): {', '.join(new_cards)}")
    if changed:
        parts.append(f"changed statement(s): {', '.join(changed)}")
    if removed:
        parts.append(f"removed/renamed: {', '.join(removed)}")
    summary = "; ".join(parts) if parts else "reviewable content changed"
    return (
        f"♻️ Re-opened for re-review — {summary}. "
        "Unchanged cards keep their verdicts."
    )


def _issue_body_safe(cfg: ReviewConfig, issue_num: int) -> Optional[str]:
    """Fetch the issue body, returning None on any failure (so the caller can
    treat an unreadable body as conservatively-not-stale)."""
    try:
        from marathon.review.github import issue_body

        return issue_body(issue_num, cfg.github_repo)
    except Exception:  # noqa: BLE001
        return None


def _issue_body_lean(body: str) -> str:
    """The issue body's own ```lean block contents (fences stripped, joined) —
    the SAME fallback signature text :func:`capture_on_verify` reads when a decl
    isn't in chapter source. Returns "" if there are no ```lean blocks (then no
    decl is body-freezable)."""
    try:
        from marathon.review.approved_statements import _LEAN_BLOCK_RE

        return "\n\n".join(m.group(1) for m in _LEAN_BLOCK_RE.finditer(body))
    except Exception:  # noqa: BLE001
        return ""


def _current_carded_decls(body: str) -> set[str]:
    """The current carded declaration set parsed from an issue body — the
    same extraction the deck/audit/capture use."""
    try:
        from marathon.review.verified_decls import (
            extract_declarations_from_issue_body,
        )

        return extract_declarations_from_issue_body(body)
    except Exception:  # noqa: BLE001
        return set()

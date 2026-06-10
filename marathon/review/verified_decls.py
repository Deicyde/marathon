"""Post-iteration audit: did this refine iteration modify a declaration
that the human already verified?

Once the human runs ``marathon review verify N``, the cited Lean
declaration(s) in that sub-issue's body should be frozen. Subsequent
``marathon refine`` iterations targeting other rejections in the same
chapter should leave verified declarations alone. This module
implements a post-iteration audit that compares the just-landed
commit's diff against the set of verified declaration names extracted
from every verified sub-issue's GitHub body, and flags any overlap.

Soft-warning behavior (v1):

* Print a loud per-violation line to the iteration log.
* Save a structured JSONL record to
  ``<workdir>/marathon-audit-violations.jsonl``.
* Attach a note to the iteration state.
* Do **not** auto-revert. The user inspects and decides.

Possible follow-ups (deferred):

* Pre-prompt warning: pass the verified-declaration list into the
  Hermes prompt so Aristotle is told upfront what to leave alone.
* Auto-revert: ``git checkout HEAD~1 -- <file>`` for the offending
  files, then re-commit.
* Auto-re-reject: post a comment on each violated verified sub-issue
  asking the daemon to undo the change.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from marathon.review.config import ReviewConfig
from marathon.review.github import fetch_issues_bulk, gh, issue_labels


# Lean declaration-keyword set we care about. ``opaque`` and ``axiom``
# included for completeness; ``example`` deliberately excluded (it's
# anonymous + not a tracked declaration). ``mutual ... end`` blocks
# contain nested decls — each inner ``def`` / ``theorem`` matches the
# regex independently.
DECL_KEYWORDS = (
    "def",
    "theorem",
    "lemma",
    "abbrev",
    "instance",
    "structure",
    "class",
    "inductive",
    "opaque",
    "axiom",
)

# Match: optional `@[...]` attribute clusters (e.g. `@[simp]`,
# `@[ext]`, `@[fun_prop, mk_iff]` — possibly multiple separated by
# whitespace), then optional word-prefix modifiers (`noncomputable`,
# `private`, `protected`), then a decl keyword, then the identifier
# (possibly dotted, possibly with `'`). Anonymous `instance`
# declarations (no name) don't match — they're considered "unnamed"
# and excluded from both extraction sides.
_ATTR = r"(?:@\[[^\]]*\]\s*)*"
_MOD = r"(?:noncomputable\s+)?(?:private\s+)?(?:protected\s+)?"
_DECL_RE = re.compile(
    r"^\s*" + _ATTR + _MOD
    + r"(?:" + "|".join(DECL_KEYWORDS) + r")\s+"
    + r"(?P<name>[A-Za-z_][\w'.]*)",
    re.MULTILINE,
)


@dataclass
class AuditViolation:
    """A verified declaration that was modified by the iteration."""
    decl_name: str
    issue_num: int
    issue_title: Optional[str] = None


@dataclass
class AuditResult:
    violations: list[AuditViolation]
    verified_issue_count: int
    verified_decl_count: int
    modified_decl_count: int

    def has_violations(self) -> bool:
        return bool(self.violations)


# --- extraction ------------------------------------------------------


def extract_declarations_from_lean_block(lean_code: str) -> set[str]:
    """Find Lean declaration names in a code block.

    Returns a set of bare names (no namespace qualification stripping
    or addition — the name is whatever follows the keyword, dots
    included). Anonymous declarations are not returned.
    """
    return {m.group("name") for m in _DECL_RE.finditer(lean_code)}


_LEAN_BLOCK_RE = re.compile(r"```lean\n(.*?)\n```", re.DOTALL)


def extract_declarations_from_issue_body(body: str) -> set[str]:
    """Parse ` ```lean ... ``` ` code blocks in an issue body and return
    the union of Lean declarations they contain.

    A typical sub-issue body has a ``### Lean signatures`` section with
    one or more ` ```lean ``` ` blocks; this function scans every
    ``` ```lean ``` ``` block in the body, not just the signatures
    section, so declarations cited elsewhere (e.g. in mechanical-
    accuracy bullets) are also counted as verified.
    """
    out: set[str] = set()
    for m in _LEAN_BLOCK_RE.finditer(body):
        out.update(extract_declarations_from_lean_block(m.group(1)))
    return out


def _fetch_body(num: int, repo: str) -> Optional[str]:
    cp = gh(
        "issue", "view", str(num),
        "--repo", repo,
        "--json", "body",
        "--jq", ".body",
        check=False,
    )
    if cp.returncode != 0:
        return None
    return cp.stdout


def _fetch_title(num: int, repo: str) -> Optional[str]:
    cp = gh(
        "issue", "view", str(num),
        "--repo", repo,
        "--json", "title",
        "--jq", ".title",
        check=False,
    )
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


def verified_declarations(
    cfg: ReviewConfig, chapter: int
) -> dict[int, set[str]]:
    """For every verified sub-issue in ``chapter``, fetch its body and
    extract the set of Lean declaration names it cites. Returns
    ``{issue_num: {decl_name, ...}}``.

    Sub-issues that lack the ``review:verified`` label are skipped.
    Body-fetch failures are logged (stdout, with the iteration log) and
    skipped.

    Labels + bodies for the whole registry are fetched in ONE GraphQL
    call (``fetch_issues_bulk``); previously this was 1–2 ``gh issue
    view`` subprocesses per issue, serially, on every post-iteration
    audit. If the bulk call fails we warn and fall back to the
    per-issue path so the audit still runs.
    """
    registry = cfg.chapter_registry(chapter)
    meta = fetch_issues_bulk(
        [num for num, _ in registry.entries], cfg.github_repo
    )
    if meta is None:
        print(
            "  audit: warning: bulk GraphQL issue fetch failed; "
            "falling back to per-issue gh calls (slower)"
        )
    out: dict[int, set[str]] = {}
    for num, _pattern in registry.entries:
        if meta is not None and num in meta:
            labels: set[str] = meta[num]["labels"]
            body: Optional[str] = meta[num]["body"]
        else:
            # Bulk call failed, or this one issue was absent from the
            # bulk response — per-issue fallback.
            labels = issue_labels(num, cfg.github_repo) or set()
            body = None  # fetched below only if the label gate passes
        if cfg.labels.verified not in labels:
            continue
        if body is None:
            body = _fetch_body(num, cfg.github_repo)
        if body is None:
            print(f"  audit: warning: could not fetch body for #{num}; skipping")
            continue
        decls = extract_declarations_from_issue_body(body)
        if decls:
            out[num] = decls
    return out


def declarations_modified_in_diff(
    repo_dir: Path, ref_old: str, ref_new: str
) -> set[str]:
    """Run ``git diff <ref_old>..<ref_new> -- '*.lean'`` and return the
    set of declaration names that appear on changed (+ or -) lines.

    Uses ``--unified=0`` so context lines don't pollute the match (we
    only want changed lines). A decl name appearing on both a removed
    and an added line is counted once — typical for a body edit that
    keeps the signature unchanged.

    NOTE: false positives possible if a Lean keyword (`def`,
    `theorem`, etc.) appears inside a comment or string on a changed
    line. Empirically rare in this codebase and acceptable for a v1
    audit; the human can dismiss false positives by inspection.
    """
    cp = subprocess.run(
        [
            "git", "diff",
            f"{ref_old}..{ref_new}",
            "--unified=0",
            "--", "*.lean",
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode != 0:
        return set()
    out: set[str] = set()
    for line in cp.stdout.splitlines():
        # We want diff lines starting with '+' or '-' (but not '+++' / '---'
        # which are file headers).
        if (line.startswith("+") and not line.startswith("+++")) or (
            line.startswith("-") and not line.startswith("---")
        ):
            # Strip the leading +/- and match the decl regex.
            content = line[1:]
            m = _DECL_RE.match(content)
            if m:
                out.add(m.group("name"))
    return out


def audit_iteration(
    cfg: ReviewConfig,
    chapter: int,
    repo_dir: Path,
    ref_old: str,
    ref_new: str = "HEAD",
) -> AuditResult:
    """Run the post-iteration audit. Returns an ``AuditResult`` with
    the list of verified declarations that were modified between
    ``ref_old`` and ``ref_new``.

    Typical call site: after ``marathon refine``'s auto-commit step,
    with ``ref_old=HEAD~1`` and ``ref_new=HEAD``.
    """
    verified = verified_declarations(cfg, chapter)
    modified = declarations_modified_in_diff(repo_dir, ref_old, ref_new)

    violations: list[AuditViolation] = []
    for issue_num, decls in verified.items():
        violated = decls & modified
        if not violated:
            continue
        title = _fetch_title(issue_num, cfg.github_repo)
        for decl in sorted(violated):
            violations.append(AuditViolation(
                decl_name=decl,
                issue_num=issue_num,
                issue_title=title,
            ))

    total_verified_decls = sum(len(d) for d in verified.values())
    return AuditResult(
        violations=sorted(violations, key=lambda v: (v.issue_num, v.decl_name)),
        verified_issue_count=len(verified),
        verified_decl_count=total_verified_decls,
        modified_decl_count=len(modified),
    )


def write_audit_log(workdir: Path, result: AuditResult, ref_old: str, ref_new: str) -> Path:
    """Append the audit result to ``<workdir>/marathon-audit-violations.jsonl``.

    One JSON object per call (not per violation), so the file is a
    growing log of audit runs; the latest one is the last line.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = workdir / "marathon-audit-violations.jsonl"
    record = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ref_old": ref_old,
        "ref_new": ref_new,
        "verified_issue_count": result.verified_issue_count,
        "verified_decl_count": result.verified_decl_count,
        "modified_decl_count": result.modified_decl_count,
        "violations": [
            {
                "decl_name": v.decl_name,
                "issue_num": v.issue_num,
                "issue_title": v.issue_title,
            }
            for v in result.violations
        ],
    }
    with log_path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return log_path

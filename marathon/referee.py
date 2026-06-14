"""The ``marathon referee`` subcommand and ``update_referee`` helper.

Runs a Claude agent that scans the LeeSM repo + all per-chapter workdirs +
the existing ``standing-items.md``, and rewrites its content to reflect
the most pressing project-specific issues.

Two entry points:

- ``referee_command(args)`` — invoked via ``marathon referee``. One-shot
  pass with optional ``--review`` to write to ``standing-items.md.proposed``
  instead of overwriting.
- ``update_referee(...)`` — library function. (The auto-trigger from
  ``marathon refine``'s inner loop has been removed; run manually when
  you want a fresh standing-items snapshot.)

``standing-items.md`` is a **purely machine-managed** file written by
this module. ``referee.md`` is now purely user-managed and is no longer
touched by Claude — only the human edits failure modes / calibration
rules there.

Backward compat: the sentinel/header split logic is preserved on read
so that any legacy ``standing-items.md`` written by an older version
(with sentinels and an empty user-header) can be parsed and replaced.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from marathon.claude_proc import run_claude

REFEREE_FILENAME = ".marathon/standing-items.md"
REFEREE_PROPOSED_SUFFIX = ".proposed"
REFEREE_MODEL = "claude-opus-4-7"

# Large-context budget (plan §2 "Referee with teeth": summarize counts +
# top-N offenders, don't dump 400k chars). The ledger digest below caps
# every offender list at TOP_N rows so the prompt grows by counts, not by
# the project's whole declaration census — the repo Lean files already
# carry the per-decl detail under their own 400k cap.
DIGEST_TOP_N = 12

# Legacy sentinels. ``standing-items.md`` is purely machine-managed so
# new writes don't emit them, but reads still split on them in case an
# older file is on disk (or someone hand-pastes the legacy structure).
BEGIN_SENTINEL = "<!-- BEGIN: Marathon-managed referee tail (do not edit below this line; use `marathon referee` to refresh) -->"
END_SENTINEL = "<!-- END: Marathon-managed referee tail -->"

REFEREE_COAUTHOR_TRAILER = "Co-authored-by: Claude <noreply@anthropic.com>"


@dataclass
class RefereeResult:
    """Outcome of one referee pass."""
    ok: bool = False
    output_path: Optional[Path] = None
    commit_sha: Optional[str] = None
    diff_summary: Optional[str] = None  # short stats: lines +/-
    machine_tail_len: Optional[int] = None
    pushed: Optional[bool] = None  # None = not attempted; True/False = outcome
    push_message: Optional[str] = None
    skipped_reason: Optional[str] = None  # if we declined to run
    error: Optional[str] = None  # if Claude failed / output unparseable
    bloat_warnings: Optional[list[str]] = None  # see _check_tail_bloat
    task_emission: Optional["TaskEmissionResult"] = None  # if --emit-tasks


# Structural caps mirrored from referee_agent.md (rule 10). Violations
# are soft warnings, not errors: the agent is allowed to drift, but we
# print loudly so the human sees the bloat creeping in across passes.
_TAIL_LINE_CAP = 100             # rule 9 hard cap
_TAIL_LINE_TARGET = 80           # rule 9 target
_CLOSURES_BULLET_CAP = 5         # "Recent iteration closures" — rolling window
_TOP_LEVERAGE_BULLET_CAP = 6
_CALIBRATION_BULLET_CAP = 6
_NEXT_ITER_BULLET_CAP = 6


def _count_top_level_bullets(section_text: str) -> int:
    """Count lines that look like a top-level markdown bullet
    (``- ``/``* ``/numbered) at column 0. Sub-bullets (indented) are
    excluded — they're part of the parent bullet."""
    import re
    bullet_re = re.compile(r"^(?:[-*]|\d+\.)\s+")
    return sum(1 for line in section_text.splitlines() if bullet_re.match(line))


def _check_tail_bloat(machine_tail: str) -> list[str]:
    """Soft-check the new machine tail against referee_agent.md's rule
    10 / rule 9 structural caps. Returns a list of warning strings (one
    per violation). Empty list means clean.

    Section detection is deliberately tolerant: the agent may rename a
    subsection slightly, so we match by leading keyword rather than
    exact heading. Sections we don't recognize don't generate warnings.
    """
    import re

    warnings: list[str] = []

    total_lines = len(machine_tail.splitlines())
    if total_lines > _TAIL_LINE_CAP:
        warnings.append(
            f"machine tail is {total_lines} lines (>{_TAIL_LINE_CAP} hard cap; "
            f"target {_TAIL_LINE_TARGET}). Rule 9 violation — likely under-pruning."
        )
    elif total_lines > _TAIL_LINE_TARGET:
        warnings.append(
            f"machine tail is {total_lines} lines (>{_TAIL_LINE_TARGET} target, "
            f"≤{_TAIL_LINE_CAP} hard cap). Drifting; flag for next pass."
        )

    # Split into ### subsections so we can bullet-count each.
    # A subsection runs from one ``### `` heading to the next.
    section_heading_re = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
    matches = list(section_heading_re.finditer(machine_tail))
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        heading = m.group(1).lower()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(machine_tail)
        sections.append((heading, machine_tail[body_start:body_end]))

    # Forbidden section: output discipline belongs in the user header.
    for heading, _ in sections:
        if "output discipline" in heading:
            warnings.append(
                "machine tail contains an 'Output discipline' subsection — "
                "rule 10 violation. Belongs in the user-managed header."
            )

    # Per-section bullet caps.
    section_caps = (
        ("top-leverage", _TOP_LEVERAGE_BULLET_CAP, "Top-leverage open items"),
        ("iteration closure", _CLOSURES_BULLET_CAP, "Recent iteration closures"),
        ("calibration", _CALIBRATION_BULLET_CAP, "Calibration sharpening"),
        ("next-iter", _NEXT_ITER_BULLET_CAP, "Next-iter target priority"),
    )
    for keyword, cap, pretty in section_caps:
        for heading, body in sections:
            if keyword in heading:
                count = _count_top_level_bullets(body)
                if count > cap:
                    warnings.append(
                        f"section '{pretty}' has {count} bullets (>{cap} cap). "
                        "Rule 10 violation — prune older entries."
                    )
                break  # only check first matching section

    return warnings


def _split_referee(text: str) -> tuple[str, Optional[str]]:
    """Split an existing standing-items.md into (user_header, machine_tail).

    Since ``standing-items.md`` is purely machine-managed:
    - No sentinels → whole file is the prior machine tail, empty user
      header. (This is the new default for files written by this
      module's current ``_assemble_referee``.)
    - Sentinels present → legacy format; split into user_header /
      machine_tail at the sentinels as before.
    """
    if BEGIN_SENTINEL not in text or END_SENTINEL not in text:
        # No sentinel: pure machine-managed file — treat the whole
        # content as the prior machine tail with no user header.
        return "", text.strip()
    begin_idx = text.index(BEGIN_SENTINEL)
    end_idx = text.index(END_SENTINEL)
    if end_idx < begin_idx:
        # Malformed — fall back to "whole thing is the prior tail".
        return "", text.strip()
    user_header = text[:begin_idx].rstrip()
    machine_tail = text[begin_idx + len(BEGIN_SENTINEL):end_idx].strip()
    return user_header, machine_tail


def _assemble_referee(user_header: str, new_machine_tail: str) -> str:
    """Recombine user_header + new_machine_tail into a single body.

    ``standing-items.md`` is purely machine-managed, so when
    ``user_header`` is empty (the new default), we skip the sentinel
    markers entirely — the file is just the tail content plus a
    trailing newline. Legacy callers that supply a non-empty
    ``user_header`` still get the sentinel-bracketed form for
    backward compatibility.
    """
    if not user_header.strip():
        return new_machine_tail.strip() + "\n"
    parts = [user_header.rstrip()]
    parts.append("")  # blank line before sentinel
    parts.append(BEGIN_SENTINEL)
    parts.append("")
    parts.append(new_machine_tail.strip())
    parts.append("")
    parts.append(END_SENTINEL)
    parts.append("")  # trailing newline
    return "\n".join(parts)


def _read_chapter_artifacts(workdir: Path, max_chars_each: int = 8_000) -> str:
    """Bundle one chapter's marathon.md + ratings.jsonl tail + refine-log
    tail into a markdown section keyed by the chapter name. Truncates
    each artifact to ``max_chars_each`` (tail-biased, so the most recent
    content survives)."""
    import json

    state_path = workdir / "marathon-refine-state.json"
    if not state_path.is_file():
        return ""
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return ""
    target = state.get("target_folder") or ""
    chap_label = Path(target).name if target else workdir.name

    parts = [f"### {chap_label}"]
    parts.append(
        f"- status: {state.get('status')!r}, "
        f"iterations: {state.get('iterations_completed')}/"
        f"{state.get('current_iteration_idx')}"
    )

    marathon_md = workdir / "marathon.md"
    if marathon_md.is_file():
        try:
            text = marathon_md.read_text()
            if len(text) > max_chars_each:
                text = "... (earlier marathon.md content trimmed)\n" + text[-max_chars_each:]
            parts.append("#### marathon.md")
            parts.append(text)
        except OSError:
            pass

    ratings = workdir / "marathon-ratings.jsonl"
    if ratings.is_file():
        try:
            lines = [l for l in ratings.read_text().splitlines() if l.strip()]
            # Keep all rating entries — each is one line of JSON, parsed below.
            entries = []
            for line in lines:
                try:
                    d = json.loads(line)
                    r = d.get("rating") or {}
                    iter_n = d.get("iteration")
                    scores = (
                        f"q={r.get('quality')} m={r.get('math_correctness')} "
                        f"g={r.get('generality')} api={r.get('api_coverage')} "
                        f"con={r.get('concision')} l4={r.get('modern_lean4')} "
                        f"struct={r.get('structural_focus')}"
                    )
                    notes = r.get("notes") or ""
                    entries.append(f"- iter {iter_n}: {scores}\n  notes: {notes}")
                except (ValueError, AttributeError):
                    continue
            if entries:
                parts.append("#### Rater diagnoses (per iteration)")
                joined = "\n".join(entries)
                if len(joined) > max_chars_each:
                    joined = joined[:max_chars_each] + "\n... (trimmed)"
                parts.append(joined)
        except OSError:
            pass

    refine_log = workdir / "marathon-refine-log.md"
    if refine_log.is_file():
        try:
            text = refine_log.read_text()
            if len(text) > max_chars_each:
                text = "... (earlier refine-log content trimmed)\n" + text[-max_chars_each:]
            parts.append("#### Hermes' drafted prompts (refine-log.md)")
            parts.append(text)
        except OSError:
            pass

    return "\n\n".join(parts)


def _gather_workdir_context(workdirs_parent: Optional[Path]) -> str:
    """Aggregate per-chapter artifacts from every subdir of
    ``workdirs_parent`` that looks like a marathon refine workdir.
    Returns a markdown block or empty string."""
    if workdirs_parent is None or not workdirs_parent.is_dir():
        return ""
    blocks = []
    for entry in sorted(workdirs_parent.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "marathon-refine-state.json").is_file():
            continue
        block = _read_chapter_artifacts(entry)
        if block:
            blocks.append(block)
    return "\n\n---\n\n".join(blocks)


def _read_repo_lean(repo_dir: Path, max_chars: int = 400_000) -> str:
    """Read every Lean file under repo_dir (gitignore-filtered).
    Truncates aggregate output at ``max_chars``."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=str(repo_dir),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    parts: list[str] = []
    total = 0
    for path_bytes in result.stdout.split(b"\0"):
        if not path_bytes:
            continue
        rel = path_bytes.decode("utf-8")
        full = repo_dir / rel
        if not full.is_file() or full.suffix != ".lean":
            continue
        try:
            content = full.read_text()
        except OSError:
            continue
        section = f"=== FILE: {rel} ===\n{content}"
        if total + len(section) > max_chars:
            parts.append(f"... ({max_chars - total} more chars of Lean files trimmed)")
            break
        parts.append(section)
        total += len(section)
    return "\n\n".join(parts)


def _read_rubrics(marathon_pkg: Path) -> str:
    """Return the contents of the two reviewer rubrics for the referee
    agent to deduplicate against."""
    prompts = marathon_pkg / "prompts"
    parts = []
    for name in ("review_skeleton.md", "review.md"):
        p = prompts / name
        if p.is_file():
            parts.append(f"=== {name} ===\n{p.read_text()}")
    return "\n\n".join(parts)


def _read_git_log(repo_dir: Path, limit: int = 40) -> str:
    """Recent git log (one-line) for context on what landed when."""
    proc = subprocess.run(
        ["git", "log", "--oneline", f"-{limit}"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _invoke_claude_referee(prompt: str) -> tuple[bool, str]:
    """Call the claude CLI synchronously. Returns (ok, response_or_error)."""
    claude_path = shutil.which("claude")
    if not claude_path:
        return False, "claude (Claude Code CLI) not on PATH"

    # Subprocess conventions (Max-OAuth API-key scrub, cross-process slot
    # limiter) live in marathon.claude_proc.run_claude. The prompt now
    # travels via stdin instead of argv — this was the last argv call
    # site, and the referee prompt bundles the whole repo's Lean files,
    # which can exceed the OS argv limit (E2BIG).
    try:
        proc = run_claude(prompt, model=REFEREE_MODEL)
    except OSError as e:
        return False, f"could not exec claude (errno {e.errno}: {e.strerror})"

    if proc.returncode != 0:
        err = ((proc.stderr or proc.stdout) or "").strip()[:500]
        return False, f"claude exited {proc.returncode}: {err}"

    response = (proc.stdout or "").strip()
    if not response:
        return False, "claude returned empty stdout"

    return True, response


# ===========================================================================
# Ledger-fed structured inputs + structured fix-task emission (the "teeth").
#
# The Claude call stays a PURE prose call (no tool access). The teeth are
# the STRUCTURED ROWS Python derives and persists around it:
#   - gather_referee_inputs() reads the ledger + audit snapshot + dedup
#     groups and builds a counts-first DIGEST passed into the prompt;
#   - emit_referee_tasks() runs the self-accountability pass, generates
#     dedup tasks DIRECTLY from fingerprints (never trusting Claude to
#     find them), parses Claude's optional task JSON, and persists every
#     task as a referee-origin ledger row.
# ===========================================================================


@dataclass
class RefereeDigest:
    """Structured, counts-first summary of the runtime ledger + latest
    audit snapshot, rendered into the prompt alongside the repo/git inputs.

    Every list is capped at :data:`DIGEST_TOP_N` (the large-context
    budget): the digest grows by COUNTS, not by the whole declaration
    census. ``markdown`` is the rendered block; the structured fields back
    the deterministic task-emission side (Claude never re-derives them)."""

    #: {target status -> count}, e.g. {"planned": 4, "blocked": 1}.
    target_status_counts: dict = field(default_factory=dict)
    #: Names of blocked targets (capped).
    blocked_targets: list = field(default_factory=list)
    #: Decls whose tier is degraded/invalidated (qualifier-bearing), capped.
    invalidated_decls: list = field(default_factory=list)
    #: {tier -> count} over the snapshot.
    tier_distribution: dict = field(default_factory=dict)
    #: {deception tag -> [decls]} census (capped per tag).
    deception_census: dict = field(default_factory=dict)
    #: The mechanically-detected cross-chapter duplicate groups.
    duplicate_groups: list = field(default_factory=list)  # DuplicateGroup
    #: The referee's own prior tasks (RefereeTask), open ones first.
    prior_tasks: list = field(default_factory=list)
    #: Rendered markdown for the prompt.
    markdown: str = ""


def _load_ledger(repo_dir: Path):
    """Open the repo's ledger, or None if it cannot be opened (degrade to
    prose-only — a missing/newer ledger never breaks a referee pass)."""
    from marathon.ledger import Ledger, LedgerError

    ledger = Ledger.for_repo(repo_dir)
    try:
        ledger.init()
    except (LedgerError, Exception) as e:  # noqa: BLE001 — degrade, never crash
        print(f"  referee: ledger unavailable ({e}); structured digest skipped")
        return None
    return ledger


def gather_referee_inputs(
    repo_dir: Path, *, top_n: int = DIGEST_TOP_N
) -> RefereeDigest:
    """Build the structured ledger/audit digest for the referee prompt.

    Reads (all best-effort, each absent surface contributes nothing):

    * the ``targets`` ledger — status counts + the blocked/overdue ones;
    * ``decl_verdicts`` + the latest audit snapshot — the tier
      distribution and the tier-degraded (qualifier-bearing) decls;
    * the latest audit snapshot — the deception-tag census;
    * :func:`marathon.audit.dedup.find_duplicates` — the cross-chapter
      duplicate groups (computed from FINGERPRINTS, not Claude);
    * the referee's own prior ``referee_tasks`` — its memory across passes.

    Pure of side effects on the ledger (read-only). Returns a
    :class:`RefereeDigest` whose ``markdown`` is the counts-first block;
    callers that don't want structured emission can ignore it."""
    digest = RefereeDigest()
    ledger = _load_ledger(repo_dir)
    snapshot = None
    try:
        from marathon.audit.engine import load_snapshot

        snapshot = load_snapshot(repo_dir)
    except Exception as e:  # noqa: BLE001 — snapshot is optional evidence
        print(f"  referee: audit snapshot unavailable ({e})")

    if ledger is not None:
        try:
            targets = ledger.all_targets()
            counts: dict[str, int] = {}
            for t in targets:
                counts[t.status] = counts.get(t.status, 0) + 1
            digest.target_status_counts = counts
            digest.blocked_targets = [
                t.name for t in targets if t.status == "blocked"
            ][:top_n]
        except Exception as e:  # noqa: BLE001
            print(f"  referee: targets digest skipped ({e})")
        try:
            digest.prior_tasks = ledger.all_referee_tasks()
        except Exception as e:  # noqa: BLE001
            print(f"  referee: prior-tasks digest skipped ({e})")

    if snapshot is not None:
        digest.deception_census = _deception_census(snapshot, top_n)
        digest.duplicate_groups = _safe_duplicates(snapshot)
        if ledger is not None:
            digest.tier_distribution, digest.invalidated_decls = _tier_digest(
                snapshot, ledger, top_n
            )

    digest.markdown = _render_digest(digest, top_n)
    return digest


def _deception_census(snapshot, top_n: int) -> dict:
    """{deception tag -> [decls]} from the snapshot (capped per tag)."""
    census: dict[str, list[str]] = {}
    for decl in snapshot.decls:
        for tag in decl.tags:
            census.setdefault(tag, [])
            if len(census[tag]) < top_n:
                census[tag].append(decl.name)
    return census


def _safe_duplicates(snapshot) -> list:
    """Run dedup detection; never let a malformed snapshot break the pass."""
    try:
        from marathon.audit.dedup import find_duplicates

        return find_duplicates(snapshot)
    except Exception as e:  # noqa: BLE001 — dedup is best-effort
        print(f"  referee: dedup detection skipped ({e})")
        return []


def _tier_digest(snapshot, ledger, top_n: int) -> tuple[dict, list]:
    """(tier distribution, qualifier-bearing decl names) from the trust
    computation. Degrades to empty on any failure."""
    try:
        from marathon.audit.trust import compute_tiers

        tiers = compute_tiers(snapshot, ledger)
    except Exception as e:  # noqa: BLE001
        print(f"  referee: tier digest skipped ({e})")
        return {}, []
    dist: dict[str, int] = {}
    degraded: list[str] = []
    for tr in tiers:
        dist[tr.tier] = dist.get(tr.tier, 0) + 1
        if tr.qualifiers and len(degraded) < top_n:
            degraded.append(f"{tr.decl_name} [{','.join(tr.qualifiers)}]")
    return dist, degraded


def _render_digest(digest: RefereeDigest, top_n: int) -> str:
    """Render the digest as a counts-first markdown block. Empty sections
    are omitted; the whole thing is a few hundred chars even on a large
    project (the budget ruling — summarize counts, not the census)."""
    lines: list[str] = ["# Ledger + audit digest (structured, counts-first)"]

    if digest.target_status_counts:
        pairs = ", ".join(
            f"{k}={v}" for k, v in sorted(digest.target_status_counts.items())
        )
        lines.append(f"- target status: {pairs}")
    if digest.blocked_targets:
        lines.append(
            "- blocked targets: " + ", ".join(digest.blocked_targets)
        )
    if digest.tier_distribution:
        pairs = ", ".join(
            f"{k}={v}" for k, v in sorted(digest.tier_distribution.items())
        )
        lines.append(f"- tier distribution: {pairs}")
    if digest.invalidated_decls:
        lines.append(
            "- tier-degraded decls (re-pin / re-read): "
            + "; ".join(digest.invalidated_decls)
        )
    if digest.deception_census:
        for tag, decls in sorted(digest.deception_census.items()):
            lines.append(f"- deception tag `{tag}`: {', '.join(decls)}")

    if digest.duplicate_groups:
        lines.append(
            f"- cross-chapter duplicates ({len(digest.duplicate_groups)} "
            "group(s), MECHANICAL — already tasked by Python, do not re-file):"
        )
        for g in digest.duplicate_groups[:top_n]:
            lines.append(
                f"  - keep `{g.canonical}`; redundant: "
                + ", ".join(g.redundant)
                + f" (across {', '.join(g.modules)})"
            )

    open_tasks = [t for t in digest.prior_tasks if t.status == "open"]
    done_tasks = [t for t in digest.prior_tasks if t.status == "done"]
    if open_tasks:
        lines.append("- YOUR open fix-tasks (self-accountability):")
        for t in open_tasks[:top_n]:
            overdue = (
                f" — {t.passes_overdue} referee pass(es) overdue"
                if t.passes_overdue
                else ""
            )
            lines.append(
                f"  - [{t.severity}] {t.kind}: {t.title}"
                f" ({', '.join(t.target_decls)}){overdue}"
            )
    if done_tasks:
        lines.append(
            f"- YOUR fix-tasks resolved to date: {len(done_tasks)} "
            "(move any freshly-closed one to iteration closures)"
        )

    if len(lines) == 1:
        lines.append("- (no ledger/audit evidence yet — first structured pass)")
    return "\n".join(lines)


# --- dedup-key derivation (stable identity for upsert-by-key) --------------


def _dedup_task_key(group) -> str:
    """Stable dedup_key for a duplicate group: its kind-class + shared
    fingerprint(s). Identity-stable across passes (the same duplicate keeps
    the same key, so the task escalates in place) and independent of which
    member is canonical, so renaming a non-canonical member doesn't fork
    the task.

    For ``key_kind == 'def'`` groups BOTH the type AND value fingerprints
    are folded in: two distinct def-duplicate groups can share an elaborated
    type yet differ in body (the IsPositivelyOriented wrapper-class case), so
    keying on the type half alone would collide them — the second upsert
    would overwrite the first on the UNIQUE dedup_key column and silently
    drop one genuine cross-chapter duplicate. We hash the value fingerprint
    (it can be a large pp string) to keep the key bounded. Type-keyed groups
    (theorems/structures) carry no value half (distinct types cannot share a
    group) and key on the type fingerprint alone."""
    if group.key_kind == "def":
        value_fp = group.fingerprint_value or ""
        value_hash = hashlib.sha256(value_fp.encode("utf-8")).hexdigest()[:16]
        return f"dedup:def:{group.fingerprint}:{value_hash}"
    return f"dedup:{group.key_kind}:{group.fingerprint}"


def _claude_task_key(kind: str, target_decls: list) -> str:
    """Stable dedup_key for a Claude-proposed task: kind + sorted decls.
    Two passes that name the same defect on the same decls escalate one
    row instead of accreting duplicates (the coordinateCoframe fix)."""
    joined = ",".join(sorted(target_decls)) or "(none)"
    return f"{kind}:{joined}"


_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```json\s*\{.*?\}\s*```\s*$", re.DOTALL)
#: Matches a ```json fence with ANY body (used to detect "the agent tried
#: to emit a JSON block but it didn't parse" vs "no block at all").
_JSON_ANY_FENCE_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)


def _strip_task_json(response: str) -> str:
    """Drop a trailing ``​json`` fenced block from the response so the
    prose tail written to referee.md never carries the task JSON. Only a
    block at the END of the response is stripped (the contract says it
    comes AFTER the prose tail); a ```json fence anywhere earlier is left
    alone (it would be the agent's own prose example)."""
    return _JSON_FENCE_RE.sub("", response)


def _parse_task_json(response: str) -> tuple[list[dict], Optional[str]]:
    """Extract the ``{"tasks": [...]}`` JSON block from Claude's response.

    Returns ``(tasks, error)``. A missing block is NOT an error (the
    prose-only path, or Claude declining to add structured tasks) — it
    yields ``([], None)``. A present-but-malformed block yields
    ``([], "<reason>")`` so the caller logs it without crashing. Each
    task dict is shape-validated lazily by :func:`emit_referee_tasks`."""
    match = _JSON_BLOCK_RE.search(response)
    if match is None:
        # No well-formed ``{...}`` block. Distinguish "no block at all"
        # (prose-only / declined) from "a ```json fence whose body is
        # malformed" — the latter is a parse_error the caller logs.
        fence = _JSON_ANY_FENCE_RE.search(response)
        if fence is not None and fence.group(1).strip():
            return [], "task JSON fence present but not a parseable object"
        return [], None
    try:
        obj = json.loads(match.group(1))
    except (ValueError, TypeError) as e:
        return [], f"task JSON block did not parse: {e}"
    if not isinstance(obj, dict):
        return [], "task JSON block was not an object"
    tasks = obj.get("tasks")
    if tasks is None:
        return [], None
    if not isinstance(tasks, list):
        return [], "task JSON 'tasks' was not a list"
    return [t for t in tasks if isinstance(t, dict)], None


@dataclass
class TaskEmissionResult:
    """Outcome of one structured-task emission pass."""

    dedup_tasks: int = 0  # mechanical, from fingerprints
    claude_tasks: int = 0  # Claude-proposed, parsed from JSON
    resolved: int = 0  # prior tasks the self-accountability pass closed
    escalated: int = 0  # prior open tasks bumped (overdue + maybe severity)
    parse_error: Optional[str] = None
    warnings: list = field(default_factory=list)


def emit_referee_tasks(
    repo_dir: Path,
    digest: RefereeDigest,
    claude_response: str,
) -> TaskEmissionResult:
    """Persist structured fix-tasks after the prose pass (the teeth).

    Three things, in order:

    1. **Self-accountability** (the coordinateCoframe fix): re-check every
       prior OPEN task against the current ledger/snapshot. A dedup task
       whose duplicate is gone, a deception task whose tag cleared, a
       decl-tier task whose decl reached its claimed tier → mark DONE.
       Every still-unresolved open task → escalate (bump ``passes_overdue``
       and raise severity once it has been overdue a while).
    2. **Mechanical dedup tasks** (never trust Claude to find them):
       generate one referee-origin task per duplicate group DIRECTLY from
       :func:`marathon.audit.dedup.find_duplicates`, so a duplicate always
       becomes a task even if Claude's JSON misses it.
    3. **Claude-proposed tasks**: parse the optional ``{"tasks": [...]}``
       block and persist each well-formed, non-dedup-shadowing task.

    All upserts are keyed (``upsert_referee_task`` escalates an existing
    key in place), so re-running the referee never duplicates a task.
    Returns a :class:`TaskEmissionResult`; never raises for ordinary
    failures (a structured-emission hiccup must not abort the prose
    write)."""
    from marathon.ledger import RefereeTask

    result = TaskEmissionResult()
    ledger = _load_ledger(repo_dir)
    if ledger is None:
        result.warnings.append("ledger unavailable; no structured tasks emitted")
        return result

    # Current evidence for the self-accountability re-check.
    snapshot = None
    try:
        from marathon.audit.engine import load_snapshot

        snapshot = load_snapshot(repo_dir)
    except Exception:  # noqa: BLE001
        snapshot = None

    live_dup_keys = {_dedup_task_key(g) for g in digest.duplicate_groups}

    # 1. Self-accountability over prior OPEN tasks.
    for task in digest.prior_tasks:
        if task.status != "open":
            continue
        if _task_resolved(task, snapshot, live_dup_keys):
            if ledger.resolve_referee_task(task.dedup_key):
                result.resolved += 1
        else:
            # Escalate: bump overdue; raise severity once it's lingered.
            bump = _escalated_severity(task)
            if ledger.escalate_referee_task(task.dedup_key, severity=bump):
                result.escalated += 1

    # 2. Mechanical dedup tasks from fingerprints.
    for g in digest.duplicate_groups:
        key = _dedup_task_key(g)
        title = (
            f"Cross-chapter duplicate: keep {g.canonical}, "
            f"redirect {len(g.redundant)} restatement(s)"
        )
        rationale = (
            f"{g.key_kind}-fingerprint identical across "
            f"{', '.join(g.modules)} — unify on the canonical to keep the "
            "code navigable/reusable"
        )
        try:
            ledger.upsert_referee_task(
                RefereeTask(
                    dedup_key=key,
                    kind="dedup",
                    title=title,
                    target_decls=list(g.members),
                    severity="high",
                    blocks_target=g.canonical,
                    rationale=rationale,
                )
            )
            result.dedup_tasks += 1
        except Exception as e:  # noqa: BLE001
            result.warnings.append(f"dedup task {key} not persisted: {e}")

    # 3. Claude-proposed tasks (optional JSON block).
    tasks, parse_error = _parse_task_json(claude_response)
    result.parse_error = parse_error
    for raw in tasks:
        kind = raw.get("kind")
        title = raw.get("title")
        if kind not in ("dedup", "deception", "naming", "doc", "structural"):
            result.warnings.append(f"task with bad kind {kind!r} dropped")
            continue
        if not isinstance(title, str) or not title.strip():
            result.warnings.append("task with empty title dropped")
            continue
        decls = raw.get("target_decls") or []
        if not isinstance(decls, list):
            decls = []
        decls = [str(d) for d in decls if isinstance(d, (str,))]
        severity = raw.get("severity")
        if severity not in ("low", "medium", "high", "critical"):
            severity = "medium"
        key = _claude_task_key(kind, decls)
        # Don't let Claude shadow/duplicate a mechanical dedup task.
        if key in live_dup_keys or (
            kind == "dedup" and _shadows_mechanical(decls, digest)
        ):
            result.warnings.append(
                f"Claude {kind} task shadows a mechanical dedup task; dropped"
            )
            continue
        try:
            ledger.upsert_referee_task(
                RefereeTask(
                    dedup_key=key,
                    kind=kind,
                    title=title.strip(),
                    target_decls=decls,
                    severity=severity,
                    rationale=(raw.get("rationale") or None),
                )
            )
            result.claude_tasks += 1
        except Exception as e:  # noqa: BLE001
            result.warnings.append(f"claude task {key} not persisted: {e}")

    return result


def _task_resolved(task, snapshot, live_dup_keys: set) -> bool:
    """Has this prior open task's defect cleared, per current evidence?

    * dedup: resolved iff its dedup group is no longer detected (its key
      is absent from the live duplicate-group keys);
    * deception: resolved iff NONE of its target decls still carry a
      deception tag in the snapshot;
    * naming/doc/structural: resolved iff every target decl is absent
      from the snapshot (renamed/removed — the cited code is gone) OR the
      snapshot is unavailable is NOT resolution (we can't see the code, so
      we keep escalating rather than falsely closing).

    Conservative on missing evidence: with no snapshot, only dedup tasks
    can resolve (their key list is authoritative from the digest); the
    rest stay open. Absence of evidence never closes a task."""
    if task.kind == "dedup":
        return task.dedup_key not in live_dup_keys
    if snapshot is None:
        return False
    by_name = snapshot.by_name()
    if task.kind == "deception":
        # Cleared iff no named decl still carries a tag.
        for name in task.target_decls:
            decl = by_name.get(name)
            if decl is not None and decl.tags:
                return False
        return True
    # naming / doc / structural: resolved iff the cited code is gone.
    if not task.target_decls:
        return False
    return all(name not in by_name for name in task.target_decls)


# Severity ladder for the escalation bump (mirrors ledger's, kept local so
# this module never imports the ledger's private constant).
_SEVERITY_LADDER = ("low", "medium", "high", "critical")

#: A task overdue this many passes gets its severity bumped one rung.
_ESCALATE_AFTER_PASSES = 2


def _escalated_severity(task) -> Optional[str]:
    """The severity to escalate an overdue task TO, or None to leave it.

    A task lingering past :data:`_ESCALATE_AFTER_PASSES` open passes is
    bumped one rung up the ladder (the coordinateCoframe-survives-twelve-
    iterations enforcement: an item nobody fixes gets LOUDER, not
    forgotten). Already-critical stays critical."""
    # passes_overdue counts passes BEFORE this one; this pass adds one.
    after_this = task.passes_overdue + 1
    if after_this < _ESCALATE_AFTER_PASSES:
        return None
    try:
        idx = _SEVERITY_LADDER.index(task.severity)
    except ValueError:
        return None
    if idx + 1 < len(_SEVERITY_LADDER):
        return _SEVERITY_LADDER[idx + 1]
    return None


def _shadows_mechanical(decls: list, digest: RefereeDigest) -> bool:
    """True if a Claude dedup task names decls already covered by a
    mechanical duplicate group (belt-and-braces against double-filing)."""
    named = set(decls)
    for g in digest.duplicate_groups:
        if named & set(g.members):
            return True
    return False


def update_referee(
    repo_dir: Path,
    referee_path: Path,
    workdirs_parent: Optional[Path] = None,
    auto_commit: bool = True,
    auto_push: bool = False,
    write_to_proposed_only: bool = False,
    emit_tasks: bool = False,
) -> RefereeResult:
    """Run one referee agent pass to refresh the machine-managed tail of
    ``referee_path``.

    Returns a :class:`RefereeResult` describing what happened. Always
    returns; never raises for ordinary failures (so the auto-referee
    hook in refine doesn't abort a batch over a referee hiccup).
    """
    if not repo_dir.is_dir():
        return RefereeResult(error=f"repo_dir not a directory: {repo_dir}")

    marathon_pkg = Path(__file__).parent

    # 1. Read existing referee.md (or start with empty).
    if referee_path.is_file():
        existing = referee_path.read_text()
        user_header, existing_machine_tail = _split_referee(existing)
    else:
        existing = ""
        user_header = ""
        existing_machine_tail = None

    # 2. Gather context.
    repo_lean = _read_repo_lean(repo_dir)
    rubrics = _read_rubrics(marathon_pkg)
    workdir_ctx = _gather_workdir_context(workdirs_parent)
    git_log = _read_git_log(repo_dir)

    # 2a. Ledger-fed structured digest (only when emitting tasks). Read-only
    # over the ledger/snapshot; degrades to an empty digest if neither
    # exists (the prompt then sees a "first structured pass" line).
    digest: Optional[RefereeDigest] = None
    if emit_tasks:
        digest = gather_referee_inputs(repo_dir)

    # 3. Read system prompt.
    system_prompt_path = marathon_pkg / "prompts" / "referee_agent.md"
    if not system_prompt_path.is_file():
        return RefereeResult(error=f"referee_agent.md missing at {system_prompt_path}")
    system_prompt = system_prompt_path.read_text()

    # 4. Assemble user message.
    sections = [f"# Referee agent system prompt\n\n{system_prompt}"]
    sections.append(
        "# Current referee.md\n\n"
        "## User-managed header (do not touch)\n\n"
        + (user_header or "(empty)")
        + "\n\n## Existing machine-managed tail\n\n"
        + (existing_machine_tail or "(empty — first referee pass, propose a fresh tail)")
    )
    sections.append(f"# Generic reviewer rubrics (do not duplicate these in referee output)\n\n{rubrics}")
    if workdir_ctx:
        sections.append(f"# Per-chapter workdir artifacts (marathon.md, ratings, refine-log)\n\n{workdir_ctx}")
    if git_log:
        sections.append(f"# Recent git log (top 40 commits)\n\n{git_log}")
    if digest is not None:
        sections.append(digest.markdown)
    sections.append(f"# Repo Lean files (current state)\n\n{repo_lean}")
    if emit_tasks:
        sections.append(
            "# Structured fix-task request\n\n"
            "After the prose tail, emit the structured fix-task JSON block "
            "per the system prompt's 'Structured fix-tasks' contract. Do NOT "
            "re-file the mechanical cross-chapter duplicates listed in the "
            "ledger digest (Python already tasks those); add only "
            "deception/naming/doc/structural defects you can name by "
            "declaration. Honor the self-accountability instruction for your "
            "prior tasks shown in the digest."
        )
    sections.append(
        "Emit ONLY the new machine-managed tail of referee.md, following the "
        "rules above"
        + (
            " (and, if requested, the structured fix-task JSON block AFTER "
            "the prose tail)."
            if emit_tasks
            else "."
        )
    )
    prompt = "\n\n---\n\n".join(sections)

    print(f"  referee: invoking Claude (prompt size: {len(prompt):,} chars)")

    # 5. Invoke Claude.
    ok, response = _invoke_claude_referee(prompt)
    if not ok:
        return RefereeResult(error=response)

    # The prose tail is the response with any trailing structured-task JSON
    # block stripped (the block is emitted_tasks-only and never belongs in
    # referee.md). The FULL response feeds emit_referee_tasks below.
    full_response = response
    new_machine_tail = _strip_task_json(response).strip()
    if not new_machine_tail:
        return RefereeResult(error="agent returned empty machine tail")

    # 6. Validate output isn't accidentally embedding sentinels.
    if BEGIN_SENTINEL in new_machine_tail or END_SENTINEL in new_machine_tail:
        return RefereeResult(error="agent embedded sentinels in its output; refusing to write")

    # 7. Assemble new referee.md content.
    new_text = _assemble_referee(user_header, new_machine_tail)

    # 8. Decide where to write.
    output_path = referee_path
    if write_to_proposed_only:
        output_path = referee_path.with_suffix(referee_path.suffix + REFEREE_PROPOSED_SUFFIX)

    # 9. Compute a tiny diff summary if we have a prior version.
    diff_summary: Optional[str] = None
    if existing_machine_tail is not None and not write_to_proposed_only:
        old_lines = existing_machine_tail.splitlines()
        new_lines = new_machine_tail.splitlines()
        delta = len(new_lines) - len(old_lines)
        delta_str = f" ({delta:+d})" if delta else ""
        diff_summary = (
            f"machine tail: {len(old_lines)} → {len(new_lines)} lines{delta_str}"
        )

    # 9a. Bloat audit against referee_agent.md rules 9/10. Soft: we
    # warn loudly but still write — the human inspects and re-runs if
    # needed. Prints prominently so the auto-referee log surfaces drift.
    bloat_warnings = _check_tail_bloat(new_machine_tail)
    if bloat_warnings:
        print("  referee: BLOAT WARNINGS — output drifted past structural caps:")
        for w in bloat_warnings:
            print(f"    ! {w}")
        print(
            "    (soft warnings; the file was still written. Re-run "
            "`marathon referee --review` to inspect a fresh draft before "
            "next iteration.)"
        )

    # 10. Write.
    try:
        output_path.write_text(new_text)
    except OSError as e:
        return RefereeResult(error=f"could not write {output_path}: {e}")

    # 10a. Structured fix-task emission (the teeth). Runs after the prose
    # write so a referee.md failure doesn't strand the ledger half-updated;
    # the ledger is gitignored runtime state, so tasks persist regardless
    # of the prose commit. Self-accountability + mechanical dedup tasks +
    # Claude's optional JSON tasks (see emit_referee_tasks).
    task_emission: Optional[TaskEmissionResult] = None
    if emit_tasks and digest is not None:
        task_emission = emit_referee_tasks(repo_dir, digest, full_response)
        _print_task_emission(task_emission)

    # 11. Optional commit.
    commit_sha: Optional[str] = None
    if auto_commit and not write_to_proposed_only:
        commit_sha = _commit_referee(repo_dir, output_path)

    # 12. Optional push (only when we actually landed a commit).
    pushed: Optional[bool] = None
    push_message: Optional[str] = None
    if auto_push and commit_sha is not None:
        from marathon.post_pipeline import run_git_push
        pushed, push_message = run_git_push(repo_dir)

    return RefereeResult(
        ok=True,
        output_path=output_path,
        commit_sha=commit_sha,
        pushed=pushed,
        push_message=push_message,
        diff_summary=diff_summary,
        machine_tail_len=len(new_machine_tail.splitlines()),
        bloat_warnings=bloat_warnings or None,
        task_emission=task_emission,
    )


def _print_task_emission(em: "TaskEmissionResult") -> None:
    """Surface the structured-task emission outcome in the referee log."""
    print(
        f"  referee: structured tasks — {em.dedup_tasks} dedup (mechanical), "
        f"{em.claude_tasks} Claude-proposed; self-accountability: "
        f"{em.resolved} resolved, {em.escalated} escalated"
    )
    if em.parse_error:
        print(f"    ! task JSON: {em.parse_error}")
    for w in em.warnings:
        print(f"    ! {w}")


def _commit_referee(repo_dir: Path, output_path: Path) -> Optional[str]:
    """Stage ``output_path`` and commit. Returns the new HEAD sha (short)
    on success, None on failure / nothing to commit."""
    try:
        rel = output_path.relative_to(repo_dir)
    except ValueError:
        print(f"  referee: skipping commit — {output_path} not under {repo_dir}")
        return None

    add_proc = subprocess.run(
        ["git", "add", "--", str(rel)],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if add_proc.returncode != 0:
        print(f"  referee: git add failed — {(add_proc.stderr or '').strip()}")
        return None

    diff_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_dir),
        capture_output=True,
        check=False,
    )
    if diff_check.returncode == 0:
        # nothing staged — referee said the same thing
        print("  referee: no change vs HEAD; skipping commit")
        return None

    message = (
        "referee: refresh machine-managed tail\n\n"
        "Auto-update by `marathon referee` based on current repo state,\n"
        "per-chapter rater notes, and marathon.md design log.\n\n"
        + REFEREE_COAUTHOR_TRAILER
    )
    commit_proc = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_proc.returncode != 0:
        print(f"  referee: git commit failed — {(commit_proc.stderr or '').strip()}")
        return None

    sha_proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    return sha_proc.stdout.strip() or None


def referee_command(args) -> None:
    """Entry point for ``marathon referee`` (the default prose pass) and
    ``marathon referee tasks`` (list referee-origin fix-tasks)."""
    # The `tasks` subcommand is a pure read of the ledger — no Claude, no
    # git mutation. Dispatch before the git-repo guard so it works on any
    # repo with a ledger.
    if getattr(args, "referee_command", None) == "tasks":
        referee_tasks_command(args)
        return

    if args.repo_dir is None:
        sys.exit("marathon referee: --repo-dir is required")
    repo_dir: Path = args.repo_dir.resolve()
    if not repo_dir.is_dir():
        sys.exit(f"--repo-dir not found: {repo_dir}")
    if not (repo_dir / ".git").exists():
        sys.exit(f"--repo-dir is not a git repo: {repo_dir}")

    referee_path: Path = (args.referee or (repo_dir / REFEREE_FILENAME)).resolve()
    if referee_path.is_dir():
        sys.exit(f"--referee path is a directory: {referee_path}")

    workdirs_parent: Optional[Path] = None
    if args.workdirs_parent is not None:
        workdirs_parent = args.workdirs_parent.resolve()
        if not workdirs_parent.is_dir():
            sys.exit(f"--workdirs-parent not a directory: {workdirs_parent}")

    auto_commit = not (args.review or args.no_commit)
    auto_push = bool(args.push) and auto_commit

    mode_str = (
        "REVIEW (write to .proposed only)" if args.review
        else ("WRITE (no commit)" if args.no_commit
              else ("WRITE + auto-commit + auto-push" if auto_push
                    else "WRITE + auto-commit"))
    )

    print(f"repo dir:           {repo_dir}")
    print(f"referee path:       {referee_path}")
    if workdirs_parent is not None:
        print(f"workdirs parent:    {workdirs_parent}")
    print(f"mode:               {mode_str}")

    emit_tasks = bool(getattr(args, "emit_tasks", False))
    if emit_tasks:
        print("emit-tasks:         ON (persist structured fix-tasks)")

    result = update_referee(
        repo_dir=repo_dir,
        referee_path=referee_path,
        workdirs_parent=workdirs_parent,
        auto_commit=auto_commit,
        auto_push=auto_push,
        write_to_proposed_only=args.review,
        emit_tasks=emit_tasks,
    )

    if result.error:
        print(f"\nreferee: ERROR — {result.error}")
        sys.exit(1)
    if not result.ok:
        print(f"\nreferee: did not run — {result.skipped_reason or 'unknown reason'}")
        sys.exit(2)

    print(f"\nreferee: wrote {result.output_path}")
    if result.diff_summary:
        print(f"  delta: {result.diff_summary}")
    if result.machine_tail_len is not None:
        print(f"  new machine tail: {result.machine_tail_len} lines")
    if result.bloat_warnings:
        print(f"  bloat warnings: {len(result.bloat_warnings)} "
              "(see above; consider re-running with --review to inspect)")
    if result.task_emission is not None:
        em = result.task_emission
        print(
            f"  fix-tasks: {em.dedup_tasks} dedup + {em.claude_tasks} "
            f"Claude-proposed; {em.resolved} resolved, {em.escalated} escalated"
        )
    if result.commit_sha:
        print(f"  commit: {result.commit_sha}")
    if result.pushed is True:
        print(f"  push: ok ({result.push_message})")
    elif result.pushed is False:
        print(f"  push: failed — {result.push_message}")


def referee_tasks_command(args) -> None:
    """Entry point for ``marathon referee tasks`` — list the referee-origin
    fix-tasks in the ledger with status + overdue counts. Read-only."""
    repo_dir: Path = args.repo_dir.resolve()
    if not repo_dir.is_dir():
        sys.exit(f"--repo-dir not found: {repo_dir}")

    from marathon.ledger import Ledger, LedgerError

    ledger = Ledger.for_repo(repo_dir)
    try:
        ledger.init()
        tasks = ledger.all_referee_tasks(
            status=("open" if getattr(args, "open_only", False) else None)
        )
    except (LedgerError, Exception) as e:  # noqa: BLE001
        sys.exit(f"referee tasks: ledger unavailable — {e}")

    if not tasks:
        scope = "open " if getattr(args, "open_only", False) else ""
        print(f"no {scope}referee fix-tasks in {repo_dir}")
        return

    open_n = sum(1 for t in tasks if t.status == "open")
    overdue_n = sum(1 for t in tasks if t.status == "open" and t.passes_overdue)
    print(
        f"{len(tasks)} referee fix-task(s) — {open_n} open, "
        f"{overdue_n} overdue:\n"
    )
    for t in tasks:
        flag = "OPEN" if t.status == "open" else "done"
        overdue = (
            f"  [{t.passes_overdue} pass(es) overdue]" if t.passes_overdue else ""
        )
        block = f"  blocks {t.blocks_target}" if t.blocks_target else ""
        print(f"  [{flag}] [{t.severity:>8}] {t.kind}: {t.title}{overdue}{block}")
        if t.target_decls:
            print(f"            decls: {', '.join(t.target_decls)}")

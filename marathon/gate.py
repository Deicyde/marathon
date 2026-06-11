"""Mode-aware quality gate: pure, deterministic checks over a target
folder, producing a structured ``GateReport``.

WHY: today "build passed" is the only machine truth, and ``[build:FAIL]``
PRs get merged on title-string eyeballing — six merged PRs carry the tag
and main was red Jun 6–10 (the #50 ``SmoothCovectorField`` reshape broke
Ch.12 for four days). The gate turns the rest of the machine-checkable
surface (axioms, sorry accounting, syntax-extension tampering) into a
single structured verdict the wiring layer can render into PR bodies and
— later — enforce.

Binding design constraints (marathon-v2 plan §2 ruling 3):

* **No faithfulness judging.** The information firewall means Claude
  never sees the ``.tex``; grading LLM-rendered statements against LLM
  renderings is circular. Faithfulness stays human. Nothing in this
  module reads source texts or calls an LLM.
* **Mode-aware.** ``skeleton`` mode expects sorry bodies (gate
  statements: build, axioms, forbidden keywords, sorry *delta on
  definitions*); ``proof`` mode additionally treats new sorries as
  regressions.
* **Report, don't block.** The engine computes pass/warn/fail and
  carries per-check findings; the default posture is WARN. Enforcement
  and overrides are wired by the caller, never hardcoded here.

The gate never re-runs ``lake build`` — it consumes the post-pipeline's
existing build outcome via ``build_ok``. All filesystem checks are
line-based scans (documented limitation: block comments and string
literals are not excluded; a real exclusion needs Lean's tokenizer,
which is the phase-5 audit engine's job, not this one's).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from marathon import formalization

# --- vocabulary -------------------------------------------------------------

MODE_SKELETON = "skeleton"
MODE_PROOF = "proof"
MODES = (MODE_SKELETON, MODE_PROOF)

# Finding severities. ``info`` is evidence/accounting; ``warn`` degrades
# the verdict to warn; ``fail`` degrades it to fail.
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_FAIL = "fail"

# Check statuses. ``skip`` means the check couldn't run (reason given);
# a skipped check never degrades the verdict — absence of evidence is
# reported, not punished (the wiring layer can choose to treat skips
# more harshly when enforcing).
STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

CHECK_BUILD = "build"
CHECK_AXIOMS = "axioms"
CHECK_SORRIES = "sorries"
CHECK_FORBIDDEN = "forbidden-keywords"

# Axioms every Mathlib-built statement may legitimately depend on
# (classical logic's standard trio). ``sorryAx`` is deliberately NOT in
# the whitelist and NOT a failure either: sorries are legal in both
# modes — they are ACCOUNTED (listed per decl) so the sorry-delta check
# and the human can see them. What fails is any axiom outside
# whitelist + sorryAx (e.g. a smuggled ``axiom`` declaration).
AXIOM_WHITELIST = frozenset({"propext", "Classical.choice", "Quot.sound"})
SORRY_AXIOM = "sorryAx"

# Syntax-extension commands that can change what statements *mean*
# (autoform-bot's anti-compiler-tampering rule): an ``elab``/``macro``
# can make ``theorem foo : P`` elaborate to something other than P.
# ``notation`` is warn-level — it's routinely legitimate (display
# sugar) but still worth a human glance; the rest are fail-level.
FORBIDDEN_FAIL_KEYWORDS = ("elab", "elab_rules", "macro", "macro_rules", "syntax")
FORBIDDEN_WARN_KEYWORDS = ("notation",)
# Longest-first alternation so ``macro_rules`` doesn't tokenize as
# ``macro``. Leading attribute/modifier prefixes (``local notation``,
# ``scoped macro`` …) still count — scoping doesn't stop tampering.
_FORBIDDEN_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:local|scoped|private|protected|partial|unsafe|noncomputable)\s+)*"
    r"(elab_rules|elab|macro_rules|macro|syntax|notation)\b"
)

_STATUS_ICONS = {
    STATUS_PASS: "✅",
    STATUS_WARN: "⚠️",
    STATUS_FAIL: "❌",
    STATUS_SKIP: "⏭️",
}

# Cap rendered findings per check in the markdown (PR-body) view so a
# pathological iteration can't balloon the PR page; console rendering
# is local and uncapped.
_MARKDOWN_FINDINGS_CAP = 25


# --- report structure -------------------------------------------------------


@dataclass
class Finding:
    """One observation from a check, with an optional file:line citation
    (repo-relative path) so PR bodies can point at the exact site."""

    level: str  # LEVEL_INFO | LEVEL_WARN | LEVEL_FAIL
    message: str
    file: Optional[str] = None
    line: Optional[int] = None

    def location(self) -> Optional[str]:
        if self.file is None:
            return None
        return f"{self.file}:{self.line}" if self.line is not None else self.file


@dataclass
class CheckResult:
    """One check's outcome. ``status`` is derived from the findings
    (worst level wins) except for skips, which carry their reason in
    ``summary``. The findings list is the structure the wiring agent
    renders and enforces against — don't collapse it into prose."""

    name: str
    status: str  # STATUS_PASS | STATUS_WARN | STATUS_FAIL | STATUS_SKIP
    summary: str
    findings: list[Finding] = field(default_factory=list)


@dataclass
class SorryCounts:
    """Sorry-token accounting for a folder. ``definitions`` is the
    subset attributed to definition-form decls (def/abbrev/instance/
    structure/class/inductive) — the ones where a sorry changes what
    downstream statements mean rather than deferring proof work."""

    total: int = 0
    definitions: int = 0


@dataclass
class GateReport:
    """The gate's full output: mode, per-check results, and an overall
    verdict computed from check statuses (never stored — recomputed on
    read so post-hoc finding edits can't desynchronize it)."""

    mode: str
    target: str  # repo-relative target folder, for display
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """pass | warn | fail. Fail if any check failed, else warn if
        any check warned; skipped checks don't degrade the verdict."""
        statuses = {c.status for c in self.checks}
        if STATUS_FAIL in statuses:
            return STATUS_FAIL
        if STATUS_WARN in statuses:
            return STATUS_WARN
        return STATUS_PASS

    def check(self, name: str) -> Optional[CheckResult]:
        for c in self.checks:
            if c.name == name:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped dump (verdict included) for the wiring layer's
        enforcement decisions and ledger rows."""
        return {
            "mode": self.mode,
            "target": self.target,
            "verdict": self.verdict,
            "checks": [asdict(c) for c in self.checks],
        }

    def render_console(self) -> str:
        """Plain-text rendering for terminal output. Uncapped — local."""
        lines = [
            f"gate [{self.mode} mode] target={self.target} → "
            f"{self.verdict.upper()}"
        ]
        for c in self.checks:
            lines.append(f"  [{c.status:<4}] {c.name:<18} {c.summary}")
            for f in c.findings:
                loc = f.location()
                cite = f"  ({loc})" if loc else ""
                lines.append(f"      - {f.level}: {f.message}{cite}")
        return "\n".join(lines)

    def render_markdown(self) -> str:
        """PR-body rendering: a per-check table plus warn/fail findings
        with citations. Info findings are kept out of the bullet list
        (they're in the table summaries) so the PR page stays readable."""
        icon = _STATUS_ICONS[self.verdict]
        lines = [
            f"### Marathon gate — `{self.mode}` mode: {icon} **{self.verdict.upper()}**",
            "",
            "| check | status | summary |",
            "| --- | --- | --- |",
        ]
        for c in self.checks:
            summary = c.summary.replace("|", "\\|")
            lines.append(
                f"| {c.name} | {_STATUS_ICONS[c.status]} {c.status} | {summary} |"
            )
        notable = [
            (c, f)
            for c in self.checks
            for f in c.findings
            if f.level in (LEVEL_WARN, LEVEL_FAIL)
        ]
        if notable:
            lines += ["", "**Findings**", ""]
            for c, f in notable[:_MARKDOWN_FINDINGS_CAP]:
                loc = f.location()
                cite = f" (`{loc}`)" if loc else ""
                level_icon = "❌" if f.level == LEVEL_FAIL else "⚠️"
                lines.append(f"- {level_icon} `{c.name}` — {f.message}{cite}")
            overflow = len(notable) - _MARKDOWN_FINDINGS_CAP
            if overflow > 0:
                lines.append(f"- … and {overflow} more finding(s)")
        lines += [
            "",
            "_Machine gate only — faithfulness review stays human "
            "(information firewall: the gate never reads the source text)._",
        ]
        return "\n".join(lines)


def _status_from_findings(findings: list[Finding]) -> str:
    levels = {f.level for f in findings}
    if LEVEL_FAIL in levels:
        return STATUS_FAIL
    if LEVEL_WARN in levels:
        return STATUS_WARN
    return STATUS_PASS


# --- measurement helpers ----------------------------------------------------


def measure_sorries(target_folder: Path) -> SorryCounts:
    """Count sorry tokens under ``target_folder`` (recursive), split into
    definition-form vs total. Folder-scoped sibling of
    ``formalization.count_sorries``: the gate audits one chapter's
    folder, not the whole repo, and must not depend on git index state
    (mid-iteration files may be untracked). Reuses the shared line-based
    decl scanner — not a naive grep — so attribution matches the yaml's
    accounting; orphan sorries (before any decl) count in ``total`` only.
    """
    counts = SorryCounts()
    for lean_file in sorted(target_folder.rglob("*.lean")):
        if not lean_file.is_file():
            continue
        try:
            text = lean_file.read_text()
        except OSError:
            continue
        decls, orphans = formalization.scan_lean_source(text)
        counts.total += orphans + sum(d.sorry_count for d in decls)
        counts.definitions += sum(
            d.sorry_count
            for d in decls
            if d.kind in formalization.DEFINITION_KINDS
        )
    return counts


def _coerce_sorry_counts(value: Any) -> Optional[SorryCounts]:
    """Accept ``SorryCounts`` or a JSON-shaped mapping (the wiring layer
    round-trips baselines through ledger rows / PR comments)."""
    if value is None or isinstance(value, SorryCounts):
        return value
    if isinstance(value, Mapping):
        return SorryCounts(
            total=int(value.get("total", 0) or 0),
            definitions=int(value.get("definitions", 0) or 0),
        )
    raise TypeError(
        f"prev_sorry_counts must be SorryCounts or a mapping, got {type(value)!r}"
    )


def _discover_decls(
    repo_dir: Path, target_folder: Path
) -> tuple[list[tuple[str, str]], dict[str, tuple[str, int, str]], int]:
    """Walk the target folder's ``.lean`` files and return

    * ``pairs`` — ``(qualified_decl_name, module)`` for ``check_axioms``
      batching (deduped, first sighting wins);
    * ``sites`` — ``{qualified_name: (rel_path, line, kind)}`` for
      citations;
    * ``anonymous`` — count of unnamed decls (anonymous instances) the
      axiom check can't address by name.
    """
    pairs: list[tuple[str, str]] = []
    sites: dict[str, tuple[str, int, str]] = {}
    anonymous = 0
    for lean_file in sorted(target_folder.rglob("*.lean")):
        if not lean_file.is_file():
            continue
        rel = lean_file.relative_to(repo_dir).as_posix()
        module = formalization.module_from_file_path(rel)
        if module is None:
            continue
        try:
            text = lean_file.read_text()
        except OSError:
            continue
        decls, _ = formalization.scan_lean_source(text)
        for d in decls:
            if d.qualified is None:
                anonymous += 1
                continue
            if d.qualified in sites:
                continue
            sites[d.qualified] = (rel, d.line, d.kind)
            pairs.append((d.qualified, module))
    return pairs, sites, anonymous


# --- individual checks ------------------------------------------------------


def _check_build(
    build_ok: Optional[bool], build_log_tail: Optional[str]
) -> CheckResult:
    """Consume the post-pipeline's lake-build outcome. The gate never
    re-runs the build — re-building here would double the dominant cost
    and could disagree with the outcome the PR was opened on."""
    if build_ok is None:
        return CheckResult(
            name=CHECK_BUILD,
            status=STATUS_SKIP,
            summary="skipped (no build info)",
        )
    if build_ok:
        return CheckResult(
            name=CHECK_BUILD,
            status=STATUS_PASS,
            summary="lake build passed (caller-reported)",
        )
    findings = [Finding(level=LEVEL_FAIL, message="lake build failed")]
    if build_log_tail:
        tail = build_log_tail.strip()
        if len(tail) > 2_000:
            tail = "… (truncated)\n" + tail[-2_000:]
        findings.append(
            Finding(level=LEVEL_INFO, message=f"build log tail:\n{tail}")
        )
    return CheckResult(
        name=CHECK_BUILD,
        status=STATUS_FAIL,
        summary="lake build failed (caller-reported)",
        findings=findings,
    )


def _check_axioms(
    repo_dir: Path, target_folder: Path, build_ok: Optional[bool]
) -> CheckResult:
    """Batch ``#print axioms`` over every named decl in the target
    folder (one ``lake env lean`` spawn via formalization.check_axioms).

    Whitelist: propext / Classical.choice / Quot.sound. ``sorryAx`` is
    ACCOUNTED — listed per decl, fails nothing by itself (sorries are
    legal in both modes). What fails is any axiom outside
    whitelist + sorryAx. Requires a green build: ``#print axioms``
    reads ``.olean`` files, so a failed/unknown build yields stale or
    missing answers — skip honestly instead.
    """
    if build_ok is not True:
        return CheckResult(
            name=CHECK_AXIOMS,
            status=STATUS_SKIP,
            summary="skipped (needs a successful build — "
            "#print axioms reads .olean files)",
        )
    if not target_folder.is_dir():
        return CheckResult(
            name=CHECK_AXIOMS,
            status=STATUS_SKIP,
            summary=f"skipped (target folder not found: {target_folder})",
        )
    pairs, sites, anonymous = _discover_decls(repo_dir, target_folder)
    if not pairs:
        return CheckResult(
            name=CHECK_AXIOMS,
            status=STATUS_SKIP,
            summary="skipped (no named declarations discovered under target)",
        )
    axioms_by_decl = formalization.check_axioms(repo_dir, pairs)

    findings: list[Finding] = []
    sorry_backed = 0
    undetermined = 0
    offenders = 0
    for decl, _module in pairs:
        rel, line, _kind = sites[decl]
        axioms = axioms_by_decl.get(decl)
        if axioms is None:
            # Couldn't be determined (private decl, name-resolution
            # mismatch from the line-based qualifier, Lean error).
            # Accounted, not failed — the audit engine (phase 5) is the
            # authoritative pass; here absence of evidence is reported.
            undetermined += 1
            continue
        extra = sorted(set(axioms) - AXIOM_WHITELIST - {SORRY_AXIOM})
        if extra:
            offenders += 1
            findings.append(
                Finding(
                    level=LEVEL_FAIL,
                    message=(
                        f"`{decl}` depends on non-whitelisted axiom(s): "
                        f"{', '.join(extra)}"
                    ),
                    file=rel,
                    line=line,
                )
            )
        if SORRY_AXIOM in axioms:
            sorry_backed += 1
            findings.append(
                Finding(
                    level=LEVEL_INFO,
                    message=f"`{decl}` depends on {SORRY_AXIOM} (sorry-backed)",
                    file=rel,
                    line=line,
                )
            )
    bits = [f"{len(pairs)} decl(s) checked"]
    if offenders:
        bits.append(f"{offenders} with non-whitelisted axioms")
    else:
        bits.append("all axioms within whitelist+sorryAx")
    if sorry_backed:
        bits.append(f"{sorry_backed} sorry-backed")
    if undetermined:
        bits.append(f"{undetermined} undetermined")
    if anonymous:
        bits.append(f"{anonymous} anonymous decl(s) not addressable by name")
    return CheckResult(
        name=CHECK_AXIOMS,
        status=_status_from_findings(findings),
        summary="; ".join(bits),
        findings=findings,
    )


def _check_sorries(
    target_folder: Path,
    mode: str,
    prev: Optional[SorryCounts],
) -> CheckResult:
    """Sorry accounting with mode-aware delta semantics.

    Proof mode: a positive total delta (or a positive definitions delta
    masked by proof-side removals) is a regression → fail. Skeleton
    mode: theorem-body sorries are the expected product; only a positive
    delta on DEFINITION bodies warns — statement scaffolds shouldn't
    gain sorry'd defs silently, because a sorry'd def changes what every
    downstream statement means. Without a baseline the counts are
    reported and the delta is explicitly not evaluated.
    """
    if not target_folder.is_dir():
        return CheckResult(
            name=CHECK_SORRIES,
            status=STATUS_SKIP,
            summary=f"skipped (target folder not found: {target_folder})",
        )
    counts = measure_sorries(target_folder)
    findings: list[Finding] = [
        Finding(
            level=LEVEL_INFO,
            message=(
                f"{counts.total} sorry token(s) under target, "
                f"{counts.definitions} in definition bodies "
                "(line-based decl scan; block comments/strings not excluded)"
            ),
        )
    ]
    if prev is None:
        findings.append(
            Finding(
                level=LEVEL_INFO,
                message="no baseline provided; sorry delta not evaluated",
            )
        )
        summary = (
            f"{counts.total} sorries ({counts.definitions} in defs); no baseline"
        )
        return CheckResult(
            name=CHECK_SORRIES,
            status=_status_from_findings(findings),
            summary=summary,
            findings=findings,
        )

    d_total = counts.total - prev.total
    d_defs = counts.definitions - prev.definitions
    d_proofs = d_total - d_defs
    if mode == MODE_PROOF:
        if d_total > 0:
            findings.append(
                Finding(
                    level=LEVEL_FAIL,
                    message=(
                        f"sorry regression: {d_total:+d} vs baseline "
                        f"({prev.total} → {counts.total}); proof mode treats "
                        "new sorries as regressions"
                    ),
                )
            )
        elif d_defs > 0:
            # Total flat-or-down but definitions UP: proof-side removals
            # are masking new sorry'd defs — still a regression.
            findings.append(
                Finding(
                    level=LEVEL_FAIL,
                    message=(
                        f"definition bodies gained {d_defs:+d} sorries "
                        f"({prev.definitions} → {counts.definitions}) despite "
                        "flat total; regression in proof mode"
                    ),
                )
            )
    else:  # skeleton
        if d_defs > 0:
            findings.append(
                Finding(
                    level=LEVEL_WARN,
                    message=(
                        f"definition bodies gained {d_defs:+d} sorries "
                        f"({prev.definitions} → {counts.definitions}); "
                        "statement scaffolds shouldn't gain sorry'd "
                        "definitions silently"
                    ),
                )
            )
        if d_proofs > 0:
            findings.append(
                Finding(
                    level=LEVEL_INFO,
                    message=(
                        f"{d_proofs:+d} theorem-body sorries "
                        "(expected in skeleton mode)"
                    ),
                )
            )
    if d_total < 0:
        findings.append(
            Finding(
                level=LEVEL_INFO,
                message=f"net {-d_total} fewer sorries than baseline",
            )
        )
    summary = (
        f"{counts.total} sorries ({counts.definitions} in defs); "
        f"Δtotal={d_total:+d}, Δdefs={d_defs:+d}"
    )
    return CheckResult(
        name=CHECK_SORRIES,
        status=_status_from_findings(findings),
        summary=summary,
        findings=findings,
    )


def _check_forbidden(repo_dir: Path, target_folder: Path) -> CheckResult:
    """Scan target ``.lean`` files for syntax-extension declarations
    (``elab``/``elab_rules``/``macro``/``macro_rules``/``syntax`` fail;
    ``notation`` warns) with file:line citations.

    Line-based scan: ``--`` comment lines are skipped; block comments
    and string literals are NOT excluded — a keyword at the start of a
    ``/- … -/`` continuation line can false-positive (documented
    limitation, acceptable for a warn-posture gate; the human sees the
    citation and dismisses it in seconds).
    """
    if not target_folder.is_dir():
        return CheckResult(
            name=CHECK_FORBIDDEN,
            status=STATUS_SKIP,
            summary=f"skipped (target folder not found: {target_folder})",
        )
    findings: list[Finding] = []
    scanned = 0
    for lean_file in sorted(target_folder.rglob("*.lean")):
        if not lean_file.is_file():
            continue
        try:
            text = lean_file.read_text()
        except OSError:
            continue
        scanned += 1
        rel = lean_file.relative_to(repo_dir).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("--"):
                continue
            m = _FORBIDDEN_RE.match(line)
            if not m:
                continue
            keyword = m.group(1)
            level = (
                LEVEL_WARN if keyword in FORBIDDEN_WARN_KEYWORDS else LEVEL_FAIL
            )
            # Backticks in the snippet would break the markdown
            # rendering's inline-code span; swap for apostrophes.
            snippet = line.strip().replace("`", "'")
            if len(snippet) > 100:
                snippet = snippet[:100] + "…"
            findings.append(
                Finding(
                    level=level,
                    message=(
                        f"`{keyword}` declaration can change what statements "
                        f"mean: `{snippet}`"
                    ),
                    file=rel,
                    line=lineno,
                )
            )
    if scanned == 0:
        return CheckResult(
            name=CHECK_FORBIDDEN,
            status=STATUS_SKIP,
            summary="skipped (no .lean files under target)",
        )
    if not findings:
        summary = f"no syntax-extension declarations in {scanned} file(s)"
    else:
        fails = sum(1 for f in findings if f.level == LEVEL_FAIL)
        warns = sum(1 for f in findings if f.level == LEVEL_WARN)
        bits = []
        if fails:
            bits.append(f"{fails} fail-level (elab/macro/syntax)")
        if warns:
            bits.append(f"{warns} warn-level (notation)")
        summary = "syntax extensions found: " + ", ".join(bits)
    return CheckResult(
        name=CHECK_FORBIDDEN,
        status=_status_from_findings(findings),
        summary=summary,
        findings=findings,
    )


# --- engine entry point -----------------------------------------------------


def run_gate(
    repo_dir: Path,
    target_folder: Path,
    *,
    mode: str,
    build_ok: Optional[bool],
    build_log_tail: Optional[str] = None,
    prev_sorry_counts: SorryCounts | Mapping[str, int] | None = None,
) -> GateReport:
    """Run every gate check over ``target_folder`` and return the report.

    Pure with respect to the working tree except for the axiom check's
    temp file (``formalization.check_axioms`` writes-and-removes one
    ``.lean`` scratch file in ``repo_dir``); never mutates targets,
    never calls an LLM, never reads source texts.

    Args:
        repo_dir: Project root (where ``lake env lean`` runs and what
            citations are relative to).
        target_folder: The folder under audit (absolute, or relative to
            ``repo_dir``). Must live under ``repo_dir`` — module paths
            and citations are derived from the repo-relative path.
        mode: ``"skeleton"`` or ``"proof"`` (see module docstring).
        build_ok: The post-pipeline's lake-build outcome. ``None`` ⇒
            build check (and the build-dependent axiom check) skip with
            a reason; the gate never re-runs the build itself.
        build_log_tail: Optional build log excerpt attached as evidence
            when ``build_ok`` is False.
        prev_sorry_counts: Baseline for the sorry-delta semantics —
            ``SorryCounts`` or a ``{"total": …, "definitions": …}``
            mapping. ``None`` ⇒ counts reported, delta not evaluated.
    """
    if mode not in MODES:
        raise ValueError(f"unknown gate mode {mode!r}; expected one of {MODES}")
    repo_dir = Path(repo_dir)
    target = Path(target_folder)
    if not target.is_absolute():
        target = repo_dir / target
    try:
        rel_target = target.relative_to(repo_dir).as_posix()
    except ValueError:
        raise ValueError(
            f"target_folder {target} is not under repo_dir {repo_dir}"
        ) from None
    prev = _coerce_sorry_counts(prev_sorry_counts)

    checks = [
        _check_build(build_ok, build_log_tail),
        _check_axioms(repo_dir, target, build_ok),
        _check_sorries(target, mode, prev),
        _check_forbidden(repo_dir, target),
    ]
    return GateReport(mode=mode, target=rel_target, checks=checks)

"""Headless chapter intake — the server-side engine behind the deck's
"Add Chapter" action.

Where :func:`marathon.review.chapter_sessions.open_chapter_session` opens a
*human-driven* VS Code Claude session to bootstrap a chapter's review sub-issues,
this module runs the same decomposition **headlessly** on the server: it gathers
the chapter's full Lean source + audit inventory + (optional) human
informal-statements, feeds them to a single ``claude -p`` completion (via
:func:`marathon.claude_proc.run_claude`), and parses the returned drafts markdown
into proposed sub-issues. The deck surfaces those for human approval before
:func:`marathon.review.subissues.create_subissues_from_drafts` opens the GitHub
issues.

The Claude call only ever DRAFTS — it never creates issues or edits config. The
apply step is an explicit, separate, human-approved action. Reusing the bootstrap
rubric + output-format helpers keeps the headless output byte-compatible with the
existing ``parse_drafts`` → ``create_subissues_from_drafts`` apply path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from marathon.claude_proc import run_claude
from marathon.review.chapter_sessions import (
    _common_decision_rubric_section,
    _common_output_format_section,
)
from marathon.review.config import ReviewConfig
from marathon.review.drafts import parse_drafts

#: Env override for the intake model. A whole chapter's source is large, so a
#: 1M-context model (e.g. ``claude-opus-4-8[1m]``) is recommended; falls back to
#: ``run_claude``'s default resolution when unset.
INTAKE_MODEL_ENV = "MARATHON_INTAKE_MODEL"

#: Generous ceiling for one intake completion (single-shot, large context).
INTAKE_TIMEOUT_SECONDS = 1800.0


@dataclass
class ProposedSubIssue:
    """One drafted review sub-issue awaiting human approval."""

    index: int  # 1-based draft index (becomes the registry entry index)
    title: str
    body: str


@dataclass
class IntakeResult:
    """Outcome of one headless intake. ``error`` is set (and ``subissues`` empty)
    on any failure, so the deck surfaces it without the call ever raising."""

    chapter: int
    drafts_path: Path
    subissues: list[ProposedSubIssue]
    raw_drafts: str
    model: str
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.subissues)


def drafts_path_for(cfg: ReviewConfig, chapter: int) -> Path:
    return cfg.repo_dir / ".marathon" / "review" / "drafts" / f"Chapter{chapter}.md"


def _read_chapter_sources(cfg: ReviewConfig, chapter: int) -> list[tuple[str, str]]:
    """``[(rel_path, text), …]`` for every ``.lean`` file in the chapter's target
    folder, sorted. Empty when the folder is absent (chapter not formalized)."""
    target = cfg.target_path(chapter)
    if not target.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for p in sorted(target.rglob("*.lean")):
        try:
            rel = str(p.relative_to(cfg.repo_dir))
        except ValueError:
            rel = str(p)
        try:
            out.append((rel, p.read_text()))
        except OSError:
            continue
    return out


def _audit_inventory(cfg: ReviewConfig, chapter: int) -> list[str]:
    """``["<kind> <name>  [has sorry]", …]`` from the audit snapshot for the
    chapter's modules, or ``[]`` when no snapshot exists (best-effort — the
    source already carries the signatures; the inventory is a completeness aid)."""
    try:
        from marathon.audit.engine import load_snapshot

        snap = load_snapshot(cfg.repo_dir)
    except Exception:  # noqa: BLE001 — no snapshot / signature drift is non-fatal
        return []
    if snap is None:
        return []
    needle = f"Chapter{chapter}"
    rows: list[str] = []
    for d in getattr(snap, "decls", []) or ():
        if needle not in (getattr(d, "module", "") or ""):
            continue
        flag = "  [has sorry]" if getattr(d, "has_sorry", False) else ""
        rows.append(f"{getattr(d, 'kind', 'decl')} {getattr(d, 'name', '?')}{flag}")
    return rows


def tracker_chapter_section(cfg: ReviewConfig, chapter: int) -> Optional[str]:
    """The Chapter-``chapter`` slice of the parent tracker issue body — the
    project's human-curated roadmap of textbook results (one line per result:
    page citation + name + short description). ``None`` when the tracker can't be
    read or has no section for this chapter. Mirrors the section bounds used by
    :mod:`marathon.review.tracker` (``### Chapter N`` up to the next ``### Chapter``)."""
    try:
        from marathon.review.github import issue_body

        body = issue_body(cfg.parent_issue, cfg.github_repo)
    except Exception:  # noqa: BLE001 — a gh/network failure is non-fatal here
        return None
    if not body:
        return None
    marker = cfg.tracker_section(chapter)
    start = body.find(marker)
    if start == -1:
        return None
    end = body.find("\n### Chapter", start + 1)
    if end == -1:
        end = len(body)
    section = body[start:end].strip()
    return section or None


def _build_intake_prompt(
    chapter: int,
    sources: list[tuple[str, str]],
    inventory: list[str],
    informal_text: Optional[str],
    informal_kind: Optional[str] = None,
) -> str:
    rubric = _common_decision_rubric_section()
    output = _common_output_format_section("bootstrap")

    if informal_text is not None and informal_kind == "tracker":
        informal_directive = (
            "A section of the project's **tracker roadmap** for this chapter is "
            "included below — a human-curated list where each numbered line is one "
            "textbook result: a page citation, the result's name, and a short "
            "description (any trailing status emoji like 🟡/🔵/🟢 is a progress "
            "marker — ignore it). Open EXACTLY ONE sub-issue per roadmap line — "
            "never split a line into multiple sub-issues. List that result's KEY "
            "declarations (the substantive lemmas/definitions/predicates that "
            "realize it — e.g. forward + converse + roundtrip) in its `### Lean "
            "declarations` section; the deck turns each into a review card, and "
            "trivial helpers go under `### Scaffolding folded`. Quote the roadmap "
            "line VERBATIM (minus the status emoji) "
            "in each Informal Statement section and open it with `*📋 Anchored to the "
            "project tracker roadmap (user-curated); confirm the full statement "
            "against the cited page on review.*`. The roadmap gives the citation and "
            "name, NOT the full formal statement — do NOT fabricate the full "
            "statement; the citation lets the human reviewer confirm faithfulness "
            "against the page."
        )
        informal_block = (
            f"\n## Project tracker roadmap — Chapter {chapter}\n\n{informal_text}\n"
        )
    elif informal_text is not None:
        informal_directive = (
            "A human-provided **informal-statements file** is included below. "
            "Pair each of its sections with the matching Lean declaration(s); a "
            "single section may map to several declarations (e.g. a three-part "
            "lemma) — put all of them in ONE sub-issue. Quote the human's wording "
            "VERBATIM in each Informal Statement section and open it with "
            "`*✅ Verified against the textbook reference (user-provided).*`. Do "
            "NOT paraphrase or LLM-render."
        )
        informal_block = f"\n## Human informal-statements file\n\n{informal_text}\n"
    else:
        informal_directive = (
            "No informal-statements file was provided. LLM-render one informal "
            "statement per first-class declaration from general mathematical "
            "knowledge, and open EVERY such section with `*⚠️ LLM-rendered from "
            "common knowledge; verification pending.*` so the human verifies it on "
            "the first review pass."
        )
        informal_block = ""

    src_block = "\n\n".join(
        f"### `{rel}`\n\n```lean\n{text}\n```" for rel, text in sources
    ) or "_(the chapter's target folder has no `.lean` files yet)_"
    inv_block = "\n".join(f"- {row}" for row in inventory) or "_(no audit snapshot)_"

    return f"""# Marathon headless chapter-intake — Chapter {chapter}

## Role

You are the chapter-bootstrap reviewer for marathon's per-declaration review
workflow. Decompose Chapter {chapter}'s formalized Lean code into review
sub-issues — one per first-class textbook result — exactly as the rubric below
prescribes. You are running HEADLESSLY: you have NO tools and NO file access;
everything you need is embedded in this prompt. {informal_directive}

{rubric}

## Declaration inventory (from the audit snapshot)

{inv_block}

## Chapter {chapter} Lean source (verbatim)

{src_block}
{informal_block}
{output}

## Output discipline (CRITICAL)

Output ONLY the drafts markdown — nothing before the first `## 1/<total> — …`
heading and nothing after the final `## Coverage summary`. No preamble, no
explanation, no code fences wrapping the whole thing. The text is parsed
mechanically by `parse_drafts`, so the `## N/<total> — <description>` headers and
the ``**Title**: `…` `` lines must be exact.
"""


def run_chapter_intake(
    cfg: ReviewConfig,
    chapter: int,
    *,
    informal_path: Optional[Path] = None,
    use_tracker: bool = False,
    model: Optional[str] = None,
    timeout: Optional[float] = INTAKE_TIMEOUT_SECONDS,
    _runner=run_claude,
) -> IntakeResult:
    """Run the headless intake for ``chapter`` and write its drafts file.

    Never raises: every failure returns an :class:`IntakeResult` with ``error``
    set so the deck can render it. ``_runner`` is injectable for tests."""
    model = model or os.environ.get(INTAKE_MODEL_ENV) or None
    model_label = model or "(default)"
    dpath = drafts_path_for(cfg, chapter)

    sources = _read_chapter_sources(cfg, chapter)
    if not sources:
        return IntakeResult(
            chapter, dpath, [], "", model_label,
            error=f"no .lean files under {cfg.target_path(chapter)} — "
            "formalize the chapter (e.g. `marathon skeleton`) before intake",
        )

    informal_text: Optional[str] = None
    informal_kind: Optional[str] = None
    if informal_path is not None:
        try:
            informal_text = Path(informal_path).read_text()
            informal_kind = "file"
        except OSError as e:
            return IntakeResult(
                chapter, dpath, [], "", model_label,
                error=f"could not read informal-statements file {informal_path}: {e}",
            )
    elif use_tracker:
        informal_text = tracker_chapter_section(cfg, chapter)
        informal_kind = "tracker"
        if not informal_text:
            return IntakeResult(
                chapter, dpath, [], "", model_label,
                error=(
                    f"no '{cfg.tracker_section(chapter)}' section found in the "
                    f"tracker roadmap (issue #{cfg.parent_issue}) — add that chapter "
                    f"to the roadmap, or choose a file / 'none' informal source"
                ),
            )

    prompt = _build_intake_prompt(
        chapter, sources, _audit_inventory(cfg, chapter), informal_text, informal_kind
    )
    try:
        cp = _runner(prompt, model=model, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — surface, never crash the deck thread
        return IntakeResult(chapter, dpath, [], "", model_label,
                            error=f"claude intake call failed: {e}")
    if cp.returncode != 0:
        return IntakeResult(
            chapter, dpath, [], (cp.stdout or ""), model_label,
            error=f"claude exited {cp.returncode}: {(cp.stderr or '').strip()[:300]}",
        )

    raw = (cp.stdout or "").strip()
    parsed = parse_drafts(raw)
    if not parsed:
        return IntakeResult(
            chapter, dpath, [], raw, model_label,
            error="claude output had no parseable `## N/total — …` draft sections",
        )

    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text(raw + "\n")
    subs = [
        ProposedSubIssue(idx, title, body)
        for idx, (title, body) in sorted(parsed.items())
    ]
    return IntakeResult(chapter, dpath, subs, raw, model_label)

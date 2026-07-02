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

#: Env override for the intake reasoning EFFORT — passed to ``claude -p`` as
#: ``--effort <level>`` (e.g. ``max``). Intake-scoped on purpose (a whole-chapter
#: decomposition warrants a high level; a global effort env would slow every
#: Claude call). Unset → the CLI's default effort.
INTAKE_EFFORT_ENV = "MARATHON_INTAKE_EFFORT"

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


def _chapter_inventory(cfg: ReviewConfig, chapter: int) -> list[str]:
    """``["<kind> <name>  [has sorry]", …]`` for EVERY declaration in the
    chapter's source — the audit snapshot where a module is covered, UNIONED
    with a source scan of any module the audit does NOT cover.

    A module that does not currently compile is absent from the audit snapshot
    (the build-targets-first audit skips broken modules), but it is still in
    scope for intake — issue generation must never depend on whether the code
    builds. Without the source-scan union the inventory would silently drop a
    broken module's declarations, which led Claude to defer whole roadmap lines
    as "out of inventory scope". Best-effort: a scan failure just yields fewer
    rows (the full source is fed to the model regardless)."""
    from marathon.formalization import module_from_file_path

    needle = f"Chapter{chapter}"
    rows: list[str] = []
    audited_modules: set[str] = set()
    try:
        from marathon.audit.engine import load_snapshot

        snap = load_snapshot(cfg.repo_dir)
    except Exception:  # noqa: BLE001 — no snapshot / signature drift is non-fatal
        snap = None
    if snap is not None:
        for d in getattr(snap, "decls", []) or ():
            mod = getattr(d, "module", "") or ""
            if needle not in mod:
                continue
            audited_modules.add(mod)
            flag = "  [has sorry]" if getattr(d, "has_sorry", False) else ""
            rows.append(f"{getattr(d, 'kind', 'decl')} {getattr(d, 'name', '?')}{flag}")

    # Source-only modules: present in the chapter's .lean but absent from the
    # audit (typically because they don't currently build). List their decls
    # from a source scan so no roadmap line loses its declarations.
    from marathon.fill import _extract_decls_from_text, _find_sorries_in_file

    for rel, text in _read_chapter_sources(cfg, chapter):
        module = module_from_file_path(rel) or ""
        if module and module in audited_modules:
            continue
        try:
            sorry_names = set(_find_sorries_in_file(cfg.repo_dir / rel))
        except Exception:  # noqa: BLE001
            sorry_names = set()
        short = module.rsplit(".", 1)[-1] if module else rel
        for name in _extract_decls_from_text(text):
            flag = "  [has sorry]" if name in sorry_names else ""
            rows.append(
                f"decl {name}{flag}  [source only — `{short}` not in audit "
                "snapshot (may not build); still in scope]"
            )
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
    inv_block = "\n".join(f"- {row}" for row in inventory) or "_(no declarations found)_"

    return f"""# Marathon headless chapter-intake — Chapter {chapter}

## Role

You are the chapter-bootstrap reviewer for marathon's per-declaration review
workflow. Decompose Chapter {chapter}'s formalized Lean code into review
sub-issues — one per first-class textbook result — exactly as the rubric below
prescribes. You are running HEADLESSLY: you have NO tools and NO file access;
everything you need is embedded in this prompt. {informal_directive}

{rubric}

## Declaration inventory (completeness aid — NOT the scope)

This lists the chapter's declarations (audit snapshot where a module is
audited, plus a source scan of modules that aren't). It is a hint for locating
declarations, **never** a scope boundary. Rows tagged `[source only — … not in
audit snapshot (may not build)]` come from modules that don't currently
compile — they are STILL IN SCOPE. Decompose EVERY roadmap line (1..N) even
when its module is absent or its declarations are missing here; the roadmap and
the verbatim source below are authoritative, not this list. Never defer a
roadmap line as "out of inventory scope".

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
    effort = os.environ.get(INTAKE_EFFORT_ENV) or None
    model_label = (model or "(default)") + (f" · effort={effort}" if effort else "")
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
        chapter, sources, _chapter_inventory(cfg, chapter), informal_text, informal_kind
    )
    try:
        runner_kw = {"model": model, "timeout": timeout}
        if effort:  # only when set → test-injected runners without extra_args are unaffected
            runner_kw["extra_args"] = ("--effort", effort)
        cp = _runner(prompt, **runner_kw)
    except Exception as e:  # noqa: BLE001 — surface, never crash the deck thread
        return IntakeResult(chapter, dpath, [], "", model_label,
                            error=f"claude intake call failed: {e}")
    if cp.returncode != 0:
        # claude -p reports many failures (context overflow, auth, model
        # errors) on STDOUT with an empty stderr — surface whichever end
        # carries the message, or the UI shows a useless "exited 1:".
        detail = (cp.stderr or "").strip() or (cp.stdout or "").strip()[-400:]
        return IntakeResult(
            chapter, dpath, [], (cp.stdout or ""), model_label,
            error=f"claude exited {cp.returncode}: {detail[:400]}",
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

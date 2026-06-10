"""Chapter-scale coreviewer sessions: bootstrap (create-many) and audit.

Two operations, both at *chapter* scope rather than per-sub-issue:

* ``bootstrap_chapter`` — first-time setup for a chapter that has no
  sub-issues yet. The coreviewer reads the chapter's `.lean` files,
  pairs declarations with a user-supplied informal-statements file,
  drafts ``.marathon/review/drafts/Chapter{N}.md`` in the project's
  standard sub-issue-body format, and proposes the full set of
  sub-issues. On human go-ahead, it creates the issues, registers the
  chapter via ``marathon review register-chapter`` (the config.toml
  ``[[chapters]]`` block is machine-managed — agents no longer
  hand-edit it), and patches the parent tracker.

* ``audit_chapter`` — maintenance pass for a chapter that already has
  a queue. The coreviewer cross-references existing sub-issue bodies
  against current code, identifies drift (stale snippets, shifted
  refs, unverified Informal Statements), surfaces coverage gaps, and
  proposes a unified set of body refreshes plus new sub-issues for
  gaps. On human go-ahead, it applies the edits.

Both commands open an interactive Claude Code chat in the user's VS
Code via the same ``vscode://anthropic.claude-code/open?prompt=…``
URI handler used by ``marathon review open``. Because chapter-scale
briefings exceed the 5000-char URI ceiling, the *full* briefing is
written to disk under ``.marathon/review/sessions/`` and the URI
prompt is a short pointer that tells the agent to read the briefing
file via ``@``-mention. The briefing is the load-bearing instruction.

Naming note: the user proposed ``open-issues`` / ``update-issues``;
``bootstrap-chapter`` / ``audit-chapter`` is used here because
``open`` is already a sibling subcommand (per-issue chat) and
``-chapter`` makes the scope explicit. Aliases can be added later.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import quote

from marathon.review.config import ReviewConfig


PROMPT_CHAR_BUDGET = 5_000

# Briefing files live under .marathon/review/sessions/. Persistent
# (not /tmp) so they form an audit trail of what the coreviewer was
# instructed to do; consumers can gitignore the dir.
SESSIONS_RELDIR = Path(".marathon/review/sessions")


SessionKind = Literal["bootstrap", "audit"]


@dataclass
class ChapterSessionResult:
    uri: str
    prompt_chars: int
    briefing_path: Path


# --- platform URL opener (duplicated from open_session.py because the
# logic is small and inlining keeps this module standalone) ------------

def _platform_url_opener() -> Optional[list[str]]:
    if sys.platform == "darwin":
        return ["open"]
    if sys.platform.startswith("linux"):
        for cand in ("xdg-open", "gnome-open", "kde-open5", "kde-open"):
            if shutil.which(cand):
                return [cand]
        return None
    if sys.platform.startswith("win"):
        return ["cmd", "/c", "start", ""]
    return None


# --- briefing assembly -----------------------------------------------


def _common_inputs_section(cfg: ReviewConfig, chapter: int) -> str:
    target = cfg.target_path(chapter)
    try:
        rel_target = target.relative_to(cfg.repo_dir)
    except ValueError:
        rel_target = target
    parent_url = f"https://github.com/{cfg.github_repo}/issues/{cfg.parent_issue}"
    return (
        f"## Inputs at hand\n\n"
        f"* **Repo**: {cfg.github_repo}\n"
        f"* **Chapter**: {chapter}\n"
        f"* **Target Lean folder**: `{rel_target}` — read every `.lean` "
        f"file inside.\n"
        f"* **Parent tracker issue**: [#{cfg.parent_issue}]({parent_url}) — "
        f"the project's chapter-by-chapter tracker; per-chapter sections "
        f"hold one numbered line per sub-issue.\n"
        f"* **Sibling chapters' drafts** at `@.marathon/review/drafts/` — "
        f"use whichever sibling is closest to this chapter's style as the "
        f"sub-issue-body template. Match its section structure, status-"
        f"line conventions, and verdict-footer format verbatim.\n"
        f"* **Review config registry** at "
        f"`@.marathon/review/config.toml` — the `[[chapters]]` block for "
        f"chapter {chapter} maps each sub-issue's GitHub number to the "
        f"tracker substring it patches in the parent issue body. The "
        f"block is machine-managed (written by `marathon review "
        f"register-chapter`): read it, never hand-edit it.\n"
        f"* **State file** at `@.marathon/review/state.json` — per-issue "
        f"rejection state; entries for currently-rejected sub-issues "
        f"carry notes the coreviewer should be aware of.\n"
    )


def _common_decision_rubric_section() -> str:
    return (
        "## Decision rubric: first-class vs scaffolding\n\n"
        "* **First-class** (gets its own sub-issue): a textbook-named "
        "result (Theorem, Proposition, Lemma, Corollary, Exercise, or a "
        "named Definition) **or** a Lean declaration the human would "
        "want as a tracking handle (a bundled `LM` linear-map version, "
        "a `Submodule` packaging, a type-level abbreviation downstream "
        "consumers route through, a notion that's defined in this "
        "chapter but used heavily in later chapters).\n"
        "* **Scaffolding** (folded under a first-class umbrella, no own "
        "sub-issue): helper lemmas, `_apply` simp lemmas, instance "
        "fields, private declarations, structure fields, the underscore-"
        "named one-liners, any declaration the textbook doesn't name "
        "and that a human reviewer would not reach for directly.\n\n"
        "When in doubt, mark first-class. Merging two later is cheap; "
        "splitting one is not.\n"
    )


def _common_apply_section(cfg: ReviewConfig, chapter: int) -> str:
    target = cfg.target_path(chapter)
    try:
        rel_target = target.relative_to(cfg.repo_dir)
    except ValueError:
        rel_target = target
    return (
        "## Apply only on explicit human go-ahead\n\n"
        "When (and only when) the human says \"go\" / \"approve\" / "
        "\"create them\" / \"apply\", run the apply step. If the human "
        "asks for specific edits to your proposal, regenerate the "
        "proposal and re-surface for approval — do *not* apply with "
        "edits unsanctioned.\n\n"
        "**Apply sequence** (when going from drafts → live sub-issues):\n"
        f"1. Ensure `.marathon/review/drafts/Chapter{chapter}.md` is "
        "saved with the proposed sub-issue sections.\n"
        f"2. `marathon review subissues create "
        f".marathon/review/drafts/Chapter{chapter}.md` — creates one "
        "GitHub issue per draft section, labels with `review` + "
        f"`chapter-{chapter}`, attaches each as a sub-issue of the "
        "parent tracker.\n"
        f"3. Register the chapter's FULL entry list via the CLI (do "
        f"NOT hand-edit `.marathon/review/config.toml` — the "
        f"`[[chapters]]` block is machine-managed):\n"
        f"   `marathon review register-chapter --chapter {chapter} "
        f"--target {rel_target} \\\n"
        f"        --entry \"<issue_num>:<tracker_substring>\" --entry "
        f"... [--replace]`\n"
        "   One `--entry` per sub-issue, in textbook order, covering "
        "every entry for the chapter (existing + new) — the command "
        "rewrites the chapter's whole list, so omitting an existing "
        "entry drops it. Add `--replace` iff the chapter is already "
        "registered. The tracker substring must match exactly one line "
        "in the parent issue's chapter section. Confirm the printed "
        "block (or `marathon review show-registry`) before moving on.\n"
        f"4. Update parent tracker `#<parent>` body's `### Chapter "
        f"{chapter}:` section: insert numbered lines in textbook order; "
        "renumber subsequent lines; default status emoji is 🟠. Use "
        "`gh issue view <parent> --json body --jq .body > /tmp/b.md`, "
        "edit, `gh issue edit <parent> --body-file /tmp/b.md`.\n"
        f"5. Verify with `marathon review list --chapter {chapter}` — "
        "the new entries should appear at their textbook positions.\n\n"
        "**Body refreshes** (when fixing drift on existing sub-issues):\n"
        "* `gh issue view N --json body --jq .body > /tmp/issue<N>-body.md`\n"
        "* Edit in place (Python or sed; preserve comments + historical "
        "content).\n"
        "* `gh issue edit N --body-file /tmp/issue<N>-body.md`.\n\n"
        "**Stopping is the load-bearing step.** Stop after proposing; "
        "stop again between any two apply steps if surprises surface. "
        "Never paper over an error mid-apply."
    )


def _common_output_format_section(kind: SessionKind) -> str:
    if kind == "bootstrap":
        tldr = (
            "`<N> first-class sub-issues drafted across <M> declarations. "
            "<K> scaffolding declarations folded under umbrellas. <P> "
            "unpaired declarations (no informal statement provided).`"
        )
        body_proposals = (
            "* **Proposed sub-issues** (one line each, in textbook "
            "order): `<idx>. <title> — <textbook ref>`\n"
            "* **Scaffolding folded** (grouped by umbrella, one line "
            "each): `<umbrella>: <decl₁>, <decl₂>, …`\n"
            "* **Unpaired declarations** (couldn't match to an informal "
            "statement): `<decl>: <proposed action> (drop / forward-"
            "scaffold / ask human)`\n"
        )
    else:  # audit
        tldr = (
            "`<D> drift-edits, <G> coverage gaps (new sub-issues), <R> "
            "readability passes proposed.`"
        )
        body_proposals = (
            "* **Drift edits** (one line each): "
            "`#<num> — <kind of drift> — <proposed fix>`\n"
            "* **New sub-issues** (coverage gaps, one line each, in "
            "textbook order): `<idx>. <title> — <textbook ref>`\n"
            "* **Readability passes** (one line each): "
            "`#<num> — <one-line description of the polish>`\n"
            "* **Status-label mismatches** (if any): "
            "`#<num> — body says <X>, label says <Y> — recommended <fix>`\n"
        )
    return (
        "## Output format (concise — humans move fast, read slowly)\n\n"
        "* **TL;DR** (one line): " + tldr + "\n"
        + body_proposals +
        "* **Then stop.** Do not run any apply step yet. Do not "
        "speculate on edits the human hasn't asked for. **Stopping is "
        "the load-bearing step.**\n"
        "* Use clickable links for every cited file:line "
        "(`[file.lean:NN-MM](path/to/file.lean#LNN-LMM)`) and every "
        "GitHub issue (`[#N](https://github.com/<repo>/issues/N)`). "
        "Bare backticks for file refs are not enough — they don't "
        "render as VS Code jumps.\n"
    )


def build_bootstrap_briefing(
    cfg: ReviewConfig,
    chapter: int,
    informal_statements_path: Optional[Path],
) -> str:
    """Assemble the on-disk briefing for ``bootstrap-chapter``."""
    inputs = _common_inputs_section(cfg, chapter)
    rubric = _common_decision_rubric_section()
    output = _common_output_format_section("bootstrap")
    apply = _common_apply_section(cfg, chapter)
    target = cfg.target_path(chapter)
    try:
        rel_target = target.relative_to(cfg.repo_dir)
    except ValueError:
        rel_target = target

    if informal_statements_path is not None:
        try:
            rel_informal = informal_statements_path.relative_to(cfg.repo_dir)
        except ValueError:
            rel_informal = informal_statements_path
        informal_block = (
            f"* **Informal-statements file** (user-provided): "
            f"`@{rel_informal}`. Each section corresponds to one named "
            f"textbook result (a ref + an informal statement). Pair "
            f"each section with one Lean declaration. Some informal "
            f"sections may map to multiple declarations (e.g., a "
            f"three-part lemma `Lemma_X_Y_a/b/c`) — these go in **one** "
            f"sub-issue with all three signatures in the Lean-signatures "
            f"section.\n"
        )
        informal_caveat = (
            "Because the human provided informal statements, the "
            "Informal Statement section of each drafted sub-issue "
            "should *quote the human's wording verbatim* (with a "
            "`*✅ Verified against the textbook reference (user-"
            "provided).*` line at the top of the section). Do NOT "
            "paraphrase, summarise, or LLM-render."
        )
    else:
        informal_block = (
            "* **No informal-statements file provided.** You will need "
            "to LLM-render an informal statement per first-class "
            "declaration from general mathematical knowledge. Every "
            "such LLM-rendered Informal Statement MUST open with "
            "`*⚠️ LLM-rendered from common knowledge; verification "
            "pending.*` so the human knows to verify each one before "
            "their first review pass.\n"
        )
        informal_caveat = (
            "Because no human-provided informal-statements file was "
            "supplied, every Informal Statement section in the drafts "
            "MUST carry the `⚠️ LLM-rendered … verification pending` "
            "marker. The first review pass on each sub-issue (via "
            "`marathon review open N`) will be where the human approves "
            "or corrects them."
        )

    return f"""# Marathon chapter-bootstrap briefing — Chapter {chapter}

## Role

You are the **chapter-bootstrap reviewer** for marathon's per-declaration
review queue. This is a one-time setup pass for Chapter {chapter}. You
read every Lean file in the chapter folder, pair declarations with the
human's informal-statement file (or LLM-render statements if none was
provided), draft a `Chapter{chapter}.md` drafts file in the project's
standard format, propose the full set of sub-issues, and **stop and
wait** for the human's go-ahead before creating any GitHub issues.

You are a thinking partner. You never apply on your own. The human
approves the proposal first; only then do you create issues, register
the chapter via `marathon review register-chapter`, and patch the
parent tracker.

## Hard constraints (non-negotiable)

* **NEVER modify `.lean` files.** You read the Lean code to draft
  sub-issue bodies — you do not edit it. If you find a code issue
  during bootstrapping, note it in the draft's Mechanical accuracy
  section; the human will address it during the review cycle.
* **NEVER modify GitHub labels directly.** Labels are managed by
  `marathon review verify` and `marathon review reject`.
* **NEVER modify `state.json` directly.** State transitions go
  through the marathon CLI.
* **NEVER hand-edit `.marathon/review/config.toml`.** The
  `[[chapters]]` registry is machine-managed; register entries only
  via `marathon review register-chapter` (exact command in the
  apply-sequence below). Hand-edits are a documented desync source.

{inputs}
{informal_block}

## Workflow

1. **Read every `.lean` file in `{rel_target}`** (use Glob + Read).
   Enumerate top-level declarations: `def`, `theorem`, `lemma`,
   `abbrev`, `instance`, `structure`, `class`, `opaque`. Record line
   ranges for each. Note leading docstrings or comments — they often
   identify the textbook reference (e.g., "Lee p. 352", "Theorem
   14.23(b)").

2. **Read the informal-statements file** (if one was provided). Pair
   each section with one or more Lean declarations. If a declaration
   maps to no informal section, flag it as unpaired.

3. **Classify each declaration** using the decision rubric below.
   First-class declarations get their own sub-issue; scaffolding folds
   under an umbrella.

4. **Draft `.marathon/review/drafts/Chapter{chapter}.md`**. Use a
   sibling chapter's drafts file (e.g.,
   `@.marathon/review/drafts/Chapter14.md` if it exists) as the
   structural template. Match its section layout, status-line format,
   verdict-footer wording. Each section is one first-class declaration
   and contains:
   * Top header: `## <idx>/<total> — <Name> (<textbook ref>)`
   * Title field: `**Title**: \\`<short title for the GitHub issue>\\``
   * Status header: `**Parent**: #{cfg.parent_issue} — **LeeSM ref**: <ref> — **Status**: 🟠`
   * `### Lean signatures` section: code blocks for each declaration
     in this sub-issue, each followed by a clickable GitHub-pinned
     link (`[<decl> at <file>:<lines>](https://github.com/{cfg.github_repo}/blob/<SHA>/<path>#L<a>-L<b>)`).
     Use `git rev-parse HEAD` for the SHA.
   * `### Informal Statement` section: the user's wording (verbatim,
     with the `*✅ Verified*` line) **or** LLM-rendered text with the
     `*⚠️ LLM-rendered*` marker.
   * `### Mechanical accuracy` section: drift-detection bullets the
     reviewer can use during their first pass (signatures correct,
     hypotheses tight, naming, downstream consumers).
   * `### Verification questions` section: open questions for the
     human to confirm.
   * `**Verdict**: _(VERIFIED 🟡 / REJECTED ❌)_` footer.

5. **Propose the sub-issue list** to the human in the chat (do not
   write to GitHub yet). Use the output format below. Include both the
   first-class list AND a "scaffolding folded" summary so the human
   can verify nothing important was lost.

6. **Stop and wait.** When the human signals approval (possibly with
   edits to specific drafts), apply per the apply-sequence below. If
   they request edits, regenerate the draft file and re-propose
   without applying.

{rubric}

{informal_caveat}

{output}

{apply}

---

Go.
"""


def build_audit_briefing(cfg: ReviewConfig, chapter: int) -> str:
    """Assemble the on-disk briefing for ``audit-chapter``."""
    inputs = _common_inputs_section(cfg, chapter)
    rubric = _common_decision_rubric_section()
    output = _common_output_format_section("audit")
    apply = _common_apply_section(cfg, chapter)
    target = cfg.target_path(chapter)
    try:
        rel_target = target.relative_to(cfg.repo_dir)
    except ValueError:
        rel_target = target

    return f"""# Marathon chapter-audit briefing — Chapter {chapter}

## Role

You are the **chapter-audit reviewer**. The chapter already has a
per-declaration sub-issue queue; your job is to compare each
sub-issue body against the current code, identify drift + coverage
gaps + readability problems, propose a unified set of edits, and
**stop and wait** for the human's go-ahead before applying anything.

You are a thinking partner. You never apply on your own. The human
approves; only then do you edit issue bodies, create new sub-issues
for gaps, or fix status-label mismatches.

## Hard constraints (non-negotiable)

These override any other instruction in this briefing. Violations
break the human's trust and invalidate the audit.

* **NEVER modify `.lean` files.** The audit is **read-only** on Lean
  code. You read, compare, and report — you do not edit. Code changes
  go through the `marathon review reject` → daemon → iterate cycle,
  not through the audit coreviewer. If you identify a code change
  that's needed, describe it in the chat as a proposed drift-edit;
  the human will decide whether to reject the sub-issue for iteration
  or make the change manually.
* **NEVER modify GitHub labels.** Labels (`review:verified`,
  `review:rejected`, etc.) are managed exclusively by
  `marathon review verify` and `marathon review reject`. Do not call
  `gh issue edit --add-label` or `--remove-label`. Do not invent new
  labels. If you detect a label mismatch, report it in your output;
  do not fix it.
* **NEVER modify `state.json` directly.** State transitions go
  through the marathon CLI, not through file edits.
* **NEVER hand-edit `.marathon/review/config.toml`.** The
  `[[chapters]]` registry is machine-managed; if the audit creates
  new sub-issues for coverage gaps, register them only via
  `marathon review register-chapter --replace` (exact command in the
  apply-sequence below). Hand-edits are a documented desync source.

{inputs}

## Workflow

1. **List all sub-issues**: `marathon review list --chapter {chapter}`.
   For each, fetch the current GitHub body via
   `gh issue view N --repo {cfg.github_repo} --json body --jq .body`.

2. **Read every `.lean` file in `{rel_target}`** (Glob + Read).
   Enumerate top-level declarations as for `bootstrap-chapter`.

3. **Cross-reference each sub-issue against current code**:
   * **Body drift**: is the inline Lean snippet in the issue body the
     same as what's currently in the file? Note: line numbers in the
     body's commit-pinned URLs are **not** drift on their own — those
     links resolve against the historical commit and stay correct.
     Inline snippet contents and *intra-body* line references (e.g.,
     "see `DifferentialForm.lean:178-181`") are what matter.
   * **Verdict-status mismatch**: does the body's verdict footer
     (✅ / ❌) match the issue label (`review:verified` / `review:rejected`)?
     Could the verdict have been overtaken by recent code changes —
     e.g., a verified issue whose code was then deleted, or a rejected
     issue whose fix landed but verdict wasn't flipped?
   * **Informal Statement freshness**: still flagged
     `⚠️ LLM-rendered … verification pending`? Has the human signaled
     approval in a comment? If yes → propose to remove the ⚠️ marker
     and add `*✅ Verified against textbook (human-approved YYYY-MM-DD).*`.
   * **Stale references**: any inline `file:line` or commit SHA that
     points to long-gone code.

4. **Coverage gaps**: walk every top-level declaration in
   `{rel_target}` and check whether it (or its umbrella) is covered
   by a sub-issue. Use the decision rubric. Flag first-class
   declarations that have no sub-issue.

5. **Readability passes**: places where an issue body could be
   tightened — bullet lists that lost context, paragraphs that became
   too long, code snippets that could be shorter, sections marked
   `outstanding` whose content is now resolved.

6. **Propose the unified edit set** to the human in the chat. Use the
   output format below. Include drift, gaps, status mismatches, and
   readability passes as four distinct lists. Show diffs at the
   one-line summary level — full diffs come during apply.

7. **Stop and wait.** Apply on explicit go-ahead, one sub-issue at a
   time. For each apply, surface the actual body diff before pushing
   so the human can inline-veto.

{rubric}

For body refreshes, minimal-diff is preferred. Don't rewrite a body
that's basically current; just patch the stale parts. Match the prior
verdict comments' wording style if a comment thread exists.

{output}

{apply}

---

Go.
"""


def _write_briefing(cfg: ReviewConfig, chapter: int, kind: SessionKind, text: str) -> Path:
    sessions_dir = cfg.repo_dir / SESSIONS_RELDIR
    sessions_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = sessions_dir / f"c{chapter}-{kind}-{ts}.md"
    path.write_text(text)
    return path


def _build_pointer_prompt(
    cfg: ReviewConfig, chapter: int, kind: SessionKind, briefing_path: Path
) -> str:
    """The short URI prompt that just points at the on-disk briefing."""
    try:
        rel_briefing = briefing_path.relative_to(cfg.repo_dir)
    except ValueError:
        rel_briefing = briefing_path
    parent_url = f"https://github.com/{cfg.github_repo}/issues/{cfg.parent_issue}"
    target = cfg.target_path(chapter)
    try:
        rel_target = target.relative_to(cfg.repo_dir)
    except ValueError:
        rel_target = target

    label = "chapter-bootstrap" if kind == "bootstrap" else "chapter-audit"
    return (
        f"# Marathon {label} session — Chapter {chapter}\n\n"
        f"Your full briefing is at `@{rel_briefing}` — read it first, "
        f"then follow its workflow precisely. The briefing is the "
        f"load-bearing instruction; this URI prompt is just the "
        f"pointer.\n\n"
        f"**Repo**: {cfg.github_repo}  •  **Chapter**: {chapter}  •  "
        f"**Target folder**: `{rel_target}/`  •  "
        f"**Parent tracker**: [#{cfg.parent_issue}]({parent_url})\n\n"
        f"You are a thinking partner — read, analyse, propose, then "
        f"**stop and wait** for the human's go-ahead. Never apply on "
        f"your own. When you do apply (on the human's signal), follow "
        f"the apply-sequence in the briefing precisely; stop between "
        f"steps if surprises surface.\n\n"
        f"Go.\n"
    )


def open_chapter_session(
    cfg: ReviewConfig,
    chapter: int,
    kind: SessionKind,
    *,
    informal_statements_path: Optional[Path] = None,
    dry_run: bool = False,
) -> ChapterSessionResult:
    """Assemble the briefing + URI for ``bootstrap-chapter`` / ``audit-chapter``.

    Writes the full briefing to ``.marathon/review/sessions/`` and fires
    the platform URL opener with a short pointer prompt (unless
    ``dry_run`` is True, in which case only the URI is returned)."""
    if kind == "bootstrap":
        text = build_bootstrap_briefing(cfg, chapter, informal_statements_path)
    elif kind == "audit":
        text = build_audit_briefing(cfg, chapter)
    else:
        raise ValueError(f"unknown session kind: {kind!r}")

    briefing_path = _write_briefing(cfg, chapter, kind, text)
    pointer = _build_pointer_prompt(cfg, chapter, kind, briefing_path)
    if len(pointer) > PROMPT_CHAR_BUDGET:
        raise RuntimeError(
            f"pointer prompt is {len(pointer)} chars, exceeds budget "
            f"{PROMPT_CHAR_BUDGET}; this is a code bug (the pointer "
            "should always be tiny)"
        )
    uri = f"vscode://anthropic.claude-code/open?prompt={quote(pointer, safe='')}"

    result = ChapterSessionResult(
        uri=uri,
        prompt_chars=len(pointer),
        briefing_path=briefing_path,
    )
    if dry_run:
        return result

    opener = _platform_url_opener()
    if opener is None:
        raise RuntimeError(
            f"no URL opener detected for platform {sys.platform!r}; "
            "copy the URI manually and paste into your browser or "
            "VS Code's URI handler"
        )
    subprocess.run([*opener, uri], check=False)
    return result

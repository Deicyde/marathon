"""``marathon repair`` — dispatch Aristotle to make a non-compiling module build.

The THIRD kind of Aristotle work (after ``refine`` = human-review response,
``fill`` = proving): a REPAIR takes a module that fails ``lake build`` and fixes
it to compile against the CURRENT codebase — updating references to renamed /
removed upstream declarations and fixing type mismatches — while PRESERVING the
mathematical meaning of every statement. A downstream chapter breaks whenever an
upstream API changes under it; this self-heals it, systematically, instead of
ad-hoc manual patches.

Like ``fill``, it wraps ``marathon refine`` with a focus directive (built from
the module's actual ``lake build`` errors), so the whole iteration engine —
auto-build, auto-commit, auto-pr, the ``[build:*]`` tag — Just Works. A repair
is skeleton-level (it makes statements compile), so it lands on ``main`` and
then flows into the normal review queue, where faithfulness is judged by the
human (and, before merge, by the repair faithfulness jury — build-OK proves it
compiles, never that it stayed faithful).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Optional

from marathon.review.config import ReviewConfig

#: Matches the audit snapshot's build-failure entries (see
#: ``marathon.audit.engine.run_audit``): ``module <M> failed to build; …``.
_FAILED_MODULE_RE = re.compile(r"module\s+(\S+)\s+failed to build")

#: Extract the integer from a target folder name like ``Chapter16``.
_CHAPTER_RE = re.compile(r"Chapter(\d+)", re.IGNORECASE)


def broken_modules(cfg: ReviewConfig, chapter: int) -> list[str]:
    """The chapter's modules the LATEST audit snapshot recorded as failing to
    build — the repair work-list. Empty when there is no snapshot or nothing is
    broken. Cheap (reads the persisted snapshot); the repair itself re-builds to
    get the current errors, so a slightly stale snapshot only over/under-lists."""
    try:
        from marathon.audit.engine import load_snapshot

        snap = load_snapshot(cfg.repo_dir)
    except Exception:  # noqa: BLE001 — a missing/broken snapshot is non-fatal
        return []
    if snap is None:
        return []
    needle = f"Chapter{chapter}"
    out: set[str] = set()
    for f in getattr(snap, "failures", []) or ():
        m = _FAILED_MODULE_RE.search(f or "")
        if m and needle in m.group(1):
            out.add(m.group(1))
    return sorted(out)


def _module_build_errors(repo_dir: Path, module: str, timeout: int = 1800) -> str:
    """Run ``lake build <module>`` and return its error tail (empty-ish note when
    it actually builds — the snapshot was stale)."""
    from marathon.post_pipeline import run_lake_build

    res = run_lake_build(repo_dir, timeout, targets=[module])
    if res.ok:
        return "(this module currently builds — the audit snapshot was stale)"
    return (res.log_tail or "(no build log captured)").strip()


def _build_repair_directive(module_errors: "list[tuple[str, str]]") -> str:
    """The highest-priority focus directive for a repair iteration, built from
    the target modules and their real compiler errors."""
    mods = ", ".join(f"`{m}`" for m, _ in module_errors)
    blocks = "\n\n".join(
        f"### `{m}` — `lake build` errors\n```\n{errs}\n```"
        for m, errs in module_errors
    )
    return (
        f"REPAIR TASK — the following module(s) do NOT compile: {mods}. Make "
        f"`lake build` succeed for them.\n\n"
        f"{blocks}\n\n"
        "The codebase's API has evolved since these modules were written — "
        "upstream chapters have renamed or removed declarations and changed "
        "signatures. Repair the module(s) to compile against the CURRENT code:\n"
        "- Update references to renamed / removed declarations to their current "
        "equivalents (search the codebase for the new name/signature).\n"
        "- Fix type mismatches by adapting to the current signatures.\n"
        "- You MAY add small private bridging definitions if a needed upstream "
        "declaration was removed outright.\n\n"
        "CRITICAL — preserve meaning (a human, and a faithfulness jury, review "
        "this):\n"
        "- PRESERVE the mathematical MEANING of every definition and theorem "
        "statement. This is a compilation repair, not a re-statement.\n"
        "- NEVER weaken a statement to force a build: do not drop or strengthen "
        "hypotheses, weaken a conclusion, replace a statement with `True` / "
        "`trivial`, or make a definition vacuous.\n"
        "- If a statement genuinely cannot compile without changing its meaning, "
        "leave its proof body `sorry` and keep the STATEMENT as close to the "
        "original intent as possible — a still-`sorry` faithful statement is "
        "correct here; a compiling-but-weakened one is not.\n"
        "- Only edit the module(s) named above (plus any private helpers they "
        "need); leave every other file untouched."
    )


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def add_repair_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Wire ``marathon repair`` into the top-level argparse."""
    from marathon.fill import _add_passthrough_refine_flags

    p = subparsers.add_parser(
        "repair",
        help="Make a non-compiling module build again via Aristotle.",
        description=(
            "Run a focused refine iteration that repairs a module which fails "
            "`lake build` — updating renamed/removed upstream references and "
            "fixing type mismatches to compile against the CURRENT code, while "
            "PRESERVING every statement's meaning. Wraps `marathon refine` with "
            "a directive built from the real compiler errors. Lands on `main` "
            "under a `marathon/repair-c<N>` branch. Pass `--module`/`--file` to "
            "target one, or omit both to repair every broken module the latest "
            "audit snapshot records for the chapter."
        ),
    )
    p.add_argument("target", type=Path,
                   help="Path to the Lean chapter folder to repair.")
    p.add_argument("--repo-dir", type=Path, required=True, metavar="PATH",
                   help="Path to the Lean project repo (must be a git repo).")
    grp = p.add_mutually_exclusive_group(required=False)
    grp.add_argument("--module", type=str, action="append", default=None,
                     dest="modules", metavar="NAME",
                     help="Fully-qualified module to repair (repeatable; e.g. "
                          "GeometricAnalysis.LeeSM.Chapter16.IntegrationManifold). "
                          "The conductor passes the broken set explicitly.")
    grp.add_argument("--file", type=Path, default=None, metavar="PATH",
                     help="Repo-relative or absolute .lean file to repair.")
    _add_passthrough_refine_flags(p)
    p.set_defaults(func=_run_repair, _verb="repair")


def _chapter_of_target(target: Path) -> Optional[int]:
    m = _CHAPTER_RE.search(target.name) or _CHAPTER_RE.search(str(target))
    return int(m.group(1)) if m else None


def _module_of_file(repo_dir: Path, file_path: Path) -> Optional[str]:
    from marathon.formalization import module_from_file_path

    try:
        rel = file_path.resolve().relative_to(repo_dir.resolve())
    except ValueError:
        rel = file_path
    return module_from_file_path(str(rel))


async def _run_repair(args) -> None:
    """``marathon repair``: resolve broken module(s), build a repair directive
    from their real compiler errors, and delegate to ``refine_command``."""
    from marathon.refine import refine_command

    repo_dir: Path = args.repo_dir.resolve()
    chapter = _chapter_of_target(args.target)

    modules: list[str] = []
    if getattr(args, "modules", None):
        modules = list(args.modules)
    elif args.file is not None:
        fp = args.file if args.file.is_absolute() else (repo_dir / args.file)
        mod = _module_of_file(repo_dir, fp)
        if mod is None:
            raise SystemExit(f"could not map {args.file} to a module name")
        modules = [mod]
    else:
        if chapter is None:
            raise SystemExit(
                f"could not infer a chapter from target {args.target!r}; "
                "pass --module or --file explicitly."
            )
        from marathon.review.config import load_config

        try:
            cfg = load_config(repo_dir)
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"could not load review config for discovery: {e}")
        modules = broken_modules(cfg, chapter)
        if not modules:
            raise SystemExit(
                f"no broken modules recorded for chapter {chapter} in the "
                "latest audit snapshot — run `marathon audit run` first, or "
                "pass --module/--file. (Nothing to repair.)"
            )
        print(f"  repair: {len(modules)} broken module(s) in chapter {chapter}: "
              f"{', '.join(modules)}")

    module_errors = [
        (m, _module_build_errors(repo_dir, m, getattr(args, "build_timeout", 1800)))
        for m in modules
    ]

    args.focus_directive = _build_repair_directive(module_errors)
    # Mark this a REPAIR run: branch kind `repair-` (→ main) and the
    # faithfulness jury. Not tied to a review issue → chapter-scoped branch.
    args.repair_run = True
    # A repair may legitimately leave `sorry` bodies (statement compiles, proof
    # deferred), so run the gate in warn (the [build:OK] automerge gate is what
    # keeps a still-broken repair off main) and let it iterate a few times to
    # converge on a green build.
    args.max_iterations = max(int(getattr(args, "max_iterations", 0) or 0), 3)
    args.skeleton = False
    args.review_rejection = None
    args.update_formalization = getattr(args, "update_formalization", True)
    args.tex = getattr(args, "tex", None)
    args.max_prompt_words = getattr(args, "max_prompt_words", None)
    args.no_cross_chapter = getattr(args, "no_cross_chapter", True)
    args.dry_run = getattr(args, "dry_run", False)
    await refine_command(args)

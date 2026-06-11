"""``marathon fill`` and ``marathon fill-file`` — declaration- and
file-scoped Aristotle fill primitives.

Both wrap ``marathon refine`` with a focused ``--focus-directive`` so
the iteration targets ONE declaration (or ONE file's worth of
``sorry``-bodies) rather than the whole chapter. Two new CLI verbs
sit on top of the same iteration engine, so all the existing
machinery — ``--auto-build``, ``--auto-commit``, ``--auto-pr``,
``cross-chapter staging``, the verified-decl audit, rater — Just
Works.

When ``--issue N`` is passed, the issue's body is fetched, ` ```lean ``
code blocks are scanned for declaration names, and the focus directive
is constructed against those names. This makes the slash-command
pattern ``/marathon:fill 55`` natural: the issue number is the primary
key, the cited declarations are the fill targets.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import Optional


# Lean decl-keyword set, plus the regex matching one declaration's
# opening line. Mirrors ``marathon/review/verified_decls.py`` so the
# two stay in sync; if you change DECL_KEYWORDS there, change it here.
_DECL_KEYWORDS = (
    "def", "theorem", "lemma", "abbrev", "instance", "structure",
    "class", "inductive", "opaque", "axiom",
)
_DECL_RE = re.compile(
    r"^\s*"
    + r"(?:@\[[^\]]*\]\s*)*"
    + r"(?:noncomputable\s+)?(?:private\s+)?(?:protected\s+)?"
    + r"(?:" + "|".join(_DECL_KEYWORDS) + r")\s+"
    + r"(?P<name>[A-Za-z_][\w'.]*)",
    re.MULTILINE,
)
_LEAN_BLOCK_RE = re.compile(r"```lean\n(.*?)\n```", re.DOTALL)


def _extract_decls_from_text(text: str) -> list[str]:
    """Return declaration names found in plain Lean text (not inside
    ``` ```lean ``` `` blocks). Used to walk a ``.lean`` file."""
    return [m.group("name") for m in _DECL_RE.finditer(text)]


def _extract_decls_from_issue_body(body: str) -> list[str]:
    """Return declaration names cited in ``` ```lean ``` `` code blocks
    inside an issue body. Dedup preserving first-occurrence order."""
    seen: set[str] = set()
    out: list[str] = []
    for block in _LEAN_BLOCK_RE.finditer(body):
        for m in _DECL_RE.finditer(block.group(1)):
            name = m.group("name")
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _fetch_issue_body(issue_num: int, repo: str) -> Optional[str]:
    """``gh issue view N --json body --jq .body``. Returns ``None`` on
    failure (network error, issue not found, repo not specified)."""
    proc = subprocess.run(
        ["gh", "issue", "view", str(issue_num),
         "--repo", repo,
         "--json", "body", "--jq", ".body"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout or None


def _find_sorries_in_file(file_path: Path) -> list[str]:
    """Walk ``file_path`` and return the names of declarations whose
    body contains ``sorry``. Approximation: tracks the most recent
    decl-keyword line and flags it when a subsequent line in the same
    declaration contains ``sorry`` (before the next decl-keyword line).
    Line comments are skipped. False positives possible for sorries
    inside string literals — empirically rare in Lean source."""
    if not file_path.is_file():
        return []
    try:
        lines = file_path.read_text().splitlines()
    except OSError:
        return []
    sorry_re = re.compile(r"\bsorry\b")
    out: list[str] = []
    seen: set[str] = set()
    current_decl: Optional[str] = None
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        m = _DECL_RE.match(line)
        if m:
            current_decl = m.group("name")
            continue
        if current_decl is not None and sorry_re.search(line):
            if current_decl not in seen:
                seen.add(current_decl)
                out.append(current_decl)
    return out


# ---------------------------------------------------------------------------
# Subparser wiring
# ---------------------------------------------------------------------------


def add_fill_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Wire ``marathon fill`` and ``marathon fill-file`` into the top-
    level argparse. Both delegate to ``refine_command`` after building
    a focus directive — the slash commands shell out to these verbs and
    don't need to know the focus-directive incantation."""

    p_fill = subparsers.add_parser(
        "fill",
        help="Fill the `sorry` body of a single declaration via Aristotle.",
        description=(
            "Run one focused refine iteration whose Hermes prompt is "
            "constrained to fill ONLY the named declaration's `sorry`. "
            "Wraps `marathon refine` with `--focus-directive` and a "
            "single-iteration default — all the existing refine "
            "machinery (auto-build, auto-commit, auto-pr, rater) "
            "applies. Pass `--issue N` instead of `--decl` to fill the "
            "declarations cited in a GitHub sub-issue's body."
        ),
    )
    p_fill.add_argument("target", type=Path,
        help="Path to the Lean folder containing the declaration.")
    p_fill.add_argument("--repo-dir", type=Path, required=True, metavar="PATH",
        help="Path to the Lean project repo (must be a git repo).")
    grp = p_fill.add_mutually_exclusive_group(required=True)
    grp.add_argument("--decl", type=str, default=None, metavar="NAME",
        help="Fully-qualified declaration name to fill (e.g. "
             "`DifferentialForm.coordinateCoframeWedge`).")
    grp.add_argument("--issue", type=int, default=None, metavar="ISSUE_NUM",
        help="Fill the declarations cited in this GitHub sub-issue's body. "
             "Requires the consumer repo to use `marathon review` "
             "(reads `.marathon/review/config.toml` for the repo).")
    _add_passthrough_refine_flags(p_fill)
    p_fill.set_defaults(func=_run_fill, _verb="fill")

    p_fill_file = subparsers.add_parser(
        "fill-file",
        help="Fill every `sorry` body in a single Lean file via Aristotle.",
        description=(
            "Run one focused refine iteration whose Hermes prompt is "
            "constrained to fill every `sorry`-bodied declaration in "
            "the named file, leaving every other file in the chapter "
            "exactly as it is. Wraps `marathon refine` with "
            "`--focus-directive`."
        ),
    )
    p_fill_file.add_argument("target", type=Path,
        help="Path to the Lean folder (chapter) containing the file.")
    p_fill_file.add_argument("--repo-dir", type=Path, required=True, metavar="PATH",
        help="Path to the Lean project repo (must be a git repo).")
    p_fill_file.add_argument("--file", type=Path, required=True, metavar="PATH",
        help="Repo-relative or absolute path to the .lean file to fill.")
    _add_passthrough_refine_flags(p_fill_file)
    p_fill_file.set_defaults(func=_run_fill_file, _verb="fill-file")


def _add_passthrough_refine_flags(p: argparse.ArgumentParser) -> None:
    """Add the subset of refine flags that ``fill``/``fill-file``
    pass through. Defaults match the slash-command-driven usage:
    auto-build + auto-commit + auto-pr on by default since the fill
    primitives are meant for one-shot landings."""
    p.add_argument("--workdir", type=Path, default=None, metavar="DIR")
    p.add_argument("--max-retries", type=int, default=2, metavar="N")
    p.add_argument("--polling-interval", type=int, default=60, metavar="SECONDS")
    p.add_argument("--build-timeout", type=int, default=1800, metavar="SECONDS")
    p.add_argument("--auto-build", action="store_true", default=True)
    p.add_argument("--no-auto-build", dest="auto_build", action="store_false")
    p.add_argument("--auto-commit", action="store_true", default=True)
    p.add_argument("--no-auto-commit", dest="auto_commit", action="store_false")
    p.add_argument("--auto-push", action="store_true", default=True)
    p.add_argument("--no-auto-push", dest="auto_push", action="store_false")
    p.add_argument("--auto-rate", action="store_true", default=True)
    p.add_argument("--no-auto-rate", dest="auto_rate", action="store_false")
    p.add_argument("--auto-pr", action="store_true", default=True)
    p.add_argument("--no-auto-pr", dest="auto_pr", action="store_false")
    p.add_argument("--auto-pr-repo", default=None, metavar="OWNER/NAME")
    p.add_argument("--auto-pr-base", default="main", metavar="BRANCH")
    p.add_argument("--audit-verified", action="store_true", default=True)
    p.add_argument("--no-audit-verified", dest="audit_verified", action="store_false")
    # Machine gate (phase-2). Fill's defaults-on philosophy does NOT
    # escalate the posture: the gate stays at warn here too (enforcement
    # is an explicit operator choice everywhere), and the jury stays
    # opt-in because it bills a Max-session Claude call per landing.
    p.add_argument("--gate", choices=("off", "warn", "enforce"), default="warn")
    p.add_argument("--gate-override", type=str, default=None, metavar="REASON")
    p.add_argument("--jury", action="store_true", default=False)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def _run_fill(args) -> None:
    """``marathon fill``: resolve the decl(s), build a focus directive,
    delegate to refine_command."""
    from marathon.refine import refine_command

    decls: list[str] = []
    if args.decl:
        decls = [args.decl]
    elif args.issue is not None:
        repo = _infer_review_repo(args.repo_dir)
        if repo is None:
            raise SystemExit(
                "could not infer the GitHub repo for --issue lookup; "
                "ensure `.marathon/review/config.toml` exists at the repo "
                "root or pass --auto-pr-repo OWNER/NAME explicitly."
            )
        body = _fetch_issue_body(args.issue, repo)
        if body is None:
            raise SystemExit(
                f"could not fetch issue #{args.issue} from {repo}; "
                "check `gh auth status` and that the issue exists."
            )
        decls = _extract_decls_from_issue_body(body)
        if not decls:
            raise SystemExit(
                f"issue #{args.issue} has no ` ```lean ``` ` code blocks; "
                "cannot derive fill targets. Pass --decl NAME instead."
            )
        print(f"  fill: targeting {len(decls)} declaration(s) cited in "
              f"#{args.issue}: {', '.join(decls)}")

    args.focus_directive = _build_fill_directive(decls)
    # Fill is one-shot: a single iteration that either lands or doesn't.
    # The human re-runs `marathon fill` with sharper context if it
    # didn't land.
    args.max_iterations = 1
    args.skeleton = False  # filling sorries is the opposite of skeleton mode
    # Pass --review-rejection to inherit the per-issue branch naming
    # (`marathon/refine-c<N>-i<issue>`) when --auto-pr is on. If --issue
    # wasn't given but --decl was, leave it None — the branch will use
    # the chapter-only fallback.
    if args.issue is not None:
        args.review_rejection = args.issue
    else:
        args.review_rejection = getattr(args, "review_rejection", None)
    # Forward an `--update-formalization`-compatible default (refine's
    # CLI uses the dest="update_formalization").
    args.update_formalization = getattr(args, "update_formalization", True)
    # refine.py expects these even when None.
    args.tex = getattr(args, "tex", None)
    args.referee = getattr(args, "referee", None)
    args.max_prompt_words = getattr(args, "max_prompt_words", None)
    args.no_cross_chapter = getattr(args, "no_cross_chapter", True)
    args.dry_run = getattr(args, "dry_run", False)
    args.auto_referee_every = getattr(args, "auto_referee_every", 0)
    await refine_command(args)


async def _run_fill_file(args) -> None:
    """``marathon fill-file``: enumerate sorry-bodied decls in the
    target file, build a file-scoped focus directive, delegate."""
    from marathon.refine import refine_command

    file_path: Path = args.file
    if not file_path.is_absolute():
        file_path = (args.repo_dir / file_path).resolve()
    if not file_path.is_file():
        raise SystemExit(f"--file not found: {file_path}")
    decls = _find_sorries_in_file(file_path)
    if not decls:
        raise SystemExit(
            f"no `sorry`-bodied declarations found in {file_path.name}. "
            "Nothing to fill."
        )
    rel = file_path.relative_to(args.repo_dir) if file_path.is_relative_to(args.repo_dir) else file_path
    print(f"  fill-file: targeting {len(decls)} sorry-bodied decl(s) in "
          f"{rel}: {', '.join(decls)}")

    args.focus_directive = _build_fill_file_directive(rel, decls)
    args.max_iterations = 1
    args.skeleton = False
    args.review_rejection = getattr(args, "review_rejection", None)
    args.update_formalization = getattr(args, "update_formalization", True)
    args.tex = getattr(args, "tex", None)
    args.referee = getattr(args, "referee", None)
    args.max_prompt_words = getattr(args, "max_prompt_words", None)
    args.no_cross_chapter = getattr(args, "no_cross_chapter", True)
    args.dry_run = getattr(args, "dry_run", False)
    args.auto_referee_every = getattr(args, "auto_referee_every", 0)
    await refine_command(args)


# ---------------------------------------------------------------------------
# Focus-directive construction
# ---------------------------------------------------------------------------


def _build_fill_directive(decls: list[str]) -> str:
    """Construct the focus-directive text for `marathon fill`."""
    if len(decls) == 1:
        return (
            f"Fill the `sorry` body of `{decls[0]}` with an honest proof. "
            "Do NOT modify the declaration's signature. Do NOT add, "
            "remove, or rename any other declaration in the file. Do "
            "NOT touch any other file in the chapter."
        )
    name_list = ", ".join(f"`{d}`" for d in decls)
    return (
        f"Fill the `sorry` bodies of these declarations with honest "
        f"proofs: {name_list}. Do NOT modify any declaration's "
        "signature. Do NOT add, remove, or rename any other "
        "declaration in any file. Do NOT touch declarations not "
        "named above."
    )


def _build_fill_file_directive(rel_path: Path, decls: list[str]) -> str:
    """Construct the focus-directive text for `marathon fill-file`."""
    return (
        f"Fill every `sorry` body in `{rel_path}` with an honest "
        f"proof. The current `sorry`-bodied declarations in this file "
        f"are: {', '.join(f'`{d}`' for d in decls)}. Do NOT modify any "
        "declaration's signature. Do NOT add, remove, or rename any "
        "declaration. Do NOT touch any OTHER file in the chapter — "
        f"every change must be inside `{rel_path}`."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_review_repo(repo_dir: Path) -> Optional[str]:
    """Read the `github_repo` field from
    ``<repo_dir>/.marathon/review/config.toml``. Returns None if the
    file doesn't exist (project doesn't use marathon review)."""
    try:
        import tomllib
    except ImportError:
        return None
    cfg_path = Path(repo_dir) / ".marathon" / "review" / "config.toml"
    if not cfg_path.is_file():
        return None
    try:
        data = tomllib.loads(cfg_path.read_text())
    except (OSError, ValueError):
        return None
    return data.get("github_repo")

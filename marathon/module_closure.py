"""Project-local import-closure scoping — "chapter N depends on chapter N only".

A refine/fill/repair of Chapter N must be judged by the build state of Chapter
N and its UPSTREAM dependencies only — never by a broken DOWNSTREAM chapter
(e.g. a Chapter 16 that stopped compiling when Chapter 15's API moved under
it). Two places previously leaked downstream state into an iteration:

* the Aristotle submission bundle staged the ENTIRE repo, so a broken
  downstream module rode along and could push Aristotle's own verification to
  ``COMPLETE_WITH_ERRORS`` no matter how good the in-chapter work was (the #170
  stall);
* the post-pipeline's bare ``lake build`` fallback compiled the default
  target's closure, which may not even include the refined chapter.

This module computes the transitive project-local import closure of a target
folder (plus the lakefile's root lib modules, so the staged tree still
satisfies a default ``lake build``), giving both call sites the same honest
scope: everything the target actually needs, nothing downstream or orphaned.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from marathon.formalization import module_from_file_path

#: ``import Foo.Bar`` at line start (Lean 4 has no indented imports; the
#: MULTILINE anchor keeps commented samples in docstrings from matching most
#: of the time, and a false positive only ever ADDS an upstream module).
_IMPORT_RE = re.compile(r"^import\s+([A-Za-z_«][\w.'«»]*)", re.MULTILINE)

#: ``lean_lib «Name»`` / ``lean_lib Name`` in a lakefile.lean.
_LEAN_LIB_RE = re.compile(r"lean_lib\s+(?:«([^»]+)»|([A-Za-z_][\w.']*))")


def local_module_map(repo_dir: str | Path) -> dict[str, Path]:
    """``{module name: source file}`` for every mappable ``.lean`` under the
    repo, skipping dot-directories (``.lake``, ``.git``, ``.marathon``)."""
    repo_dir = Path(repo_dir)
    out: dict[str, Path] = {}
    for f in sorted(repo_dir.rglob("*.lean")):
        try:
            rel = f.relative_to(repo_dir)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        mod = module_from_file_path(str(rel))
        if mod is not None:
            out[mod] = f
    return out


def _imports_of(path: Path) -> list[str]:
    try:
        text = path.read_text()
    except OSError:
        return []
    return _IMPORT_RE.findall(text)


def import_closure(
    module_map: dict[str, Path], seeds: "set[str]"
) -> "set[str]":
    """Transitive closure of *seeds* over project-local imports. External
    imports (Mathlib, Std, …) are not in *module_map* and are ignored."""
    seen = {m for m in seeds if m in module_map}
    frontier = list(seen)
    while frontier:
        nxt: list[str] = []
        for m in frontier:
            for imp in _imports_of(module_map[m]):
                if imp in module_map and imp not in seen:
                    seen.add(imp)
                    nxt.append(imp)
        frontier = nxt
    return seen


def lakefile_root_modules(
    repo_dir: str | Path, module_map: dict[str, Path]
) -> "set[str]":
    """The lakefile's ``lean_lib`` root modules that exist as source files.
    These seed the closure so a scoped tree still satisfies a bare
    ``lake build`` of the default target (whose root module must resolve)."""
    repo_dir = Path(repo_dir)
    roots: set[str] = set()
    toml_path = repo_dir / "lakefile.toml"
    if toml_path.is_file():
        try:
            data = tomllib.loads(toml_path.read_text())
            for lib in data.get("lean_lib") or []:
                if isinstance(lib, dict) and isinstance(lib.get("name"), str):
                    roots.add(lib["name"])
        except (OSError, tomllib.TOMLDecodeError):
            pass
    lean_path = repo_dir / "lakefile.lean"
    if lean_path.is_file():
        try:
            for m in _LEAN_LIB_RE.finditer(lean_path.read_text()):
                roots.add(m.group(1) or m.group(2))
        except OSError:
            pass
    return {r for r in roots if r in module_map}


def target_scope(
    repo_dir: str | Path, target_dir: str | Path
) -> "tuple[set[str], list[str]]":
    """``(allowed modules, excluded .lean rel-paths)`` for a run targeting
    *target_dir*.

    Allowed = closure(target folder's modules ∪ lakefile root modules).
    Excluded = every other mappable project ``.lean`` — downstream consumers
    and orphans whose build state must not leak into this run's verdict."""
    repo_dir = Path(repo_dir).resolve()
    target = Path(target_dir)
    if not target.is_absolute():
        target = repo_dir / target
    target = target.resolve()
    module_map = local_module_map(repo_dir)
    seeds = {
        m for m, f in module_map.items()
        if f.resolve().is_relative_to(target)
    }
    seeds |= lakefile_root_modules(repo_dir, module_map)
    allowed = import_closure(module_map, seeds)
    excluded = sorted(
        str(f.relative_to(repo_dir))
        for m, f in module_map.items()
        if m not in allowed
    )
    return allowed, excluded

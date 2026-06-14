"""marathon plan — Phase 7 planner intake (the hands-off entry point).

This is goal 2's intake surface (``docs/marathon-v2-plan.md`` §3 Phase 7):
point marathon at an axiom, a repo's sorries, or a textbook and it builds
a **target ledger** to work through — the per-statement work model the
``order.txt`` chapter granularity could never give ("is theorem X done?"
had no machine answer beyond ``lake build``; now every statement is a
:class:`~marathon.ledger.Target` row with a ``status``).

THREE intake modes live here (the NON-textbook ones — the firewall-safe
modes that never read a source ``.tex``):

* :func:`plan_from_sorries` — one ``sorry`` target per sorry-bodied
  declaration across a folder/repo. Generalizes
  :func:`marathon.fill._find_sorries_in_file` (the proven scanner) across
  many files, reusing its exact regex so the two never drift.
* :func:`plan_from_axiom` — a single target for a named axiom/decl to
  discharge.
* :func:`plan_from_repo` — every sorry across the gitignore-filtered repo
  (``plan_from_sorries`` with the whole repo as the folder).

The TEXTBOOK mode (the autoform chunk → k-extractor → merge Claude
pipeline) is the OTHER agent's territory (``marathon.extraction`` +
``prompts/``); this module deliberately leaves a clean extension point
(:func:`Plan.from_targets`) and never imports it.

THE FIREWALL (binding, plan §2 firewall ruling). The three modes here
read only **Lean source** (sorries, axiom names) — never a copyrighted
source text — so they are firewall-safe regardless of the per-project
firewall setting. The setting (:func:`source_mode`, a per-project config
field defaulting to the SAFE ``copyrighted`` interpretation) governs only
the textbook path: ``copyrighted`` forbids Claude-reads-the-book
extraction (targets must come from human informal-statement files or
Aristotle-side runs), ``open`` permits the autoform Claude pipeline. The
default is the safe interpretation so a project that never sets the field
cannot accidentally feed a copyrighted book to Claude.

GATE POLICY (plan §2 ruling 6 "one machinery, two modes"). Every target
carries a ``gate_policy`` ∈ {auto, human}. The planner resolves it from a
mode:

* ``auto`` — hands-off: every target is ``auto`` (jury-passed work lands
  automatically; humans sign spec digests over milestone cones).
* ``human`` — review mode: every target is ``human`` (today's
  per-declaration ceremony).
* ``mixed`` — free per the plan: a configurable set is ``human`` and the
  rest ``auto``. The selection rule (documented, overridable): a target
  is ``human`` iff its decl/source matches a *milestone* substring
  (default :data:`DEFAULT_MILESTONE_KEYWORDS` — Stokes-critical names),
  else ``auto``. "Stokes-critical declarations human, scaffolding auto."

DEPENDENCY EDGES. Where an audit snapshot exists
(``.marathon/audit/latest.json``), a sorry target depends on the targets
for the project-local **defs in its statement cone** — derived purely via
:func:`marathon.audit.kernel.compute_kernel` (Mathlib is trusted
vocabulary, not a dep). Where no snapshot exists, NO edges are derived
(documented degradation — absence of evidence, never a guess). Edges are
only ever drawn BETWEEN targets the plan actually has (a cone def with no
sorry of its own is not a target, so it contributes no edge).

Stdlib only; pure Python; fully offline (no lake, no Aristotle, no
network). The Claude/extraction path lives elsewhere.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from marathon.fill import _DECL_RE
from marathon.ledger import Ledger, Target

# ---------------------------------------------------------------------------
# Gate-policy resolution (plan §2 ruling 6)
# ---------------------------------------------------------------------------

#: The three CLI-time gate-policy resolution modes. 'auto'/'human' map
#: every target uniformly; 'mixed' uses the milestone selection rule.
GATE_MODES = ("auto", "human", "mixed")

#: Default substrings that mark a target 'human' under --gate-policy mixed.
#: These are the Stokes-critical / milestone names the plan calls out as
#: the things a human signs off on; everything else is scaffolding → auto.
#: Matched case-insensitively against both the decl name and source_ref.
DEFAULT_MILESTONE_KEYWORDS = ("stokes", "theorem", "main", "milestone")

#: Per-project firewall/source-mode field name + values (plan firewall
#: ruling). Stored in .marathon/review/config.toml as ``source_mode``;
#: ABSENT defaults to the SAFE 'copyrighted' interpretation.
SOURCE_MODES = ("copyrighted", "open")
DEFAULT_SOURCE_MODE = "copyrighted"


def resolve_gate_policy(
    *,
    gate_mode: str,
    decl: Optional[str] = None,
    source_ref: Optional[str] = None,
    milestone_keywords: tuple[str, ...] = DEFAULT_MILESTONE_KEYWORDS,
) -> str:
    """Resolve one target's stored ``gate_policy`` ('auto' | 'human') from
    the CLI ``gate_mode`` ('auto' | 'human' | 'mixed').

    'auto'/'human' are uniform. 'mixed' applies the milestone selection
    rule: 'human' iff any milestone keyword is a case-insensitive
    substring of the decl name OR source_ref, else 'auto'. Documented and
    overridable (pass a different ``milestone_keywords``)."""
    if gate_mode == "auto":
        return "auto"
    if gate_mode == "human":
        return "human"
    if gate_mode != "mixed":
        raise ValueError(
            f"gate_mode must be one of {GATE_MODES}; got {gate_mode!r}"
        )
    haystack = " ".join(filter(None, (decl, source_ref))).lower()
    for kw in milestone_keywords:
        if kw.lower() in haystack:
            return "human"
    return "auto"


def source_mode(repo_dir: Path) -> str:
    """The per-project firewall source mode (plan firewall ruling).

    Reads ``source_mode`` from ``<repo>/.marathon/review/config.toml``.
    ABSENT / unreadable / unknown value → the SAFE default 'copyrighted'
    (so a project that never sets it cannot accidentally run the
    Claude-reads-the-book extraction path). Pure read; never raises."""
    cfg_path = Path(repo_dir) / ".marathon" / "review" / "config.toml"
    if not cfg_path.is_file():
        return DEFAULT_SOURCE_MODE
    try:
        import tomllib

        data = tomllib.loads(cfg_path.read_text())
    except (OSError, ValueError):
        return DEFAULT_SOURCE_MODE
    value = data.get("source_mode")
    return value if value in SOURCE_MODES else DEFAULT_SOURCE_MODE


# ---------------------------------------------------------------------------
# Sorry scanning (generalizes marathon.fill._find_sorries_in_file)
# ---------------------------------------------------------------------------

# The sorry token. Same word-boundary pattern marathon.fill uses; the
# DECL regex itself is imported (not re-spelled) so the scanners can never
# drift on what counts as a declaration.
_SORRY_RE = re.compile(r"\bsorry\b")


@dataclass(frozen=True)
class SorryHit:
    """One sorry-bodied declaration found by :func:`find_sorries_in_file`.

    The richer cousin of ``fill._find_sorries_in_file``'s bare name list:
    it additionally pins the 1-based ``decl_line`` (where the declaration
    opened) so a target's ``source_ref`` can be ``file:line``."""

    decl: str
    decl_line: int


def find_sorries_in_file(file_path: Path) -> list[SorryHit]:
    """Sorry-bodied declarations in one ``.lean`` file, with decl line
    numbers.

    Same approximation as :func:`marathon.fill._find_sorries_in_file`
    (track the most recent decl-keyword line; flag it when a line in the
    same declaration contains ``sorry`` before the next decl line; skip
    ``--`` line comments), reusing the SAME shared ``_DECL_RE`` /
    ``_SORRY_RE`` so the two scanners never diverge on what counts as a
    declaration or a sorry token.

    ONE deliberate generalization beyond ``fill``'s file-scoped helper:
    fill ``continue``\\ s immediately after a decl-keyword line, so it
    misses a single-line body like ``theorem t : P := by sorry`` (fill is
    only ever pointed at files it already knows hold sorries, so this gap
    is harmless there). The PLANNER must enumerate every sorry, so here we
    also test the decl line's own tail (after the matched name) for a
    sorry — single-line sorry bodies are ubiquitous. This widens coverage
    without changing fill or forking the regex. Unreadable files → empty
    (honest absence, never a crash)."""
    try:
        lines = file_path.read_text().splitlines()
    except OSError:
        return []
    out: list[SorryHit] = []
    seen: set[str] = set()
    current_decl: Optional[str] = None
    current_line = 0

    def _flag(decl: str, decl_line: int) -> None:
        if decl not in seen:
            seen.add(decl)
            out.append(SorryHit(decl=decl, decl_line=decl_line))

    for lineno, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        m = _DECL_RE.match(line)
        if m:
            current_decl = m.group("name")
            current_line = lineno
            # Single-line body: a sorry in the decl line's own tail (past
            # the matched declaration name) counts immediately.
            if _SORRY_RE.search(line[m.end():]):
                _flag(current_decl, current_line)
            continue
        if current_decl is not None and _SORRY_RE.search(line):
            _flag(current_decl, current_line)
    return out


def _lean_files_under(folder: Path) -> list[Path]:
    """Sorted ``.lean`` files under ``folder`` (recursive), skipping
    dot-directories (``.lake``/``.marathon`` scratch — never source).
    Same dot-part skip the audit engine's ``derive_modules`` uses."""
    if folder.is_file() and folder.suffix == ".lean":
        return [folder]
    if not folder.is_dir():
        return []
    out: list[Path] = []
    for f in sorted(folder.rglob("*.lean")):
        if any(part.startswith(".") for part in f.parts):
            continue
        out.append(f)
    return out


def _gitignore_filtered_lean_files(repo_dir: Path) -> list[Path]:
    """Tracked + untracked-not-gitignored ``.lean`` files under
    ``repo_dir``, as absolute paths.

    Reuses the SAME ``git ls-files --cached --others --exclude-standard``
    filter the skeleton bundler uses, so the planner sees exactly the
    files Aristotle would (a gitignored build artifact is never a target).
    Falls back to a plain recursive walk when ``repo_dir`` is not a git
    repo (so the function is usable on a bare folder fixture in tests)."""
    repo_dir = Path(repo_dir)
    if not (repo_dir / ".git").exists():
        return _lean_files_under(repo_dir)
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            cwd=str(repo_dir),
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return _lean_files_under(repo_dir)
    out: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8")
        if not rel.endswith(".lean"):
            continue
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        out.append(repo_dir / rel)
    return sorted(out)


# ---------------------------------------------------------------------------
# The Plan
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    """A built (but not yet committed) set of targets + dependency edges.

    The planner produces a ``Plan`` in memory (so ``--dry-run`` can print
    it without touching the ledger); :meth:`commit` upserts the targets
    and replaces their dep edges in one pass. ``edges`` are
    ``(target name, depends-on target name)`` pairs over the plan's own
    targets (a dep on a non-target is dropped — the planner never invents
    a target just to hang an edge)."""

    targets: list[Target] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_targets(
        cls,
        targets: list[Target],
        edges: Optional[list[tuple[str, str]]] = None,
    ) -> "Plan":
        """Build a ``Plan`` from pre-built targets + edges.

        The clean extension point for the OTHER agent's textbook path:
        once extraction yields its ``statement``-kind targets it wraps
        them here and reuses :meth:`commit` (and the CLI summary). This
        module never builds 'statement' targets itself — only the
        firewall-safe sorry/axiom kinds."""
        return cls(targets=list(targets), edges=list(edges or []))

    def gate_breakdown(self) -> dict[str, int]:
        """``{gate_policy: count}`` over the plan's targets."""
        out: dict[str, int] = {p: 0 for p in ("auto", "human")}
        for t in self.targets:
            out[t.gate_policy] = out.get(t.gate_policy, 0) + 1
        return out

    def commit(self, ledger: Ledger) -> dict[str, int]:
        """Write the plan into the ledger: upsert every target (idempotent
        by unique name), then replace each target's outgoing dep edges.

        Returns ``{"targets": n, "edges": m}`` actually written. Targets
        are upserted first so every edge endpoint has an id; edges that
        reference a name not in the plan are dropped (never auto-create a
        phantom target). Dep replacement is wholesale per target — a
        re-plan re-derives the same edges idempotently."""
        name_to_id: dict[str, int] = {}
        for target in self.targets:
            name_to_id[target.name] = ledger.upsert_target(target)
        deps_by_target: dict[str, list[int]] = {t.name: [] for t in self.targets}
        edges_written = 0
        for src, dst in self.edges:
            if src in name_to_id and dst in name_to_id and src != dst:
                deps_by_target.setdefault(src, []).append(name_to_id[dst])
        for name, dep_ids in deps_by_target.items():
            edges_written += ledger.replace_target_deps(
                name_to_id[name], dep_ids
            )
        return {"targets": len(self.targets), "edges": edges_written}


# ---------------------------------------------------------------------------
# Intake mode: sorries (folder / repo)
# ---------------------------------------------------------------------------


def _target_name_for_sorry(lean_decl: str, lean_file_rel: str) -> str:
    """The unique ``targets.name`` for a sorry target.

    Qualified by the module path so two files with a like-named local decl
    (``aux``) don't collide on the unique-name constraint. Shape:
    ``sorry:Module.Path:DeclName``."""
    from marathon.formalization import module_from_file_path

    module = module_from_file_path(lean_file_rel) or lean_file_rel
    return f"sorry:{module}:{lean_decl}"


def plan_from_sorries(
    repo_dir: Path,
    target_folder: Optional[Path] = None,
    *,
    gate_mode: str = "human",
    milestone_keywords: tuple[str, ...] = DEFAULT_MILESTONE_KEYWORDS,
    derive_deps: bool = True,
) -> Plan:
    """Build one ``sorry`` target per sorry-bodied declaration under
    ``target_folder`` (default: the whole gitignore-filtered repo).

    Each target: ``kind='sorry'``, ``source_ref='<file:line>'``,
    ``lean_file`` repo-relative, ``lean_decl`` the declaration name,
    ``gate_policy`` resolved from ``gate_mode``. Dependency edges (when
    ``derive_deps`` and an audit snapshot exists): a sorry depends on the
    sorry-targets for the project-local defs in its statement cone.

    Reuses the proven ``fill``-shared scanner; pure and offline."""
    repo_dir = Path(repo_dir).resolve()
    if target_folder is None:
        files = _gitignore_filtered_lean_files(repo_dir)
    else:
        folder = Path(target_folder)
        if not folder.is_absolute():
            folder = repo_dir / folder
        files = _lean_files_under(folder)

    targets: list[Target] = []
    # decl name -> target name, for cone-based dep derivation. A given
    # local decl name may appear in two files; the cone only carries the
    # name, so we map name -> the (first) sorry target that defines it.
    decl_to_target: dict[str, str] = {}
    for file_path in files:
        try:
            rel = file_path.relative_to(repo_dir).as_posix()
        except ValueError:
            rel = file_path.as_posix()
        for hit in find_sorries_in_file(file_path):
            name = _target_name_for_sorry(hit.decl, rel)
            targets.append(
                Target(
                    name=name,
                    kind="sorry",
                    source_ref=f"{rel}:{hit.decl_line}",
                    lean_file=rel,
                    lean_decl=hit.decl,
                    gate_policy=resolve_gate_policy(
                        gate_mode=gate_mode,
                        decl=hit.decl,
                        source_ref=rel,
                        milestone_keywords=milestone_keywords,
                    ),
                )
            )
            decl_to_target.setdefault(hit.decl, name)

    edges: list[tuple[str, str]] = []
    if derive_deps:
        edges = _derive_sorry_dep_edges(repo_dir, targets, decl_to_target)
    return Plan(targets=targets, edges=edges)


def plan_from_repo(
    repo_dir: Path,
    *,
    gate_mode: str = "human",
    milestone_keywords: tuple[str, ...] = DEFAULT_MILESTONE_KEYWORDS,
    derive_deps: bool = True,
) -> Plan:
    """Every sorry across the gitignore-filtered repo (``plan_from_sorries``
    with no folder restriction)."""
    return plan_from_sorries(
        repo_dir,
        None,
        gate_mode=gate_mode,
        milestone_keywords=milestone_keywords,
        derive_deps=derive_deps,
    )


def _bare_name(qualified: str) -> str:
    """The last dotted component of a (possibly namespaced) Lean name.
    ``Proj.M.foo`` -> ``foo``; ``foo`` -> ``foo``."""
    return qualified.rsplit(".", 1)[-1]


def _resolve_target_decl(
    target: Target,
    snapshot,
) -> Optional[str]:
    """Map one sorry target's BARE scanner name to the FULLY-QUALIFIED
    decl name the audit snapshot uses (lean_template emits qualified
    names; the source-line scanner can only see the bare name).

    Match rule: a snapshot decl resolves the target when its bare name
    (last dotted component) equals ``target.lean_decl`` AND — when both
    modules are known — its module equals the target's module (derived
    from ``lean_file``). The module guard disambiguates two files that
    share a like-named local decl. An exact-name hit (already qualified,
    or a bare-named test snapshot) short-circuits. Returns None when no
    snapshot decl matches (honest absence — no edge is then drawn)."""
    decl = target.lean_decl
    if decl is None:
        return None
    by_name = snapshot.by_name()
    # Already-qualified / bare-named-snapshot exact hit: trust it as-is.
    if decl in by_name:
        return decl
    from marathon.formalization import module_from_file_path

    want_module = (
        module_from_file_path(target.lean_file) if target.lean_file else None
    )
    candidates = [
        name for name in by_name if _bare_name(name) == decl
    ]
    if not candidates:
        return None
    if want_module is not None:
        module_hits = [
            name for name in candidates
            if by_name[name].module == want_module
        ]
        if module_hits:
            candidates = module_hits
    # Deterministic pick (avoids order-dependent edges when the module
    # guard cannot disambiguate); a genuine ambiguity is rare and the
    # stable choice keeps re-plans idempotent.
    return sorted(candidates)[0]


def _derive_sorry_dep_edges(
    repo_dir: Path,
    targets: list[Target],
    decl_to_target: dict[str, str],
) -> list[tuple[str, str]]:
    """Cone-derived dep edges among sorry targets (plan §2: reuse
    ``compute_kernel``).

    For each sorry target whose decl resolves into the latest audit
    snapshot, the kernel members (project-local defs in its statement
    cone) that ALSO have a sorry target become its dependencies. No
    snapshot → no edges (documented degradation). A cone def that is
    itself sorry-free, or absent from the snapshot, contributes no edge —
    the planner never invents a target to hang an edge on.

    Name resolution (binding correctness fix): the source-line scanner
    yields BARE decl names while a real audit snapshot stores FULLY-
    QUALIFIED names, so a target is resolved to its qualified snapshot
    name via :func:`_resolve_target_decl` (module-disambiguated). Cone
    members come back qualified and are mapped to targets through the
    same resolution, so edges derive on namespaced repos — not only on
    bare-named test fixtures."""
    # Local imports: the audit package is heavier and this path is the
    # only consumer, so importing it here keeps `marathon plan sorries`
    # cheap when no snapshot exists.
    from marathon.audit.engine import load_snapshot
    from marathon.audit.kernel import compute_kernel

    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        return []

    # Resolve every target to its qualified snapshot name once, and build
    # the reverse map (qualified snapshot name -> target name) the cone
    # members are looked up through. Bare-name fallback (``decl_to_target``)
    # still covers a member whose own target the snapshot didn't resolve.
    resolved: dict[str, str] = {}  # target.name -> qualified snapshot name
    qualified_to_target: dict[str, str] = {}  # qualified name -> target.name
    for target in targets:
        q = _resolve_target_decl(target, snapshot)
        if q is None:
            continue
        resolved[target.name] = q
        # First writer wins on a genuine qualified collision (mirrors the
        # decl_to_target setdefault discipline).
        qualified_to_target.setdefault(q, target.name)

    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        qname = resolved.get(target.name)
        if qname is None:
            continue
        kernel = compute_kernel(qname, snapshot)
        for member in kernel.members:
            dep_target = qualified_to_target.get(member.name)
            if dep_target is None:
                # Member's own target wasn't snapshot-resolved; fall back
                # to the bare-name map so a partially-audited cone still
                # yields edges where it unambiguously can.
                dep_target = decl_to_target.get(_bare_name(member.name))
            if dep_target is None or dep_target == target.name:
                continue
            edge = (target.name, dep_target)
            if edge not in seen:
                seen.add(edge)
                edges.append(edge)
    return edges


# ---------------------------------------------------------------------------
# Intake mode: axiom
# ---------------------------------------------------------------------------


def plan_from_axiom(
    repo_dir: Path,
    axiom_name: str,
    *,
    gate_mode: str = "human",
    milestone_keywords: tuple[str, ...] = DEFAULT_MILESTONE_KEYWORDS,
) -> Plan:
    """A single target for a named axiom/decl to discharge.

    ``kind='axiom'``, ``source_ref`` = the axiom name itself,
    ``lean_decl`` = the name. ``lean_file`` is left NULL unless the latest
    audit snapshot knows the decl's module (best-effort enrichment; absent
    snapshot → NULL, honest). No dependency edges (a single target has
    nothing in the plan to depend on)."""
    repo_dir = Path(repo_dir).resolve()
    lean_file: Optional[str] = None
    # Best-effort: pin the module from a snapshot if one exists. Never a
    # hard requirement — the axiom name is the load-bearing field.
    try:
        from marathon.audit.engine import load_snapshot

        snapshot = load_snapshot(repo_dir)
    except Exception:  # pragma: no cover — defensive; load_snapshot is total
        snapshot = None
    if snapshot is not None:
        decl = snapshot.by_name().get(axiom_name)
        if decl is not None and decl.module:
            lean_file = decl.module.replace(".", "/") + ".lean"

    target = Target(
        name=f"axiom:{axiom_name}",
        kind="axiom",
        source_ref=axiom_name,
        lean_file=lean_file,
        lean_decl=axiom_name,
        gate_policy=resolve_gate_policy(
            gate_mode=gate_mode,
            decl=axiom_name,
            source_ref=axiom_name,
            milestone_keywords=milestone_keywords,
        ),
    )
    return Plan(targets=[target], edges=[])

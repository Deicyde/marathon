"""Maintain ``formalization.yaml`` (the mathlib-initiative schema v0.2)
at the project root, auto-updating the machine-derivable fields after
each AI turn while preserving every hand-edited field.

The schema lives at
https://github.com/mathlib-initiative/formalization.yaml/blob/main/formalization.yaml.
Auto-fields are a strict subset of the schema; everything not in
``_AUTO_PATHS`` is treated as human-curated and never overwritten.

Hook point: ``post_pipeline.run_post_pipeline`` calls
``update_formalization`` immediately before the auto-commit so the
yaml change is bundled into the same commit as the iteration's
``.lean`` edits. If auto-commit is off the file is still updated
in-place; the user picks up the diff on their next commit.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FORMALIZATION_FILENAME = "formalization.yaml"
SCHEMA_VERSION = "v0.2"

# --- Schema template -------------------------------------------------------
#
# Used when ``formalization.yaml`` doesn't exist yet. Mirrors v0.2 from
# the mathlib-initiative repo. All fields are present (some empty) so a
# user editing the file sees the full surface.

_TEMPLATE: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "project": {
        "name": "",
        "authors": [],
        "license": "",
    },
    "sources": [],
    "automation": {
        "method": "",
        "models": [],
        "framework": "",
        "cost": {
            "wall_time": "",
            "spend_usd": "",
            "hardware": "",
        },
        "notes": "",
    },
    "status": {
        "scope": "",
        "sorry_count": 0,
        "sorry_in_definitions": 0,
        "axioms": [],
        "main_results": [],
    },
    "fidelity": {
        "divergences": "",
    },
    "review": {
        "status": "",
        "reviewers": [],
        "notes": "",
    },
    "alignment": {},
}

# Paths into the schema that the auto-updater is allowed to write. Every
# other key is treated as human-curated and preserved verbatim. Each path
# is a tuple of keys (dict descent); list items are not addressed by this
# mechanism on purpose (lists are either fully auto or fully manual).
_AUTO_PATHS: tuple[tuple[str, ...], ...] = (
    ("version",),
    ("automation", "models"),
    ("automation", "framework"),
    ("automation", "cost", "wall_time"),
    ("status", "sorry_count"),
    ("status", "sorry_in_definitions"),
)

# Sidecar file holding the cumulative iteration seconds the
# ``automation.cost.wall_time`` field is derived from. Kept separate
# from the yaml so the yaml field can stay in human-readable format
# (e.g. ``"3h 24m"``) while the underlying source-of-truth is the
# unambiguous numeric total here.
_WALL_TIME_SIDECAR = Path(".marathon") / "wall-time.json"


# --- yaml IO ---------------------------------------------------------------


def _try_import_yaml():
    """PyYAML is optional in marathon's pinned deps; import lazily so
    callers get a clear error rather than an ImportError on tool start."""
    try:
        import yaml  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "formalization.yaml support requires PyYAML; "
            "install via `pip install PyYAML`."
        ) from e
    return yaml


def read_formalization(path: Path) -> dict[str, Any]:
    """Load ``formalization.yaml`` or return a fresh template."""
    if not path.is_file():
        return _deep_copy(_TEMPLATE)
    yaml = _try_import_yaml()
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        logger.warning(
            "formalization.yaml at %s is malformed (%s); starting from template",
            path, e,
        )
        return _deep_copy(_TEMPLATE)
    # Merge into the template so missing keys land at their defaults
    # without clobbering existing data.
    out = _deep_copy(_TEMPLATE)
    _deep_merge(out, loaded)
    return out


def write_formalization(path: Path, data: dict[str, Any]) -> None:
    """Write the dict to disk with deterministic key order (matches the
    template), trailing newline, no tags. The deterministic ordering
    minimises diff noise across runs.

    Multi-line strings are emitted as literal block scalars (``|-``)
    rather than the default expanded-plain style, which preserves
    paragraph structure for the human-curated ``notes`` / ``scope`` /
    ``divergences`` fields.
    """
    yaml = _try_import_yaml()
    ordered = _reorder_to_template(data)

    def _str_representer(dumper, value: str):
        # Multi-line → literal block; single-line → default plain.
        if "\n" in value:
            return dumper.represent_scalar(
                "tag:yaml.org,2002:str", value, style="|"
            )
        return dumper.represent_scalar("tag:yaml.org,2002:str", value)

    dumper = yaml.SafeDumper
    dumper.add_representer(str, _str_representer)
    text = yaml.dump(
        ordered,
        Dumper=dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10000,
    )
    path.write_text(text)


# --- auto-field computation -----------------------------------------------


def count_sorries(repo_dir: Path) -> tuple[int, int]:
    """Count ``sorry`` occurrences across all ``.lean`` files under
    ``repo_dir`` (gitignore-filtered).

    Returns ``(total_sorries, sorries_in_definitions)``. The second
    is an approximation: we count sorries whose nearest preceding
    declaration keyword is ``def``/``abbrev``/``instance`` (the
    declaration-form decls), as opposed to ``theorem``/``lemma`` (the
    proof-form decls).

    The approximation can miss sorries inside ``where`` clauses or
    ``let rec`` blocks; for a precise count, run ``#print axioms``
    on a built repo. The schema's ``status.sorry_in_definitions``
    field is "the number of sorries used for definitions", which the
    approximation tracks well enough to be useful between proper
    audits.
    """
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=str(repo_dir),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return 0, 0
    total = 0
    in_defs = 0
    decl_re = re.compile(
        r"^\s*(?:@\[[^\]]*\]\s*)*(?:noncomputable\s+)?"
        r"(?:private\s+)?(?:protected\s+)?"
        r"(def|theorem|lemma|abbrev|instance|structure|class|inductive|opaque|axiom)\b"
    )
    sorry_re = re.compile(r"\bsorry\b")
    # Token kinds that count as "definitions" rather than proof-form.
    def_kinds = {"def", "abbrev", "instance", "structure", "class", "inductive"}
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8")
        if not rel.endswith(".lean"):
            continue
        full = repo_dir / rel
        if not full.is_file():
            continue
        try:
            lines = full.read_text().splitlines()
        except OSError:
            continue
        # Walk the file; the current declaration's kind is the kind of
        # the most recent declaration-keyword line we've seen.
        current_kind: str | None = None
        for line in lines:
            # Skip line comments; we don't try to detect block comments,
            # which would require a tokenizer.
            stripped = line.lstrip()
            if stripped.startswith("--"):
                continue
            m = decl_re.match(line)
            if m:
                current_kind = m.group(1)
            # Count sorry occurrences on this line (ignoring strings is
            # out of scope — false positives are rare and benign).
            n = len(sorry_re.findall(line))
            if n:
                total += n
                if current_kind in def_kinds:
                    in_defs += n
    return total, in_defs


def compute_auto_fields(
    repo_dir: Path,
    models: list[str] | None = None,
    framework: str | None = None,
) -> dict[str, Any]:
    """Compute the auto-field subset that ``update_formalization`` will
    overlay onto the read-in dict."""
    total, in_defs = count_sorries(repo_dir)
    out: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "status": {
            "sorry_count": total,
            "sorry_in_definitions": in_defs,
        },
        "automation": {},
    }
    if models is not None:
        out["automation"]["models"] = list(models)
    if framework is not None:
        out["automation"]["framework"] = framework
    # Pull cumulative wall_time from the sidecar (if present) and
    # format as human-readable. Missing sidecar / zero seconds means
    # the field doesn't get overlaid — the on-disk value is preserved.
    cumulative_seconds = read_cumulative_wall_seconds(repo_dir)
    if cumulative_seconds > 0:
        out["automation"]["cost"] = {
            "wall_time": format_wall_time(cumulative_seconds),
        }
    return out


# --- wall_time accumulation ------------------------------------------


def read_cumulative_wall_seconds(repo_dir: Path) -> int:
    """Read total seconds from the sidecar at
    ``<repo_dir>/.marathon/wall-time.json``. Returns 0 if absent."""
    import json
    path = repo_dir / _WALL_TIME_SIDECAR
    if not path.is_file():
        return 0
    try:
        return int(json.loads(path.read_text()).get("total_seconds", 0))
    except (OSError, ValueError):
        return 0


def add_wall_seconds(repo_dir: Path, seconds: float) -> int:
    """Add ``seconds`` to the cumulative wall-time sidecar. Returns the
    new cumulative total (in seconds). Creates the sidecar if absent.

    Called by ``post_pipeline.run_post_pipeline`` after each
    iteration's build to accumulate iteration durations. Iteration
    durations include build + Aristotle compute (when both are
    measured); only build duration is tracked here today since that's
    what the post-pipeline has in hand. Aristotle duration could be
    added later by also reading ``project.duration_seconds`` from
    refine-state.json.
    """
    import json
    if seconds <= 0:
        return read_cumulative_wall_seconds(repo_dir)
    path = repo_dir / _WALL_TIME_SIDECAR
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_cumulative_wall_seconds(repo_dir)
    new_total = current + int(seconds)
    path.write_text(json.dumps({
        "total_seconds": new_total,
        "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2))
    return new_total


def format_wall_time(seconds: int) -> str:
    """Format a duration in seconds as a human-readable string for the
    formalization.yaml ``automation.cost.wall_time`` field.

    Output shapes (largest applicable unit, two-component max):
    * ``"42s"`` (< 1 min)
    * ``"5m 23s"`` (< 1 hr)
    * ``"3h 24m"`` (< 1 day)
    * ``"5d 12h"`` (≥ 1 day)
    """
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days, hrs = divmod(hours, 24)
    return f"{days}d {hrs}h"


# --- axiom checking ------------------------------------------------------
#
# ``status.main_results[].axioms`` is the per-declaration axiom set
# Mathlib's ``#print axioms`` reports. Auto-checking requires the
# project to be built (`.olean` files present); the checker batches
# every main-result decl into one Lean invocation so multi-result
# projects only pay one process spawn per iteration.


def _module_from_file_path(file_path: str) -> str | None:
    """Convert a relative source path (``GeometricAnalysis/LeeSM/Chapter16/
    StokesTheorem.lean``) to a Lean module path
    (``GeometricAnalysis.LeeSM.Chapter16.StokesTheorem``).

    Returns ``None`` for inputs that don't look like a Lean source path
    (no ``.lean`` suffix, absolute paths, empty)."""
    if not file_path or not file_path.endswith(".lean"):
        return None
    if file_path.startswith("/"):
        # Strip a leading absolute prefix the user accidentally pasted —
        # we can't recover the module path from an absolute path without
        # knowing the project root, so refuse.
        return None
    stem = file_path[: -len(".lean")]
    return stem.replace("/", ".")


# `#print axioms` output shapes:
#   <decl> depends on axioms: [a, b, c]
#   <decl> does not depend on any axioms
# The "depends on axioms:" form is multi-line — axioms can wrap. The
# "does not depend" form is one line.
_AXIOMS_LIST_RE = re.compile(
    r"'(?P<decl>[^']+)' depends on axioms:\s*\[(?P<axioms>.*?)\]",
    re.DOTALL,
)
_AXIOMS_NONE_RE = re.compile(
    r"'(?P<decl>[^']+)' does not depend on any axioms"
)


def check_axioms(
    repo_dir: Path,
    decl_to_module: list[tuple[str, str]],
    *,
    timeout: int = 120,
) -> dict[str, list[str] | None]:
    """Run ``#print axioms`` on a batch of declarations in one Lean
    invocation. Returns ``{decl_name: axioms | None}``; ``None`` means
    the declaration's axioms couldn't be determined (build missing,
    decl not in scope, Lean error). Decls that genuinely depend on no
    axioms return an empty list.

    Args:
        repo_dir: Project root (where ``lake env lean`` runs).
        decl_to_module: List of ``(decl_name, module_path)`` pairs.
            ``module_path`` is the dotted Lean module the decl lives
            in (use ``_module_from_file_path`` to convert source paths).
        timeout: Seconds to wait for ``lake env lean`` before
            aborting (returns ``None`` for every decl on timeout).
    """
    if not decl_to_module:
        return {}
    # Dedup module imports, preserve order so the test file is stable.
    modules_seen: set[str] = set()
    modules_ordered: list[str] = []
    for _, mod in decl_to_module:
        if mod not in modules_seen:
            modules_seen.add(mod)
            modules_ordered.append(mod)
    # Build the temp Lean file: imports + print-axioms commands.
    lines = [f"import {m}" for m in modules_ordered]
    lines.append("")
    for decl, _ in decl_to_module:
        lines.append(f"#print axioms {decl}")
    tmp_path = repo_dir / ".marathon-axioms-check.lean"
    out: dict[str, list[str] | None] = {decl: None for decl, _ in decl_to_module}
    try:
        tmp_path.write_text("\n".join(lines) + "\n")
        try:
            proc = subprocess.run(
                ["lake", "env", "lean", str(tmp_path.name)],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "axiom check timed out after %ds for %d decls",
                timeout, len(decl_to_module),
            )
            return out
        # Lean writes "<decl> depends on axioms: [..]" or "does not
        # depend" lines to stdout. Errors go to stderr; if stderr is
        # noisy but the decl printed, we still take the printed value.
        text = proc.stdout
        for m in _AXIOMS_LIST_RE.finditer(text):
            decl = m.group("decl")
            ax_blob = m.group("axioms")
            # Axioms are comma-separated bare identifiers (or dotted).
            axs = [a.strip() for a in ax_blob.split(",") if a.strip()]
            out[decl] = axs
        for m in _AXIOMS_NONE_RE.finditer(text):
            out[m.group("decl")] = []
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return out


def update_main_results_axioms(
    data: dict[str, Any], repo_dir: Path
) -> int:
    """Walk ``data['status']['main_results']`` and replace each entry's
    ``axioms`` list with the verified set from ``#print axioms``.
    Entries whose axioms can't be determined (build missing, decl
    not in scope) are left unchanged.

    Returns the number of entries whose ``axioms`` field was updated.
    """
    main_results = (data.get("status") or {}).get("main_results") or []
    decl_to_module: list[tuple[str, str]] = []
    for entry in main_results:
        decl = entry.get("declaration") if isinstance(entry, dict) else None
        file_path = entry.get("file") if isinstance(entry, dict) else None
        if not decl or not file_path:
            continue
        module = _module_from_file_path(file_path)
        if module is None:
            continue
        decl_to_module.append((decl, module))
    if not decl_to_module:
        return 0
    axioms_by_decl = check_axioms(repo_dir, decl_to_module)
    updated = 0
    for entry in main_results:
        if not isinstance(entry, dict):
            continue
        decl = entry.get("declaration")
        result = axioms_by_decl.get(decl)
        if result is None:
            continue
        entry["axioms"] = result
        updated += 1
    return updated


# --- orchestrator --------------------------------------------------------


def update_formalization(
    repo_dir: Path,
    models: list[str] | None = None,
    framework: str | None = None,
    yaml_path: Path | None = None,
    create_if_missing: bool = False,
    check_axioms_on_build: bool = False,
) -> Path | None:
    """Read, update the auto-fields, write back. Returns the path
    written, or ``None`` if the file was absent and
    ``create_if_missing`` was False.

    Args:
        repo_dir: Project root. ``formalization.yaml`` lives here by
            default.
        models: Model identifiers used this turn (e.g.
            ``["claude-opus-4-7"]`` or ``["Aristotle-v2"]``).
        framework: Pipeline name (e.g. ``"Marathon"``,
            ``"Marathon + Aristotle"``). Set if not already present
            in the file.
        yaml_path: Override file location. Defaults to
            ``repo_dir / formalization.yaml``.
        create_if_missing: If True, create the file from the v0.2
            template when it doesn't exist. Default False — opt-in
            per project, so marathon doesn't silently create the
            file in repos that haven't asked for it. Use
            ``marathon formalization init`` to initialize a fresh
            file in a new project.
        check_axioms_on_build: If True, run ``#print axioms`` on
            every declaration in ``status.main_results`` and replace
            their ``axioms`` lists with the verified set. Costs one
            ``lake env lean`` invocation per call (batched across
            all main results). Only meaningful when the build is
            current — the caller is responsible for gating this on a
            successful ``lake build``. Default False — opt-in via
            ``--check-axioms`` on the CLI or ``build.ok`` in
            ``post_pipeline``.
    """
    yaml_path = yaml_path or (repo_dir / FORMALIZATION_FILENAME)
    if not yaml_path.is_file() and not create_if_missing:
        return None
    current = read_formalization(yaml_path)
    auto = compute_auto_fields(repo_dir, models=models, framework=framework)
    merged = _overlay_auto_fields(current, auto)
    if check_axioms_on_build:
        try:
            updated = update_main_results_axioms(merged, repo_dir)
            if updated:
                logger.info(
                    "formalization: refreshed axioms for %d main_result(s)",
                    updated,
                )
        except Exception:  # noqa: BLE001 — soft-warning
            logger.exception(
                "formalization: axiom check failed; existing axioms preserved"
            )
    write_formalization(yaml_path, merged)
    return yaml_path


# --- private helpers -----------------------------------------------------


def _deep_copy(d: Any) -> Any:
    """Shallow-recursive deepcopy via dict/list comprehension. Safe for
    yaml-shaped data (no cycles, only dicts/lists/scalars)."""
    if isinstance(d, dict):
        return {k: _deep_copy(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_deep_copy(v) for v in d]
    return d


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """In-place: copy values from ``src`` into ``dst``, recursing into
    dicts; lists and scalars are overwritten outright."""
    for k, v in src.items():
        if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = _deep_copy(v)


def _overlay_auto_fields(
    base: dict[str, Any], auto: dict[str, Any]
) -> dict[str, Any]:
    """Take ``base`` (the on-disk file's contents merged with the
    template) and overlay only the auto-paths from ``auto``. Everything
    else in ``base`` is preserved."""
    out = _deep_copy(base)
    for path in _AUTO_PATHS:
        # Walk into auto along the path; if the leaf is missing or
        # explicitly None, leave the base value alone.
        node: Any = auto
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if node is None:
            continue
        # Walk into out along the path, creating dicts as needed.
        cursor = out
        for key in path[:-1]:
            cursor = cursor.setdefault(key, {})
            if not isinstance(cursor, dict):
                # Pre-existing scalar where the path expects a dict —
                # skip this auto-path rather than blow up the file.
                logger.warning(
                    "formalization.yaml: path %s blocked by non-dict; "
                    "skipping auto-update", ".".join(path)
                )
                cursor = None
                break
        if cursor is None:
            continue
        cursor[path[-1]] = node
    # Always stamp the last-updated marker in automation.notes as a
    # trailing line, preserving any human-authored notes above it.
    _stamp_last_updated(out)
    return out


_LAST_UPDATED_PREFIX = "_auto: last updated by marathon at "


def _stamp_last_updated(data: dict[str, Any]) -> None:
    """Append/replace a single ``_auto: last updated …`` line in
    ``automation.notes``. Lets the user see when the file was last
    touched without bloating the on-disk schema."""
    autom = data.setdefault("automation", {})
    existing = autom.get("notes", "") or ""
    # Strip any prior auto-line we wrote.
    cleaned = "\n".join(
        line for line in existing.splitlines()
        if not line.startswith(_LAST_UPDATED_PREFIX)
    ).rstrip()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_line = f"{_LAST_UPDATED_PREFIX}{ts}"
    autom["notes"] = (cleaned + ("\n" if cleaned else "") + new_line).strip()


def _reorder_to_template(data: dict[str, Any]) -> dict[str, Any]:
    """Reorder ``data`` keys to match the template's order at each
    level. Unknown keys (user additions) sort to the end of their
    section, preserving their relative order."""
    return _reorder_section(data, _TEMPLATE)


def _reorder_section(data: Any, template: Any) -> Any:
    if not isinstance(data, dict) or not isinstance(template, dict):
        return data
    out: dict[str, Any] = {}
    for k in template:
        if k in data:
            out[k] = _reorder_section(data[k], template[k])
    for k, v in data.items():
        if k not in out:
            out[k] = v
    return out

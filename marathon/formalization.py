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
    ("status", "sorry_count"),
    ("status", "sorry_in_definitions"),
)


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
    minimises diff noise across runs."""
    yaml = _try_import_yaml()
    ordered = _reorder_to_template(data)
    text = yaml.safe_dump(
        ordered,
        sort_keys=False,  # we apply our own ordering
        default_flow_style=False,
        allow_unicode=True,
        width=10000,  # avoid wrapping long lines
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
    return out


# --- orchestrator --------------------------------------------------------


def update_formalization(
    repo_dir: Path,
    models: list[str] | None = None,
    framework: str | None = None,
    yaml_path: Path | None = None,
    create_if_missing: bool = False,
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
    """
    yaml_path = yaml_path or (repo_dir / FORMALIZATION_FILENAME)
    if not yaml_path.is_file() and not create_if_missing:
        return None
    current = read_formalization(yaml_path)
    auto = compute_auto_fields(repo_dir, models=models, framework=framework)
    merged = _overlay_auto_fields(current, auto)
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

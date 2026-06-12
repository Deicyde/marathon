"""marathon.audit.engine — run the Lean audit script and parse its output.

The usable Python engine over the contract defined in
:mod:`marathon.audit.lean_template`:

* :func:`run_audit` — derive target modules + trusted-package prefixes,
  render the script, run it with ``lake env lean`` *inside the target
  repo's workspace* (so its pinned toolchain and deps elaborate it), and
  parse stdout into an :class:`~marathon.audit.records.AuditSnapshot`.
* :func:`save_snapshot` / :func:`load_snapshot` — persist at
  ``<repo>/.marathon/audit/latest.json`` (previous run rotated to
  ``previous.json``).  The directory self-gitignores (``.gitignore`` with
  ``*``), same convention as the conductor's ``jobs.json``: snapshots are
  derived cache, recomputable from source — never committed, never merged
  (crit-feas §3: machine evidence is derived, only human verdicts are
  durable).
* :func:`diff_snapshots` — pure per-decl change classification
  (added / removed / type-changed / value-changed / axioms-changed /
  status-changed), the future T2/T3 invalidation feed.

Degradation contract (mirrors ``formalization.check_axioms``): lake
missing, nonzero exit, timeout, or unparseable output never raise — the
snapshot records the failure strings and callers see honest absence of
evidence.  Tolerant parsing: non-sentinel stdout lines are ignored;
malformed sentinel lines become failure entries, never crashes; a
missing/inconsistent ``AUDIT_DONE`` trailer is recorded as a truncated
run.

Trusted-prefix derivation (plan §2 ruling 4): the partition list is
derived from the workspace where possible — top-level Lean module roots
of every ``.lake/packages/*`` checkout, plus ``require`` names from
``lakefile.toml`` / ``lakefile.lean`` — always unioned with the
hardcoded :data:`~marathon.audit.lean_template.DEFAULT_TRUSTED_PREFIXES`
fallback (which also covers the toolchain-builtin ``Init``/``Lean``/
``Lake`` roots that never appear under ``.lake/packages``).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from marathon.audit.lean_template import (
    AUDIT_FIELD_COUNT,
    BEGIN_SENTINEL,
    DEFAULT_TRUSTED_PREFIXES,
    DONE_SENTINEL,
    KINDS,
    META_SENTINEL,
    SCHEMA_VERSION,
    SENTINEL,
    _LEAN_NAME_RE,
    render_audit_script,
)
from marathon.audit.records import AuditSnapshot, DeclAudit, STATUSES
from marathon.formalization import module_from_file_path

logger = logging.getLogger(__name__)

#: Derived-cache home inside the consumer repo (self-gitignored).
AUDIT_STATE_RELPATH = Path(".marathon/audit")
LATEST_NAME = "latest.json"
PREVIOUS_NAME = "previous.json"

#: Change classes reported by :func:`diff_snapshots`, in display order.
DIFF_KEYS = (
    "added",
    "removed",
    "type_changed",
    "value_changed",
    "axioms_changed",
    "status_changed",
)

_LINE_PREFIXES = (SENTINEL + "|", META_SENTINEL + "|", DONE_SENTINEL + "|")


# ---------------------------------------------------------------------------
# Workspace derivation
# ---------------------------------------------------------------------------

def _find_lake() -> str | None:
    """``lake`` from PATH, else ``~/.elan/bin/lake``, else None."""
    found = shutil.which("lake")
    if found:
        return found
    cand = Path.home() / ".elan" / "bin" / "lake"
    return str(cand) if cand.exists() else None


def derive_modules(
    repo_dir: Path, target_folder: str | Path
) -> tuple[list[str], list[str]]:
    """Map the target folder's ``.lean`` files to module names.

    Returns ``(sorted module names, failures)``.  Files that cannot be
    mapped to a Lean-spliceable module name (weird characters, hyphens)
    are recorded as failures and skipped; dot-directories (``.lake``,
    ``.marathon``, …) are skipped silently."""
    repo_dir = Path(repo_dir)
    target = Path(target_folder)
    if not target.is_absolute():
        target = repo_dir / target
    failures: list[str] = []
    if target.is_file() and target.suffix == ".lean":
        files = [target]
    elif target.is_dir():
        files = sorted(target.rglob("*.lean"))
    else:
        return [], [f"audit target not found: {target}"]
    modules: set[str] = set()
    for f in files:
        try:
            rel = f.relative_to(repo_dir)
        except ValueError:
            failures.append(f"{f} is outside the repo; skipped")
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue  # .lake/.marathon scratch — never source modules
        mod = module_from_file_path(str(rel))
        if mod is None or not _LEAN_NAME_RE.match(mod):
            failures.append(
                f"cannot map {rel} to a safe Lean module name; skipped"
            )
            continue
        modules.add(mod)
    return sorted(modules), failures


# `require foo`, `require foo from git "..."`, `require "scope" / "foo"`,
# `require «foo»` — lakefile.lean DSL forms.  The scope chunk is optional.
_REQUIRE_RE = re.compile(
    r"^\s*require\s+(?:\"[^\"]*\"\s*/\s*)?"
    r"(?:«([^»]+)»|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_.']*))",
    re.MULTILINE,
)


def _lakefile_require_names(repo_dir: Path) -> list[str]:
    """Dependency names declared by the repo's lakefile(s).  Best-effort:
    unreadable/malformed files contribute nothing."""
    names: list[str] = []
    toml_path = repo_dir / "lakefile.toml"
    if toml_path.is_file():
        try:
            data = tomllib.loads(toml_path.read_text())
            for req in data.get("require") or []:
                if isinstance(req, dict) and isinstance(req.get("name"), str):
                    names.append(req["name"])
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.debug("lakefile.toml unreadable: %s", exc)
    lean_path = repo_dir / "lakefile.lean"
    if lean_path.is_file():
        try:
            text = lean_path.read_text()
        except OSError as exc:
            logger.debug("lakefile.lean unreadable: %s", exc)
        else:
            for m in _REQUIRE_RE.finditer(text):
                name = m.group(1) or m.group(2) or m.group(3)
                if name:
                    names.append(name)
    return names


def _add_prefix(prefixes: set[str], name: str) -> None:
    """Admit *name* as a trusted module-root prefix if it is plausibly one:
    spliceable into Lean source and capitalized (module roots are; this
    also rejects ``lakefile``/``scripts``-style noise)."""
    if name and name[0].isupper() and _LEAN_NAME_RE.match(name):
        prefixes.add(name)


def derive_trusted_prefixes(repo_dir: Path) -> list[str]:
    """Trusted-package module prefixes for the project-local partition.

    Workspace-derived where possible, per the plan ruling:

    * every ``.lake/packages/<pkg>`` checkout contributes its top-level
      module roots (``X.lean`` files and directories containing ``.lean``
      files at the package root — e.g. ``Mathlib.lean`` + ``Mathlib/``);
    * ``require`` names from ``lakefile.toml``/``lakefile.lean``
      contribute the literal name and a first-letter-capitalized variant
      (``mathlib`` → ``Mathlib``; imperfect for camel-case packages like
      ``proofwidgets`` → ``Proofwidgets``, but a never-matching prefix is
      harmless — the packages scan supplies the real ``ProofWidgets``);
    * always unioned with :data:`DEFAULT_TRUSTED_PREFIXES` (covers the
      toolchain builtins and serves as the hardcoded fallback when the
      workspace has no ``.lake`` yet)."""
    repo_dir = Path(repo_dir)
    prefixes: set[str] = set(DEFAULT_TRUSTED_PREFIXES)
    pkgs_dir = repo_dir / ".lake" / "packages"
    if pkgs_dir.is_dir():
        for pkg in sorted(p for p in pkgs_dir.iterdir() if p.is_dir()):
            try:
                entries = list(pkg.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_file() and entry.suffix == ".lean":
                    _add_prefix(prefixes, entry.stem)
                elif entry.is_dir() and any(entry.glob("*.lean")):
                    _add_prefix(prefixes, entry.name)
    for name in _lakefile_require_names(repo_dir):
        _add_prefix(prefixes, name)
        _add_prefix(prefixes, name[:1].upper() + name[1:])
    return sorted(prefixes)


def read_toolchain(repo_dir: Path) -> str | None:
    """Content of ``<repo>/lean-toolchain`` (stripped), or None."""
    try:
        return (Path(repo_dir) / "lean-toolchain").read_text().strip() or None
    except OSError:
        return None


def read_package_revs(repo_dir: Path) -> dict[str, str]:
    """``{package name: git rev}`` from ``<repo>/lake-manifest.json``.
    Best-effort ("when cheaply available"): missing manifest, path
    dependencies, or format drift yield a smaller (possibly empty) dict."""
    path = Path(repo_dir) / "lake-manifest.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    revs: dict[str, str] = {}
    if not isinstance(data, dict):
        return revs
    for pkg in data.get("packages") or []:
        if not isinstance(pkg, dict):
            continue
        name = pkg.get("name")
        rev = pkg.get("rev")
        if rev is None and isinstance(pkg.get("git"), dict):
            rev = pkg["git"].get("rev")  # older manifest versions nest it
        if isinstance(name, str) and isinstance(rev, str):
            revs[name] = rev
    return revs


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

@dataclass
class ParsedOutput:
    """Result of :func:`parse_audit_output`."""

    meta: dict = field(default_factory=dict)
    decls: list[DeclAudit] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    #: Whether any contract line at all was seen (distinguishes "ran but
    #: truncated" from "produced no audit output whatsoever").
    saw_contract: bool = False


def _normalize_line(raw: str) -> str | None:
    """Return the contract line carried by *raw*, or None.

    Handles the documented toolchain quirk where the first ``#eval`` print
    is folded into a ``file:line:col: info:`` diagnostic prefix."""
    line = raw.rstrip()
    if line == BEGIN_SENTINEL or line.startswith(_LINE_PREFIXES):
        return line
    idx = line.find(SENTINEL)
    if idx > 0 and "info:" in line[:idx]:
        rest = line[idx:]
        if rest == BEGIN_SENTINEL or rest.startswith(_LINE_PREFIXES):
            return rest
    return None


def _b64_field(raw: str) -> str | None:
    """Decode a ``-``-or-base64 contract field.  Raises ValueError on
    invalid base64/UTF-8 (caller converts to a failure entry)."""
    if raw == "-":
        return None
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"bad base64 field {raw!r}: {exc}") from exc


def _parse_audit_line(line: str) -> tuple[DeclAudit | None, str | None]:
    """One ``AUDIT|`` line → (DeclAudit, None) or (None, failure string).

    Split from the RIGHT (the contract's documented defense): only the
    name field can contain ``|`` (exotic ``«...»`` atoms); the trailing
    ten fields are pipe-free by construction."""
    chunks = line.rsplit("|", AUDIT_FIELD_COUNT - 2)
    if len(chunks) != AUDIT_FIELD_COUNT - 1 or not chunks[0].startswith(
        SENTINEL + "|"
    ):
        return None, f"malformed audit line (field count): {line!r}"
    name = chunks[0][len(SENTINEL) + 1 :]
    (kind, mod, status, type_b64, value_b64, cone, axioms,
     has_sorry_s, tags, reason_b64) = chunks[1:]
    if not name:
        return None, f"malformed audit line (empty name): {line!r}"
    if kind not in KINDS:
        return None, f"malformed audit line (kind {kind!r}): {line!r}"
    if status not in STATUSES:
        return None, f"malformed audit line (status {status!r}): {line!r}"
    if has_sorry_s not in ("true", "false", "-"):
        return None, f"malformed audit line (has_sorry {has_sorry_s!r}): {line!r}"
    try:
        type_pp = _b64_field(type_b64)
        value_pp = _b64_field(value_b64)
        reason = _b64_field(reason_b64)
    except ValueError as exc:
        return None, f"malformed audit line ({exc}): {line!r}"
    if status == "ok" and type_pp is None:
        return None, f"malformed audit line (ok without type): {line!r}"
    decl = DeclAudit(
        name=name,
        kind=kind,
        module=mod,
        status=status,
        type_pp=type_pp,
        value_pp=value_pp,
        cone=[] if cone == "-" else cone.split(","),
        axioms=[] if axioms == "-" else axioms.split(","),
        has_sorry=None if has_sorry_s == "-" else has_sorry_s == "true",
        tags=[] if tags == "-" else tags.split(";"),
        reason=reason,
    )
    return decl, None


def parse_audit_output(stdout: str) -> ParsedOutput:
    """Parse raw ``lake env lean`` stdout per the lean_template contract.

    Tolerant by design: non-sentinel lines are ignored, malformed
    sentinel lines and contract violations become ``failures`` entries.
    Never raises on untrusted input."""
    out = ParsedOutput(meta={"modules": []})
    seen: set[str] = set()
    audit_line_count = 0
    done_count: int | None = None
    for raw in stdout.splitlines():
        line = _normalize_line(raw)
        if line is None:
            continue
        out.saw_contract = True
        if line == BEGIN_SENTINEL:
            continue
        if line.startswith(META_SENTINEL + "|"):
            parts = line.split("|", 2)
            if len(parts) != 3:
                out.failures.append(f"malformed meta line: {line!r}")
                continue
            _, key, value = parts
            if key == "module":
                out.meta["modules"].append(value)
            elif key == "trusted_prefixes":
                out.meta[key] = [] if value == "-" else value.split(",")
            else:
                out.meta[key] = value
            continue
        if line.startswith(DONE_SENTINEL + "|"):
            try:
                done_count = int(line.split("|", 1)[1])
            except ValueError:
                out.failures.append(f"malformed trailer: {line!r}")
            continue
        # AUDIT| declaration line.
        audit_line_count += 1
        decl, err = _parse_audit_line(line)
        if err is not None:
            out.failures.append(err)
            continue
        assert decl is not None
        if decl.name in seen:
            out.failures.append(
                f"duplicate declaration {decl.name}; keeping first"
            )
            continue
        seen.add(decl.name)
        out.decls.append(decl)
    schema = out.meta.get("schema")
    if schema is not None and schema != SCHEMA_VERSION:
        out.failures.append(
            f"unexpected audit schema {schema!r} "
            f"(engine speaks {SCHEMA_VERSION})"
        )
    if out.saw_contract and done_count is None:
        out.failures.append(
            "missing AUDIT_DONE trailer — output truncated; declarations "
            "not seen are unknown-by-absence"
        )
    elif done_count is not None and done_count != audit_line_count:
        out.failures.append(
            f"AUDIT_DONE reports {done_count} declaration line(s) but "
            f"{audit_line_count} seen — truncated run"
        )
    return out


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_audit(
    repo_dir: str | Path, target_folder: str | Path, *, timeout: int = 900
) -> AuditSnapshot:
    """Audit every declaration of the target folder's modules.

    Renders the lean_template script for the derived modules + trusted
    prefixes, writes it to a temp file OUTSIDE the repo (never dirties
    the workspace, never leaks into Aristotle bundles), runs
    ``lake env lean <script>`` with ``cwd=repo_dir`` so the repo's own
    pinned toolchain elaborates, and parses stdout.

    Never raises on toolchain trouble: lake missing / nonzero exit /
    timeout produce a snapshot with ``failures`` recorded and ``decls``
    possibly empty — callers see honest absence of evidence (same
    degradation pattern as ``formalization.check_axioms``)."""
    repo = Path(repo_dir).resolve()
    created_at = _now_iso()
    toolchain = read_toolchain(repo)
    package_revs = read_package_revs(repo)
    modules, failures = derive_modules(repo, target_folder)
    trusted = derive_trusted_prefixes(repo)

    def snapshot(
        decls: list[DeclAudit], extra_failures: list[str],
        lean_version: str | None = None,
    ) -> AuditSnapshot:
        return AuditSnapshot(
            repo_dir=str(repo),
            modules=modules,
            toolchain=toolchain,
            lean_version=lean_version,
            package_revs=package_revs,
            trusted_prefixes=trusted,
            created_at=created_at,
            decls=decls,
            failures=failures + extra_failures,
        )

    if not modules:
        return snapshot([], [f"no auditable .lean modules under {target_folder}"])
    try:
        script = render_audit_script(modules, trusted)
    except ValueError as exc:  # belt-and-braces: inputs are pre-filtered
        return snapshot([], [f"could not render audit script: {exc}"])
    lake = _find_lake()
    if lake is None:
        return snapshot(
            [],
            ["lake not found (no PATH lake, no ~/.elan/bin/lake) — "
             "audit evidence undetermined"],
        )

    run_failures: list[str] = []
    stdout = ""
    proc: subprocess.CompletedProcess | None = None
    with tempfile.TemporaryDirectory(prefix="marathon-audit-") as td:
        script_path = Path(td) / "marathon_audit.lean"
        script_path.write_text(script)
        try:
            proc = subprocess.run(
                [lake, "env", "lean", str(script_path)],
                cwd=str(repo),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            stdout = partial or ""
            run_failures.append(
                f"audit script timed out after {timeout}s — evidence "
                "undetermined for everything not seen"
            )
        except OSError as exc:
            return snapshot([], [f"failed to run `lake env lean`: {exc}"])
    if proc is not None:
        stdout = proc.stdout or ""
        if proc.returncode != 0:
            tail = " / ".join(
                (proc.stderr or "").strip().splitlines()[-5:]
            ) or "(no stderr)"
            run_failures.append(
                f"`lake env lean` exited {proc.returncode}: {tail}"
            )
    parsed = parse_audit_output(stdout)
    run_failures.extend(parsed.failures)
    if proc is not None and proc.returncode == 0 and not parsed.saw_contract:
        run_failures.append(
            "no audit contract lines on stdout — nothing audited"
        )
    reported = parsed.meta.get("modules") or []
    if parsed.saw_contract and reported and reported != modules:
        run_failures.append(
            f"script reported modules {reported} but {modules} were "
            "requested"
        )
    return snapshot(
        parsed.decls, run_failures,
        lean_version=parsed.meta.get("lean_version"),
    )


# ---------------------------------------------------------------------------
# Persistence (derived cache — recomputable, self-gitignored, never merged)
# ---------------------------------------------------------------------------

def audit_state_dir(repo_dir: str | Path) -> Path:
    return Path(repo_dir) / AUDIT_STATE_RELPATH


def save_snapshot(
    snapshot: AuditSnapshot, repo_dir: str | Path | None = None
) -> Path:
    """Write ``latest.json`` (rotating any existing one to
    ``previous.json``) under the repo's ``.marathon/audit/``.  The dir
    self-gitignores on first write — same convention as the conductor's
    ``jobs.json``: this is droppable derived cache living inside the
    consumer repo."""
    repo = Path(repo_dir) if repo_dir is not None else Path(snapshot.repo_dir)
    state_dir = audit_state_dir(repo)
    state_dir.mkdir(parents=True, exist_ok=True)
    gitignore = state_dir / ".gitignore"
    if not gitignore.is_file():
        gitignore.write_text("*\n")
    latest = state_dir / LATEST_NAME
    tmp = latest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot.to_json(), indent=2) + "\n")
    if latest.is_file():
        latest.replace(state_dir / PREVIOUS_NAME)
    tmp.replace(latest)
    return latest


def load_snapshot(
    repo_dir: str | Path, name: str = LATEST_NAME
) -> AuditSnapshot | None:
    """Load a persisted snapshot; None when absent or unparseable
    (a corrupt cache is absence of evidence, not an error)."""
    path = audit_state_dir(repo_dir) / name
    try:
        return AuditSnapshot.from_json(json.loads(path.read_text()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("unreadable audit snapshot %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Diff (pure — the future tier-invalidation feed)
# ---------------------------------------------------------------------------

def diff_snapshots(
    old: AuditSnapshot, new: AuditSnapshot
) -> dict[str, list[str]]:
    """Classify per-decl changes between two snapshots.

    Returns ``{change class: sorted decl names}`` for :data:`DIFF_KEYS`
    plus a ``warnings`` list of strings.  A decl may appear in several
    classes.  Pure function: no I/O, no tier policy — callers decide what
    a change *means*.

    Comparison rules:

    * fingerprints are compared only when both sides have them — an
      ``unknown`` side yields ``status_changed``, never a phantom
      type/value change (absence of evidence is not evidence of change);
    * ``value_changed``/``axioms_changed`` require both sides ``ok``
      (an ``unknown`` line carries no value/axiom evidence);
    * cross-toolchain / cross-rev comparisons are *flagged* in
      ``warnings`` instead of trusted (binding ruling: pp output may
      legitimately drift across toolchains)."""
    diff: dict[str, list[str]] = {key: [] for key in DIFF_KEYS}
    warnings: list[str] = []
    if old.toolchain != new.toolchain or (
        old.lean_version and new.lean_version
        and old.lean_version != new.lean_version
    ):
        warnings.append(
            "cross-toolchain comparison "
            f"({old.toolchain or old.lean_version!r} → "
            f"{new.toolchain or new.lean_version!r}): pp-string "
            "fingerprints may differ without a source change"
        )
    if old.package_revs and new.package_revs \
            and old.package_revs != new.package_revs:
        changed = sorted(
            name
            for name in set(old.package_revs) | set(new.package_revs)
            if old.package_revs.get(name) != new.package_revs.get(name)
        )
        warnings.append(
            "dependency revs changed (" + ", ".join(changed) + "): "
            "trusted-vocabulary meaning may have shifted"
        )
    old_by = old.by_name()
    new_by = new.by_name()
    diff["added"] = sorted(set(new_by) - set(old_by))
    diff["removed"] = sorted(set(old_by) - set(new_by))
    for name in sorted(set(old_by) & set(new_by)):
        o, n = old_by[name], new_by[name]
        if o.status != n.status:
            diff["status_changed"].append(name)
        if (
            o.fingerprint_type is not None
            and n.fingerprint_type is not None
            and o.fingerprint_type != n.fingerprint_type
        ):
            diff["type_changed"].append(name)
        if o.status == "ok" and n.status == "ok":
            if o.fingerprint_value != n.fingerprint_value:
                diff["value_changed"].append(name)
            if sorted(o.axioms) != sorted(n.axioms):
                diff["axioms_changed"].append(name)
    diff["warnings"] = warnings
    return diff

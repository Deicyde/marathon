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
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import signal
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections import deque
from dataclasses import dataclass, field, replace
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
from marathon.lake_build_lock import (
    LakeBuildLockUnavailable,
    LakeBuildLockTimeout,
    lake_build_lock,
    lake_build_lock_supported,
)

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

# Evidence extraction does substantially more work than importing an olean:
# it pretty-prints declarations and walks their dependency/axiom cones.  Avoid
# spending the entire command deadline on a known-large all-module script.
_AUDIT_BATCH_MODULE_LIMIT = 6
# Run at most five proactive batches.  Each gets the historical full command
# ceiling, and the whole stage gets one additional timeout-sized recovery
# slice.  This lets a genuinely healthy but expensive chapter (12 minutes has
# been observed in production) complete without returning to the old
# ``number of modules * timeout`` worst case.
_AUDIT_MAX_INITIAL_BATCHES = 5
_AUDIT_MAX_EVIDENCE_BUDGET_FACTOR = _AUDIT_MAX_INITIAL_BATCHES + 1


# Git pathspecs whose dirty state makes a HEAD-bound audit dishonest.  Build
# artefacts and Marathon runtime files are intentionally excluded; Lean source,
# its dependency/toolchain inputs, and Lake configuration are included.
_AUDIT_SOURCE_PATHS = (
    ":(glob)**/*.lean",
    "lean-toolchain",
    "lakefile.toml",
    "lakefile.lean",
    "lake-manifest.json",
)

# A timed-out ``subprocess.run`` kills only the immediate process.  Lake is a
# launcher (``lake -> lean -> lean`` in the common audit path), so that leaves
# the actual elaborator orphaned and consuming a core after Marathon has
# already recorded the timeout.  Keep every audit/build invocation in its own
# process group and tear down the whole group on timeout.
def _kill_process_group(proc: subprocess.Popen) -> None:
    """Best-effort termination of *proc* and every descendant it launched.

    POSIX sessions give us a reliable process-group boundary.  Windows does
    not expose an equivalent through :mod:`subprocess`, so use ``taskkill /T``
    when available and retain ``Popen.kill`` as a fail-soft fallback.  Cleanup
    errors must never mask the original timeout.
    """
    killed_group = False
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            killed_group = True
        except ProcessLookupError:
            return
        except OSError as exc:  # pragma: no cover - rare OS race/denial
            logger.debug("could not kill process group %s: %s", proc.pid, exc)
    elif os.name == "nt":  # pragma: no cover - exercised on Windows
        try:
            killed = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=5,
            )
            killed_group = killed.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("could not taskkill process tree %s: %s", proc.pid, exc)
    if not killed_group:
        try:
            proc.kill()
        except OSError:
            pass


def _run_process_group(
    command: list[str],
    *,
    cwd: str,
    timeout: int | float,
    lock_fd: int | None = None,
) -> subprocess.CompletedProcess:
    """Run a captured text command with a timeout-safe descendant boundary."""
    kwargs: dict = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
        if lock_fd is not None:
            # Keep the shared build lock alive if this Python process is
            # terminated while its Lake/Lean descendants are still running.
            kwargs["pass_fds"] = (lock_fd,)
    elif os.name == "nt":  # pragma: no cover - exercised on Windows
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    proc = subprocess.Popen(command, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # Reap the immediate launcher and drain its pipes. Descendants share
        # the killed group, so none can retain a pipe fd and make this hang.
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=timeout,
            output=stdout,
            stderr=stderr,
        )
    return subprocess.CompletedProcess(
        command, proc.returncode, stdout=stdout, stderr=stderr
    )


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


def _lake_build_ok(
    lake: str,
    repo: Path,
    targets: list[str],
    timeout: int | float,
    *,
    lock_fd: int | None = None,
) -> bool:
    """``lake build <targets>`` in *repo*; True iff it exits 0. Never raises.

    Lake's own exit code is the authoritative build signal: it keys off a
    content-hash trace, not file mtimes, so it (a) treats a module whose
    source mtime was merely bumped by a ``git checkout`` as up-to-date, and
    (b) reports a genuine failure even though Lean leaves the previous
    ``.olean`` in place on a failed rebuild. Probing on-disk oleans instead
    gets both of those cases wrong (false-broken / false-buildable), which
    is why classification goes through lake, not the filesystem."""
    if not targets:
        return True
    try:
        proc = _run_process_group(
            [lake, "build", *targets],
            cwd=str(repo),
            timeout=timeout,
            lock_fd=lock_fd,
        )
        return proc.returncode == 0
    except subprocess.TimeoutExpired as exc:
        logger.debug("lake build %s: %s", targets, exc)
        return False
    except OSError as exc:
        logger.debug("lake build %s: %s", targets, exc)
        return False


def partition_buildable(
    lake: str,
    repo: Path,
    modules: list[str],
    timeout: int | float,
    *,
    lock_fd: int | None = None,
) -> tuple[list[str], list[str]]:
    """Build *modules* and split them into ``(buildable, excluded)``.

    "Build-targets-first": every module is compiled before evidence scripts
    run, so that one uncompilable module (an
    in-progress ``StokesTheorem``, a chapter that broke against a renamed
    upstream API) cannot take the whole ``lake env lean`` audit down with
    it — the classic single-bad-leaf failure that leaves every declaration
    reading as "no audit evidence".

    The healthy case remains one batched ``lake build``. A failed batch is
    bisected breadth-first under one additional ``timeout``-sized recovery
    deadline and a logarithmic-path invocation ceiling. Recovery probes get a
    fixed fair slice sized to let both sides of one binary-search level run;
    unlike geometric halving, a healthy sibling does not receive a smaller
    timeout merely because it is deeper in the tree. This is enough to isolate
    one pathological leaf while preventing the old 2n−1 commands × timeout
    worst case. A module that itself needs longer than the fair recovery slice,
    or is not reached before either global bound, is conservatively returned
    as excluded; callers must not claim each excluded module was individually
    proven broken.

    The buildable set is import-closed under exclusion: a module that imports
    a broken one cannot itself build, so it too remains excluded — which is
    exactly why a surviving audit batch never loads a stale or inconsistent
    olean."""
    if not modules:
        return [], []

    requested = list(modules)
    if _lake_build_ok(
        lake,
        repo,
        requested,
        timeout,
        lock_fd=lock_fd,
    ):
        return sorted(requested), []
    if len(requested) == 1:
        return [], requested

    command_ceiling = max(0.0, float(timeout))
    deadline = time.monotonic() + command_ceiling
    depth = math.ceil(math.log2(len(requested)))
    # Isolating one failed path needs at most two probes per tree level. Give
    # each of those probes the same fair share of the one-time recovery budget
    # instead of repeatedly halving it (which used to turn a 900s ceiling into
    # a ~32s probe at depth five and falsely exclude healthy 60s siblings).
    recovery_probe_cap = command_ceiling / max(1, 2 * depth)
    # Two probes per level isolate one failed path. A second allowance keeps
    # exact classification for a small number of independent broken leaves,
    # while still capping a 27-module systemic fast failure at 21 total builds
    # instead of 53.
    max_recovery_invocations = max(2, 4 * depth)
    midpoint = len(requested) // 2
    pending = deque(
        [
            (requested[:midpoint], recovery_probe_cap),
            (requested[midpoint:], recovery_probe_cap),
        ]
    )
    buildable: list[str] = []
    excluded: list[str] = []
    invocations = 0

    while pending:
        batch, batch_cap = pending.popleft()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or invocations >= max_recovery_invocations:
            excluded.extend(batch)
            continue
        attempt_timeout = min(command_ceiling, batch_cap, remaining)
        if attempt_timeout <= 0:
            excluded.extend(batch)
            continue
        ok = _lake_build_ok(
            lake,
            repo,
            batch,
            attempt_timeout,
            lock_fd=lock_fd,
        )
        invocations += 1
        if ok:
            buildable.extend(batch)
            continue
        if len(batch) == 1:
            excluded.extend(batch)
            continue
        if (
            invocations >= max_recovery_invocations
            or time.monotonic() >= deadline
        ):
            excluded.extend(batch)
            continue
        midpoint = len(batch) // 2
        pending.append((batch[:midpoint], recovery_probe_cap))
        pending.append((batch[midpoint:], recovery_probe_cap))

    return sorted(buildable), sorted(excluded)


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


def source_state(repo_dir: str | Path) -> tuple[str | None, bool | None]:
    """Return ``(HEAD revision, audit-source-clean?)`` for *repo_dir*.

    ``(None, None)`` means the directory is not a readable Git checkout.  A
    revision with ``False`` means HEAD is known but one of the Lean/Lake inputs
    in :data:`_AUDIT_SOURCE_PATHS` differs from that commit.  Callers must not
    attach the HEAD revision to evidence in that case: doing so would make an
    uncommitted working-tree elaboration look like committed evidence.

    This helper is deliberately fail-soft because audits also run against
    generated fixture directories.  In a real Git checkout, however, ordinary
    snapshot loads use this state to withhold stale or dirty evidence.
    """
    repo = Path(repo_dir)
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=False,
        )
        if rev.returncode != 0:
            return None, None
        revision = (rev.stdout or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
            return None, None
        status = subprocess.run(
            [
                "git", "status", "--porcelain=v1", "--untracked-files=all",
                "--", *_AUDIT_SOURCE_PATHS,
            ],
            cwd=str(repo), capture_output=True, text=True, check=False,
        )
        if status.returncode != 0:
            return revision, None
        return revision, not bool((status.stdout or "").strip())
    except OSError:
        return None, None


@dataclass(frozen=True)
class _AuditExecutionSource:
    """Repository path from which one audit must read and elaborate.

    ``immutable_revision`` is set only for Marathon's private detached
    worktree.  ``failure`` means the original checkout was an eligible clean
    Git root, but that private worktree could not be prepared or verified; the
    caller may still collect unbound diagnostics from the original checkout,
    but must never attach its HEAD to those rows.
    """

    repo: Path
    immutable_revision: str | None = None
    failure: str | None = None


def _git_capture(
    repo: Path,
    arguments: list[str],
    *,
    timeout: int | float = 15,
) -> subprocess.CompletedProcess:
    """Bounded Git command used only while preparing an audit source."""

    return subprocess.run(
        ["git", *arguments],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _resolved_git_path(repo: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def _is_git_worktree_root(repo: Path) -> bool:
    """Whether *repo* itself (not merely an ancestor) is a Git worktree."""

    try:
        proc = _git_capture(repo, ["rev-parse", "--show-toplevel"])
        return (
            proc.returncode == 0
            and Path((proc.stdout or "").strip()).resolve() == repo
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _prepare_immutable_audit_source(
    repo: Path,
    revision: str,
) -> _AuditExecutionSource:
    """Return a stable detached worktree at exactly *revision* when eligible.

    The checkout lives under Git's common administrative directory at a path
    derived from the original worktree root.  Reusing that path is important:
    Lake traces include workspace paths, so a fresh temporary checkout would
    turn every audit into a cold rebuild.  Its ``.lake`` points at the original
    checkout's cache, keeping package downloads and compiled outputs shared;
    callers therefore must invoke this helper only while holding the original
    repository's shared Lake-build lock.

    Nested fixture directories and non-Git directories deliberately retain the
    historical sampled-checkout fallback.  A clean, real Git root that is
    eligible but cannot be isolated returns a failure so the caller fails
    closed on revision binding.
    """

    if not _is_git_worktree_root(repo):
        # Tests and generated packages are often nested inside Marathon's own
        # checkout rather than being repositories in their own right.
        return _AuditExecutionSource(repo=repo)

    exact_revision, revision_error = _exact_commit_oid(repo, revision)
    if exact_revision is None:
        return _AuditExecutionSource(
            repo=repo,
            failure=(
                "could not prepare immutable audit source: "
                + (revision_error or "unreadable start revision")
            ),
        )

    try:
        common_proc = _git_capture(repo, ["rev-parse", "--git-common-dir"])
    except subprocess.TimeoutExpired:
        return _AuditExecutionSource(
            repo=repo,
            failure="immutable audit Git-directory lookup timed out",
        )
    except OSError as exc:
        return _AuditExecutionSource(
            repo=repo,
            failure=f"could not locate immutable audit Git directory: {exc}",
        )
    if common_proc.returncode != 0:
        return _AuditExecutionSource(
            repo=repo,
            failure="could not locate immutable audit Git directory",
        )
    common_dir = _resolved_git_path(
        repo, (common_proc.stdout or "").strip()
    )
    checkout_key = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:20]
    checkout = (
        common_dir / "marathon-audit-worktrees" / checkout_key
    ).resolve()

    try:
        checkout.parent.mkdir(parents=True, exist_ok=True)
        if checkout.exists():
            if not checkout.is_dir():
                return _AuditExecutionSource(
                    repo=repo,
                    failure=(
                        "immutable audit checkout path exists but is not a "
                        f"directory: {checkout}"
                    ),
                )
            registered = _git_capture(
                checkout, ["rev-parse", "--git-common-dir"]
            )
            if registered.returncode != 0 or _resolved_git_path(
                checkout, (registered.stdout or "").strip()
            ) != common_dir:
                return _AuditExecutionSource(
                    repo=repo,
                    failure=(
                        "immutable audit checkout is not registered to the "
                        "reviewed repository"
                    ),
                )
        else:
            added = _git_capture(
                repo,
                [
                    "worktree", "add", "--detach", "--force",
                    str(checkout), exact_revision,
                ],
                timeout=60,
            )
            if added.returncode != 0:
                detail = (added.stderr or added.stdout or "").strip()
                return _AuditExecutionSource(
                    repo=repo,
                    failure=(
                        "could not create immutable audit checkout"
                        + (f": {detail}" if detail else "")
                    ),
                )

        checked_out = _git_capture(
            checkout,
            ["checkout", "--detach", "--force", exact_revision],
            timeout=60,
        )
        if checked_out.returncode != 0:
            detail = (checked_out.stderr or checked_out.stdout or "").strip()
            return _AuditExecutionSource(
                repo=repo,
                failure=(
                    "could not reset immutable audit checkout"
                    + (f": {detail}" if detail else "")
                ),
            )

        # Lake's cache is derived state and is safe to share under the same
        # flock already used by audit and Verso. A pre-existing private cache
        # from an early implementation is migrated below; every subsequent
        # audit shares the original checkout's cache by symlink.
        original_lake = (repo / ".lake").resolve()
        checkout_lake = checkout / ".lake"
        original_lake.mkdir(parents=True, exist_ok=True)
        if checkout_lake.is_symlink():
            if checkout_lake.resolve() != original_lake:
                return _AuditExecutionSource(
                    repo=repo,
                    failure=(
                        "immutable audit checkout has an unexpected .lake "
                        "cache link"
                    ),
                )
        else:
            if checkout_lake.exists():
                tracked_lake = _git_capture(
                    checkout, ["ls-files", "--", ".lake"]
                )
                if (
                    tracked_lake.returncode != 0
                    or (tracked_lake.stdout or "").strip()
                ):
                    return _AuditExecutionSource(
                        repo=repo,
                        failure=(
                            "immutable audit checkout contains tracked or "
                            "unverifiable .lake content; its cache was not "
                            "replaced"
                        ),
                    )
                # This path is Marathon-private and verified against the same
                # Git common directory above. Any real .lake here is derived
                # cache left by an older audit implementation; migrate it once
                # so future audits and the reviewed checkout truly share Lake
                # packages, oleans, and trace state under one lock.
                if checkout_lake.is_dir():
                    shutil.rmtree(checkout_lake)
                else:
                    checkout_lake.unlink()
            checkout_lake.symlink_to(original_lake, target_is_directory=True)

        checkout_state = source_state(checkout)
        if checkout_state != (exact_revision, True):
            return _AuditExecutionSource(
                repo=repo,
                failure=(
                    "immutable audit checkout did not verify as the clean "
                    f"start revision {exact_revision}"
                ),
            )
    except subprocess.TimeoutExpired:
        return _AuditExecutionSource(
            repo=repo,
            failure="immutable audit checkout preparation timed out",
        )
    except OSError as exc:
        return _AuditExecutionSource(
            repo=repo,
            failure=f"could not prepare immutable audit checkout: {exc}",
        )

    return _AuditExecutionSource(
        repo=checkout,
        immutable_revision=exact_revision,
    )


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


@dataclass
class _AuditBatchOutcome:
    """One bounded ``lake env lean`` contract invocation.

    ``complete`` is deliberately stronger than "some declarations were
    printed": only a zero exit with a valid trailer and an exact module list
    can grant revision-bound evidence.  ``splittable`` distinguishes failures
    that may be isolated by retrying smaller module batches from process-wide
    setup failures for which recursive retries would only create noise.
    """

    parsed: ParsedOutput = field(default_factory=ParsedOutput)
    failures: list[str] = field(default_factory=list)
    complete: bool = False
    splittable: bool = True


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
    seen_singleton_meta: set[str] = set()
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
            else:
                if key in seen_singleton_meta:
                    out.failures.append(
                        f"duplicate audit metadata key {key!r}"
                    )
                seen_singleton_meta.add(key)
                if key == "trusted_prefixes":
                    out.meta[key] = [] if value == "-" else value.split(",")
                else:
                    out.meta[key] = value
            continue
        if line.startswith(DONE_SENTINEL + "|"):
            if done_count is not None:
                out.failures.append("duplicate AUDIT_DONE trailer")
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
    if out.saw_contract:
        if schema is None:
            out.failures.append("missing audit schema metadata")
        elif schema != SCHEMA_VERSION:
            out.failures.append(
                f"unexpected audit schema {schema!r} "
                f"(engine speaks {SCHEMA_VERSION})"
            )
    lean_version = out.meta.get("lean_version")
    if out.saw_contract and (
        not isinstance(lean_version, str) or not lean_version.strip()
    ):
        out.failures.append("missing or empty Lean version metadata")
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


def _run_audit_batch(
    lake: str,
    repo: Path,
    modules: list[str],
    trusted: list[str],
    timeout: int | float,
    *,
    lock_fd: int | None = None,
) -> _AuditBatchOutcome:
    """Run and validate one module batch under an independent deadline.

    A caller may safely bisect an incomplete, splittable outcome.  Partial
    stdout is retained for diagnostics, but ``complete`` remains false so it
    can never acquire a module revision merely because Lean printed a prefix
    of the contract before failing or timing out.
    """

    try:
        script = render_audit_script(modules, trusted)
    except ValueError as exc:  # derived module names are pre-filtered
        return _AuditBatchOutcome(
            failures=[f"could not render audit script: {exc}"],
            splittable=False,
        )

    stdout = ""
    proc: subprocess.CompletedProcess | None = None
    run_failures: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="marathon-audit-") as td:
            script_path = Path(td) / "marathon_audit.lean"
            script_path.write_text(script)
            try:
                proc = _run_process_group(
                    [lake, "env", "lean", str(script_path)],
                    cwd=str(repo),
                    timeout=timeout,
                    lock_fd=lock_fd,
                )
            except subprocess.TimeoutExpired as exc:
                partial = exc.stdout
                if isinstance(partial, bytes):
                    partial = partial.decode("utf-8", errors="replace")
                stdout = partial or ""
                run_failures.append(
                    f"audit script timed out after {timeout}s — complete "
                    "evidence was not produced"
                )
    except OSError as exc:
        return _AuditBatchOutcome(
            failures=[f"failed to prepare or run `lake env lean`: {exc}"],
            splittable=False,
        )

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
    if parsed.saw_contract and reported != modules:
        run_failures.append(
            f"script reported modules {reported} but {modules} were requested"
        )
    reported_trusted = parsed.meta.get("trusted_prefixes")
    trusted_matches = (
        isinstance(reported_trusted, list)
        and sorted(reported_trusted) == sorted(trusted)
    )
    if parsed.saw_contract and not trusted_matches:
        run_failures.append(
            f"script reported trusted prefixes {reported_trusted!r} but "
            f"{trusted!r} were requested"
        )
    unexpected_decl_modules = sorted(
        {decl.module for decl in parsed.decls if decl.module not in modules}
    )
    if unexpected_decl_modules:
        run_failures.append(
            "script emitted declarations attributed to unrequested modules "
            f"{unexpected_decl_modules}"
        )

    complete = (
        proc is not None
        and proc.returncode == 0
        and parsed.saw_contract
        and not parsed.failures
        and reported == modules
        and trusted_matches
        and not unexpected_decl_modules
        and not any(
            failure.startswith("no audit contract lines")
            for failure in run_failures
        )
    )
    return _AuditBatchOutcome(
        parsed=parsed,
        failures=run_failures,
        complete=complete,
    )


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_audit(
    repo_dir: str | Path,
    target_folder: str | Path,
    *,
    timeout: int | float = 900,
) -> AuditSnapshot:
    """Audit every declaration of the target folder's modules.

    For a clean Git root, source metadata, builds, and evidence extraction all
    run from a stable detached worktree at the exact starting commit.  Its
    Lake cache is shared with the reviewed checkout under one repo-scoped
    lock, but mutable source is not: an A→B→A edit in the reviewed checkout
    cannot change what Lean elaborates. Generated fixtures, nested package
    directories, and dirty/non-Git workspaces retain the historical sampled
    fallback and never acquire a dishonest revision binding.

    The lean_template script is written to a temp file OUTSIDE both checkouts
    (never dirties the workspace, never leaks into Aristotle bundles).
    Proactive bounded batches prevent large chapters from consuming one
    monolithic deadline; an incomplete batch is bisected within a strict total
    recovery budget so successful siblings retain evidence while failed
    modules remain unbound.

    ``timeout`` is a budget unit, not a multiplier per module: shared-lock
    waiting uses at most 1×, build validation at most 2×, and evidence uses
    at most five proactive batches plus one recovery slice (6×). Thus command
    stages are bounded by 9× ``timeout`` overall (135 minutes at the default),
    apart from short process cleanup and metadata I/O.

    Never raises on toolchain trouble: lake missing / nonzero exit /
    timeout produce a snapshot with ``failures`` recorded and ``decls``
    possibly empty — callers see honest absence of evidence (same
    degradation pattern as ``formalization.check_axioms``)."""
    repo = Path(repo_dir).resolve()
    created_at = _now_iso()
    # Establish the candidate revision before reading any source-derived
    # metadata. A later identical HEAD is not enough if an intervening check
    # observed another revision or a dirty tree (the cross-batch A→B→A race).
    start_revision, start_clean = source_state(repo)
    immutable_candidate = (
        start_revision is not None
        and start_clean is True
        and _is_git_worktree_root(repo)
    )
    # Do not even derive revision-bound context from the mutable checkout when
    # an immutable worktree is available. Fallback fixture/dirty semantics keep
    # their historical eager context so failure snapshots remain informative.
    if immutable_candidate:
        toolchain: str | None = None
        package_revs: dict[str, str] = {}
        modules: list[str] = []
        failures: list[str] = []
        attempted_modules: list[str] = []
        trusted: list[str] = []
    else:
        toolchain = read_toolchain(repo)
        package_revs = read_package_revs(repo)
        modules, failures = derive_modules(repo, target_folder)
        attempted_modules = list(modules)
        trusted = derive_trusted_prefixes(repo)
    if start_revision is not None and start_clean is not True:
        failures.append(
            "audit source is not a clean committed revision; evidence from "
            "this run will not be revision-bound"
        )
    pre_execution_state = source_state(repo)

    def matches_start(state: tuple[str | None, bool | None]) -> bool:
        return (
            start_revision is not None
            and start_clean is True
            and state == (start_revision, True)
        )

    source_observations_stable = matches_start(pre_execution_state)

    def snapshot(
        decls: list[DeclAudit], extra_failures: list[str],
        lean_version: str | None = None,
        *, source_revision: str | None = None,
        successful_modules: list[str] | None = None,
    ) -> AuditSnapshot:
        successful_modules = successful_modules or []
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
            source_revision=source_revision,
            module_revisions={
                module: source_revision
                for module in successful_modules
                if source_revision is not None
            },
            attempted_modules=attempted_modules,
        )

    lake = _find_lake()
    if lake is None:
        if immutable_candidate:
            toolchain = read_toolchain(repo)
            package_revs = read_package_revs(repo)
            modules, failures = derive_modules(repo, target_folder)
            attempted_modules = list(modules)
            trusted = derive_trusted_prefixes(repo)
        if not modules:
            return snapshot(
                [], [f"no auditable .lean modules under {target_folder}"]
            )
        return snapshot(
            [],
            ["lake not found (no PATH lake, no ~/.elan/bin/lake) — "
             "audit evidence undetermined"],
        )

    run_failures: list[str] = []
    audited_decls: list[DeclAudit] = []
    successful_modules: list[str] = []
    observed_lean_versions: set[str] = set()
    execution_source = _AuditExecutionSource(repo=repo)
    isolation_failed = False
    try:
        # Verso's dependency package and this audit both publish into the
        # reviewed repository's `.lake/build`.  Hold one repo-scoped lock for
        # the build partition and the importing audit process so a concurrent
        # request waits, then reuses the first process's Lake traces. On a
        # platform without inherited POSIX flock, Verso is disabled, so retain
        # audit's historical unlocked runner instead of suppressing evidence.
        build_guard = (
            lake_build_lock(
                repo,
                blocking=True,
                # Waiting on Verso is bounded independently. The evidence
                # budget starts only after this shared build lock is acquired.
                timeout=timeout,
            )
            if lake_build_lock_supported()
            else contextlib.nullcontext(None)
        )
        with build_guard as lock_fd:

            # The private worktree and the shared .lake link are themselves
            # prepared under the same lock held by every following Lake/Lean
            # process. On platforms where inherited flock is unavailable we
            # retain the prior sampled-source behavior rather than claiming an
            # immutable snapshot without cache coordination.
            if immutable_candidate and lock_fd is None:
                failures.append(
                    "immutable audit source requires the shared Lake-build "
                    "lock, which is unavailable on this platform"
                )
                isolation_failed = True
                source_observations_stable = False
            if (
                lock_fd is not None
                and start_revision is not None
                and start_clean is True
            ):
                execution_source = _prepare_immutable_audit_source(
                    repo, start_revision
                )
                if execution_source.failure is not None:
                    failures.append(execution_source.failure)
                    isolation_failed = True
                    source_observations_stable = False

            execution_repo = execution_source.repo
            immutable_revision = execution_source.immutable_revision
            if immutable_revision is not None:
                # Discard every source-derived value sampled from the mutable
                # checkout before the lock. Only the commit worktree may shape
                # a revision-bound audit contract.
                execution_target: str | Path = target_folder
                target = Path(target_folder)
                if target.is_absolute():
                    try:
                        relative_target = target.resolve().relative_to(repo)
                    except ValueError:
                        modules = []
                        derived_failures = [
                            "absolute audit target lies outside the reviewed "
                            "repository and cannot be mapped into its immutable "
                            f"source snapshot: {target_folder}"
                        ]
                    else:
                        execution_target = execution_repo / relative_target
                        modules, derived_failures = derive_modules(
                            execution_repo, execution_target
                        )
                else:
                    modules, derived_failures = derive_modules(
                        execution_repo, execution_target
                    )
                failures = derived_failures
                toolchain = read_toolchain(execution_repo)
                package_revs = read_package_revs(execution_repo)
                trusted = derive_trusted_prefixes(execution_repo)
                attempted_modules = list(modules)
                source_observations_stable = True
            elif immutable_candidate:
                # Isolation failed after we deliberately deferred all mutable
                # source reads. Preserve diagnostic fallback rows, but the
                # isolation failure above permanently prevents revision bind.
                preparation_failures = list(failures)
                toolchain = read_toolchain(repo)
                package_revs = read_package_revs(repo)
                modules, derived_failures = derive_modules(
                    repo, target_folder
                )
                failures = preparation_failures + derived_failures
                attempted_modules = list(modules)
                trusted = derive_trusted_prefixes(repo)

            if not modules:
                return snapshot(
                    [],
                    [f"no auditable .lean modules under {target_folder}"],
                )

            # Build-targets-first + skip-broken: compile every module before
            # collecting evidence, and drop any module that fails to build.
            # Without this, one uncompilable module (a broken orphan) can
            # poison every audit batch that imports it.
            build_start_state = source_state(execution_repo)
            expected_execution_state = (
                (immutable_revision, True)
                if immutable_revision is not None
                else (start_revision, True)
            )
            if build_start_state != expected_execution_state:
                source_observations_stable = False
            buildable, build_excluded = partition_buildable(
                lake,
                execution_repo,
                modules,
                timeout,
                lock_fd=lock_fd,
            )
            build_end_state = source_state(execution_repo)
            if build_end_state != expected_execution_state:
                source_observations_stable = False
            for m in build_excluded:
                failures.append(
                    f"module {m} did not complete a successful bounded build "
                    "probe; excluded from audit (it may have failed, timed "
                    "out, or remained unresolved when recovery stopped)"
                )
            modules = buildable
            if not modules:
                return snapshot(
                    [],
                    [
                        "no modules were confirmed buildable within bounded "
                        "build validation — audit evidence undetermined"
                    ],
                )

            def record_incomplete(
                batch: list[str], details: list[str]
            ) -> None:
                label = (
                    f"module {batch[0]}"
                    if len(batch) == 1
                    else "audit batch " + ", ".join(batch)
                )
                run_failures.extend(
                    f"{label}: {failure}" for failure in details
                )

            # Start proactively below the size that already proved too large
            # in production. Widen batches only when needed to cap first-pass
            # work at five commands. Every first-pass batch gets the full
            # historical timeout, and one further timeout-sized slice is shared
            # by recovery. Failed work is appended behind untouched batches, so
            # a heavy early module cannot prevent later modules from receiving
            # their first attempt.
            batch_width = max(
                _AUDIT_BATCH_MODULE_LIMIT,
                math.ceil(len(modules) / _AUDIT_MAX_INITIAL_BATCHES),
            )
            initial_batches = [
                modules[start : start + batch_width]
                for start in range(0, len(modules), batch_width)
            ]
            command_ceiling = max(0.0, float(timeout))
            total_budget = command_ceiling * (len(initial_batches) + 1)
            deadline = time.monotonic() + total_budget
            pending = deque(
                (batch, command_ceiling)
                for batch in initial_batches
            )
            # One failing leaf in a maximum-size batch needs at most two
            # children per tree level. Bound even instant systemic failures,
            # which otherwise evade the wall-clock budget and create 2n-1
            # pointless process launches.
            recovery_depth = math.ceil(
                math.log2(max(1, batch_width))
            )
            max_invocations = len(initial_batches) + 2 * recovery_depth
            invocations = 0

            while pending:
                batch, batch_cap = pending.popleft()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    record_incomplete(
                        batch,
                        [
                            f"audit evidence budget of {total_budget:g}s "
                            "was exhausted before this batch completed"
                        ],
                    )
                    continue
                if invocations >= max_invocations:
                    record_incomplete(
                        batch,
                        [
                            f"audit recovery invocation limit of "
                            f"{max_invocations} was reached before this "
                            "batch completed"
                        ],
                    )
                    continue

                attempt_timeout = min(
                    command_ceiling,
                    batch_cap,
                    remaining,
                )
                if attempt_timeout <= 0:
                    record_incomplete(
                        batch,
                        ["no audit evidence time remained for this batch"],
                    )
                    continue
                batch_start_state = source_state(execution_repo)
                if batch_start_state != expected_execution_state:
                    source_observations_stable = False
                outcome = _run_audit_batch(
                    lake,
                    execution_repo,
                    batch,
                    trusted,
                    attempt_timeout,
                    lock_fd=lock_fd,
                )
                batch_end_state = source_state(execution_repo)
                if batch_end_state != expected_execution_state:
                    source_observations_stable = False
                invocations += 1
                if outcome.complete:
                    audited_decls.extend(outcome.parsed.decls)
                    successful_modules.extend(batch)
                    lean_version = outcome.parsed.meta.get("lean_version")
                    if isinstance(lean_version, str) and lean_version:
                        observed_lean_versions.add(lean_version)
                    continue

                if (
                    outcome.splittable
                    and len(batch) > 1
                    and invocations < max_invocations
                    and time.monotonic() < deadline
                ):
                    midpoint = len(batch) // 2
                    pending.append((batch[:midpoint], command_ceiling))
                    pending.append((batch[midpoint:], command_ceiling))
                    continue

                record_incomplete(
                    batch,
                    outcome.failures
                    or ["audit contract did not complete"],
                )
    except LakeBuildLockTimeout:
        return snapshot(
            [],
            [
                f"audit timed out after {timeout}s waiting for another Lake "
                "build to finish — audit evidence undetermined"
            ],
        )
    except LakeBuildLockUnavailable as exc:
        return snapshot(
            [],
            [
                f"could not coordinate the shared Lake build: {exc} — "
                "audit evidence undetermined"
            ],
        )

    # A real clean Git root executes entirely from the detached start commit;
    # mutable-checkout A→B→A races are therefore irrelevant to its evidence.
    # Fallback fixtures retain the old boundary-sampling contract.
    end_revision, end_clean = source_state(execution_source.repo)
    expected_revision = execution_source.immutable_revision or start_revision
    stable_revision = (
        expected_revision
        if source_observations_stable
        and not isolation_failed
        and end_revision == expected_revision
        and end_clean is True
        else None
    )
    if expected_revision is not None and stable_revision is None:
        run_failures.append(
            (
                "immutable audit source could not be prepared or remained "
                "unverified; evidence was not revision-bound"
                if isolation_failed
                else "audit source revision differed or became dirty at a "
                "sampled audit boundary; evidence was not revision-bound"
            )
        )

    # Per-batch parsing enforces local uniqueness, but declaration names are a
    # global Lean namespace. Batching must not weaken that contract: duplicate
    # names across otherwise complete batches make ``by_name`` ambiguous and
    # therefore invalidate the entire evidence payload, including raw readers.
    decl_name_counts: dict[str, int] = {}
    for decl in audited_decls:
        decl_name_counts[decl.name] = decl_name_counts.get(decl.name, 0) + 1
    duplicate_decl_names = sorted(
        name for name, count in decl_name_counts.items() if count > 1
    )
    if duplicate_decl_names:
        run_failures.append(
            "duplicate declaration name(s) across complete audit batches: "
            + ", ".join(duplicate_decl_names)
            + "; all evidence from this run was withheld"
        )
        audited_decls = []
        successful_modules = []
        stable_revision = None

    # A snapshot has one in-band Lean version.  Distinct versions across
    # successful batches would make that context ambiguous, so fail closed on
    # revision binding even if every individual contract was complete.
    lean_version = (
        next(iter(observed_lean_versions))
        if len(observed_lean_versions) == 1
        else None
    )
    if len(observed_lean_versions) > 1:
        run_failures.append(
            "audit batches reported inconsistent Lean versions "
            f"{sorted(observed_lean_versions)}; evidence was not revision-bound"
        )
        stable_revision = None

    successful_modules = sorted(set(successful_modules))
    successful_set = set(successful_modules)
    # Defensive contract boundary: only declarations attributed to a module
    # whose complete batch succeeded enter the snapshot at all. Partial rows
    # are not useful evidence, and excluding them also protects raw diagnostic
    # consumers that do not apply ``load_snapshot``'s freshness filter.
    audited_decls = [
        decl for decl in audited_decls if decl.module in successful_set
    ]
    modules = successful_modules
    audited_decls.sort(key=lambda decl: decl.name)
    bound_revision = stable_revision if successful_modules else None
    return snapshot(
        audited_decls,
        run_failures,
        lean_version=lean_version,
        source_revision=bound_revision,
        successful_modules=successful_modules,
    )


# ---------------------------------------------------------------------------
# Persistence (derived cache — recomputable, self-gitignored, never merged)
# ---------------------------------------------------------------------------

def audit_state_dir(repo_dir: str | Path) -> Path:
    return Path(repo_dir) / AUDIT_STATE_RELPATH


@contextlib.contextmanager
def _snapshot_write_lock(state_dir: Path):
    """Serialize snapshot read/merge/write across audit processes."""
    lock_path = state_dir / ".write.lock"
    with open(lock_path, "a+") as lock_file:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows fallback
            yield
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, text: str) -> None:
    """Durably write *text* then atomically replace *path*."""
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink()


def _merge_snapshot(
    existing: AuditSnapshot, incoming: AuditSnapshot
) -> AuditSnapshot:
    """Merge one audit run without laundering partial evidence.

    Revision-aware runs replace only modules whose *complete* contract earned a
    ``module_revisions`` entry.  Failed/truncated modules keep their older rows
    and older revision, which means a normal load at a newer HEAD withholds
    them.  Legacy/programmatically-constructed snapshots retain the historical
    declaration-module merge behavior for compatibility.
    """
    revision_aware = bool(
        incoming.attempted_modules
        or incoming.module_revisions
        or incoming.source_revision
    )
    if not revision_aware:
        new_modules = {d.module for d in incoming.decls}
        if not new_modules:
            return existing
        kept = [d for d in existing.decls if d.module not in new_modules]
        merged = kept + list(incoming.decls)
        return replace(
            incoming, decls=merged,
            modules=sorted({d.module for d in merged}),
        )

    successful = set(incoming.module_revisions)
    if not successful:
        # Persist the latest failure channel, but do not alter any successful
        # cursor/evidence metadata merely because another process failed.
        return replace(
            existing,
            failures=list(incoming.failures),
            attempted_modules=list(incoming.attempted_modules),
        )

    kept = [d for d in existing.decls if d.module not in successful]
    accepted = [d for d in incoming.decls if d.module in successful]
    revisions = dict(existing.module_revisions)
    revisions.update({m: incoming.module_revisions[m] for m in successful})
    merged = kept + accepted
    return replace(
        incoming,
        decls=merged,
        modules=sorted(set(existing.modules) | successful),
        module_revisions=revisions,
    )


def save_snapshot(
    snapshot: AuditSnapshot,
    repo_dir: str | Path | None = None,
    *,
    merge: bool = True,
) -> Path:
    """Write ``latest.json`` (rotating any existing one to
    ``previous.json``) under the repo's ``.marathon/audit/``.  The dir
    self-gitignores on first write — same convention as the conductor's
    ``jobs.json``: this is droppable derived cache living inside the
    consumer repo.

    With ``merge=True`` (default), only modules carrying a successful revision
    replace their prior rows.  Other modules keep both their prior declarations
    *and prior revisions*, so stale evidence can never be made current by a
    failed or partial run.  The merge and atomic swap are serialized across
    processes. Pass ``merge=False`` for a clean from-scratch overwrite."""
    repo = Path(repo_dir) if repo_dir is not None else Path(snapshot.repo_dir)
    state_dir = audit_state_dir(repo)
    state_dir.mkdir(parents=True, exist_ok=True)
    gitignore = state_dir / ".gitignore"
    if not gitignore.is_file():
        gitignore.write_text("*\n")
    latest = state_dir / LATEST_NAME
    with _snapshot_write_lock(state_dir):
        if merge:
            existing = load_snapshot(repo, validate_revision=False)
            if existing is not None:
                snapshot = _merge_snapshot(existing, snapshot)
        payload = json.dumps(snapshot.to_json(), indent=2) + "\n"
        if latest.is_file():
            # Copy, then atomically replace previous; never move latest out of
            # the way before the new payload is ready.
            _atomic_write_text(
                state_dir / PREVIOUS_NAME,
                latest.read_text(),
            )
        _atomic_write_text(latest, payload)
    return latest


_AUDIT_CONFIG_PATHS = frozenset(
    {"lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json"}
)
_AUDIT_IMPORT_COMMAND_RE = re.compile(
    r"^\s*(?:public\s+)?import\s+"
    r"([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)"
    r"\s*(?:--.*)?$"
)
_AUDIT_IMPORT_LIKE_RE = re.compile(r"^\s*(?:public\s+)?import(?:\s|$)")


def _exact_commit_oid(repo: Path, revision: str) -> tuple[str | None, str | None]:
    """Resolve *revision* only when it already is the full immutable OID."""
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", revision):
        return None, f"revision {revision!r} is not a full commit object ID"
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
            cwd=str(repo), capture_output=True, text=True, check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return None, "commit-object verification timed out after 15s"
    except OSError as exc:
        return None, f"could not verify commit object: {exc}"
    if proc.returncode != 0:
        return None, f"revision {revision!r} is not a readable commit object"
    resolved = (proc.stdout or "").strip()
    if resolved.lower() != revision.lower():
        return None, f"revision {revision!r} did not resolve to itself exactly"
    return resolved, None


def _committed_local_module_map(
    repo: Path, current_revision: str,
) -> tuple[dict[str, tuple[str, str]], str | None]:
    """``module -> (path, blob OID)`` from one immutable commit tree."""
    try:
        proc = subprocess.run(
            ["git", "ls-tree", "-r", "-z", current_revision],
            cwd=str(repo), capture_output=True, text=True, check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {}, "current commit-tree enumeration timed out after 15s"
    except (OSError, UnicodeError) as exc:
        return {}, f"could not enumerate the current commit tree: {exc}"
    if proc.returncode != 0:
        return {}, f"current commit-tree enumeration exited {proc.returncode}"

    module_map: dict[str, tuple[str, str]] = {}
    for entry in (proc.stdout or "").split("\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split("\t", 1)
            mode, object_type, blob_oid = metadata.split(" ", 2)
        except ValueError:
            return {}, f"malformed Git tree entry {entry!r}"
        normalized = Path(raw_path).as_posix()
        if not normalized.endswith(".lean") or any(
            part.startswith(".") for part in Path(normalized).parts
        ):
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            return {}, (
                f"tracked Lean path {normalized!r} is not an ordinary file"
            )
        module = module_from_file_path(normalized)
        if module is None or not _LEAN_NAME_RE.fullmatch(module):
            continue
        prior = module_map.get(module)
        if prior is not None and prior[0] != normalized:
            return {}, (
                f"multiple current source paths map to module {module}: "
                f"{prior[0]} and {normalized}"
            )
        module_map[module] = (normalized, blob_oid)
    return module_map, None


def _committed_blob_text(
    repo: Path, blob_oid: str, cache: dict[str, str],
) -> tuple[str | None, str | None]:
    if blob_oid in cache:
        return cache[blob_oid], None
    try:
        proc = subprocess.run(
            ["git", "cat-file", "blob", blob_oid],
            cwd=str(repo), capture_output=True, check=False, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return None, f"reading source blob {blob_oid[:12]} timed out"
    except OSError as exc:
        return None, f"could not read source blob {blob_oid[:12]}: {exc}"
    if proc.returncode != 0:
        return None, f"source blob {blob_oid[:12]} could not be read"
    try:
        text = (proc.stdout or b"").decode("utf-8")
    except UnicodeError as exc:
        return None, f"source blob {blob_oid[:12]} is not valid UTF-8: {exc}"
    cache[blob_oid] = text
    return text, None


def _changed_audit_inputs(
    repo: Path, old_revision: str, current_revision: str,
) -> tuple[set[str], bool, str | None]:
    """Changed local modules + config flag between two committed trees.

    ``--no-renames`` deliberately exposes both sides of a rename as ordinary
    paths. A source move changes its Lean module identity even when its bytes
    are identical, so treating it as unchanged would be unsound.
    """
    try:
        proc = subprocess.run(
            [
                "git", "diff", "--name-only", "--no-renames", "-z",
                old_revision, current_revision, "--", *_AUDIT_SOURCE_PATHS,
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return set(), False, "Git source comparison timed out after 15s"
    except OSError as exc:
        return set(), False, f"could not run Git source comparison: {exc}"
    if proc.returncode != 0:
        tail = " / ".join((proc.stderr or "").strip().splitlines()[-3:])
        return set(), False, (
            f"Git source comparison exited {proc.returncode}"
            + (f": {tail}" if tail else "")
        )

    changed_modules: set[str] = set()
    config_changed = False
    for raw_path in (proc.stdout or "").split("\0"):
        if not raw_path:
            continue
        normalized = Path(raw_path).as_posix()
        if normalized in _AUDIT_CONFIG_PATHS:
            config_changed = True
            continue
        if not normalized.endswith(".lean"):
            # The pathspec should make this impossible. Fail closed if Git's
            # output or a future pathspec change violates that assumption.
            return set(), False, (
                f"unexpected changed audit input path {normalized!r}"
            )
        module = module_from_file_path(normalized)
        if module is None or not _LEAN_NAME_RE.fullmatch(module):
            return set(), False, (
                f"changed Lean path {normalized!r} cannot be mapped to a "
                "safe module name"
            )
        changed_modules.add(module)
    return changed_modules, config_changed, None


def _strict_current_import_closure(
    repo: Path,
    module: str,
    module_map: dict[str, tuple[str, str]],
    changed_modules: set[str],
    blob_cache: dict[str, str],
    trusted_prefixes: set[str],
) -> tuple[set[str], str | None]:
    """Current project-local import closure, failing closed on ambiguity."""
    if module not in module_map:
        return set(), f"current source for module {module} was not found"
    seen = {module}
    pending = [module]
    while pending:
        current = pending.pop()
        path, blob_oid = module_map[current]
        text, blob_error = _committed_blob_text(repo, blob_oid, blob_cache)
        if blob_error is not None or text is None:
            return set(), (
                f"could not read {path} while resolving the import closure: "
                f"{blob_error or 'unknown blob error'}"
            )
        for raw_line in text.splitlines():
            match = _AUDIT_IMPORT_COMMAND_RE.fullmatch(raw_line)
            if match is None:
                if _AUDIT_IMPORT_LIKE_RE.match(raw_line):
                    return set(), (
                        f"could not safely parse import command in {path}: "
                        f"{raw_line.strip()!r}"
                    )
                continue
            imported = match.group(1)
            if imported in module_map:
                if imported not in seen:
                    seen.add(imported)
                    pending.append(imported)
            elif (
                imported in changed_modules
                or imported.split(".", 1)[0] not in trusted_prefixes
            ):
                # A deleted, ignored, symlinked, or otherwise untracked local
                # dependency is absent from the immutable module map. External
                # imports are admitted only through the exact trusted-prefix
                # partition recorded by the audit contract.
                return set(), (
                    f"imported module {imported} has no unchanged ordinary "
                    "project-local source blob"
                )
    return seen, None


def _unchanged_import_closure_modules(
    snapshot: AuditSnapshot,
    repo: Path,
    current_revision: str,
    candidates: set[str],
) -> tuple[set[str], list[str]]:
    """Older evidence proven equivalent at *current_revision*.

    This is intentionally a derived read-time proof. The persisted
    ``module_revisions`` retain the actual audit SHA; a normal load may promote
    a module in its returned view only when Git reports byte-identical Lean
    sources for the module's entire *current* local import closure and
    byte-identical toolchain/Lake configuration.
    """
    if not candidates:
        return set(), []
    current_oid, current_error = _exact_commit_oid(repo, current_revision)
    if current_error is not None or current_oid is None:
        return set(), [
            "audit carry-forward could not be verified: "
            + (current_error or "current revision was not immutable")
        ]
    module_map, map_error = _committed_local_module_map(repo, current_oid)
    if map_error is not None:
        return set(), [f"audit carry-forward could not be verified: {map_error}"]

    carried: set[str] = set()
    failures: list[str] = []
    blob_cache: dict[str, str] = {}
    by_revision: dict[str, set[str]] = {}
    for module in candidates:
        audited_revision = snapshot.module_revisions.get(module)
        if not audited_revision:
            continue
        by_revision.setdefault(audited_revision, set()).add(module)

    for audited_revision, revision_modules in sorted(by_revision.items()):
        audited_oid, audited_error = _exact_commit_oid(repo, audited_revision)
        prefix = f"audit carry-forward from {audited_revision[:12]}"
        if audited_error is not None or audited_oid is None:
            failures.append(
                f"{prefix} could not be verified: "
                f"{audited_error or 'audit revision was not immutable'}"
            )
            continue
        changed, config_changed, diff_error = _changed_audit_inputs(
            repo, audited_oid, current_oid
        )
        if diff_error is not None:
            failures.append(f"{prefix} could not be verified: {diff_error}")
            continue
        if config_changed:
            failures.append(
                f"{prefix} refused: Lean toolchain or Lake configuration "
                "changed"
            )
            continue
        for module in sorted(revision_modules):
            closure, closure_error = _strict_current_import_closure(
                repo,
                module,
                module_map,
                changed,
                blob_cache,
                set(snapshot.trusted_prefixes),
            )
            if closure_error is not None:
                failures.append(
                    f"{prefix} could not verify module {module}: "
                    f"{closure_error}"
                )
                continue
            if closure.isdisjoint(changed):
                carried.add(module)
    return carried, failures


def _fresh_snapshot(
    snapshot: AuditSnapshot, revision: str, clean: bool | None,
    repo_dir: str | Path,
) -> AuditSnapshot:
    """Return a read view containing only evidence fresh for *revision*.

    The persisted file is left intact for diagnostics/diffing.  Normal callers
    receive this fail-closed view, so trust tiers, dependency fingerprints, the
    prover DAG, and deck readiness all consume the same freshness boundary.
    """
    failures = list(snapshot.failures)
    if clean is not True:
        if snapshot.decls:
            failures.append(
                "audit evidence withheld: the working tree has uncommitted "
                "or unverifiable Lean/Lake source changes"
            )
        return replace(snapshot, decls=[], modules=[], failures=failures)

    raw_by = snapshot.by_name()
    if not snapshot.module_revisions:
        if snapshot.decls:
            failures.append(
                "audit evidence withheld: legacy snapshot has no source "
                f"revision for current HEAD {revision[:12]}"
            )
        return replace(snapshot, decls=[], modules=[], failures=failures)

    evidence_modules = set(snapshot.modules) | {d.module for d in snapshot.decls}
    fresh_modules = {
        module for module, audited in snapshot.module_revisions.items()
        if audited == revision and module in evidence_modules
    }
    older_candidates = {
        module for module in evidence_modules
        if snapshot.module_revisions.get(module) not in (None, revision)
    }
    carried, carry_failures = _unchanged_import_closure_modules(
        snapshot, Path(repo_dir).resolve(), revision, older_candidates
    )
    fresh_modules |= carried
    failures.extend(carry_failures)
    stale_modules = sorted(evidence_modules - fresh_modules)
    if stale_modules:
        shown = ", ".join(stale_modules[:12])
        if len(stale_modules) > 12:
            shown += f", … (+{len(stale_modules) - 12})"
        failures.append(
            f"audit evidence stale at HEAD {revision[:12]}; withheld module(s): "
            f"{shown}"
        )

    eligible = {d.name for d in snapshot.decls if d.module in fresh_modules}
    directly_fresh = set(eligible)
    # A declaration audited now but whose statement cone reaches an older
    # module is not independently fresh.  Cascade until the remaining graph is
    # closed over fresh evidence.
    while True:
        dependent_on_stale = {
            name for name in eligible
            if any(dep in raw_by and dep not in eligible for dep in raw_by[name].cone)
        }
        if not dependent_on_stale:
            break
        eligible -= dependent_on_stale
    cascaded = len(directly_fresh - eligible)
    if cascaded:
        failures.append(
            f"audit evidence withheld for {cascaded} declaration(s) whose "
            "statement cone depends on stale module evidence"
        )

    return replace(
        snapshot,
        decls=[d for d in snapshot.decls if d.name in eligible],
        modules=sorted(m for m in snapshot.modules if m in fresh_modules),
        source_revision=revision if fresh_modules else None,
        module_revisions={module: revision for module in fresh_modules},
        failures=failures,
    )


def load_snapshot(
    repo_dir: str | Path, name: str = LATEST_NAME, *,
    validate_revision: bool = True,
) -> AuditSnapshot | None:
    """Load a persisted snapshot; None when absent or unparseable
    (a corrupt cache is absence of evidence, not an error).

    By default, a Git checkout receives a fail-closed view with dirty, legacy,
    or older-revision declarations withheld.  Diagnostic callers such as audit
    diff and the conductor's durable-cursor recovery may pass
    ``validate_revision=False`` to inspect the raw persisted evidence and its
    explicit revision map.
    """
    path = audit_state_dir(repo_dir) / name
    try:
        snapshot = AuditSnapshot.from_json(json.loads(path.read_text()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("unreadable audit snapshot %s: %s", path, exc)
        return None
    if not validate_revision:
        return snapshot
    revision, clean = source_state(repo_dir)
    if revision is None:
        return snapshot
    return _fresh_snapshot(snapshot, revision, clean, repo_dir)


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

"""Build and serve Verso's elaboration-aware view of a reviewed Lean project.

The reviewed repository must remain untouched: the disposable Lake package,
build products, manifests, and state all live below ``.lake/marathon-verso``.
That directory is already ignored by normal Lake projects.  A successful site
is immutable and keyed by the current source plus Lake/toolchain identity; a
failed refresh therefore never destroys the last site reviewers could use.

Only the Python standard library is used here.  In particular, the manifest
extractor intentionally uses :mod:`html.parser` rather than adding a web parser
to Marathon's small runtime dependency set.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Optional
from urllib.parse import unquote

from marathon.deck.definitions import definition_source_token

try:  # POSIX advisory locks
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows keeps the raw-source fallback
    _fcntl = None


DEFAULT_ROOT_RELPATH = Path(".lake") / "marathon-verso"
_STATE_FILE = "state.json"
_MANIFEST_FILE = "manifest.json"
_METADATA_FILE = "metadata.json"
_DEPENDENCY_SIGNATURE_FILE = "dependency-signature"
_BUILD_LOCK_FILE = "build.lock"
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_TOOLCHAIN_VERSION_RE = re.compile(r"(?:^|:)v(\d+\.\d+\.\d+)(?:\s*)$")
_LEAN_NAME_RE = r"[A-Za-z_][A-Za-z0-9_'.]*"
_MAX_ERROR_CHARS = 4_000
_BUILD_TIMEOUT_SECONDS = 60 * 45
_RENDER_TIMEOUT_SECONDS = 60 * 15
_LEAN_BUILD_THREADS = "2"
_MODULE_TARGET_BATCH_SIZE = 100
_ARTIFACT_RETENTION = 5  # current site plus four recent pinned-tab generations


class VersoUnavailable(RuntimeError):
    """The reviewed project cannot be rendered by the supported pipeline."""


class VersoBuildError(RuntimeError):
    """A Lake or Verso subprocess failed."""


class _VersoSourceChanged(RuntimeError):
    def __init__(self, source_token: str) -> None:
        super().__init__("Lean source changed during the Verso build")
        self.source_token = source_token


@dataclass(frozen=True)
class VersoProject:
    """The minimum project metadata needed by the nested Lake overlay."""

    package: str
    library: str
    toolchain: str
    verso_tag: str
    source_dir: str = "."
    roots: tuple[str, ...] = ()


def effective_artifact_token(source_token: str, dependency_signature: str) -> str:
    """Bind a source fingerprint to the Lake/toolchain configuration used."""

    payload = f"marathon-verso-v1\0{source_token}\0{dependency_signature}"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def verso_tag_from_toolchain(repo_dir: Path) -> str:
    """Return the stable Verso tag matching ``lean-toolchain``.

    Verso publishes version-matched tags.  Nightlies, release candidates, and
    custom toolchains are deliberately not guessed: serving an old semantic
    view as if it matched current Lean would be worse than reporting that the
    view is unavailable.
    """

    path = Path(repo_dir) / "lean-toolchain"
    try:
        toolchain = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VersoUnavailable(f"could not read {path.name}: {exc}") from exc
    match = _TOOLCHAIN_VERSION_RE.search(toolchain)
    if match is None:
        raise VersoUnavailable(
            "Verso requires a stable lean-toolchain tag such as "
            "leanprover/lean4:v4.28.0"
        )
    return f"v{match.group(1)}"


def _toml_project(
    repo_dir: Path,
) -> tuple[Optional[str], Optional[str], str, tuple[str, ...]]:
    path = repo_dir / "lakefile.toml"
    if not path.is_file():
        return None, None, ".", ()
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VersoUnavailable(f"could not parse lakefile.toml: {exc}") from exc

    package = data.get("name")
    libraries = data.get("lean_lib", [])
    if isinstance(libraries, dict):
        libraries = [libraries]
    library = None
    source_dir = "."
    roots: tuple[str, ...] = ()
    if isinstance(libraries, list):
        for entry in libraries:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                library = entry["name"].strip()
                if library:
                    if isinstance(entry.get("srcDir"), str):
                        source_dir = entry["srcDir"].strip() or "."
                    configured_roots = entry.get("roots")
                    if isinstance(configured_roots, list):
                        roots = tuple(
                            root.strip()
                            for root in configured_roots
                            if isinstance(root, str) and root.strip()
                        )
                    break
    return (
        package.strip() if isinstance(package, str) else None,
        library,
        source_dir,
        roots,
    )


def _lean_project(
    repo_dir: Path,
) -> tuple[Optional[str], Optional[str], str, tuple[str, ...]]:
    path = repo_dir / "lakefile.lean"
    if not path.is_file():
        return None, None, ".", ()
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise VersoUnavailable(f"could not read lakefile.lean: {exc}") from exc
    package_match = re.search(rf"(?m)^\s*package\s+({_LEAN_NAME_RE})\b", source)
    library_match = re.search(rf"(?m)^\s*lean_lib\s+({_LEAN_NAME_RE})\b", source)
    return (
        package_match.group(1) if package_match else None,
        library_match.group(1) if library_match else None,
        ".",
        (),
    )


def discover_verso_project(repo_dir: Path) -> VersoProject:
    """Discover the root package/library and its matching Verso release."""

    repo_dir = Path(repo_dir).resolve()
    package, library, source_dir, roots = _toml_project(repo_dir)
    if package is None or library is None:
        lean_package, lean_library, lean_source_dir, lean_roots = _lean_project(
            repo_dir
        )
        package = package or lean_package
        library = library or lean_library
        if source_dir == ".":
            source_dir = lean_source_dir
        if not roots:
            roots = lean_roots
    if not package:
        raise VersoUnavailable("the root Lake package name could not be found")
    if not library:
        raise VersoUnavailable(
            "no Lean library target was found in lakefile.toml or lakefile.lean"
        )
    toolchain_path = repo_dir / "lean-toolchain"
    try:
        toolchain = toolchain_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VersoUnavailable(f"could not read lean-toolchain: {exc}") from exc
    return VersoProject(
        package=package,
        library=library,
        toolchain=toolchain,
        verso_tag=verso_tag_from_toolchain(repo_dir),
        source_dir=source_dir,
        roots=roots or (library,),
    )


class _VersoPageParser(HTMLParser):
    """Extract module identity and definition anchors from one Verso page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.constants: list[tuple[str, str]] = []
        self._in_title = False
        self._title_parts: list[str] = []

    @property
    def title(self) -> str:
        return "".join(self._title_parts).strip()

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() != "span":
            return
        attr = {key: value or "" for key, value in attrs}
        classes = set(attr.get("class", "").split())
        binding = attr.get("data-binding", "")
        anchor = attr.get("id", "")
        # In Verso 4.28, every reference has data-binding=const-..., but only
        # the defining occurrence receives an id.  Requiring both avoids
        # accidentally indexing every use of a constant.
        if "const" not in classes or not binding.startswith("const-") or not anchor:
            return
        qualified = binding[len("const-") :]
        if qualified:
            self.constants.append((qualified, anchor))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)


def _page_url(index_path: Path, site_dir: Path) -> str:
    parent = index_path.relative_to(site_dir).parent.as_posix()
    return "index.html" if parent == "." else parent.rstrip("/") + "/"


def build_verso_manifest(site_dir: Path, artifact_token: str) -> dict[str, Any]:
    """Build a JSON-ready definition/module map from a rendered Verso site."""

    site_dir = Path(site_dir).resolve()
    constants: dict[str, dict[str, str]] = {}
    modules: dict[str, dict[str, str]] = {}
    for page in sorted(site_dir.rglob("index.html")):
        if not page.is_file():
            continue
        try:
            parser = _VersoPageParser()
            parser.feed(page.read_text(encoding="utf-8", errors="replace"))
            parser.close()
        except OSError:
            continue
        url = _page_url(page, site_dir)
        fallback_module = ".".join(page.relative_to(site_dir).parent.parts)
        module = parser.title or fallback_module
        if module:
            modules.setdefault(module, {"url": url})
        for qualified, anchor in parser.constants:
            constants.setdefault(qualified, {"url": url, "anchor": anchor})
    return {
        "artifact_token": artifact_token,
        "root_url": f"/verso/{artifact_token}/",
        "constants": constants,
        "modules": modules,
    }


def safe_artifact_path(site_dir: Path, raw_path: str) -> Optional[Path]:
    """Resolve one URL path to a contained regular file, or return ``None``.

    Backslashes and all dot segments are rejected instead of normalized.  The
    final resolved-path check also blocks symlinks in a compromised artifact
    directory from escaping the immutable site root.
    """

    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return None
    if re.search(r"%(?![0-9A-Fa-f]{2})", raw_path):
        return None
    try:
        decoded = unquote(raw_path, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    if "\\" in decoded or decoded.startswith("/"):
        return None
    raw_parts = decoded.split("/")
    if decoded.endswith("/"):
        raw_parts.pop()
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        return None
    pure = PurePosixPath(*raw_parts)
    root = Path(site_dir).resolve()
    candidate = root.joinpath(*pure.parts)
    if decoded.endswith("/"):
        candidate /= "index.html"
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


Runner = Callable[..., subprocess.CompletedProcess[str]]
SourceTokenFn = Callable[[Path], str]


def _lake_environment(
    environ: Optional[dict[str, str]] = None, *, home: Optional[Path] = None
) -> dict[str, str]:
    """Return an environment that can find elan shims in GUI services.

    macOS LaunchAgents commonly receive a minimal PATH even though Lean is
    correctly installed for the user.  Add the conventional elan bin directory
    without replacing any configured PATH entries; CLI and test environments
    therefore keep their normal executable resolution.
    """

    env = dict(os.environ if environ is None else environ)
    elan_bin = (home or Path.home()) / ".elan" / "bin"
    try:
        has_lake = (elan_bin / "lake").is_file()
    except OSError:
        has_lake = False
    if has_lake:
        entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
        if str(elan_bin) not in entries:
            env["PATH"] = os.pathsep.join([str(elan_bin), *entries])
    return env


class VersoArtifactManager:
    """Asynchronous, single-flight Verso artifact builder for one repository."""

    def __init__(
        self,
        repo_dir: Path,
        *,
        runner: Optional[Runner] = None,
        source_token_fn: Optional[SourceTokenFn] = None,
        root: Optional[Path] = None,
    ) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self.root = Path(root).resolve() if root is not None else (
            self.repo_dir / DEFAULT_ROOT_RELPATH
        )
        self._check_lake = runner is None
        self._runner: Runner = runner or subprocess.run
        self._process_env = _lake_environment()
        self._source_token_fn = source_token_fn or definition_source_token
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._building_token: Optional[str] = None
        self._pending_token: Optional[str] = None
        self._last_token: Optional[str] = None
        self._failed_token: Optional[str] = None
        self._last_error: Optional[str] = None
        self._project: Optional[VersoProject] = None
        self._project_signature: Optional[str] = None
        self._unavailable: Optional[str] = None
        self._locking_unavailable = _fcntl is None

        if self._locking_unavailable:
            self._unavailable = (
                "Verso artifact builds currently require POSIX flock; Windows "
                "uses the immediate raw-source fallback because inherited "
                "Windows byte locks cannot protect orphaned Lake processes "
                "across a deck relaunch"
            )
        else:
            self._refresh_project(self._dependency_signature())
            self._cleanup_abandoned_work()
        self._load_state()

    @property
    def project(self) -> Optional[VersoProject]:
        return self._project

    def _artifact_dir(self, token: str) -> Path:
        return self.root / "artifacts" / token

    def _artifact_valid(self, token: Optional[str]) -> bool:
        if not token or not _TOKEN_RE.fullmatch(token):
            return False
        artifact = self._artifact_dir(token)
        try:
            artifact_root = (self.root / "artifacts").resolve()
            resolved_artifact = artifact.resolve(strict=True)
            resolved_artifact.relative_to(artifact_root)
            site = (artifact / "site").resolve(strict=True)
            manifest = (artifact / _MANIFEST_FILE).resolve(strict=True)
            metadata = (artifact / _METADATA_FILE).resolve(strict=True)
            for path in (site, manifest, metadata):
                path.relative_to(resolved_artifact)
            return site.is_dir() and manifest.is_file() and metadata.is_file()
        except (OSError, ValueError):
            return False

    def _load_state(self) -> None:
        try:
            data = json.loads((self.root / _STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            data = {}
        token = data.get("artifact_token")
        if isinstance(token, str) and self._artifact_valid(token):
            self._last_token = token
        failed = data.get("failed_token")
        error = data.get("error")
        if isinstance(failed, str) and _TOKEN_RE.fullmatch(failed):
            self._failed_token = failed
            self._last_error = str(error)[:_MAX_ERROR_CHARS] if error else None
        if self._last_token is not None:
            return
        artifacts = self.root / "artifacts"
        try:
            candidates = sorted(
                (path for path in artifacts.iterdir() if self._artifact_valid(path.name)),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            candidates = []
        if candidates:
            self._last_token = candidates[0].name

    def _write_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = {
            "artifact_token": self._last_token,
            "failed_token": self._failed_token,
            "error": self._last_error,
            "updated_at": time.time(),
        }
        path = self.root / _STATE_FILE
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _refresh_project(
        self, dependency_signature: str
    ) -> Optional[VersoProject]:
        """Return project metadata paired with one verified config signature."""

        if self._locking_unavailable:
            return None
        if self._check_lake and shutil.which(
            "lake", path=self._process_env.get("PATH")
        ) is None:
            with self._lock:
                self._project = None
                self._project_signature = None
                self._unavailable = "the `lake` executable is not available"
            return None
        with self._lock:
            if (
                self._project_signature == dependency_signature
                and (self._project is not None or self._unavailable is not None)
            ):
                return self._project
        try:
            project = discover_verso_project(self.repo_dir)
            unavailable = None
        except VersoUnavailable as exc:
            project = None
            unavailable = str(exc)
        with self._lock:
            # Compare-and-set against the filesystem while serialized with
            # competing discoveries.  A slow B discovery must never overwrite
            # a newer C project after the worktree configuration moves on.
            if self._dependency_signature() != dependency_signature:
                return None
            self._project = project
            self._project_signature = dependency_signature
            self._unavailable = unavailable
            return project

    @contextmanager
    def _build_lock(self, *, blocking: bool) -> Iterator[Optional[int]]:
        """Serialize the shared overlay across deck processes.

        The descriptor is passed into every Lake subprocess.  If the Python
        deck is killed during a relaunch, the still-running Lake process keeps
        the flock until it exits, preventing the replacement deck from racing
        its literate outputs.
        """

        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / _BUILD_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            if _fcntl is None:  # defensive if capability changes after init
                raise VersoUnavailable(
                    "POSIX flock is required for crash-safe Verso builds"
                )
            flags = _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB)
            try:
                _fcntl.flock(fd, flags)
            except BlockingIOError:
                yield None
                return
            acquired = True
            yield fd
        finally:
            if acquired:
                try:
                    if _fcntl is not None:
                        _fcntl.flock(fd, _fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass

    def _cleanup_abandoned_work(self) -> None:
        """Remove staging directories left by a terminated deck process."""

        with self._build_lock(blocking=False) as lock_fd:
            if lock_fd is None:
                return
            self._cleanup_abandoned_work_unlocked()

    def _cleanup_abandoned_work_unlocked(self) -> None:
        """Remove staging directories while the cross-process lock is held."""

        work_root = self.root / "work"
        try:
            entries = list(work_root.iterdir())
        except OSError:
            return
        for path in entries:
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
            except OSError:
                continue

    def _gc_artifacts(self, current_token: str) -> None:
        """Retain a bounded window for pinned pages in long-lived review tabs."""

        artifacts = self.root / "artifacts"
        try:
            entries = list(artifacts.iterdir())
        except OSError:
            return
        valid: list[Path] = []
        invalid: list[Path] = []
        for path in entries:
            if self._artifact_valid(path.name):
                valid.append(path)
            else:
                invalid.append(path)

        def mtime(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        valid.sort(
            key=mtime,
            reverse=True,
        )
        keep = {current_token}
        # Invalid directories never consume retention slots.  Keeping several
        # generations lets a tab opened before a few rapid Aristotle landings
        # continue fetching its token-pinned pages and hover data.
        for path in valid:
            if path.name != current_token and len(keep) < _ARTIFACT_RETENTION:
                keep.add(path.name)
        for path in [*invalid, *(path for path in valid if path.name not in keep)]:
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
            except OSError:
                continue

    def _source_token(self) -> tuple[Optional[str], Optional[str]]:
        try:
            token = self._source_token_fn(self.repo_dir)
        except Exception as exc:  # fail soft at the dashboard boundary
            return None, f"could not fingerprint current Lean source: {exc}"
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            return None, "the Lean source fingerprint was not a SHA-256 token"
        return token, None

    def _current_identity(
        self,
    ) -> tuple[
        Optional[str], Optional[str], str, Optional[str], Optional[VersoProject]
    ]:
        """Return a mutually consistent source/config/project identity."""

        for _attempt in range(4):
            source_token, token_error = self._source_token()
            dependency_signature = self._dependency_signature()
            project = self._refresh_project(dependency_signature)
            if self._dependency_signature() != dependency_signature:
                continue
            artifact_token = (
                effective_artifact_token(source_token, dependency_signature)
                if source_token is not None
                else None
            )
            return (
                source_token,
                artifact_token,
                dependency_signature,
                token_error,
                project,
            )
        return (
            None,
            None,
            self._dependency_signature(),
            "Lake configuration changed repeatedly while reading it",
            None,
        )

    def _current_artifact_for(
        self, artifact_token: Optional[str]
    ) -> Optional[str]:
        """Return the exact source+configuration artifact, when available."""

        if artifact_token is not None and self._artifact_valid(artifact_token):
            return artifact_token
        return None

    def _status(
        self,
        source_token: Optional[str],
        current_artifact: Optional[str],
        effective_token: Optional[str],
        token_error: Optional[str],
        project: Optional[VersoProject],
    ) -> dict:
        available = self._artifact_valid(self._last_token)
        artifact_token = self._last_token if available else None
        stale = bool(available and artifact_token != current_artifact)
        error: Optional[str] = None
        if self._unavailable:
            state = "unavailable"
            error = self._unavailable
        elif token_error:
            state = "error"
            error = token_error
        elif self._building_token is not None:
            state = "building"
        elif current_artifact is not None:
            state = "ready"
            available = True
            artifact_token = current_artifact
            stale = False
        elif effective_token is not None and self._failed_token == effective_token:
            state = "error"
            error = self._last_error or "the Verso build failed"
        else:
            state = "stale"
            stale = True
        return {
            "state": state,
            "available": available,
            "stale": stale,
            "source_token": source_token,
            "artifact_token": artifact_token,
            "error": error,
            "verso_tag": project.verso_tag if project else None,
            "library": project.library if project else None,
        }

    def status(self, *, trigger: bool = True) -> dict:
        """Return honest JSON-ready state, optionally scheduling a refresh."""

        if trigger:
            return self.trigger()
        source_token, effective_token, _dependency, token_error, project = (
            self._current_identity()
        )
        with self._lock:
            current_artifact = self._current_artifact_for(effective_token)
            if current_artifact is not None:
                self._last_token = current_artifact
            return self._status(
                source_token,
                current_artifact,
                effective_token,
                token_error,
                project,
            )

    def trigger(self, *, force: bool = False) -> dict:
        """Schedule current source, coalescing requests and sticky failures.

        Status polling must not turn one persistent elaboration error into an
        endless series of 45-minute builds.  A changed source/config identity
        naturally gets a new effective token; callers may also explicitly
        request one retry with ``force=True``.
        """

        source_token, effective_token, _dependency, token_error, project = (
            self._current_identity()
        )
        thread: Optional[threading.Thread] = None
        with self._lock:
            if (
                self._unavailable
                or token_error
                or source_token is None
                or effective_token is None
                or self._stop.is_set()
            ):
                return self._status(
                    source_token, None, effective_token, token_error, project
                )
            current_artifact = self._current_artifact_for(effective_token)
            if current_artifact is not None:
                if (
                    self._last_token != current_artifact
                    or self._failed_token is not None
                ):
                    self._last_token = current_artifact
                    self._failed_token = None
                    self._last_error = None
                    self._write_state()
                return self._status(
                    source_token,
                    current_artifact,
                    effective_token,
                    None,
                    project,
                )
            if self._failed_token == effective_token and not force:
                return self._status(
                    source_token, None, effective_token, None, project
                )
            if self._building_token is not None:
                self._pending_token = (
                    None
                    if self._building_token == effective_token
                    else effective_token
                )
                return self._status(
                    source_token, None, effective_token, None, project
                )
            self._building_token = effective_token
            thread = threading.Thread(
                target=self._worker,
                args=(effective_token,),
                name="marathon-verso-build",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self._status(source_token, None, effective_token, None, project)

    def _worker(self, first_token: str) -> None:
        token: Optional[str] = first_token
        while token is not None and not self._stop.is_set():
            try:
                self._build(token)
            except _VersoSourceChanged as exc:
                # Do not publish a semantically valid site under the wrong
                # source token or report ordinary concurrent edits as a build
                # failure.  The same single worker immediately catches up.
                with self._lock:
                    self._pending_token = exc.source_token
            except Exception as exc:  # state must survive every build failure
                message = str(exc).strip() or type(exc).__name__
                with self._lock:
                    self._failed_token = token
                    self._last_error = message[-_MAX_ERROR_CHARS:]
                    try:
                        self._write_state()
                    except OSError:
                        pass
            with self._lock:
                pending = self._pending_token
                self._pending_token = None
                if pending and pending != token and not self._artifact_valid(pending):
                    token = pending
                    self._building_token = pending
                else:
                    token = None
                    self._building_token = None
                    self._thread = None
        with self._lock:
            if self._thread is threading.current_thread():
                self._pending_token = None
                self._building_token = None
                self._thread = None

    def _dependency_signature(self) -> str:
        digest = hashlib.sha256()
        for name in (
            "lean-toolchain",
            "lakefile.toml",
            "lakefile.lean",
            "lake-manifest.json",
        ):
            path = self.repo_dir / name
            digest.update(name.encode("utf-8") + b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<missing>")
            digest.update(b"\0")
        return digest.hexdigest()

    def _current_modules(self, project: VersoProject) -> list[str]:
        """Return every current nonignored module owned by the selected lib."""

        from marathon.deck.cards import _tracked_lean_files
        from marathon.formalization import module_from_file_path

        source_root = (self.repo_dir / project.source_dir).resolve()
        try:
            source_root.relative_to(self.repo_dir)
        except ValueError:
            return []
        modules: set[str] = set()
        for path in _tracked_lean_files(self.repo_dir):
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self.repo_dir)
                relative = resolved.relative_to(source_root)
            except (OSError, ValueError):
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            module = module_from_file_path(relative.as_posix())
            if module is None:
                continue
            if any(module == root or module.startswith(root + ".") for root in project.roots):
                modules.add(module)
        return sorted(modules)

    @staticmethod
    def _module_batches(project: VersoProject, modules: list[str]) -> list[list[str]]:
        targets = [
            f"@{project.package}/+{module}:literate" for module in modules
        ]
        return [
            targets[index : index + _MODULE_TARGET_BATCH_SIZE]
            for index in range(0, len(targets), _MODULE_TARGET_BATCH_SIZE)
        ]

    def _write_overlay(
        self, project: Optional[VersoProject] = None
    ) -> tuple[bool, str]:
        project = project or self._project
        assert project is not None
        self.root.mkdir(parents=True, exist_ok=True)
        # Lake dependency post-update hooks assume the conventional relative
        # `.lake/packages` path.  Point that conventional path at the reviewed
        # project's existing package cache instead of configuring packagesDir
        # directly (which breaks mathlib's cache hook), while keeping all new
        # state under the already-ignored `.lake` tree.
        shared_packages = self.repo_dir / ".lake" / "packages"
        shared_packages.mkdir(parents=True, exist_ok=True)
        overlay_lake = self.root / ".lake"
        overlay_lake.mkdir(parents=True, exist_ok=True)
        package_link = overlay_lake / "packages"
        expected = os.path.relpath(shared_packages, package_link.parent)
        link_matches = False
        if package_link.is_symlink():
            try:
                link_matches = os.readlink(package_link) == expected
            except OSError:
                pass
        if not link_matches:
            if package_link.is_symlink() or package_link.is_file():
                package_link.unlink()
            elif package_link.exists():
                shutil.rmtree(package_link)
            package_link.symlink_to(expected, target_is_directory=True)
        lakefile = (
            'name = "marathon_verso"\n'
            'version = "0.1.0"\n'
            'reservoir = false\n'
            'packagesDir = ".lake/packages"\n\n'
            '[[require]]\n'
            f"name = {json.dumps(project.package)}\n"
            'path = "../.."\n\n'
            '[[require]]\n'
            'name = "verso"\n'
            'git = "https://github.com/leanprover/verso.git"\n'
            f"rev = {json.dumps(project.verso_tag)}\n"
        )
        changed = False
        for path, content in (
            (self.root / "lakefile.toml", lakefile),
            (self.root / "lean-toolchain", project.toolchain + "\n"),
        ):
            try:
                old = path.read_text(encoding="utf-8")
            except OSError:
                old = None
            if old != content:
                path.write_text(content, encoding="utf-8")
                changed = True
        signature = self._dependency_signature()
        try:
            previous_signature = (
                self.root / _DEPENDENCY_SIGNATURE_FILE
            ).read_text(encoding="ascii").strip()
        except OSError:
            previous_signature = ""
        needs_update = (
            changed
            or not (self.root / "lake-manifest.json").is_file()
            or previous_signature != signature
        )
        return needs_update, signature

    def _run_command(
        self, command: list[str], timeout: int, *, lock_fd: int
    ) -> None:
        kwargs: dict[str, Any] = {
            "cwd": str(self.root),
            "capture_output": True,
            "text": True,
            "check": False,
            "timeout": timeout,
            # Lake 5 no longer exposes the old `-j` option.  Its jobs run on
            # Lean's task pool, so bound it to keep the live deck responsive.
            "env": {**self._process_env, "LEAN_NUM_THREADS": _LEAN_BUILD_THREADS},
            # The inherited POSIX descriptor keeps the flock alive if the
            # deck is killed while Lake continues during a relaunch.
            "pass_fds": (lock_fd,),
        }
        try:
            result = self._runner(command, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise VersoBuildError(
                f"`{' '.join(command[:3])}` timed out after {timeout} seconds"
            ) from exc
        except OSError as exc:
            raise VersoBuildError(f"could not run `{command[0]}`: {exc}") from exc
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout or "no subprocess output").strip()
        raise VersoBuildError(
            f"`{' '.join(command)}` exited {result.returncode}: {detail[-_MAX_ERROR_CHARS:]}"
        )

    def _prune_literate(
        self, literate: Path, project: Optional[VersoProject] = None
    ) -> None:
        """Remove only outputs that cannot describe current project source.

        The literate facet is incremental and expensive.  Keeping valid JSON
        lets Lake rebuild only changed modules; pruning the zero-byte and
        source-less cases prevents interrupted builds and deleted/renamed
        modules from leaking into the next rendered site.
        """

        project = project or self._project
        assert project is not None
        source_root = (self.repo_dir / project.source_dir).resolve()
        try:
            source_root.relative_to(self.repo_dir)
        except ValueError:
            # A source directory outside the reviewed repository is not part of
            # Marathon's current-source identity and must not justify an old
            # semantic artifact either.
            source_root = self.repo_dir
        try:
            json_files = list(literate.rglob("*.json"))
        except OSError:
            return
        for path in json_files:
            remove = False
            try:
                remove = path.stat().st_size == 0
                relative = path.relative_to(literate).with_suffix(".lean")
                candidate = (source_root / relative).resolve(strict=True)
                candidate.relative_to(self.repo_dir)
                if not candidate.is_file():
                    remove = True
                if remove:
                    path.unlink()
            except (OSError, ValueError):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _build(self, token: str) -> None:
        with self._build_lock(blocking=True) as lock_fd:
            assert lock_fd is not None
            if self._stop.is_set():
                return
            self._cleanup_abandoned_work_unlocked()

            (
                source_token,
                effective_token,
                expected_dependency,
                token_error,
                project,
            ) = self._current_identity()
            if token_error:
                raise VersoBuildError(token_error)
            if effective_token != token:
                if effective_token is not None:
                    raise _VersoSourceChanged(effective_token)
                raise VersoBuildError("could not identify current Verso source")
            if self._artifact_valid(token):
                with self._lock:
                    self._last_token = token
                    self._failed_token = None
                    self._last_error = None
                    self._write_state()
                return
            if project is None:
                raise VersoUnavailable(self._unavailable or "Verso is unavailable")

            needs_update, dependency_signature = self._write_overlay(project)
            if dependency_signature != expected_dependency:
                raise _VersoSourceChanged(
                    effective_artifact_token(source_token or "", dependency_signature)
                )
            if needs_update:
                self._run_command(
                    ["lake", "update"],
                    _BUILD_TIMEOUT_SECONDS,
                    lock_fd=lock_fd,
                )
                if not (self.root / "lake-manifest.json").is_file():
                    raise VersoBuildError(
                        "lake update produced no lake-manifest.json"
                    )
                (self.root / _DEPENDENCY_SIGNATURE_FILE).write_text(
                    dependency_signature + "\n", encoding="ascii"
                )

            modules = self._current_modules(project)
            if not modules:
                raise VersoBuildError(
                    "the selected Lean library has no current source modules"
                )
            literate = self.root / ".lake" / "build" / "literate"
            self._prune_literate(literate, project)
            batches = self._module_batches(project, modules)
            first_batch = batches.pop(0) if batches else []
            self._run_command(
                ["lake", "build", f"{project.library}:literate", *first_batch],
                _BUILD_TIMEOUT_SECONDS,
                lock_fd=lock_fd,
            )
            for batch in batches:
                self._run_command(
                    ["lake", "build", *batch],
                    _BUILD_TIMEOUT_SECONDS,
                    lock_fd=lock_fd,
                )

            self._prune_literate(literate, project)
            if not any(path.is_file() for path in literate.rglob("*.json")):
                raise VersoBuildError(
                    "the literate facets produced no module JSON files"
                )

            work = self.root / "work" / f"{token}-{uuid.uuid4().hex}"
            site = work / "site"
            work.mkdir(parents=True, exist_ok=False)
            try:
                self._run_command(
                    [
                        "lake",
                        "exe",
                        "verso-html",
                        ".lake/build/literate",
                        str(site),
                    ],
                    _RENDER_TIMEOUT_SECONDS,
                    lock_fd=lock_fd,
                )
                manifest = build_verso_manifest(site, token)
                if not manifest["modules"]:
                    raise VersoBuildError("verso-html produced no module pages")
                (work / _MANIFEST_FILE).write_text(
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                (work / _METADATA_FILE).write_text(
                    json.dumps(
                        {
                            "artifact_token": token,
                            "source_token": source_token,
                            "dependency_signature": dependency_signature,
                            "library": project.library,
                            "verso_tag": project.verso_tag,
                            "built_at": time.time(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )

                (
                    _source,
                    current_effective,
                    _dependency,
                    current_error,
                    _project,
                ) = self._current_identity()
                if current_effective != token:
                    if current_effective is not None:
                        raise _VersoSourceChanged(current_effective)
                    raise VersoBuildError(
                        current_error
                        or "could not verify source identity after rendering"
                    )

                destination = self._artifact_dir(token)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_symlink() or destination.is_file():
                    destination.unlink()
                elif destination.exists():
                    shutil.rmtree(destination)
                os.replace(work, destination)
                if not self._artifact_valid(token):
                    raise VersoBuildError(
                        "the published Verso artifact was incomplete"
                    )
                with self._lock:
                    if not self._stop.is_set():
                        self._last_token = token
                        self._failed_token = None
                        self._last_error = None
                        self._write_state()
                        self._gc_artifacts(token)
            finally:
                if work.exists():
                    shutil.rmtree(work, ignore_errors=True)

    def manifest(self, *, trigger: bool = True) -> Optional[dict]:
        """Return the last successful manifest, including during a refresh."""

        if trigger:
            self.trigger()
        with self._lock:
            token = self._last_token
        if not self._artifact_valid(token):
            return None
        try:
            data = json.loads(
                (self._artifact_dir(token) / _MANIFEST_FILE).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    def resolve_artifact(
        self, raw_path: str, *, artifact_token: Optional[str] = None
    ) -> Optional[Path]:
        """Resolve a file below one retained immutable site.

        Omitting ``artifact_token`` preserves the original last-good API.
        Token-pinned URLs let a page and all of its lazy hover assets continue
        to come from the same artifact while a newer site is published.
        """

        with self._lock:
            token = artifact_token or self._last_token
        if not self._artifact_valid(token):
            return None
        return safe_artifact_path(self._artifact_dir(token) / "site", raw_path)

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Wait for all currently scheduled builds (primarily for tests/CLI)."""

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                thread = self._thread
            if thread is None:
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            thread.join(remaining)

    def shutdown(self, *, wait: bool = False) -> None:
        """Prevent queued refreshes; optionally wait for the active subprocess."""

        self._stop.set()
        with self._lock:
            self._pending_token = None
            thread = self._thread
        if wait and thread is not None:
            thread.join()

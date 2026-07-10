"""Bulk-create and bulk-refresh review sub-issues from a drafts file.

Three modes:

* :func:`approve_chapter_drafts` — the deck's durable Add Chapter transaction.
  It creates and parent-attaches every reviewed draft exactly once, resumes
  partial GitHub success from a per-entry journal, and registers only after a
  final remote attachment check.

* :func:`create_subissues_from_drafts` — for first-time setup of a
  chapter. Reads ``drafts/<Chapter>.md``, creates one GitHub issue per
  draft section via ``gh issue create``, then attaches each as a
  sub-issue of the parent via the sub_issues REST endpoint.

* :func:`refresh_subissue_bodies` — for keeping existing issues in sync
  with a hand-edited drafts file. Walks the chapter's registry, reads
  the corresponding section from drafts, and ``gh issue edit`` the
  body in place. Issue history (comments, labels, sub-issue parent
  link) is preserved.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

try:  # POSIX advisory locks (Linux/macOS).
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX hosts
    _fcntl = None

try:  # Windows advisory byte-range locks.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised on non-Windows hosts
    _msvcrt = None

from marathon.review.config import (
    RegistryEditError,
    ReviewConfig,
    load_config,
    register_chapter,
)
from marathon.review.drafts import detect_chapter, parse_drafts
from marathon.review.github import gh


REVIEW_LABEL = "review"

# Add Chapter is a distributed transaction across GitHub and the local chapter
# registry.  GitHub does not expose an idempotency key for ``issue create``, so
# each reviewed draft carries a stable, invisible marker and every transition
# is journalled before the corresponding remote write.  A retry can therefore
# recover the exact child after an ambiguous create response instead of opening
# a duplicate.
APPROVAL_SCHEMA_VERSION = 2
APPROVAL_RELDIR = Path(".marathon/review/chapter-approvals")


@dataclass(frozen=True)
class ChapterApprovalResult:
    """A complete, durably registered Add Chapter transaction."""

    chapter: int
    created: list[tuple[int, str, str]]
    resumed: bool = False


class ChapterApprovalError(RuntimeError):
    """Base error surfaced by ``POST /api/approve-chapter``.

    ``created`` contains every durable GitHub issue ID known at the failure
    point, allowing the UI to report partial progress without inviting the user
    to repair or delete issues manually.
    """

    def __init__(
        self,
        message: str,
        *,
        chapter: int,
        created: Sequence[tuple[int, str, str]] = (),
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.chapter = chapter
        self.created = list(created)
        self.retryable = retryable


class ChapterApprovalIncomplete(ChapterApprovalError):
    """A partial/retryable approval; the journal retains remote successes."""


class ChapterApprovalConflict(ChapterApprovalError):
    """The requested reviewed manifest conflicts with durable local state."""


_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _lock_file(handle) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows-only branch
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)


def _unlock_file(handle) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows-only branch
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


@contextmanager
def _stable_lock(path: Path):
    """Serialize a transaction on a stable inode across threads/processes."""
    lock = _thread_lock(path)
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            _lock_file(handle)
            try:
                yield
            finally:
                _unlock_file(handle)


def _approval_dir(cfg: ReviewConfig) -> Path:
    return Path(cfg.repo_dir) / APPROVAL_RELDIR


def _approval_journal_path(cfg: ReviewConfig, chapter: int) -> Path:
    return _approval_dir(cfg) / f"chapter-{chapter}.json"


def _approval_lock_path(cfg: ReviewConfig, chapter: int) -> Path:
    return _approval_dir(cfg) / f"chapter-{chapter}.lock"


def _registry_lock_path(cfg: ReviewConfig) -> Path:
    # Different chapters may create their GitHub children concurrently, but
    # config.toml is one whole-file RMW store and therefore needs a repo-wide
    # critical section for its final registration.
    return _approval_dir(cfg) / "registry.lock"


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _write_journal(path: Path, journal: dict) -> None:
    """Durably atomic-replace one approval journal while its lock is held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(journal, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_parent(path)
    finally:
        tmp.unlink(missing_ok=True)


def _load_journal(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"approval journal {path} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"approval journal {path} is not a JSON object")
    return value


def _body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _entry_marker(
    cfg: ReviewConfig, chapter: int, index: int, title: str, body: str
) -> str:
    identity = json.dumps(
        {
            "repo": cfg.github_repo,
            "parent": cfg.parent_issue,
            "chapter": chapter,
            "index": index,
            "title": title,
            "body_sha256": _body_digest(body),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"<!-- marathon-add-chapter:{digest} -->"


def _reviewed_manifest(
    cfg: ReviewConfig,
    chapter: int,
    drafts: Mapping[int, tuple[str, str]],
) -> list[dict]:
    if not drafts:
        raise ChapterApprovalConflict(
            "cannot approve a chapter with no reviewed drafts",
            chapter=chapter,
            retryable=False,
        )
    out: list[dict] = []
    for raw_index in sorted(drafts):
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ChapterApprovalConflict(
                f"invalid reviewed draft index {raw_index!r}",
                chapter=chapter,
                retryable=False,
            ) from exc
        if index <= 0 or index != raw_index:
            raise ChapterApprovalConflict(
                f"invalid reviewed draft index {raw_index!r}",
                chapter=chapter,
                retryable=False,
            )
        title, body = drafts[raw_index]
        if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
            raise ChapterApprovalConflict(
                f"reviewed draft {index} has an invalid title/body",
                chapter=chapter,
                retryable=False,
            )
        out.append(
            {
                "index": index,
                "title": title,
                # The exact human-reviewed snapshot is part of the durable
                # transaction. Without it, a deck restart would lose the
                # in-memory IntakeResult and force a new (possibly different)
                # Claude draft before the partial batch could resume.
                "body": body,
                "body_sha256": _body_digest(body),
                "marker": _entry_marker(cfg, chapter, index, title, body),
                "phase": "pending",
                "issue": None,
                "url": None,
                "attached": False,
            }
        )
    return out


def _manifest_identity(entries: Sequence[dict]) -> list[dict]:
    keys = ("index", "title", "body_sha256", "marker")
    return [{key: entry.get(key) for key in keys} for entry in entries]


def _new_journal(
    cfg: ReviewConfig, chapter: int, entries: Sequence[dict]
) -> dict:
    return {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "chapter": chapter,
        "github_repo": cfg.github_repo,
        "parent_issue": cfg.parent_issue,
        "entries": [dict(entry) for entry in entries],
        "registered": False,
        "last_error": None,
    }


def _validate_journal(
    cfg: ReviewConfig, chapter: int, expected: Sequence[dict], journal: dict
) -> None:
    raw_entries = journal.get("entries")
    compatible = (
        journal.get("schema_version") == APPROVAL_SCHEMA_VERSION
        and journal.get("chapter") == chapter
        and journal.get("github_repo") == cfg.github_repo
        and journal.get("parent_issue") == cfg.parent_issue
        and isinstance(raw_entries, list)
        and all(isinstance(entry, dict) for entry in raw_entries)
        and all(
            isinstance(entry.get("body"), str)
            and _body_digest(entry["body"]) == entry.get("body_sha256")
            for entry in raw_entries
        )
        and _manifest_identity(raw_entries) == _manifest_identity(expected)
    )
    if not compatible:
        raise ChapterApprovalConflict(
            f"chapter {chapter} has a different durable approval manifest; "
            "refusing to mix reviewed drafts",
            chapter=chapter,
            created=_created_from_journal(journal),
            retryable=False,
        )


def _drafts_from_journal(journal: dict, *, chapter: int) -> dict[int, tuple[str, str]]:
    drafts: dict[int, tuple[str, str]] = {}
    try:
        raw_entries = journal["entries"]
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError("entries must be a non-empty list")
        for entry in raw_entries:
            if not isinstance(entry, dict):
                raise ValueError("entry is not an object")
            index = int(entry["index"])
            title = entry["title"]
            body = entry["body"]
            digest = entry["body_sha256"]
            if (
                index <= 0
                or index in drafts
                or not isinstance(title, str)
                or not title.strip()
                or not isinstance(body, str)
                or _body_digest(body) != digest
            ):
                raise ValueError(f"invalid reviewed draft {index}")
            drafts[index] = (title, body)
    except (KeyError, TypeError, ValueError) as exc:
        raise ChapterApprovalConflict(
            f"chapter {chapter} approval journal has no valid reviewed snapshot",
            chapter=chapter,
            created=_created_from_journal(journal),
            retryable=False,
        ) from exc
    return drafts


def _created_from_journal(journal: Optional[dict]) -> list[tuple[int, str, str]]:
    if not journal:
        return []
    out: list[tuple[int, str, str]] = []
    for entry in journal.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        issue = entry.get("issue")
        url = entry.get("url")
        if issue is not None and url:
            try:
                out.append((int(entry["index"]), str(int(issue)), str(url)))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(out)


def _registry_entries_from_journal(
    journal: dict, *, chapter: int
) -> list[list[object]]:
    entries: list[list[object]] = []
    for entry in journal.get("entries") or []:
        try:
            issue_num = int(entry["issue"])
            title = str(entry["title"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ChapterApprovalConflict(
                f"chapter {chapter} approval journal has an invalid issue record",
                chapter=chapter,
                created=_created_from_journal(journal),
                retryable=False,
            ) from exc
        if issue_num <= 0 or not title:
            raise ChapterApprovalConflict(
                f"chapter {chapter} approval journal has an invalid issue record",
                chapter=chapter,
                created=_created_from_journal(journal),
                retryable=False,
            )
        entries.append([issue_num, title])
    return entries


def _decode_api_pages(raw: str, *, operation: str) -> list[dict]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{operation} returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise RuntimeError(f"{operation} returned a non-list JSON response")
    # ``gh api --paginate --slurp`` returns [page1, page2, ...].  Test doubles
    # and older gh versions may return a single flat page, so accept both.
    if parsed and all(isinstance(page, list) for page in parsed):
        parsed = [item for page in parsed for item in page]
    return [item for item in parsed if isinstance(item, dict)]


def _chapter_label(chapter: int) -> str:
    return f"chapter-{chapter}"


def _ensure_chapter_label(cfg: ReviewConfig, chapter: int) -> None:
    """Create the ``chapter-N`` GitHub label if it doesn't already exist.

    For a BRAND-NEW chapter the label never exists, so create_subissue's
    ``--label chapter-N`` would fail on every draft — the whole batch is silently
    skipped (0 issues, chapter left unregistered). Creating it up front makes the
    Add Chapter flow self-contained. Idempotent + best-effort (label-create
    failures are non-fatal; the worst case is the pre-existing behaviour)."""
    label = _chapter_label(chapter)
    cp = gh(
        "label", "list", "--repo", cfg.github_repo, "--limit", "200",
        "--json", "name", "--jq", ".[].name", check=False,
    )
    existing = set((cp.stdout or "").splitlines()) if cp.returncode == 0 else set()
    if label in existing:
        return
    print(f"  creating missing label {label}")
    gh(
        "label", "create", label, "--repo", cfg.github_repo,
        "--color", "0e8a16", "--description", f"Chapter {chapter}", check=False,
    )


def _create_issue(
    cfg: ReviewConfig,
    title: str,
    body_path: Path,
    chapter: int,
) -> tuple[str, str]:
    """Create only the GitHub issue, returning its durable number + URL.

    Attachment is deliberately separate: Add Chapter journals the returned ID
    before it makes the second remote write, closing the old create-then-attach
    loss window.
    """
    cp = gh(
        "issue", "create",
        "--repo", cfg.github_repo,
        "--title", title,
        "--body-file", str(body_path),
        "--label", REVIEW_LABEL,
        "--label", _chapter_label(chapter),
    )
    lines = (cp.stdout or "").strip().splitlines()
    if not lines:
        raise RuntimeError("gh issue create returned no issue URL")
    url = lines[-1].strip()
    issue_num = url.rstrip("/").rsplit("/", 1)[-1]
    if not issue_num.isdigit():
        raise RuntimeError(f"gh issue create returned an invalid issue URL: {url!r}")
    return issue_num, url


def _attach_subissue(cfg: ReviewConfig, issue_num: str | int) -> None:
    """Attach one existing issue to the configured parent issue."""
    issue_num = str(issue_num)
    id_cp = gh("api", f"/repos/{cfg.github_repo}/issues/{issue_num}", "--jq", ".id")
    child_id = (id_cp.stdout or "").strip()
    if not child_id:
        raise RuntimeError(f"GitHub returned no node ID for issue #{issue_num}")
    gh(
        "api", "-X", "POST",
        f"/repos/{cfg.github_repo}/issues/{cfg.parent_issue}/sub_issues",
        "-F", f"sub_issue_id={child_id}",
    )


def create_subissue(
    cfg: ReviewConfig,
    title: str,
    body_path: Path,
    chapter: int,
) -> tuple[str, str]:
    """Create a single GitHub issue, label it, attach it as a sub-issue.

    Returns ``(issue_number_str, issue_url)``.
    """
    issue_num, url = _create_issue(cfg, title, body_path, chapter)
    _attach_subissue(cfg, issue_num)
    return issue_num, url


def _parent_subissue_numbers(cfg: ReviewConfig) -> set[int]:
    cp = gh(
        "api", "--paginate", "--slurp",
        f"/repos/{cfg.github_repo}/issues/{cfg.parent_issue}/sub_issues?per_page=100",
    )
    items = _decode_api_pages(cp.stdout, operation="listing parent sub-issues")
    out: set[int] = set()
    for item in items:
        try:
            out.add(int(item["number"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _recover_issue_for_marker(
    cfg: ReviewConfig, chapter: int, marker: str
) -> Optional[tuple[str, str]]:
    """Recover an issue after an ambiguous ``issue create`` response.

    REST listing is used instead of GitHub search so recovery does not depend
    on search-index latency.  More than one marker match is a hard conflict:
    silently choosing one would hide an already-duplicated transaction.
    """
    cp = gh(
        "api", "--paginate", "--slurp",
        f"/repos/{cfg.github_repo}/issues?state=all&labels="
        f"{_chapter_label(chapter)}&per_page=100",
    )
    items = _decode_api_pages(cp.stdout, operation="recovering chapter issues")
    matches = [item for item in items if marker in str(item.get("body") or "")]
    if len(matches) > 1:
        nums = ", ".join(f"#{m.get('number')}" for m in matches)
        raise ChapterApprovalConflict(
            f"idempotency marker appears on multiple GitHub issues ({nums}); "
            "refusing to choose one",
            chapter=chapter,
            retryable=False,
        )
    if not matches:
        return None
    match = matches[0]
    try:
        issue_num = str(int(match["number"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("recovered GitHub issue has no valid number") from exc
    url = str(match.get("html_url") or match.get("url") or "")
    if not url:
        url = f"https://github.com/{cfg.github_repo}/issues/{issue_num}"
    return issue_num, url


def _body_with_marker(body: str, marker: str) -> str:
    return body.rstrip() + "\n\n" + marker + "\n"


def _register_completed_chapter(
    cfg: ReviewConfig,
    chapter: int,
    target: Path,
    entries: Sequence[Sequence[object]],
) -> None:
    """Register once under the repo-wide config RMW lock.

    A crash after config replacement but before the approval journal update is
    recovered by comparing the already-registered entries exactly.
    """
    with _stable_lock(_registry_lock_path(cfg)):
        current = load_config(repo_dir=cfg.repo_dir)
        existing = current.chapters.get(chapter)
        expected = [(int(item[0]), str(item[1])) for item in entries]
        if existing is not None:
            if existing.entries != expected:
                raise ChapterApprovalConflict(
                    f"chapter {chapter} is already registered with different "
                    "issues; refusing to overwrite it",
                    chapter=chapter,
                    retryable=False,
                )
            return
        register_chapter(cfg.repo_dir, chapter, target, entries)


def chapter_approval_snapshot(
    cfg: ReviewConfig, chapter: int
) -> Optional[dict]:
    """Return a restart-safe UI snapshot for an interrupted approval.

    ``None`` means no durable transaction. The returned bodies are the exact
    snapshot persisted by the original explicit Approve click; callers must not
    rerun intake and substitute fresh model output while this exists. Completed
    transactions are returned too so Start Intake can fail fast without paying
    for another model run.
    """
    chapter = int(chapter)
    path = _approval_journal_path(cfg, chapter)
    with _stable_lock(_approval_lock_path(cfg, chapter)):
        journal = _load_journal(path)
        if journal is None:
            return None
        drafts = _drafts_from_journal(journal, chapter=chapter)
        expected = _reviewed_manifest(cfg, chapter, drafts)
        _validate_journal(cfg, chapter, expected, journal)
        created = _created_from_journal(journal)
        status = "approval-incomplete"
        if journal.get("registered"):
            registry_entries = _registry_entries_from_journal(
                journal, chapter=chapter
            )
            existing = load_config(repo_dir=cfg.repo_dir).chapters.get(chapter)
            expected_registry = [
                (int(item[0]), str(item[1])) for item in registry_entries
            ]
            if existing is not None and existing.entries == expected_registry:
                status = "approval-complete"
            elif existing is not None:
                raise ChapterApprovalConflict(
                    f"chapter {chapter} is registered with entries different "
                    "from its completed approval journal",
                    chapter=chapter,
                    created=created,
                    retryable=False,
                )
            else:
                # The config entry disappeared after completion. Expose the
                # persisted snapshot as resumable; Approve will freshly verify
                # parent membership before reconstructing the registry.
                journal["registered"] = False
                _write_journal(path, journal)
        return {
            "chapter": chapter,
            "status": status,
            "error": journal.get("last_error"),
            "retryable": status == "approval-incomplete",
            "created": [
                {"index": index, "issue": issue, "url": url}
                for index, issue, url in created
            ],
            "subissues": [
                {"index": index, "title": title, "body": body}
                for index, (title, body) in sorted(drafts.items())
            ],
        }


def approve_chapter_drafts(
    cfg: ReviewConfig,
    chapter: int,
    drafts: Optional[Mapping[int, tuple[str, str]]] = None,
    *,
    target: Optional[Path] = None,
    tmp_dir: Optional[Path] = None,
) -> ChapterApprovalResult:
    """Atomically/resumably approve the exact drafts reviewed in the deck.

    The operation is single-flight per ``(repo, chapter)`` across both request
    threads and deck processes.  Each successful remote transition is fsynced
    to a journal.  Registration is the final step and is attempted only after a
    fresh GitHub read confirms *every* reviewed issue is attached to the parent.

    Retrying the same manifest returns the original issue IDs.  After a deck
    restart, ``drafts=None`` resumes from the exact human-reviewed body snapshot
    in the durable journal. Retrying a different manifest is refused so
    chapters can never splice together issues from two review sessions.
    """
    chapter = int(chapter)
    journal_path = _approval_journal_path(cfg, chapter)
    target = Path(target) if target is not None else cfg.target_path(chapter)
    try:
        target = target.relative_to(cfg.repo_dir)
    except ValueError:
        pass

    with _stable_lock(_approval_lock_path(cfg, chapter)):
        journal = _load_journal(journal_path)
        if drafts is None:
            if journal is None:
                raise ChapterApprovalConflict(
                    f"no ready intake or interrupted approval for chapter {chapter}",
                    chapter=chapter,
                    retryable=False,
                )
            reviewed = _drafts_from_journal(journal, chapter=chapter)
        else:
            reviewed = drafts
        expected = _reviewed_manifest(cfg, chapter, reviewed)
        if journal is None:
            # Preserve the old safety behavior for chapters registered before
            # durable approval journals existed: do not infer ownership of
            # their issues from title/order alone.
            current = load_config(repo_dir=cfg.repo_dir)
            if chapter in current.chapters:
                raise ChapterApprovalConflict(
                    f"chapter {chapter} is already registered — it was already "
                    "approved (skipping to avoid duplicate issues)",
                    chapter=chapter,
                    retryable=False,
                )
            journal = _new_journal(cfg, chapter, expected)
            _write_journal(journal_path, journal)
            resumed = False
        else:
            _validate_journal(cfg, chapter, expected, journal)
            resumed = True

        if journal.get("registered"):
            # A completed transaction is locally idempotent and needs no more
            # GitHub writes (or even reads). The final parent-membership read
            # happened before this bit was durably set. Verify only that the
            # authoritative config still carries the exact journal entries.
            registry_entries = _registry_entries_from_journal(
                journal, chapter=chapter
            )
            existing = load_config(repo_dir=cfg.repo_dir).chapters.get(chapter)
            expected_registry = [
                (int(item[0]), str(item[1])) for item in registry_entries
            ]
            if existing is not None and existing.entries == expected_registry:
                return ChapterApprovalResult(
                    chapter=chapter,
                    created=_created_from_journal(journal),
                    resumed=True,
                )
            if existing is not None:
                raise ChapterApprovalConflict(
                    f"chapter {chapter} is registered with entries different "
                    "from its completed approval journal",
                    chapter=chapter,
                    created=_created_from_journal(journal),
                    retryable=False,
                )
            # Config was removed after completion. Clear the completion bit and
            # re-run the final remote attachment verification before restoring
            # it; never reconstruct config from a stale local claim alone.
            journal["registered"] = False
            _write_journal(journal_path, journal)

        entries_by_index = {entry["index"]: entry for entry in journal["entries"]}
        bodies = {
            int(index): body for index, (_title, body) in reviewed.items()
        }

        try:
            _ensure_chapter_label(cfg, chapter)
            with tempfile.TemporaryDirectory(
                prefix=f"marathon-approve-c{chapter}-",
                dir=str(tmp_dir) if tmp_dir is not None else None,
            ) as temp_name:
                temp_root = Path(temp_name)
                for index in sorted(entries_by_index):
                    entry = entries_by_index[index]

                    if not entry.get("issue") and entry.get("phase") == "creating":
                        recovered = _recover_issue_for_marker(
                            cfg, chapter, str(entry["marker"])
                        )
                        if recovered is not None:
                            entry["issue"], entry["url"] = recovered
                            entry["phase"] = "created"
                            entry["recovery_misses"] = 0
                            _write_journal(journal_path, journal)
                        else:
                            # A lost create response is ambiguous. Even the
                            # repository REST list can be briefly stale, so the
                            # first marker miss is NEVER permission to create a
                            # second issue. Persist the miss and ask for another
                            # retry; only two distinct reads with no match let a
                            # known-failed create be attempted again.
                            misses = int(entry.get("recovery_misses") or 0) + 1
                            entry["recovery_misses"] = misses
                            _write_journal(journal_path, journal)
                            if misses < 2:
                                raise RuntimeError(
                                    f"GitHub has not exposed the idempotency "
                                    f"marker for draft {index} yet; retry to "
                                    "recheck before any new issue is created"
                                )
                            entry["phase"] = "pending"
                            _write_journal(journal_path, journal)

                    if not entry.get("issue"):
                        # Persist intent before the non-idempotent remote call.
                        # If the response is lost, the marker recovers the exact
                        # child on the next attempt.
                        entry["phase"] = "creating"
                        entry["recovery_misses"] = 0
                        journal["last_error"] = None
                        _write_journal(journal_path, journal)
                        body_path = temp_root / f"draft-{index}-body.md"
                        body_path.write_text(
                            _body_with_marker(bodies[index], str(entry["marker"])),
                            encoding="utf-8",
                        )
                        issue_num, url = _create_issue(
                            cfg, str(entry["title"]), body_path, chapter
                        )
                        entry["issue"] = issue_num
                        entry["url"] = url
                        entry["phase"] = "created"
                        _write_journal(journal_path, journal)

                    if not entry.get("attached"):
                        # An ``attaching`` phase means a prior POST may have
                        # succeeded without its response reaching us.  Read the
                        # parent first; never blindly send a duplicate attach.
                        if entry.get("phase") == "attaching":
                            parent_nums = _parent_subissue_numbers(cfg)
                            if int(entry["issue"]) in parent_nums:
                                entry["attached"] = True
                                entry["phase"] = "attached"
                                _write_journal(journal_path, journal)
                        if not entry.get("attached"):
                            entry["phase"] = "attaching"
                            _write_journal(journal_path, journal)
                            _attach_subissue(cfg, str(entry["issue"]))
                            entry["attached"] = True
                            entry["phase"] = "attached"
                            _write_journal(journal_path, journal)

            # Do not trust the POST responses alone: registration is gated on a
            # fresh read of the parent's actual durable sub-issue collection.
            attached = _parent_subissue_numbers(cfg)
            missing = [
                entry for entry in journal["entries"]
                if int(entry["issue"]) not in attached
            ]
            if missing:
                for entry in missing:
                    entry["attached"] = False
                    entry["phase"] = "created"
                _write_journal(journal_path, journal)
                nums = ", ".join(f"#{entry['issue']}" for entry in missing)
                raise RuntimeError(
                    f"parent issue #{cfg.parent_issue} does not contain {nums}"
                )

            registry_entries = _registry_entries_from_journal(
                journal, chapter=chapter
            )
            _register_completed_chapter(
                cfg, chapter, target, registry_entries
            )
            journal["registered"] = True
            journal["last_error"] = None
            _write_journal(journal_path, journal)
            return ChapterApprovalResult(
                chapter=chapter,
                created=_created_from_journal(journal),
                resumed=resumed,
            )
        except ChapterApprovalConflict:
            raise
        except (OSError, RegistryEditError, RuntimeError) as exc:
            journal["last_error"] = str(exc)
            _write_journal(journal_path, journal)
            raise ChapterApprovalIncomplete(
                f"chapter {chapter} approval is incomplete and can be retried: {exc}",
                chapter=chapter,
                created=_created_from_journal(journal),
                retryable=True,
            ) from exc


def create_subissues_from_drafts(
    cfg: ReviewConfig,
    drafts_path: Path,
    skip: Optional[set[int]] = None,
    tmp_dir: Path = Path("/tmp"),
) -> list[tuple[int, str, str]]:
    """Walk the drafts file in entry order and create a sub-issue per
    entry. Skips entry numbers in ``skip``.

    Returns ``[(entry_idx, issue_num, url), ...]`` for entries that
    were successfully created (skipped + failed entries omitted).
    """
    skip = skip or set()
    chapter = detect_chapter(drafts_path)
    _ensure_chapter_label(cfg, chapter)  # a new chapter has no label yet → create it
    text = drafts_path.read_text()
    drafts = parse_drafts(text)
    print(f"Found {len(drafts)} draft sections in {drafts_path}")
    print(f"Chapter label: {_chapter_label(chapter)}")
    print()

    created: list[tuple[int, str, str]] = []
    # A unique directory per invocation prevents two concurrent chapters with
    # the same draft index from overwriting /tmp/review-draft-1-body.md between
    # write and ``gh issue create --body-file``.
    with tempfile.TemporaryDirectory(
        prefix=f"marathon-subissues-c{chapter}-", dir=str(tmp_dir)
    ) as temp_name:
        temp_root = Path(temp_name)
        for n in sorted(drafts.keys()):
            title, body = drafts[n]
            if n in skip:
                print(f"[{n:2d}] SKIP (--skip)")
                continue
            body_path = temp_root / f"review-draft-{n}-body.md"
            body_path.write_text(body, encoding="utf-8")
            print(f"[{n:2d}] creating: {title[:80]}")
            try:
                issue_num, url = create_subissue(cfg, title, body_path, chapter)
            except RuntimeError as e:
                print(f"  ❌ {e}")
                continue
            print(f"  ✅ #{issue_num} ({url})")
            created.append((n, issue_num, url))

    print()
    print(f"Done. Sub-issues of #{cfg.parent_issue}:")
    list_cp = gh(
        "api",
        f"/repos/{cfg.github_repo}/issues/{cfg.parent_issue}/sub_issues",
        "--jq", '.[] | "#" + (.number|tostring) + "  " + .title',
    )
    print(list_cp.stdout)
    return created


def refresh_subissue_bodies(
    cfg: ReviewConfig,
    drafts_path: Path,
    only: Optional[set[int]] = None,
    tmp_dir: Path = Path("/tmp"),
) -> tuple[int, int]:
    """Refresh existing sub-issue bodies from the drafts file.

    Walks the chapter's registry (``cfg.chapter_registry(chapter)``),
    finds each entry's section in the drafts, and edits the GitHub
    issue body in place.

    ``only`` (1-based entry indices), if provided, restricts the refresh
    to that subset.

    Returns ``(refreshed_count, skipped_count)``.
    """
    chapter = detect_chapter(drafts_path)
    registry = cfg.chapter_registry(chapter)
    drafts = parse_drafts(drafts_path.read_text())

    refreshed = 0
    skipped = 0
    with tempfile.TemporaryDirectory(
        prefix=f"marathon-refresh-c{chapter}-", dir=str(tmp_dir)
    ) as temp_name:
        temp_root = Path(temp_name)
        for entry_idx, (issue_num, _pattern) in enumerate(
            registry.entries, start=1
        ):
            if only and entry_idx not in only:
                skipped += 1
                continue
            if entry_idx not in drafts:
                print(
                    f"  entry {entry_idx} (#{issue_num}): no draft section "
                    "found, skipping"
                )
                skipped += 1
                continue
            title, body = drafts[entry_idx]
            body_path = temp_root / f"review-refresh-{issue_num}-body.md"
            body_path.write_text(body, encoding="utf-8")
            print(f"  refresh #{issue_num} (entry {entry_idx}): {title[:60]}")
            cp = gh(
                "issue", "edit", str(issue_num),
                "--repo", cfg.github_repo,
                "--body-file", str(body_path),
                check=False,
            )
            if cp.returncode != 0:
                print(f"    ❌ gh edit failed: {cp.stderr.strip()}")
                continue
            refreshed += 1
            print(f"    ✅ updated")

    print(f"\nDone. Refreshed: {refreshed}. Skipped: {skipped}.")
    return refreshed, skipped

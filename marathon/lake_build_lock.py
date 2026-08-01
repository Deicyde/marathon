"""Crash-safe coordination for Lake builds that share one repository.

Lake's traces make a second build cheap only after the first build has
finished publishing them.  Two independent Marathon services otherwise see
the same missing/stale traces and elaborate the same modules concurrently.

The lock lives beside the shared ``.lake/build`` directory, not in a
service-specific runtime directory.  Callers should pass the yielded file
descriptor to Lake via ``subprocess(..., pass_fds=(lock_fd,))``.  If the
Python parent is terminated while Lake is still running, the inherited
descriptor keeps the kernel lock held until the Lake process exits.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

try:  # POSIX advisory locks
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows has no inherited flock
    _fcntl = None


LAKE_BUILD_LOCK_RELPATH = Path(".lake") / "marathon-build.lock"


class LakeBuildLockUnavailable(RuntimeError):
    """The platform cannot provide Marathon's crash-safe Lake build lock."""


class LakeBuildLockTimeout(TimeoutError):
    """The shared Lake build lock was not acquired before its deadline."""


class LakeBuildLockCancelled(RuntimeError):
    """The caller cancelled while waiting for the shared Lake build lock."""


def lake_build_lock_supported() -> bool:
    """Whether inherited POSIX advisory locks are available."""

    return _fcntl is not None


def lake_build_lock_path(repo_dir: str | Path) -> Path:
    """Canonical lock path for the repository's shared Lake build tree."""

    return Path(repo_dir).resolve() / LAKE_BUILD_LOCK_RELPATH


@contextmanager
def lake_build_lock(
    repo_dir: str | Path,
    *,
    blocking: bool = True,
    timeout: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[int | None]:
    """Hold the shared Lake build lock for *repo_dir*.

    A nonblocking caller receives ``None`` when another process owns the
    lock.  A blocking caller polls so its wait can be bounded by ``timeout``
    and interrupted by ``cancelled``.  Closing the yielded descriptor releases
    the lock unless an in-flight child inherited it, which is the intended
    crash-safe behavior.
    """

    if _fcntl is None:
        raise LakeBuildLockUnavailable(
            "POSIX flock is required for crash-safe shared Lake builds"
        )

    path = lake_build_lock_path(repo_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise LakeBuildLockUnavailable(
            f"could not open shared Lake build lock {path}: {exc}"
        ) from exc
    acquired = False
    try:
        deadline = (
            None if timeout is None else time.monotonic() + max(0.0, timeout)
        )
        while not acquired:
            if cancelled is not None and cancelled():
                raise LakeBuildLockCancelled(
                    f"cancelled while waiting for shared Lake build lock {path}"
                )
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except BlockingIOError:
                if not blocking:
                    yield None
                    return
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LakeBuildLockTimeout(
                            f"timed out waiting for shared Lake build lock {path}"
                        )
                    time.sleep(min(0.1, remaining))
                else:
                    time.sleep(0.1)
            except OSError as exc:
                raise LakeBuildLockUnavailable(
                    f"could not acquire shared Lake build lock {path}: {exc}"
                ) from exc
            else:
                acquired = True
        yield fd
    finally:
        # Do not issue LOCK_UN: an inherited child shares this open-file
        # description, and an explicit unlock would release its protection as
        # well. Closing our descriptor releases the lock only after the final
        # inheriting Lake/Lean process has also closed its copy.
        try:
            os.close(fd)
        except OSError:
            pass

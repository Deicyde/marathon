"""Coreview — final mathematical-accuracy check before proofs land.

Coreview is the human-in-the-loop final review pass: each formalized
declaration in a skeleton chapter gets a sub-issue paired with a
plaintext rendering, mechanical-accuracy notes, and a verification
checklist. The reviewer compares each entry against its textbook source
(e.g. Lee's *Introduction to Smooth Manifolds*) and marks it VERIFIED or
REJECTED. Rejections are queued via referee.md and picked up by the
auto-refine daemon.

This subpackage absorbs the previously-standalone scripts under
``<repo>/.marathon/coreview/*.py`` into the Marathon framework so they
can be reused across projects. Project-specific facts (repo name,
parent issue, target path template, per-chapter registry) live in a
``config.toml`` under each repo's ``.marathon/coreview/`` directory.

Public surface:

* :class:`CoreviewConfig` — loaded config object
* :func:`load_config` — read ``<repo>/.marathon/coreview/config.toml``
* :func:`gh` — thin wrapper around the ``gh`` CLI
* :mod:`marathon.coreview.review` — CLI command handlers
* :mod:`marathon.coreview.daemon` — refine-on-reject single-flight daemon
* :mod:`marathon.coreview.subissues` — bulk-create and bulk-refresh helpers
"""

from marathon.coreview.config import (
    CoreviewConfig,
    ChapterRegistry,
    load_config,
    find_repo_dir,
)
from marathon.coreview.github import gh

__all__ = [
    "CoreviewConfig",
    "ChapterRegistry",
    "load_config",
    "find_repo_dir",
    "gh",
]

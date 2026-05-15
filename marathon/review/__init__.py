"""Review — final mathematical-accuracy check before proofs land.

Review is the human-in-the-loop final review pass: each formalized
declaration in a skeleton chapter gets a sub-issue paired with a
plaintext rendering, mechanical-accuracy notes, and a verification
checklist. The reviewer compares each entry against its textbook source
(e.g. Lee's *Introduction to Smooth Manifolds*) and marks it VERIFIED or
REJECTED. Rejections are queued via referee.md and picked up by the
auto-refine daemon.

This subpackage absorbs the previously-standalone scripts under
``<repo>/.marathon/review/*.py`` into the Marathon framework so they
can be reused across projects. Project-specific facts (repo name,
parent issue, target path template, per-chapter registry) live in a
``config.toml`` under each repo's ``.marathon/review/`` directory.

Public surface:

* :class:`ReviewConfig` — loaded config object
* :func:`load_config` — read ``<repo>/.marathon/review/config.toml``
* :func:`gh` — thin wrapper around the ``gh`` CLI
* :mod:`marathon.review.review` — CLI command handlers
* :mod:`marathon.review.daemon` — refine-on-reject single-flight daemon
* :mod:`marathon.review.subissues` — bulk-create and bulk-refresh helpers
"""

from marathon.review.config import (
    ReviewConfig,
    ChapterRegistry,
    load_config,
    find_repo_dir,
)
from marathon.review.github import gh

__all__ = [
    "ReviewConfig",
    "ChapterRegistry",
    "load_config",
    "find_repo_dir",
    "gh",
]

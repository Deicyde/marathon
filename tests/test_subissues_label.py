"""Add Chapter self-heals a missing `chapter-N` label — without it, every
`gh issue create --label chapter-N` for a brand-new chapter fails and the batch
is silently skipped (0 issues, chapter left unregistered)."""
from __future__ import annotations

from types import SimpleNamespace

import marathon.review.subissues as sub


def _gh_factory(existing_labels):
    calls = []

    def fake(*a, **k):
        calls.append(a)
        if len(a) >= 2 and a[0] == "label" and a[1] == "list":
            return SimpleNamespace(returncode=0, stdout="\n".join(existing_labels) + "\n")
        return SimpleNamespace(returncode=0, stdout="")

    return fake, calls


def test_ensure_chapter_label_creates_when_missing(monkeypatch):
    fake, calls = _gh_factory(["review", "chapter-14"])
    monkeypatch.setattr(sub, "gh", fake)
    sub._ensure_chapter_label(SimpleNamespace(github_repo="o/r"), 15)
    assert any(a[0] == "label" and a[1] == "create" and "chapter-15" in a for a in calls)


def test_ensure_chapter_label_skips_when_present(monkeypatch):
    fake, calls = _gh_factory(["review", "chapter-15"])
    monkeypatch.setattr(sub, "gh", fake)
    sub._ensure_chapter_label(SimpleNamespace(github_repo="o/r"), 15)
    assert not any(len(a) >= 2 and a[1] == "create" for a in calls)  # no duplicate create

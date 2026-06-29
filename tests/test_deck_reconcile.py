"""``auto_reconcile_issue`` — the SAFE hands-off replacement for the manual
"refresh from code" button.

Two safe edits, both minimal: ADD an uncited in-code decl that matches the
issue's result-key (the #94 fix), and replace a stale citation ONLY when git
proves a rename — never drop a stale citation on a guess, never re-pin an
existing block's permalink. Offline: head_sha + issue_body are monkeypatched;
the repo scan reads a real tmp .lean file; dry_run avoids the gh edit.
"""

from __future__ import annotations

from marathon.review.config import ChapterRegistry
from marathon.deck import refresh as refresh_mod
from marathon.deck.refresh import auto_reconcile_issue, SIGNATURES_HEADER

from tests.test_deck_source_fallback import (
    make_cfg, make_snapshot, install_snapshot, patch_bulk, _decl,
)

_SRC = """\
import Mathlib

namespace Demo.Chapter14.Tf

/-- Proposition 12.22 (part 2): tensor product preserves smoothness. -/
theorem tprod : True := trivial

/-- Proposition 12.22 (part 1): multiplication by a smooth function. -/
theorem smul_of_contMDiff : True := trivial

end Demo.Chapter14.Tf
"""


def _body(*names: str) -> str:
    blocks = "\n\n".join(
        f"```lean\ntheorem {n} : True\n```\n"
        f"[`{n}` at Demo/Chapter14/Tf.lean:L1-L2]"
        f"(https://github.com/x/y/blob/OLDSHA/Demo/Chapter14/Tf.lean#L1-L2)"
        for n in names
    )
    return f"{SIGNATURES_HEADER}\n\n{blocks}\n\n### Informal Statement\n\ntext\n"


def _setup(tmp_path, monkeypatch, *, title, cited):
    cfg = make_cfg(tmp_path)
    cfg.chapters[14] = ChapterRegistry(chapter=14, entries=[(22, "Prop 12.22")])
    (cfg.repo_dir / "Demo" / "Chapter14").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter14" / "Tf.lean").write_text(_SRC)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Tf.tprod", module="Demo.Chapter14.Tf"),
    ], cfg.repo_dir))
    body = _body(*cited)
    patch_bulk(monkeypatch, {22: {"title": title, "state": "OPEN",
                                  "labels": set(), "body": body}})
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEWSHA")
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: body)
    return cfg, body


def test_appends_uncited_in_code_decl(tmp_path, monkeypatch):
    """The #94 fix: an in-code decl matching the result-key but not cited
    (smul_of_contMDiff) is APPENDED; the existing tprod block is byte-identical
    (no permalink re-pin)."""
    cfg, body = _setup(
        tmp_path, monkeypatch,
        title="Proposition 12.22 — tensor product & function multiplication",
        cited=["Demo.Chapter14.Tf.tprod"],
    )
    r = auto_reconcile_issue(cfg, 22, dry_run=True)
    assert r["added"] == ["Demo.Chapter14.Tf.smul_of_contMDiff"]
    assert r["renamed"] == [] and r["kept_stale"] == []
    nb = r["new_body"]
    # the new decl is appended…
    assert "smul_of_contMDiff` at" in nb
    # …and the existing tprod block (incl. its OLD permalink SHA) is untouched.
    assert "[`Demo.Chapter14.Tf.tprod` at Demo/Chapter14/Tf.lean:L1-L2](https://github.com/x/y/blob/OLDSHA/" in nb
    # everything outside the signatures section is preserved verbatim
    assert "### Informal Statement\n\ntext\n" in nb


def test_keeps_unproven_stale_citation(tmp_path, monkeypatch):
    """A stale citation (cited, gone from the code) with NO git-proven rename is
    KEPT VERBATIM — never dropped on a guess."""
    cfg, body = _setup(
        tmp_path, monkeypatch,
        title="Proposition 12.22 — foo",
        cited=["Demo.Chapter14.Tf.tprod", "Demo.Chapter14.Tf.ghost_renamed"],
    )
    # No git repo in tmp → _proven_rename returns None → ghost is kept.
    r = auto_reconcile_issue(cfg, 22, dry_run=True)
    assert "Demo.Chapter14.Tf.ghost_renamed" in r["kept_stale"]
    assert r["renamed"] == []
    # the stale block survives in the body
    assert "ghost_renamed` at" in r["new_body"]


def test_noop_when_already_reconciled(tmp_path, monkeypatch):
    """When the issue already cites every in-code result-key decl and has no
    stale citation, nothing is written."""
    cfg, body = _setup(
        tmp_path, monkeypatch,
        title="Proposition 12.22 — foo",
        cited=["Demo.Chapter14.Tf.tprod", "Demo.Chapter14.Tf.smul_of_contMDiff"],
    )
    r = auto_reconcile_issue(cfg, 22, dry_run=True)
    assert r["updated"] is False
    assert r["added"] == [] and r["renamed"] == []


def test_no_signatures_section_is_noop(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg.chapters[14] = ChapterRegistry(chapter=14, entries=[(22, "Prop")])
    (cfg.repo_dir / "Demo" / "Chapter14").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter14" / "Tf.lean").write_text(_SRC)
    install_snapshot(cfg, make_snapshot([], cfg.repo_dir))
    body = "### Informal Statement\n\njust prose, no signatures\n"
    patch_bulk(monkeypatch, {22: {"title": "Proposition 12.22", "state": "OPEN",
                                  "labels": set(), "body": body}})
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "SHA")
    import marathon.review.github as gh_mod
    monkeypatch.setattr(gh_mod, "issue_body", lambda n, repo: body)
    r = auto_reconcile_issue(cfg, 22, dry_run=True)
    assert r["updated"] is False
    assert "no signatures section" in r["message"]


def test_conductor_reconcile_pass_gating(tmp_path, monkeypatch):
    """The conductor pass fires auto_reconcile only when HEAD MOVED, and skips
    issues with a running job."""
    from marathon import conductor
    from marathon.deck import refresh as refresh_mod

    cfg = make_cfg(tmp_path)  # chapter 14 registers issue 22
    calls: list[int] = []
    monkeypatch.setattr(
        refresh_mod, "auto_reconcile_issue",
        lambda c, n, **kw: (calls.append(n), {"updated": False})[1],
    )
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "SHA1")

    # HEAD moved (None -> SHA1): reconcile fires for the registered issue.
    new = conductor._maybe_reconcile_signatures(cfg, None, exclude_issues=frozenset())
    assert new == "SHA1" and calls == [22]

    # Same HEAD: no-op (nothing to reconcile between merges).
    calls.clear()
    assert conductor._maybe_reconcile_signatures(
        cfg, "SHA1", exclude_issues=frozenset()) == "SHA1"
    assert calls == []

    # An issue with a running job is skipped.
    calls.clear()
    conductor._maybe_reconcile_signatures(cfg, None, exclude_issues=frozenset({22}))
    assert calls == []

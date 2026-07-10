"""``auto_reconcile_issue`` conservative citation reconciliation.

It may append an uncited in-code declaration that matches the issue's result
key (the #94 fix). Stale citations are preserved; rename/split relationships
remain proposals because diff/name co-occurrence does not prove declaration
identity. Existing blocks and permalinks are never silently rewritten. Offline:
head_sha + issue_body are monkeypatched; dry_run avoids the GitHub edit.
"""

from __future__ import annotations

from marathon.review.config import ChapterRegistry
from marathon.deck import refresh as refresh_mod
from marathon.deck.refresh import (
    SIGNATURES_HEADER,
    auto_reconcile_issue,
    refresh_issue_signatures,
)

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


def test_surfaced_append_never_drops_unrelated_stale_citation(
    tmp_path, monkeypatch
):
    """SHIP's surfaced-decl write is append-only, not a hidden bulk delete."""
    stale = "Demo.Chapter14.Tf.ghost_renamed"
    cfg, body = _setup(
        tmp_path,
        monkeypatch,
        title="Proposition 12.22 — tensor product & function multiplication",
        cited=["Demo.Chapter14.Tf.tprod", stale],
    )
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Tf.tprod", module="Demo.Chapter14.Tf"),
        _decl(
            "Demo.Chapter14.Tf.smul_of_contMDiff",
            module="Demo.Chapter14.Tf",
        ),
    ], cfg.repo_dir))
    written = {}
    monkeypatch.setattr(
        refresh_mod,
        "_gh_edit_body",
        lambda repo, issue, new_body: written.update(body=new_body),
    )

    result = refresh_issue_signatures(
        cfg,
        22,
        only_surfaced={"Demo.Chapter14.Tf.smul_of_contMDiff"},
    )

    assert result["updated"] is True
    assert result["dropped"] == []
    assert result["preserved_stale"] == [stale]
    assert result["added"] == ["Demo.Chapter14.Tf.smul_of_contMDiff"]
    assert "ghost_renamed` at" in written["body"]
    assert "smul_of_contMDiff` at" in written["body"]
    assert "blob/OLDSHA" in written["body"]  # existing blocks untouched


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


def test_conductor_reopens_frozen_drift_then_reconciles_current_body(
    tmp_path, monkeypatch,
):
    """A suppressed ADD/flagged rename is a durable state transition, not a log
    line that disappears when the HEAD cursor advances."""
    from marathon import conductor
    from marathon.deck import refresh as refresh_mod
    import marathon.review.reverify as reverify_mod

    cfg = make_cfg(tmp_path)
    calls = []

    def fake_reconcile(c, n, **kw):
        calls.append((n, dict(kw)))
        if not kw.get("allow_reopened"):
            return {
                "ok": True, "updated": False,
                "flagged_renames": [{"old": "A.old", "new": "A.new"}],
                "flagged_splits": [], "flagged_deletes": [],
                "suppressed_adds": ["A.helper"],
            }
        return {"ok": True, "updated": True, "message": "current"}

    reopened = []
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEW")
    monkeypatch.setattr(conductor, "reconcile_cursor_is_ancestor", lambda *a: True)
    monkeypatch.setattr(refresh_mod, "auto_reconcile_issue", fake_reconcile)
    monkeypatch.setattr(
        reverify_mod, "unverify_for_rereview",
        lambda c, n, reasons: reopened.append((n, reasons)) or True,
    )

    got = conductor._maybe_reconcile_signatures(
        cfg, "OLD", exclude_issues=frozenset(),
    )

    assert got == "NEW"
    assert reopened and "new card: A.helper" in reopened[0][1]
    assert any("removed/renamed: A.old" in r for r in reopened[0][1])
    assert calls[1][1]["allow_reopened"] is True
    assert calls[1][1]["diff_window"] == ("OLD", "NEW")


def test_conductor_keeps_old_cursor_when_unverify_not_confirmed(
    tmp_path, monkeypatch,
):
    from marathon import conductor
    from marathon.deck import refresh as refresh_mod
    import marathon.review.reverify as reverify_mod

    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEW")
    monkeypatch.setattr(conductor, "reconcile_cursor_is_ancestor", lambda *a: True)
    monkeypatch.setattr(
        refresh_mod, "auto_reconcile_issue",
        lambda *a, **k: {
            "ok": True, "updated": False, "flagged_renames": [],
            "flagged_splits": [], "flagged_deletes": [],
            "suppressed_adds": ["A.new"],
        },
    )
    monkeypatch.setattr(reverify_mod, "unverify_for_rereview", lambda *a: False)

    assert conductor._maybe_reconcile_signatures(
        cfg, "OLD", exclude_issues=frozenset(),
    ) == "OLD"


def test_conductor_keeps_diff_window_while_identity_proposal_is_pending(
    tmp_path, monkeypatch,
):
    """A changed-leaf split may only be reconstructable from OLD..NEW.  Do not
    consume that cursor until the deck applies the human's decision."""
    from marathon import conductor
    from marathon.deck import refresh as refresh_mod

    cfg = make_cfg(tmp_path)
    seen = []
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEW")
    monkeypatch.setattr(conductor, "reconcile_cursor_is_ancestor", lambda *a: True)

    def fake_reconcile(c, n, **kwargs):
        seen.append(kwargs.get("diff_window"))
        return {
            "ok": True,
            "updated": False,
            "flagged_renames": [],
            "flagged_splits": [],
            "flagged_deletes": [],
            "suppressed_adds": [],
            "proposed_renames": [],
            "proposed_splits": [{"old": "A.old", "targets": ["A.left", "A.right"]}],
        }

    monkeypatch.setattr(refresh_mod, "auto_reconcile_issue", fake_reconcile)

    assert conductor._maybe_reconcile_signatures(
        cfg, "OLD", exclude_issues=frozenset(),
    ) == "OLD"
    assert seen == [("OLD", "NEW")]


def test_reconcile_cursor_round_trip(tmp_path):
    from marathon import conductor

    assert conductor.load_reconcile_cursor(tmp_path) is None
    conductor.save_reconcile_cursor(tmp_path, "abc123")
    assert conductor.load_reconcile_cursor(tmp_path) == "abc123"
    assert (tmp_path / ".marathon/conductor/.gitignore").read_text() == "*\n"


def test_reconcile_cursor_rejects_branch_switch_or_rebase(tmp_path):
    """Only an ancestor cursor may become a git diff provenance window."""
    from marathon import conductor

    repo = tmp_path / "repo"
    repo.mkdir()
    conductor._git(repo, "init", "-q", "-b", "main")
    conductor._git(repo, "config", "user.email", "t@example.com")
    conductor._git(repo, "config", "user.name", "T")
    f = repo / "A"
    f.write_text("base\n")
    conductor._git(repo, "add", "A")
    conductor._git(repo, "commit", "-q", "-m", "base")
    base = conductor._git(repo, "rev-parse", "HEAD").stdout.strip()
    f.write_text("old branch\n")
    conductor._git(repo, "commit", "-qam", "old branch")
    old_branch = conductor._git(repo, "rev-parse", "HEAD").stdout.strip()
    conductor._git(repo, "checkout", "-q", "-b", "rebased", base)
    f.write_text("new branch\n")
    conductor._git(repo, "commit", "-qam", "new branch")
    current = conductor._git(repo, "rev-parse", "HEAD").stdout.strip()

    assert conductor.reconcile_cursor_is_ancestor(repo, base, current)
    assert not conductor.reconcile_cursor_is_ancestor(repo, old_branch, current)
    assert not conductor.reconcile_cursor_is_ancestor(repo, "deadbeef", current)


def test_invalid_persisted_cursor_is_discarded_without_diff_window(
    tmp_path, monkeypatch,
):
    from marathon import conductor
    from marathon.deck import refresh as refresh_mod

    cfg = make_cfg(tmp_path)
    conductor.save_reconcile_cursor(cfg.repo_dir, "OLD")
    calls = []
    monkeypatch.setattr(refresh_mod, "head_sha", lambda d: "NEW")
    monkeypatch.setattr(conductor, "reconcile_cursor_is_ancestor", lambda *a: False)
    monkeypatch.setattr(
        refresh_mod, "auto_reconcile_issue",
        lambda c, n, **kw: calls.append(kw) or {"ok": True, "updated": False},
    )

    got = conductor._maybe_reconcile_signatures(
        cfg, "OLD", exclude_issues=frozenset(),
    )

    assert got == "NEW"
    assert calls == [{"diff_window": None}]
    assert not conductor.reconcile_cursor_path(cfg.repo_dir).exists()

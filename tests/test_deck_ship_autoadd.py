"""The ship step auto-adds the KEPT surfaced declarations into the issue body
(only at the explicit ship; a rejected surfaced decl is excluded)."""

from __future__ import annotations


import marathon.deck.subdeck as subdeck
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels


def _cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo="o/r",
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={14: ChapterRegistry(chapter=14, entries=[(30, "x")])},
    )


def _fake_subdeck(rejected: bool, locked: list = None) -> dict:
    """A complete subdeck: a normal verified decl, a surfaced+kept decl, and
    (optionally) a surfaced+rejected decl."""
    cards = [
        {"name": "N.Foo", "surfaced": False},
        {"name": "N.SurfKeep", "surfaced": True},
    ]
    summary = {
        "kept": ["N.Foo", "N.SurfKeep"],
        "locked": locked or [],
        "rejected": [],
        "deferred": [],
    }
    if rejected:
        cards.append({"name": "N.SurfReject", "surfaced": True})
        summary["rejected"] = [{"name": "N.SurfReject", "note": "wrong"}]
    return {
        "issue": 30, "complete": True,
        "progress": {"reviewed": len(cards), "total": len(cards)},
        "cards": cards, "auto_approved": [], "summary": summary,
    }


def _install_common_patches(monkeypatch, refresh_calls):
    import marathon.deck.refresh as refresh_mod
    import marathon.deck.verdicts as verdicts_mod

    monkeypatch.setattr(
        verdicts_mod, "apply_verdict",
        lambda cfg, issue, verdict, note=None: None,
    )
    monkeypatch.setattr(subdeck, "clear_issue", lambda cfg, issue: None)
    monkeypatch.setattr(subdeck, "_next_ready_issue", lambda cfg, issue: None)

    def fake_refresh(cfg, issue, *, only_surfaced=None):
        refresh_calls.append(only_surfaced)
        return {
            "ok": True, "updated": bool(only_surfaced),
            "added": sorted(only_surfaced or []), "dropped": [], "sha": "deadbee",
        }

    monkeypatch.setattr(refresh_mod, "refresh_issue_signatures", fake_refresh)


def test_ship_adds_only_kept_surfaced_to_issue(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    refresh_calls: list = []
    _install_common_patches(monkeypatch, refresh_calls)
    monkeypatch.setattr(subdeck, "build_subdeck", lambda c, i, **k: _fake_subdeck(rejected=True))

    res = subdeck.ship_issue(cfg, 30)

    # The surfaced decl the human KEPT is written; the rejected one is NOT.
    assert refresh_calls == [{"N.SurfKeep"}]
    assert res["added_to_issue"] == ["N.SurfKeep"]
    assert res["outcome"] == "shipped"  # there was a rejection


def test_ship_with_no_surfaced_decls_writes_nothing(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    refresh_calls: list = []
    _install_common_patches(monkeypatch, refresh_calls)
    # A subdeck whose only decl is cited (not surfaced) → no body write.
    sd = {
        "issue": 30, "complete": True,
        "progress": {"reviewed": 1, "total": 1},
        "cards": [{"name": "N.Foo", "surfaced": False}],
        "auto_approved": [],
        "summary": {"kept": ["N.Foo"], "rejected": [], "deferred": []},
    }
    monkeypatch.setattr(subdeck, "build_subdeck", lambda c, i, **k: sd)

    res = subdeck.ship_issue(cfg, 30)

    assert refresh_calls == []  # refresh never invoked
    assert res["added_to_issue"] == []
    assert res["outcome"] == "verified"


def test_ship_reject_freezes_only_locked_decls(tmp_path, monkeypatch):
    """A REJECT ship hard-freezes ONLY the LOCKED decls (opt-in), not every
    kept decl — soft-kept decls stay rewritable. The FIXED decl is unfrozen."""
    import marathon.review.approved_statements as aps

    cfg = _cfg(tmp_path)
    _install_common_patches(monkeypatch, [])
    monkeypatch.setattr(
        subdeck, "build_subdeck",
        lambda c, i, **k: _fake_subdeck(rejected=True, locked=["N.Foo"]),
    )
    captured: dict = {}

    def spy(cfg, issue, *, kept, rejected):
        captured.update(issue=issue, kept=list(kept), rejected=list(rejected))
        return (list(kept), [])

    monkeypatch.setattr(aps, "capture_kept_on_reject", spy)
    subdeck.ship_issue(cfg, 30)

    assert captured["issue"] == 30
    assert captured["kept"] == ["N.Foo"]              # ONLY the locked decl
    assert "N.SurfKeep" not in captured["kept"]       # soft-kept → not frozen
    assert captured["rejected"] == ["N.SurfReject"]


def test_ship_reject_with_no_locks_freezes_nothing(tmp_path, monkeypatch):
    """Default (no decl locked): nothing is hard-frozen — every kept decl stays
    rewritable."""
    import marathon.review.approved_statements as aps

    cfg = _cfg(tmp_path)
    _install_common_patches(monkeypatch, [])
    monkeypatch.setattr(
        subdeck, "build_subdeck", lambda c, i, **k: _fake_subdeck(rejected=True)
    )
    captured: dict = {}
    monkeypatch.setattr(
        aps, "capture_kept_on_reject",
        lambda cfg, issue, *, kept, rejected: captured.update(kept=list(kept)) or ([], []),
    )
    subdeck.ship_issue(cfg, 30)
    assert captured["kept"] == []                     # opt-in: nothing frozen


def test_ship_verify_does_not_call_reject_freeze(tmp_path, monkeypatch):
    """An all-verified ship goes through record_verification (issue-level
    capture), NOT the per-decl reject-freeze path."""
    import marathon.review.approved_statements as aps

    cfg = _cfg(tmp_path)
    _install_common_patches(monkeypatch, [])
    monkeypatch.setattr(
        subdeck, "build_subdeck", lambda c, i, **k: _fake_subdeck(rejected=False)
    )
    called = {"n": 0}
    monkeypatch.setattr(
        aps, "capture_kept_on_reject",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ([], []),
    )
    subdeck.ship_issue(cfg, 30)
    assert called["n"] == 0

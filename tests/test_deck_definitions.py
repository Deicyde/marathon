from __future__ import annotations

from marathon.deck.definitions import (
    clear_definition_index_cache,
    definition_source_fingerprint,
    definition_source_token,
    definition_terms,
    lookup_definition,
)

from tests.test_deck_source_fallback import (
    _bundle_body,
    _decl,
    install_snapshot,
    make_cfg,
    make_snapshot,
    patch_bulk,
)


def _install(tmp_path, monkeypatch, source: str, decls, body: str):
    cfg = make_cfg(tmp_path)
    path = cfg.repo_dir / "Demo" / "Chapter14.lean"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    install_snapshot(cfg, make_snapshot(decls, cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {
            "title": "Definition lookup owner",
            "state": "OPEN",
            "labels": set(),
            "body": body,
        }
    })
    clear_definition_index_cache(cfg.repo_dir)
    return cfg, path


def test_exact_definition_includes_source_audit_evidence_and_owner(
    tmp_path, monkeypatch
):
    name = "Demo.Chapter14.Widget"
    source = """\
namespace Demo.Chapter14

/-- The widget used by the result. -/
def Widget : Nat := 7

end Demo.Chapter14
"""
    cfg, _path = _install(
        tmp_path,
        monkeypatch,
        source,
        [_decl(name, kind="def", type_pp="Nat")],
        _bundle_body(name),
    )

    result = lookup_definition(cfg, name, issue_num=22)

    assert result["status"] == "exact"
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["name"] == name
    assert match["source_file"] == "Demo/Chapter14.lean"
    assert match["source_line"] == 4
    assert "def Widget : Nat := 7" in match["source_block"]
    assert match["statement_pp"] == "Nat"
    assert match["in_audit"] is True
    assert match["evidence"]["sorry"] is False
    assert match["owners"] == [{
        "issue": 22,
        "title": "Definition lookup owner",
        "chapter": 14,
    }]


def test_ambiguous_short_name_returns_explicit_choices(tmp_path, monkeypatch):
    names = ["Demo.Chapter14.Left.Widget", "Demo.Chapter14.Right.Widget"]
    source = """\
namespace Demo.Chapter14.Left
def Widget : Nat := 1
end Demo.Chapter14.Left

namespace Demo.Chapter14.Right
def Widget : Nat := 2
end Demo.Chapter14.Right
"""
    cfg, _path = _install(
        tmp_path,
        monkeypatch,
        source,
        [_decl(name, kind="def", type_pp="Nat") for name in names],
        _bundle_body(names[0]),
    )

    result = lookup_definition(cfg, "Widget")

    assert result["status"] == "ambiguous"
    assert [match["name"] for match in result["matches"]] == names
    assert "2 current declarations" in result["message"]
    terms = definition_terms(cfg)
    assert terms["short"]["Widget"] == names
    assert terms["count"] == 2


def test_source_index_invalidates_after_rename_before_next_audit(
    tmp_path, monkeypatch
):
    old = "Demo.Chapter14.OldWidget"
    new = "Demo.Chapter14.NewWidget"
    cfg, path = _install(
        tmp_path,
        monkeypatch,
        "namespace Demo.Chapter14\ndef OldWidget : Nat := 1\nend Demo.Chapter14\n",
        [_decl(old, kind="def", type_pp="Nat")],
        _bundle_body(old),
    )
    assert lookup_definition(cfg, old)["status"] == "exact"
    old_token = definition_source_token(cfg.repo_dir)
    old_fingerprint = definition_source_fingerprint(cfg.repo_dir, old)

    # Deliberately leave the audit snapshot stale.  Current source identity wins.
    path.write_text(
        "namespace Demo.Chapter14\n"
        "def NewWidget : Nat := 100\n"
        "end Demo.Chapter14\n"
    )

    assert lookup_definition(cfg, old)["status"] == "unresolved"
    renamed = lookup_definition(cfg, new)
    assert renamed["status"] == "exact"
    assert renamed["matches"][0]["in_audit"] is False
    assert renamed["matches"][0]["tier"] == "-"
    assert definition_source_token(cfg.repo_dir) != old_token
    assert definition_source_fingerprint(cfg.repo_dir, old) is None
    assert definition_source_fingerprint(cfg.repo_dir, new) != old_fingerprint
    assert definition_terms(cfg)["source_token"] == definition_source_token(
        cfg.repo_dir
    )


def test_current_root_name_never_inherits_stale_suffix_audit_identity(
    tmp_path, monkeypatch
):
    """Citation suffix resolution is not declaration identity.  A new root
    ``Widget`` must not borrow type/evidence/ownership from stale
    ``Demo.Old.Widget`` just because its last component matches."""
    stale = "Demo.Old.Widget"
    cfg, _path = _install(
        tmp_path,
        monkeypatch,
        "def Widget : String := \"current\"\n",
        [_decl(stale, kind="def", type_pp="Nat", module="Demo.Old")],
        _bundle_body(stale),
    )

    result = lookup_definition(cfg, "Widget")

    assert result["status"] == "exact"
    match = result["matches"][0]
    assert match["name"] == "Widget"
    assert match["in_audit"] is False
    assert match["statement_pp"] is None
    assert match["tier"] == "-"
    assert match["owners"] == []


def test_unresolved_identifier_fails_clearly(tmp_path, monkeypatch):
    cfg, _path = _install(
        tmp_path,
        monkeypatch,
        "namespace Demo.Chapter14\ndef Present : Nat := 1\nend Demo.Chapter14\n",
        [],
        _bundle_body("Demo.Chapter14.Present"),
    )

    result = lookup_definition(cfg, "Nat.succ")

    assert result["status"] == "unresolved"
    assert result["matches"] == []
    assert "Mathlib/core" in result["message"]


def test_source_index_refuses_lean_symlink_outside_repo(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    outside = tmp_path / "private-Leak.lean"
    outside.write_text("def SecretToken : String := \"do-not-render\"\n")
    (cfg.repo_dir / "Leak.lean").symlink_to(outside)
    patch_bulk(monkeypatch, {
        22: {
            "title": "Owner", "state": "OPEN", "labels": set(),
            "body": _bundle_body("SecretToken"),
        }
    })
    clear_definition_index_cache(cfg.repo_dir)

    assert lookup_definition(cfg, "SecretToken")["status"] == "unresolved"
    assert "SecretToken" not in definition_terms(cfg)["names"]

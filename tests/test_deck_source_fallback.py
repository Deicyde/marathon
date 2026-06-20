"""Tests for request-1: the repo-wide SOURCE FALLBACK + stale-citation
labelling, and the conservative API classification (marathon.deck.cards).

Contract under test (the deck-features task spec + CONTRACT ADDITIONS):

* SOURCE RESOLUTION — a decl's ``source_block`` comes from (1) the audit
  snapshot's recorded file, else (2) a REPO-WIDE scan of the git-tracked
  ``.lean`` files for the decl by name, else (3) ``in_code=false`` with a
  null source (a cited name absent from the code — a stale citation);
* ``_find_decl_source_in_repo`` finds an in-repo decl absent from a fabricated
  snapshot and returns the SAME extraction slice as ``_read_decl_source``;
* a truly-absent cited decl → ``in_code=false``, ``source_block=None``, tier
  ``'-'`` (kept honest);
* ``classify_api`` — true for an instance / an ``_apply``-style helper at/above
  the T1 floor with no deception tags; false for the headline, a below-floor
  decl, a deception-tagged decl, a not-in-code decl, and a textbook-named
  result.

Fully offline: the bulk issue fetch (gh) is monkeypatched; the repo scan reads
real ``.lean`` files written under a tmp_path repo. No network/lake/Aristotle.
"""

from __future__ import annotations

from dataclasses import replace

import marathon.deck.cards as cards
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.deck.cards import (
    DeclView,
    _find_decl_source_in_repo,
    _read_decl_source,
    classify_api,
)
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels

REPO_SLUG = "example/Demo"


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def make_cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo=REPO_SLUG,
        parent_issue=1,
        referee_path=repo / ".marathon/referee.md",
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(
                chapter=14,
                entries=[(22, "Def Foo")],
            ),
        },
    )


def _decl(name, *, kind="theorem", type_pp="Type", module="Demo.Chapter14",
          axioms=("propext",), tags=()) -> DeclAudit:
    return DeclAudit(
        name=name, kind=kind, module=module, status="ok", type_pp=type_pp,
        value_pp=None, cone=[], axioms=list(axioms), has_sorry=False,
        tags=list(tags), reason=None,
    )


def make_snapshot(decls, repo_dir) -> AuditSnapshot:
    return AuditSnapshot(
        repo_dir=str(repo_dir),
        modules=["Demo.Chapter14"],
        toolchain="leanprover/lean4:v4.0.0",
        lean_version="4.0.0",
        package_revs={},
        trusted_prefixes=["Mathlib", "Init"],
        created_at="2026-06-14T00:00:00+00:00",
        decls=list(decls),
        failures=[],
    )


def install_snapshot(cfg, snapshot) -> None:
    from marathon.audit.engine import save_snapshot

    save_snapshot(snapshot, cfg.repo_dir)


def patch_bulk(monkeypatch, meta: dict) -> None:
    def fake_bulk(nums, repo):
        return {n: meta[n] for n in nums if n in meta}

    monkeypatch.setattr(cards, "fetch_issues_bulk", fake_bulk, raising=False)
    import marathon.review.github as gh_mod

    monkeypatch.setattr(gh_mod, "fetch_issues_bulk", fake_bulk)


def _bundle_body(*decls: str) -> str:
    lines = "\n".join(f"def {d} : T := ?" for d in decls)
    return f"### Lean signatures\n\n```lean\n{lines}\n```"


# A small .lean source written under <repo>/Demo/Chapter14.lean. Foo IS in the
# (fabricated) snapshot; Bar is NOT (so it must be found by the repo scan).
_LEAN_SRC = """\
import Mathlib

namespace Demo.Chapter14

/-- Foo doc. -/
theorem Foo : 1 = 1 := rfl

/-- Bar doc — present in the file but absent from the snapshot. -/
theorem Bar : 2 = 2 := rfl

end Demo.Chapter14
"""


def _write_source(cfg) -> None:
    path = cfg.repo_dir / "Demo" / "Chapter14.lean"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_LEAN_SRC)


# ---------------------------------------------------------------------------
# _find_decl_source_in_repo — the repo-wide scan
# ---------------------------------------------------------------------------


def _cfg_two_issues(tmp_path) -> ReviewConfig:
    """Like make_cfg but with two issues registered in chapter 14, so the
    cross-issue surfacing exclusion can be exercised."""
    cfg = make_cfg(tmp_path)
    cfg.chapters[14] = ChapterRegistry(
        chapter=14, entries=[(22, "IsSmooth"), (23, "pullback")]
    )
    return cfg


# A source file where the Tf namespace holds: the cited result (IsSmooth),
# ANOTHER part of the same numbered result (altCriterion, docstring cites
# 12.19), a sibling result's decl (pullback, owned by issue 23), and unrelated
# plumbing (helper, no 12.19 in its docstring).
_TF_SRC = """\
import Mathlib

namespace Demo.Chapter14.Tf

/-- Proposition 12.19(a): the smoothness predicate. -/
def IsSmooth : Prop := True

/-- Proposition 12.19(b): an equivalent characterization. -/
theorem altCriterion : IsSmooth ↔ True := by simp [IsSmooth]

/-- The pullback of a tensor field (a different result). -/
def pullback : Prop := True

/-- A private algebra helper — no textbook result. -/
theorem helper : True := trivial

end Demo.Chapter14.Tf
"""


def test_surfacing_uses_docstring_result_key(tmp_path, monkeypatch):
    """Surfacing pulls in ANOTHER PART of the issue's numbered result (a
    same-namespace decl whose docstring cites the same Proposition) — the
    Prop-12.19(b)/(d) regression — while NEVER surfacing a sibling issue's
    decl or unrelated plumbing."""
    from marathon.deck.subdeck import build_subdeck

    cfg = _cfg_two_issues(tmp_path)
    (cfg.repo_dir / "Demo" / "Chapter14").mkdir(parents=True, exist_ok=True)
    (cfg.repo_dir / "Demo" / "Chapter14" / "Tf.lean").write_text(_TF_SRC)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Tf.IsSmooth", kind="def", module="Demo.Chapter14.Tf"),
        _decl("Demo.Chapter14.Tf.altCriterion", module="Demo.Chapter14.Tf"),
        _decl("Demo.Chapter14.Tf.pullback", kind="def", module="Demo.Chapter14.Tf"),
        _decl("Demo.Chapter14.Tf.helper", module="Demo.Chapter14.Tf"),
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {"title": "Proposition 12.19 — smoothness criteria", "state": "OPEN",
             "labels": set(), "body": _bundle_body("Demo.Chapter14.Tf.IsSmooth")},
        23: {"title": "Ch14 def — pullback", "state": "OPEN", "labels": set(),
             "body": _bundle_body("Demo.Chapter14.Tf.pullback")},
    })

    surfaced = {n for n in build_subdeck(cfg, 22)["reconcile"]["surfaced"]}
    # altCriterion (same result number in its docstring) IS surfaced…
    assert "Demo.Chapter14.Tf.altCriterion" in surfaced
    # …but the sibling issue's pullback and the unrelated helper are NOT.
    assert "Demo.Chapter14.Tf.pullback" not in surfaced
    assert "Demo.Chapter14.Tf.helper" not in surfaced


def test_repo_scan_finds_decl_absent_from_snapshot(tmp_path):
    cfg = make_cfg(tmp_path)
    _write_source(cfg)
    block, rel = _find_decl_source_in_repo(cfg.repo_dir, "Demo.Chapter14.Bar")
    assert block is not None
    assert "theorem Bar : 2 = 2 := rfl" in block
    assert "Bar doc" in block  # the docstring is included by the shared slice
    assert rel == "Demo/Chapter14.lean"


def test_repo_scan_matches_read_decl_source_slice(tmp_path):
    """The repo scan returns the SAME extraction slice as the snapshot path."""
    cfg = make_cfg(tmp_path)
    _write_source(cfg)
    snap_block, snap_rel = _read_decl_source(
        cfg.repo_dir, "Demo.Chapter14", "Demo.Chapter14.Foo"
    )
    scan_block, scan_rel = _find_decl_source_in_repo(
        cfg.repo_dir, "Demo.Chapter14.Foo"
    )
    assert snap_block == scan_block
    assert snap_rel == scan_rel == "Demo/Chapter14.lean"


def test_repo_scan_absent_decl_returns_none(tmp_path):
    cfg = make_cfg(tmp_path)
    _write_source(cfg)
    block, rel = _find_decl_source_in_repo(cfg.repo_dir, "Demo.Chapter14.Ghost")
    assert block is None and rel is None


# ---------------------------------------------------------------------------
# build_card_detail — source fallback + in_code labelling
# ---------------------------------------------------------------------------


def test_card_shows_source_for_decl_only_in_repo(tmp_path, monkeypatch):
    """A decl present in the repo source but absent from the snapshot still
    shows its snippet (via the repo scan) and is in_code=true."""
    cfg = make_cfg(tmp_path)
    _write_source(cfg)
    # Snapshot has ONLY Foo; the issue also cites Bar (in the file, not the snap).
    install_snapshot(cfg, make_snapshot(
        [_decl("Demo.Chapter14.Foo")], cfg.repo_dir
    ))
    patch_bulk(monkeypatch, {
        22: {"title": "#22", "state": "OPEN",
             "body": _bundle_body("Foo", "Bar"), "labels": set()},
    })
    detail = cards.build_card_detail(cfg, 22)
    by_name = {d.name: d for d in detail.decls}

    # Bar is not in the snapshot, so it stays under the body-cited (unqualified)
    # name; the repo scan still locates it and shows its source.
    bar = by_name["Bar"]
    assert bar.in_code is True
    assert bar.source_block is not None
    assert "theorem Bar : 2 = 2 := rfl" in bar.source_block
    assert bar.source_file == "Demo/Chapter14.lean"
    # Bar is not in the snapshot, so it has no audit tier evidence.
    assert bar.tier == "-"

    # Foo resolves through the snapshot path as before (qualified name).
    foo = by_name["Demo.Chapter14.Foo"]
    assert foo.in_code is True
    assert foo.source_block is not None and "theorem Foo" in foo.source_block


def test_card_labels_absent_decl_as_stale_citation(tmp_path, monkeypatch):
    """A cited decl found nowhere in the code → in_code=false, null source,
    tier '-': a stale citation."""
    cfg = make_cfg(tmp_path)
    _write_source(cfg)
    install_snapshot(cfg, make_snapshot(
        [_decl("Demo.Chapter14.Foo")], cfg.repo_dir
    ))
    patch_bulk(monkeypatch, {
        22: {"title": "#22", "state": "OPEN",
             "body": _bundle_body("Foo", "Ghost"), "labels": set()},
    })
    detail = cards.build_card_detail(cfg, 22)
    by_name = {d.name: d for d in detail.decls}

    ghost = by_name["Ghost"]
    assert ghost.in_code is False
    assert ghost.source_block is None
    assert ghost.tier == "-"
    # A stale citation is never auto-approvable.
    assert ghost.api is False


def test_snapshot_decl_renamed_in_repo_is_stale(tmp_path, monkeypatch):
    """The reported bug: a decl present in a STALE snapshot but renamed/removed
    in the CURRENT code (no source found by extraction OR by-name scan) must read
    in_code=false — a stale citation, not a phantom card with no source.
    Snapshot resolution ALONE is not 'in the current code'."""
    cfg = make_cfg(tmp_path)
    _write_source(cfg)   # Demo/Chapter14.lean has Foo + Bar — NOT 'Renamed'
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo"),
        _decl("Demo.Chapter14.Renamed"),   # in the (stale) snapshot, absent from code
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {"title": "#22", "state": "OPEN",
             "body": _bundle_body("Foo", "Renamed"), "labels": set()},
    })
    detail = cards.build_card_detail(cfg, 22)
    renamed = {d.name: d for d in detail.decls}["Demo.Chapter14.Renamed"]
    assert renamed.in_code is False        # resolved in snapshot, but gone from code
    assert renamed.source_block is None


def test_snapshot_decl_in_code_when_no_lean_repo(tmp_path, monkeypatch):
    """Safety valve: with NO scannable Lean repo (no .lean files), a
    snapshot-resolved decl whose source can't be extracted stays in_code=true —
    the snapshot is then the only source of truth (e.g. unit-test fixtures)."""
    cfg = make_cfg(tmp_path)
    # NO _write_source: the repo has no .lean files to scan.
    install_snapshot(cfg, make_snapshot([_decl("Demo.Chapter14.Foo")], cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {"title": "#22", "state": "OPEN",
             "body": _bundle_body("Foo"), "labels": set()},
    })
    detail = cards.build_card_detail(cfg, 22)
    foo = {d.name: d for d in detail.decls}["Demo.Chapter14.Foo"]
    assert foo.in_code is True             # no .lean repo → trust the snapshot


def test_card_decl_view_serializes_new_fields(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    _write_source(cfg)
    install_snapshot(cfg, make_snapshot(
        [_decl("Demo.Chapter14.Foo")], cfg.repo_dir
    ))
    patch_bulk(monkeypatch, {
        22: {"title": "#22", "state": "OPEN",
             "body": _bundle_body("Foo"), "labels": set()},
    })
    detail = cards.build_card_detail(cfg, 22)
    js = detail.decls[0].to_json()
    assert "in_code" in js and "api" in js and "api_reason" in js


# ---------------------------------------------------------------------------
# classify_api — both ways + the never-api guards
# ---------------------------------------------------------------------------


def _view(name, *, kind="theorem", tier="T1", in_code=True, tags=()) -> DeclView:
    return DeclView(
        name=name, kind=kind, tier=tier, qualifiers=[],
        source_block="<src>", source_file="X.lean", statement_pp=None,
        kernel=[], evidence={
            "axioms_beyond_whitelist": [], "sorry": None,
            "deception_tags": list(tags),
        },
        informal_rendering=None, informal_is_llm=False, semantic_delta=None,
        in_code=in_code,
    )


def test_classify_api_instance_is_api():
    is_api, reason = classify_api(_view("Demo.foo", kind="instance"), "Demo.Head")
    assert is_api is True
    assert reason == "instance"


def test_classify_api_apply_suffix_is_api():
    is_api, reason = classify_api(_view("Demo.bar_apply"), "Demo.Head")
    assert is_api is True
    assert "_apply" in reason


def test_classify_api_segment_is_api():
    # An LM / IsSmooth / coordChange segment marks bundled-map plumbing.
    is_api, reason = classify_api(_view("Demo.Map.IsSmooth.aux"), "Demo.Head")
    assert is_api is True
    assert "IsSmooth" in reason


def test_classify_api_textbook_name_is_main():
    is_api, reason = classify_api(_view("Demo.fundamentalTheorem"), "Demo.Head")
    assert is_api is False and reason is None


def test_classify_api_headline_never_api():
    # Even an _apply-named decl is MAIN when it is the headline.
    is_api, _ = classify_api(_view("Demo.foo_apply"), "Demo.foo_apply")
    assert is_api is False


def test_classify_api_below_floor_never_api():
    is_api, _ = classify_api(_view("Demo.foo_apply", tier="T0"), "Demo.Head")
    assert is_api is False
    is_api, _ = classify_api(_view("Demo.foo_apply", tier="-"), "Demo.Head")
    assert is_api is False


def test_classify_api_deception_tagged_never_api():
    v = _view("Demo.foo_apply", tags=["axiom-hack"])
    is_api, _ = classify_api(v, "Demo.Head")
    assert is_api is False


def test_classify_api_not_in_code_never_api():
    v = _view("Demo.foo_apply", in_code=False)
    is_api, _ = classify_api(v, "Demo.Head")
    assert is_api is False

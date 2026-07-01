"""Fill discovery: a fill target is a VERIFIED sub-issue with a still-`sorry`
declaration. Proved decls and unverified sorries are excluded."""
from __future__ import annotations

from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels
from marathon.review.fill_discovery import (
    chapter_skeleton_done,
    discover_fill_targets,
)


def _cfg(tmp_path, chapters=None) -> ReviewConfig:
    repo = tmp_path / "repo"
    if not repo.exists():
        repo.mkdir()
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo="owner/repo",
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters=chapters or {},
    )


def _write(cfg):
    d = cfg.target_path(12)
    d.mkdir(parents=True)
    (d / "F.lean").write_text(
        "theorem proved : P := by exact h\n"      # verified, no sorry  → not a target
        "theorem unproved : Q := by sorry\n"      # verified + sorry    → target
        "theorem unverified : R := by sorry\n"    # sorry, NOT verified → not a target
    )


def test_discover_returns_verified_sorries_only(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg)
    targets = discover_fill_targets(
        cfg, 12, _verified=lambda c, ch: {55: {"proved", "unproved"}},
    )
    assert len(targets) == 1
    assert targets[0].issue == 55
    assert targets[0].chapter == 12
    assert targets[0].decls == ["unproved"]  # proved excluded; unverified excluded


def test_discover_empty_when_nothing_verified(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg)
    assert discover_fill_targets(cfg, 12, _verified=lambda c, ch: {}) == []


def test_discover_empty_when_all_proved(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.target_path(12).mkdir(parents=True)
    (cfg.target_path(12) / "F.lean").write_text("theorem a : P := by exact h\n")
    assert discover_fill_targets(cfg, 12, _verified=lambda c, ch: {7: {"a"}}) == []


def test_discover_never_raises_on_bad_verified(tmp_path):
    cfg = _cfg(tmp_path)

    def boom(c, ch):
        raise RuntimeError("gh down")

    assert discover_fill_targets(cfg, 12, _verified=boom) == []


# --- chapter_skeleton_done: proving gate ------------------------------------------


def _reg(chapter, entries):
    return {chapter: ChapterRegistry(chapter=chapter, entries=list(entries))}


def test_skeleton_done_true_when_all_registered_verified(tmp_path):
    cfg = _cfg(tmp_path, _reg(12, [(55, "A"), (56, "B")]))
    assert chapter_skeleton_done(
        cfg, 12, _verified=lambda c, ch: {55: {"a"}, 56: {"b"}}
    ) is True


def test_skeleton_done_false_when_one_still_churning(tmp_path):
    cfg = _cfg(tmp_path, _reg(12, [(55, "A"), (56, "B")]))
    # 56 not yet verified → the chapter's skeleton is not done.
    assert chapter_skeleton_done(
        cfg, 12, _verified=lambda c, ch: {55: {"a"}}
    ) is False


def test_skeleton_done_false_when_no_registry(tmp_path):
    cfg = _cfg(tmp_path)  # chapters={}
    assert chapter_skeleton_done(
        cfg, 12, _verified=lambda c, ch: {55: {"a"}}
    ) is False


def test_skeleton_done_ignores_verified_with_empty_declset(tmp_path):
    cfg = _cfg(tmp_path, _reg(12, [(55, "A")]))
    # A verified-labelled issue that surfaced no decls doesn't count as done.
    assert chapter_skeleton_done(
        cfg, 12, _verified=lambda c, ch: {55: set()}
    ) is False


def test_skeleton_done_never_raises(tmp_path):
    cfg = _cfg(tmp_path, _reg(12, [(55, "A")]))

    def boom(c, ch):
        raise RuntimeError("gh down")

    assert chapter_skeleton_done(cfg, 12, _verified=boom) is False


# --- Phase 3: direct-sorry leaves only (transitive self-clears) --------------------


def test_discover_targets_only_direct_body_sorries(tmp_path):
    """A decl whose OWN body is sorry-free is never a fill target, even if it
    calls a still-sorry lemma — transitive sorries self-clear once the leaf is
    proven and the module rebuilds, so the prover only dispatches direct-sorry
    leaves and never wastes a fill on a transitively-blocked dependent."""
    cfg = _cfg(tmp_path)
    d = cfg.target_path(12)
    d.mkdir(parents=True)
    (d / "F.lean").write_text(
        "theorem leaf : P := by sorry\n"            # direct sorry → target
        "theorem dependent : Q := by exact leaf\n"  # clean body, uses leaf → NOT
    )
    targets = discover_fill_targets(
        cfg, 12, _verified=lambda c, ch: {9: {"leaf", "dependent"}},
    )
    assert len(targets) == 1
    assert targets[0].decls == ["leaf"]  # only the direct-sorry leaf

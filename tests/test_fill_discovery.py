"""Fill discovery: a fill target is a VERIFIED sub-issue with a still-`sorry`
declaration. Proved decls and unverified sorries are excluded."""
from __future__ import annotations

from marathon.review.config import ReviewConfig, ReviewLabels
from marathon.review.fill_discovery import discover_fill_targets


def _cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo="owner/repo",
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={},
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

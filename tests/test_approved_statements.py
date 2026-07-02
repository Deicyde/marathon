"""Approved-statements manifest + signature extraction (foundation of the hard
fill-immutability guard). The key property: the signature is everything up to the
top-level `:=`, so a multi-line TYPE change is caught even when the decl-opening
line is byte-identical — which the legacy verified_decls audit (opening-line only)
misses."""
from __future__ import annotations

import subprocess

import marathon.review.github as ghmod
from marathon.review.approved_statements import (
    ApprovedStatement,
    capture_on_verify,
    extract_signature,
    find_decl_block,
    guard_touched_files,
    load_manifest,
    manifest_path,
    save_manifest,
)
from marathon.review.config import ReviewConfig, ReviewLabels


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


# --- extract_signature -------------------------------------------------------

def test_extract_signature_single_line_body():
    assert extract_signature("theorem foo : True := by sorry", "foo") == "theorem foo : True"


def test_extract_signature_multiline_type_normalized():
    src = "theorem foo {k : ℕ} :\n    P k := by\n  sorry\n"
    assert extract_signature(src, "foo") == "theorem foo {k : ℕ} : P k"


def test_extract_signature_ignores_assign_inside_binders():
    # the `:=` inside the (default-valued) binder must NOT be read as the body sep
    src = "def foo (n : ℕ := 0) : ℕ := n\n"
    assert extract_signature(src, "foo") == "def foo (n : ℕ := 0) : ℕ"


def test_extract_signature_issue_block_no_body_is_whole_decl():
    block = "theorem foo {k : ℕ} :\n  smoothPullback k = 0"
    assert extract_signature(block, "foo") == "theorem foo {k : ℕ} : smoothPullback k = 0"


def test_extract_signature_catches_type_change_opening_line_identical():
    # The decl-opening line `theorem foo {k : ℕ} :` is identical; only the stated
    # type on the next line changed. The legacy opening-line audit would miss this.
    frozen = extract_signature("theorem foo {k : ℕ} :\n    P k := by sorry", "foo")
    changed = extract_signature("theorem foo {k : ℕ} :\n    Q k := by simp", "foo")
    assert frozen is not None and frozen != changed


def test_extract_signature_body_only_change_is_equal():
    # filling the sorry (body change after `:=`) leaves the signature unchanged
    a = extract_signature("theorem foo : P := by sorry", "foo")
    b = extract_signature("theorem foo : P := by exact trivial", "foo")
    assert a == b == "theorem foo : P"


def test_extract_signature_missing_returns_none():
    assert extract_signature("theorem bar : True := by sorry", "foo") is None


# --- find_decl_block ---------------------------------------------------------

def test_find_decl_block_spans_to_next_decl_and_end():
    lines = (
        "theorem a : True := by\n  trivial\n"
        "theorem b : True := by sorry\n"
        "end Foo\n"
    ).splitlines()
    assert find_decl_block(lines, "a") == (0, 2)
    assert find_decl_block(lines, "b") == (2, 3)   # stops before `end Foo`
    assert find_decl_block(lines, "missing") is None


# --- manifest + capture ------------------------------------------------------

def test_manifest_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    save_manifest(cfg.repo_dir, {"foo": ApprovedStatement("foo", "theorem foo : True", 5, 12, "t")})
    back = load_manifest(cfg.repo_dir)
    assert back["foo"].signature == "theorem foo : True"
    assert back["foo"].issue == 5 and back["foo"].chapter == 12


def test_capture_on_verify_prefers_source_signature(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    d = cfg.target_path(12)
    d.mkdir(parents=True)
    (d / "F.lean").write_text("theorem foo {k : ℕ} :\n    P k := by sorry\n")
    monkeypatch.setattr(ReviewConfig, "chapter_of_issue", lambda self, n: 12)
    monkeypatch.setattr(
        ghmod, "issue_body",
        lambda num, repo: "### Lean signatures\n```lean\ntheorem foo {k : ℕ} : P k\n```\n",
    )
    frozen = capture_on_verify(cfg, 42)
    assert frozen == ["foo"]
    man = load_manifest(cfg.repo_dir)
    assert man["foo"].signature == "theorem foo {k : ℕ} : P k"
    assert man["foo"].issue == 42 and man["foo"].chapter == 12


def test_capture_on_verify_falls_back_to_issue_block(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)  # no chapter source files written
    monkeypatch.setattr(ReviewConfig, "chapter_of_issue", lambda self, n: 12)
    monkeypatch.setattr(ghmod, "issue_body", lambda num, repo: "```lean\ntheorem bar : True\n```")
    assert capture_on_verify(cfg, 7) == ["bar"]
    assert load_manifest(cfg.repo_dir)["bar"].signature == "theorem bar : True"


def test_capture_on_verify_no_body_is_noop(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(ghmod, "issue_body", lambda num, repo: None)
    assert capture_on_verify(cfg, 9) == []
    assert not manifest_path(cfg.repo_dir).exists()


# --- the hard guard (A2): adversarial ---------------------------------------

def _git_init(repo):
    repo.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _commit(repo, msg="c"):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True, capture_output=True)


def _chapter_file(cfg):
    d = cfg.target_path(12)
    d.mkdir(parents=True, exist_ok=True)
    return d / "F.lean", "Demo/Chapter12/F.lean"


def test_guard_reverts_signature_change_but_keeps_body_fill(tmp_path):
    """The core safety property: in a file where one decl was honestly filled and
    another had its STATEMENT changed, only the statement-violator is reverted (to
    its frozen skeleton); the honest fill survives."""
    cfg = _cfg(tmp_path)
    repo = cfg.repo_dir
    _git_init(repo)
    f, rel = _chapter_file(cfg)
    f.write_text("theorem a : P := by sorry\ntheorem b {k : ℕ} : Q k := by sorry\n")
    _commit(repo, "skeleton")
    save_manifest(cfg.repo_dir, {
        "a": ApprovedStatement("a", "theorem a : P", 1, 12, "t"),
        "b": ApprovedStatement("b", "theorem b {k : ℕ} : Q k", 2, 12, "t"),
    })
    # a: honest body fill (sig unchanged) ; b: signature CHANGED + body filled
    f.write_text("theorem a : P := by exact hP\ntheorem b {k : ℕ} : QChanged k := by exact hQ\n")
    reverted = guard_touched_files(repo, "HEAD", [rel])

    assert [r.decl_name for r in reverted] == ["b"]
    assert reverted[0].issue == 2 and reverted[0].file == rel
    text = f.read_text()
    assert "theorem a : P := by exact hP" in text             # honest fill kept
    assert "theorem b {k : ℕ} : Q k := by sorry" in text      # violator restored to skeleton
    assert "QChanged" not in text                              # changed statement is gone


def test_guard_reverts_multiline_type_change_opening_line_identical(tmp_path):
    """The case the legacy opening-line audit missed: the `theorem a {k : ℕ} :`
    line is byte-identical; only the stated type on the next line changed."""
    cfg = _cfg(tmp_path)
    repo = cfg.repo_dir
    _git_init(repo)
    f, rel = _chapter_file(cfg)
    f.write_text("theorem a {k : ℕ} :\n    P k := by sorry\n")
    _commit(repo, "skeleton")
    save_manifest(cfg.repo_dir, {"a": ApprovedStatement("a", "theorem a {k : ℕ} : P k", 1, 12, "t")})
    f.write_text("theorem a {k : ℕ} :\n    Q k := by exact h\n")  # type changed
    reverted = guard_touched_files(repo, "HEAD", [rel])

    assert [r.decl_name for r in reverted] == ["a"]
    text = f.read_text()
    assert "P k := by sorry" in text and "Q k" not in text


def test_guard_body_only_fill_is_not_reverted(tmp_path):
    cfg = _cfg(tmp_path)
    repo = cfg.repo_dir
    _git_init(repo)
    f, rel = _chapter_file(cfg)
    f.write_text("theorem a : P := by sorry\n")
    _commit(repo, "skeleton")
    save_manifest(cfg.repo_dir, {"a": ApprovedStatement("a", "theorem a : P", 1, 12, "t")})
    f.write_text("theorem a : P := by exact hP\n")  # body only
    assert guard_touched_files(repo, "HEAD", [rel]) == []
    assert "by exact hP" in f.read_text()  # fill kept untouched


def test_guard_empty_manifest_is_noop(tmp_path):
    cfg = _cfg(tmp_path)
    assert guard_touched_files(cfg.repo_dir, "HEAD", ["x.lean"]) == []


# --- per-declaration freeze at reject-ship (the freeze-gap fix) ---------------

def test_capture_kept_on_reject_freezes_kept_unfreezes_rejected(tmp_path, monkeypatch):
    from marathon.review.approved_statements import capture_kept_on_reject

    cfg = _cfg(tmp_path)
    d = cfg.target_path(15)
    d.mkdir(parents=True)
    (d / "H.lean").write_text(
        "def keepA (hyp : T) : U := by sorry\n"
        "theorem keepB (hyp : T) : P hyp := by sorry\n"
        "theorem fixMe (hyp : T) : Q hyp := by sorry\n"
    )
    monkeypatch.setattr(ReviewConfig, "chapter_of_issue", lambda self, n: 15)

    frozen, unfrozen = capture_kept_on_reject(
        cfg, 170, kept=["keepA", "keepB"], rejected=["fixMe"],
    )
    assert sorted(frozen) == ["keepA", "keepB"]
    assert unfrozen == []                       # fixMe wasn't frozen before
    man = load_manifest(cfg.repo_dir)
    assert man["keepA"].signature == "def keepA (hyp : T) : U"
    assert man["keepB"].signature == "theorem keepB (hyp : T) : P hyp"
    assert "fixMe" not in man                   # a rejected decl is never frozen
    assert man["keepA"].issue == 170


def test_capture_kept_on_reject_unfreezes_a_previously_kept_decl(tmp_path, monkeypatch):
    """The human keeps X (freezing it), then on a later re-review REJECTS X —
    X must be UNFROZEN so the refine can change it (not reverted by the guard)."""
    from marathon.review.approved_statements import capture_kept_on_reject

    cfg = _cfg(tmp_path)
    d = cfg.target_path(15)
    d.mkdir(parents=True)
    (d / "H.lean").write_text("def X (a : T) : U := by sorry\n")
    monkeypatch.setattr(ReviewConfig, "chapter_of_issue", lambda self, n: 15)

    # First review: keep X → frozen.
    capture_kept_on_reject(cfg, 170, kept=["X"], rejected=[])
    assert "X" in load_manifest(cfg.repo_dir)

    # Re-review: reject X → unfrozen.
    frozen, unfrozen = capture_kept_on_reject(cfg, 170, kept=[], rejected=["X"])
    assert unfrozen == ["X"] and frozen == []
    assert "X" not in load_manifest(cfg.repo_dir)


def test_capture_kept_on_reject_never_raises(tmp_path, monkeypatch):
    from marathon.review.approved_statements import capture_kept_on_reject

    cfg = _cfg(tmp_path)  # no source, chapter lookup will fail
    def boom(self, n):
        raise RuntimeError("no chapter")
    monkeypatch.setattr(ReviewConfig, "chapter_of_issue", boom)
    # Missing source → nothing frozen, but no exception.
    assert capture_kept_on_reject(cfg, 1, kept=["Y"], rejected=[]) == ([], [])

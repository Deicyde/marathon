"""Chapter-scoped dependency closure — the "based on chapter N ONLY" guarantees.

A Chapter-15 iteration must be judged by Ch15 + upstream only: the Aristotle
bundle must exclude downstream/orphaned modules (the #170 stall: broken Ch16
files rode along and wedged Aristotle at COMPLETE_WITH_ERRORS), and the build
gate's no-changes fallback must build the target chapter, not the default
closure.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from marathon import module_closure as mc
from marathon.post_pipeline import _target_lean_modules


def _mk_repo(tmp_path: Path) -> Path:
    """Demo/Chapter14 ← Chapter15 ← Chapter16 (downstream) + orphan + root lib."""
    repo = tmp_path / "repo"
    (repo / "Demo" / "Chapter14").mkdir(parents=True)
    (repo / "Demo" / "Chapter15").mkdir(parents=True)
    (repo / "Demo" / "Chapter16").mkdir(parents=True)
    (repo / "lakefile.toml").write_text(
        'name = "Demo"\ndefaultTargets = ["Demo"]\n[[lean_lib]]\nname = "Demo"\n'
    )
    (repo / "Demo.lean").write_text("import Demo.Main\n")
    (repo / "Demo" / "Main.lean").write_text("import Demo.Chapter14.Alt\n")
    (repo / "Demo" / "Chapter14" / "Alt.lean").write_text(
        "import Mathlib\ndef alt14 : Nat := 1\n"
    )
    (repo / "Demo" / "Chapter15" / "Hyper.lean").write_text(
        "import Mathlib\nimport Demo.Chapter14.Alt\ndef hyper : Nat := alt14\n"
    )
    (repo / "Demo" / "Chapter16" / "Stokes.lean").write_text(
        "import Demo.Chapter15.Hyper\ndef broken : Nat := this_does_not_exist\n"
    )
    (repo / "Demo" / "Chapter16" / "Orphan.lean").write_text(
        "def orphan : Nat := also_broken\n"
    )
    return repo


def test_scope_excludes_downstream_and_orphans(tmp_path):
    repo = _mk_repo(tmp_path)
    allowed, excluded = mc.target_scope(repo, repo / "Demo" / "Chapter15")
    assert "Demo.Chapter15.Hyper" in allowed
    assert "Demo.Chapter14.Alt" in allowed          # upstream dep
    assert "Demo" in allowed and "Demo.Main" in allowed  # root lib seeds
    assert "Demo.Chapter16.Stokes" not in allowed   # downstream consumer
    assert "Demo.Chapter16.Orphan" not in allowed   # orphan
    assert sorted(excluded) == [
        "Demo/Chapter16/Orphan.lean", "Demo/Chapter16/Stokes.lean",
    ]


def test_scope_of_downstream_chapter_includes_upstream(tmp_path):
    """Repairing Chapter16 must ship its Ch15 dependency (upstream is fine —
    only DOWNstream is excluded)."""
    repo = _mk_repo(tmp_path)
    allowed, excluded = mc.target_scope(repo, repo / "Demo" / "Chapter16")
    assert {"Demo.Chapter16.Stokes", "Demo.Chapter16.Orphan",
            "Demo.Chapter15.Hyper", "Demo.Chapter14.Alt"} <= allowed
    assert excluded == []


def test_scope_can_include_prompt_cited_context_without_downstream(tmp_path):
    """A rejection note may point at an upstream helper not imported yet. Ship
    that file as read-only context without reopening the downstream floodgate."""
    repo = _mk_repo(tmp_path)
    (repo / "Demo" / "Chapter1").mkdir(parents=True)
    (repo / "Demo" / "Chapter1" / "BoundarySmooth.lean").write_text(
        "import Demo.Chapter14.Alt\ndef boundarySmooth : Nat := alt14\n"
    )

    allowed, excluded = mc.target_scope(
        repo,
        repo / "Demo" / "Chapter15",
        extra_seed_modules={"Demo.Chapter1.BoundarySmooth"},
    )

    assert "Demo.Chapter1.BoundarySmooth" in allowed
    assert "Demo.Chapter14.Alt" in allowed          # context file's upstream dep
    assert "Demo.Chapter16.Stokes" not in allowed   # downstream still excluded
    assert "Demo/Chapter1/BoundarySmooth.lean" not in excluded
    assert "Demo/Chapter16/Stokes.lean" in excluded


def test_closure_ignores_external_imports(tmp_path):
    repo = _mk_repo(tmp_path)
    mmap = mc.local_module_map(repo)
    closure = mc.import_closure(mmap, {"Demo.Chapter15.Hyper"})
    assert closure == {"Demo.Chapter15.Hyper", "Demo.Chapter14.Alt"}
    assert not any(m.startswith("Mathlib") for m in closure)


def test_submission_dir_scoped_to_target(tmp_path):
    """The staged Aristotle bundle drops downstream/orphaned .lean files but
    keeps upstream deps, root modules, and non-.lean scaffolding."""
    from marathon.refine import _build_refine_submission_dir

    repo = _mk_repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    work = tmp_path / "work"
    work.mkdir()
    staged = _build_refine_submission_dir(
        repo_dir=repo, tex_path=None, workdir_log=None,
        work_dir=work, target_dir=repo / "Demo" / "Chapter15",
    )
    assert (staged / "Demo" / "Chapter15" / "Hyper.lean").is_file()
    assert (staged / "Demo" / "Chapter14" / "Alt.lean").is_file()
    assert (staged / "Demo.lean").is_file()
    assert (staged / "lakefile.toml").is_file()      # scaffolding always ships
    assert not (staged / "Demo" / "Chapter16" / "Stokes.lean").exists()
    assert not (staged / "Demo" / "Chapter16" / "Orphan.lean").exists()


def test_submission_dir_includes_prompt_cited_context(tmp_path):
    """Regression for #171-like focused rejections: if the prompt says "look
    at BoundarySmooth.lean", the scoped Aristotle bundle must contain it."""
    from marathon.refine import _build_refine_submission_dir

    repo = _mk_repo(tmp_path)
    (repo / "Demo" / "Chapter1").mkdir(parents=True)
    cited = repo / "Demo" / "Chapter1" / "BoundarySmooth.lean"
    cited.write_text(
        "import Demo.Chapter14.Alt\ndef boundarySmooth : Nat := alt14\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    work = tmp_path / "work"
    work.mkdir()
    staged = _build_refine_submission_dir(
        repo_dir=repo,
        tex_path=None,
        workdir_log=None,
        work_dir=work,
        target_dir=repo / "Demo" / "Chapter15",
        bundle_context_text=f"Use {cited}:12 as the existing boundary API.",
    )
    assert (staged / "Demo" / "Chapter1" / "BoundarySmooth.lean").is_file()
    assert (staged / "Demo" / "Chapter14" / "Alt.lean").is_file()
    assert not (staged / "Demo" / "Chapter16" / "Stokes.lean").exists()
    assert not (staged / "Demo" / "Chapter16" / "Orphan.lean").exists()


def test_submission_dir_unscoped_without_target(tmp_path):
    """No target_dir → full-repo staging (back-compat)."""
    from marathon.refine import _build_refine_submission_dir

    repo = _mk_repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    work = tmp_path / "work"
    work.mkdir()
    staged = _build_refine_submission_dir(
        repo_dir=repo, tex_path=None, workdir_log=None,
        work_dir=work,
    )
    assert (staged / "Demo" / "Chapter16" / "Stokes.lean").is_file()


def test_target_lean_modules_fallback(tmp_path):
    repo = _mk_repo(tmp_path)
    mods = _target_lean_modules(repo, repo / "Demo" / "Chapter15")
    assert mods == ["Demo.Chapter15.Hyper"]
    assert _target_lean_modules(repo, repo / "Demo" / "NoSuch") == []

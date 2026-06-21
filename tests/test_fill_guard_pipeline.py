"""The statement-immutability guard wired into run_post_pipeline: an extracted
fill that changed a frozen signature is reverted BEFORE build/commit, so the
violation never lands. The decl returns to its approved statement + `sorry` body."""
from __future__ import annotations

import subprocess

from marathon.post_pipeline import PipelineConfig, run_post_pipeline
from marathon.review.approved_statements import ApprovedStatement, save_manifest


def _setup(tmp_path):
    repo = tmp_path / "repo"
    (repo / "Demo" / "Chapter12").mkdir(parents=True)
    for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    f = repo / "Demo" / "Chapter12" / "F.lean"
    f.write_text("theorem a : P := by sorry\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "skel"], cwd=repo, check=True, capture_output=True)
    save_manifest(repo, {"a": ApprovedStatement("a", "theorem a : P", 1, 12, "t")})
    # simulate an extracted fill that CHANGED the frozen signature
    f.write_text("theorem a : PChanged := by exact h\n")
    return repo, f


def test_pipeline_guard_reverts_frozen_signature_change(tmp_path):
    repo, f = _setup(tmp_path)
    cfg = PipelineConfig(gate="off", update_formalization=False)  # guard defaults on
    out = run_post_pipeline(
        cfg, repo, repo / "Demo" / "Chapter12", "Chapter12",
        iteration=1, project_id=None,
    )
    assert out["frozen_reverts"] == [
        {"decl": "a", "issue": 1, "file": "Demo/Chapter12/F.lean"}
    ]
    text = f.read_text()
    assert "PChanged" not in text
    assert "theorem a : P := by sorry" in text  # restored to the approved statement


def test_pipeline_guard_disabled_leaves_change(tmp_path):
    repo, f = _setup(tmp_path)
    cfg = PipelineConfig(gate="off", update_formalization=False,
                         enforce_frozen_statements=False)
    out = run_post_pipeline(
        cfg, repo, repo / "Demo" / "Chapter12", "Chapter12",
        iteration=1, project_id=None,
    )
    assert out["frozen_reverts"] == []
    assert "PChanged" in f.read_text()  # untouched when enforcement is off

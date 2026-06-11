"""Tests for the phase-2 gate/jury wiring in the post-extraction pipeline.

The engine (marathon.gate) and jury (marathon.jury) are tested in their
own modules; this file pins down the WIRING contracts in
``post_pipeline.run_post_pipeline``:

* the gate baseline snapshot persists in the workdir and the sorry
  DELTA flows into the second run's report — and the snapshot is
  mode-keyed, so a baseline written under a different gate mode is
  dropped (with a printed note) instead of feeding a misleading delta;
* ``enforce`` blocks ONLY the PR open/update step — the commit boundary
  is still crossed (work preserved);
* ``--gate-override`` unblocks the PR and the reason lands in the PR
  body's Gate section;
* ``--review-rejection`` runs demote enforce → warn (human-demanded
  iterations are never blocked — the PR-#99 lesson);
* the jury jsonl is appended only when ``--jury`` is on;
* ``off`` skips the gate entirely (no engine call, no state file, no
  Gate section in the PR body);
* the CLI parsers default the new flags to the warn/off posture;
* ``marathon skeleton`` threads the gate/jury flags into its
  PipelineConfig (skeleton_mode always True — it IS the skeleton
  command), not just ``refine``.

All git/gh boundaries (``run_lake_build``, ``run_git_commit``,
``run_git_push``, ``_infer_repo``, ``open_or_update_pr``) are
monkeypatched — no subprocesses, no network. ``repo_dir`` is a plain tmp
dir, so the auto-pr block's incidental ``git diff`` calls fail closed
into their documented fallbacks.
"""

import json
from pathlib import Path

import marathon.gate as gate_mod
import marathon.jury as jury_mod
import marathon.post_pipeline as pp
from marathon.__main__ import _build_parser
from marathon.jury import JuryVerdict
from marathon.post_pipeline import (
    GATE_STATE_FILENAME,
    JURY_LOG_FILENAME,
    BuildResult,
    CommitResult,
    PipelineConfig,
    run_post_pipeline,
)


# --- fixture helpers ----------------------------------------------------------

THEOREM_SORRY = "theorem thmA : True := by\n  sorry\n"
DEF_SORRY = "def defB : Nat := by\n  sorry\n"


def _make_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Plain (non-git) repo dir + target chapter folder + workdir."""
    repo = tmp_path / "repo"
    target = repo / "Chapter1"
    target.mkdir(parents=True)
    (target / "A.lean").write_text(THEOREM_SORRY)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    return repo, target, workdir


def _install_boundaries(
    monkeypatch, calls: dict, *, build_ok: bool = True
) -> None:
    """Replace every subprocess-crossing boundary the pipeline owns and
    record crossings into ``calls`` — the block-vs-not assertions read
    from here."""

    def fake_build(repo_dir, timeout):
        calls.setdefault("build", []).append(repo_dir)
        return BuildResult(
            ok=build_ok,
            duration_seconds=1.0,
            log_tail=None if build_ok else "error: kaboom",
        )

    def fake_commit(repo_dir, target_path, message, project_id=None,
                    claude_in_loop=False, extra_paths=None):
        calls.setdefault("commit", []).append(message)
        return CommitResult(sha="abc1234")

    def fake_pr(*, repo_dir, branch, base, repo, title, body):
        calls.setdefault("pr", []).append(
            {"branch": branch, "base": base, "title": title, "body": body}
        )
        return True, "https://github.com/owner/repo/pull/1"

    monkeypatch.setattr(pp, "run_lake_build", fake_build)
    monkeypatch.setattr(pp, "run_git_commit", fake_commit)
    monkeypatch.setattr(pp, "run_git_push", lambda repo_dir: (True, "ok"))
    monkeypatch.setattr(pp, "_infer_repo", lambda repo_dir: "owner/repo")
    monkeypatch.setattr(pp, "open_or_update_pr", fake_pr)


def _config(workdir: Path, **kwargs) -> PipelineConfig:
    """PipelineConfig with workdir-side gate/jury paths pre-wired (the
    refine.py threading) and formalization refresh off (filesystem-only
    tests don't want the yaml machinery)."""
    defaults = dict(
        gate_state_path=workdir / GATE_STATE_FILENAME,
        jury_log_path=workdir / JURY_LOG_FILENAME,
        update_formalization=False,
    )
    defaults.update(kwargs)
    return PipelineConfig(**defaults)


def _run(config, repo, target, iteration=1):
    return run_post_pipeline(
        config=config,
        repo_dir=repo,
        target_path=target,
        chapter_label="Chapter1",
        iteration=iteration,
        project_id="proj-1234",
    )


# --- gate state snapshot + delta -----------------------------------------------


def test_gate_state_persists_and_delta_flows_on_second_run(tmp_path):
    repo, target, workdir = _make_repo(tmp_path)
    config = _config(workdir, gate="warn")

    out1 = _run(config, repo, target, iteration=1)
    report1 = out1["gate"]
    assert report1 is not None
    # First run has no baseline: counts reported, delta not evaluated.
    assert "no baseline" in report1.check("sorries").summary
    state = json.loads((workdir / GATE_STATE_FILENAME).read_text())
    assert state["target"] == "Chapter1"
    assert state["sorry_counts"] == {"total": 1, "definitions": 0}
    assert state["mode"] == "proof"
    assert state["iteration"] == 1

    # Second run gains one definition-body sorry: the persisted baseline
    # must feed the delta — proof mode treats +1 as a regression.
    (target / "B.lean").write_text(DEF_SORRY)
    out2 = _run(config, repo, target, iteration=2)
    sorries = out2["gate"].check("sorries")
    assert sorries.status == "fail"
    assert "Δtotal=+1" in sorries.summary
    # And the snapshot now reflects THIS run, for the next one.
    state2 = json.loads((workdir / GATE_STATE_FILENAME).read_text())
    assert state2["sorry_counts"] == {"total": 2, "definitions": 1}
    assert state2["iteration"] == 2


def test_stale_snapshot_for_another_target_is_ignored(tmp_path):
    """A recycled workdir must not feed another chapter's counts into
    the delta — target mismatch means no baseline."""
    repo, target, workdir = _make_repo(tmp_path)
    (workdir / GATE_STATE_FILENAME).write_text(json.dumps({
        "target": "Chapter9",
        "sorry_counts": {"total": 50, "definitions": 50},
    }))
    out = _run(_config(workdir, gate="warn"), repo, target)
    assert "no baseline" in out["gate"].check("sorries").summary


def test_skeleton_mode_flows_into_gate_mode(tmp_path):
    repo, target, workdir = _make_repo(tmp_path)
    out = _run(_config(workdir, gate="warn", skeleton_mode=True), repo, target)
    assert out["gate"].mode == "skeleton"


def test_baseline_from_different_mode_is_ignored(tmp_path, capsys):
    """The snapshot is mode-keyed: a baseline written by a skeleton run
    must not feed a proof run's delta (skeleton EXPECTS new theorem-body
    sorries; the same counts mean something different per mode). The
    mismatch drops to no-baseline with a printed note."""
    repo, target, workdir = _make_repo(tmp_path)

    _run(_config(workdir, gate="warn", skeleton_mode=True), repo, target, iteration=1)
    state = json.loads((workdir / GATE_STATE_FILENAME).read_text())
    assert state["mode"] == "skeleton"

    # Same target, mode flipped to proof: the skeleton baseline is
    # ignored, so the +1 def sorry reports as "no baseline", not a delta.
    (target / "B.lean").write_text(DEF_SORRY)
    out = _run(_config(workdir, gate="warn"), repo, target, iteration=2)
    assert "no baseline" in out["gate"].check("sorries").summary
    console = capsys.readouterr().out
    assert "ignoring sorry baseline" in console
    assert "'skeleton'" in console and "'proof'" in console


# --- enforce blocks PR-open, not commit ------------------------------------------


def test_enforce_blocks_pr_open_but_not_commit(tmp_path, monkeypatch, capsys):
    repo, target, workdir = _make_repo(tmp_path)
    calls: dict = {}
    _install_boundaries(monkeypatch, calls, build_ok=False)  # FAIL verdict
    config = _config(
        workdir, gate="enforce",
        auto_build=True, auto_commit=True, auto_pr=True,
    )

    out = _run(config, repo, target)

    assert out["gate"].verdict == "fail"
    assert out["gate_posture"] == "enforce"
    # The commit boundary was crossed; the PR boundary was NOT.
    assert len(calls["commit"]) == 1
    assert "pr" not in calls
    # The console says exactly what was blocked and why.
    console = capsys.readouterr().out
    assert "BLOCKED by gate" in console
    assert "marathon/refine-c1" in console  # the blocked branch
    assert "failing checks" in console and "build" in console
    assert "abc1234" in console and "NOT blocked" in console
    assert "--gate-override" in console


def test_warn_posture_never_blocks_on_fail(tmp_path, monkeypatch):
    repo, target, workdir = _make_repo(tmp_path)
    calls: dict = {}
    _install_boundaries(monkeypatch, calls, build_ok=False)
    config = _config(
        workdir, gate="warn",
        auto_build=True, auto_commit=True, auto_pr=True,
    )

    out = _run(config, repo, target)

    assert out["gate"].verdict == "fail"
    assert len(calls["pr"]) == 1
    # The report still lands in the PR body, as a "## Gate" section.
    body = calls["pr"][0]["body"]
    assert "## Gate" in body
    assert "Marathon gate" in body and "FAIL" in body


def test_enforce_with_passing_gate_opens_pr(tmp_path, monkeypatch):
    repo, target, workdir = _make_repo(tmp_path)
    calls: dict = {}
    _install_boundaries(monkeypatch, calls, build_ok=True)
    # Green build makes the gate run the axiom check; stub the one
    # `lake env lean` seam (same contract the gate's own tests pin).
    monkeypatch.setattr(
        gate_mod.formalization, "check_axioms",
        lambda repo_dir, pairs, **kw: {d: ["propext"] for d, _ in pairs},
    )
    config = _config(
        workdir, gate="enforce",
        auto_build=True, auto_commit=True, auto_pr=True,
    )

    out = _run(config, repo, target)

    assert out["gate"].verdict == "pass"
    assert len(calls["pr"]) == 1


# --- override -----------------------------------------------------------------


def test_gate_override_unblocks_and_reason_lands_in_pr_body(
    tmp_path, monkeypatch, capsys
):
    repo, target, workdir = _make_repo(tmp_path)
    calls: dict = {}
    _install_boundaries(monkeypatch, calls, build_ok=False)
    reason = "cross-chapter refactor; red is expected mid-transit"
    config = _config(
        workdir, gate="enforce", gate_override=reason,
        auto_build=True, auto_commit=True, auto_pr=True,
    )

    out = _run(config, repo, target)

    assert out["gate"].verdict == "fail"
    assert len(calls["pr"]) == 1  # unblocked
    body = calls["pr"][0]["body"]
    assert "## Gate" in body
    assert "Gate override" in body and reason in body
    console = capsys.readouterr().out
    assert "override accepted" in console and reason in console
    assert "BLOCKED" not in console


# --- review-rejection demotion ----------------------------------------------------


def test_review_rejection_forces_warn_under_enforce(
    tmp_path, monkeypatch, capsys
):
    repo, target, workdir = _make_repo(tmp_path)
    calls: dict = {}
    _install_boundaries(monkeypatch, calls, build_ok=False)
    config = _config(
        workdir, gate="enforce", review_rejection_run=True,
        auto_build=True, auto_commit=True, auto_pr=True,
        auto_pr_review_issue=42,
    )

    out = _run(config, repo, target)

    # Demoted to warn: a FAIL verdict reports but never blocks the
    # human-demanded iteration, and the note is printed.
    assert out["gate_posture"] == "warn"
    assert out["gate"].verdict == "fail"
    assert len(calls["pr"]) == 1
    console = capsys.readouterr().out
    assert "demoted enforce → warn" in console
    assert "--review-rejection" in console
    assert "BLOCKED" not in console


# --- jury ---------------------------------------------------------------------


def test_jury_jsonl_appended_with_flag_and_line_joins_pr_body(
    tmp_path, monkeypatch, capsys
):
    repo, target, workdir = _make_repo(tmp_path)
    calls: dict = {}
    _install_boundaries(monkeypatch, calls, build_ok=False)
    verdict = JuryVerdict(proof_integrity=4, code_quality=3, verdict="pass")
    monkeypatch.setattr(
        jury_mod, "run_jury", lambda repo_dir, target, **kw: verdict
    )
    config = _config(
        workdir, gate="warn", jury=True,
        auto_build=True, auto_commit=True, auto_pr=True,
    )

    out = _run(config, repo, target)

    assert out["jury"] is verdict
    lines = (workdir / JURY_LOG_FILENAME).read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["chapter"] == "Chapter1"
    assert entry["project_id"] == "proj-1234"
    assert entry["commit_sha"] == "abc1234"
    assert entry["jury"]["proof_integrity"] == 4
    assert entry["jury"]["verdict"] == "pass"
    # The verdict line joins console + PR body.
    assert "jury (advisory): integrity=4 quality=3 → PASS" in capsys.readouterr().out
    assert "integrity=4 quality=3 → PASS" in calls["pr"][0]["body"]


def test_jury_not_called_and_no_jsonl_without_flag(tmp_path, monkeypatch):
    repo, target, workdir = _make_repo(tmp_path)

    def explode(*args, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("run_jury must not be called without --jury")

    monkeypatch.setattr(jury_mod, "run_jury", explode)
    out = _run(_config(workdir, gate="warn", jury=False), repo, target)
    assert out["jury"] is None
    assert not (workdir / JURY_LOG_FILENAME).exists()


def test_jury_none_result_is_not_logged(tmp_path, monkeypatch):
    """An advisory jury failure (None) leaves no jsonl line — only real
    verdicts enter the trail."""
    repo, target, workdir = _make_repo(tmp_path)
    monkeypatch.setattr(jury_mod, "run_jury", lambda *a, **kw: None)
    out = _run(_config(workdir, gate="warn", jury=True), repo, target)
    assert out["jury"] is None
    assert not (workdir / JURY_LOG_FILENAME).exists()


# --- off ----------------------------------------------------------------------


def test_gate_off_skips_engine_state_and_pr_section(tmp_path, monkeypatch):
    repo, target, workdir = _make_repo(tmp_path)
    calls: dict = {}
    _install_boundaries(monkeypatch, calls, build_ok=True)

    def explode(*args, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("run_gate must not be called with --gate off")

    monkeypatch.setattr(gate_mod, "run_gate", explode)
    config = _config(
        workdir, gate="off",
        auto_build=True, auto_commit=True, auto_pr=True,
    )

    out = _run(config, repo, target)

    assert out["gate"] is None
    assert out["gate_posture"] == "off"
    assert not (workdir / GATE_STATE_FILENAME).exists()
    # PR still opens, with no Gate section at all.
    assert len(calls["pr"]) == 1
    assert "## Gate" not in calls["pr"][0]["body"]


# --- CLI flag plumbing -----------------------------------------------------------


def test_refine_parser_gate_flags_default_warn():
    args = _build_parser().parse_args(["refine", "T", "--repo-dir", "R"])
    assert args.gate == "warn"
    assert args.gate_override is None
    assert args.jury is False


def test_refine_parser_gate_flags_roundtrip():
    args = _build_parser().parse_args([
        "refine", "T", "--repo-dir", "R",
        "--gate", "enforce", "--gate-override", "operator says so", "--jury",
    ])
    assert args.gate == "enforce"
    assert args.gate_override == "operator says so"
    assert args.jury is True


def test_fill_parser_gate_stays_warn_despite_defaults_on():
    """fill defaults its auto-* family on, but the gate posture stays
    warn and the jury stays opt-in."""
    args = _build_parser().parse_args(
        ["fill", "T", "--repo-dir", "R", "--decl", "Foo.bar"]
    )
    assert args.auto_build is True and args.auto_pr is True  # philosophy
    assert args.gate == "warn"
    assert args.gate_override is None
    assert args.jury is False


def test_skeleton_threads_gate_flags_into_pipeline_config(tmp_path, monkeypatch):
    """``marathon skeleton --gate enforce --jury`` must actually reach
    the pipeline: parsing alone is not wiring. The per-chapter boundary
    (``_run_chapter``) is monkeypatched to capture the PipelineConfig —
    skeleton_mode is always True (it IS the skeleton command), the gate
    flags come from the CLI, and the state/jury paths sit in the input
    folder beside marathon-state.json."""
    import asyncio

    import marathon.skeleton as skeleton_mod

    folder = tmp_path / "input"
    folder.mkdir()
    (folder / "order.txt").write_text("ch1.tex -> Chapter1\n")
    repo = tmp_path / "repo"
    repo.mkdir()

    captured: dict = {}

    async def fake_run_chapter(*, entry, pipeline_config, **kwargs):
        captured["config"] = pipeline_config
        return skeleton_mod.ChapterState(
            input_file=entry.input_file,
            output_folder=entry.output_folder,
            status="COMPLETE",
            duration_seconds=1.0,
            output_path="Out/Chapter1",
        )

    monkeypatch.setattr(skeleton_mod, "_ensure_api_key", lambda: "arstl_test_key")
    monkeypatch.setattr(skeleton_mod, "_run_chapter", fake_run_chapter)

    args = _build_parser().parse_args([
        "skeleton", str(folder), "--repo-dir", str(repo),
        "--output-base", "Out",
        "--gate", "enforce", "--gate-override", "operator says so", "--jury",
    ])
    asyncio.run(skeleton_mod.skeleton_command(args))

    config = captured["config"]
    assert config.skeleton_mode is True
    assert config.gate == "enforce"
    assert config.gate_override == "operator says so"
    assert config.jury is True
    assert config.gate_state_path == folder / GATE_STATE_FILENAME
    assert config.jury_log_path == folder / JURY_LOG_FILENAME

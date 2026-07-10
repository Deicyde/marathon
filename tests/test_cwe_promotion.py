"""COMPLETE_WITH_ERRORS promotion — the chapter's build state is the verdict.

Aristotle's self-report must never veto a green result: on
COMPLETE_WITH_ERRORS with extracted output, `_run_refine_attempt` probes the
chapter-scoped build; green promotes to the success path (post-pipeline runs),
red keeps the retry ladder (post-pipeline skipped). The #170 failure mode:
8/8 note-compliant, locally-green skeletons were discarded on self-report.
"""
from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from types import SimpleNamespace

from aristotlelib import TaskStatus
from aristotlelib import AristotleAPIError

import marathon.refine as refine
from marathon.post_pipeline import BuildResult, PipelineConfig
from marathon.state import RefineState


def _drive(
    monkeypatch,
    tmp_path,
    *,
    probe_ok: bool,
    changed=("Chapter1.A",),
    pipeline_result=None,
):
    """Run _run_refine_attempt with a CWE task; return (status, calls)."""
    calls: dict = {}
    repo = tmp_path / "repo"
    (repo / "Chapter1").mkdir(parents=True)

    async def fake_run_to_completion(**kw):
        return None

    async def fake_download(project, path):
        return path

    monkeypatch.setattr(refine, "run_task_to_completion", fake_run_to_completion)
    monkeypatch.setattr(refine, "download_result", fake_download)
    monkeypatch.setattr(refine, "_writable_paths_for_attempt",
                        lambda repo_dir, expected, rej: [])
    monkeypatch.setattr(refine, "_extract_solution",
                        lambda tar, expected, repo_dir, log, additional_writable_paths:
                        (True, True, [], []))
    monkeypatch.setattr(refine, "save_refine_state", lambda p, s: None)
    monkeypatch.setattr("marathon.post_pipeline._changed_lean_targets",
                        lambda repo_dir: list(changed))

    def fake_probe_build(repo_dir, timeout, targets=None):
        calls.setdefault("probe", []).append(targets)
        return BuildResult(ok=probe_ok, duration_seconds=1.0,
                           log_tail=None if probe_ok else "error: nope")

    monkeypatch.setattr("marathon.post_pipeline.run_lake_build", fake_probe_build)

    def fake_pipeline(**kw):
        calls.setdefault("pipeline", []).append(kw)
        return pipeline_result or {"push_ok": None, "pr_ok": None}

    monkeypatch.setattr(refine, "run_post_pipeline", fake_pipeline)

    task = SimpleNamespace(status=TaskStatus.COMPLETE_WITH_ERRORS,
                           agent_task_id="t-1")
    project = SimpleNamespace(project_id="p-1")
    state = RefineState(target_folder=str(repo / "Chapter1"))
    state.project_id = "p-1"
    calls["state"] = state

    status = asyncio.run(refine._run_refine_attempt(
        prompt=None,
        repo_dir=repo,
        tex_path=None,
        workdir_log=None,
        expected_path=PurePosixPath("Chapter1"),
        output_path_str="Chapter1",
        polling_interval=1,
        state=state,
        state_path=tmp_path / "state.json",
        attempt_idx=0,
        max_retries=0,
        pipeline_config=PipelineConfig(auto_build=True, build_timeout=60),
        iteration_idx=1,
        target_folder_name="Chapter1",
        existing_project=project,
        existing_task=task,
        existing_mode="reattach",
    ))
    return status, calls


def test_green_probe_promotes_to_success(monkeypatch, tmp_path):
    status, calls = _drive(monkeypatch, tmp_path, probe_ok=True)
    assert status == TaskStatus.COMPLETE            # promoted
    assert calls["probe"] == [["Chapter1.A"]]        # chapter-scoped probe ran
    assert len(calls.get("pipeline", [])) == 1       # post-pipeline judged it


def test_red_probe_keeps_retry_ladder(monkeypatch, tmp_path):
    status, calls = _drive(monkeypatch, tmp_path, probe_ok=False)
    assert status == TaskStatus.COMPLETE_WITH_ERRORS  # not promoted
    assert "pipeline" not in calls                    # no commit/PR of red work


def test_no_lean_change_is_never_promoted(monkeypatch, tmp_path):
    """A COMPLETE_WITH_ERRORS result that extracted ZERO Lean changes is a
    no-op — it must NOT be promoted (never probe-built, never landed). This is
    the a730a7ab defect: a no-op landed [build:OK] via the whole-chapter
    fallback and re-queued the issue with the note unsatisfied."""
    status, calls = _drive(monkeypatch, tmp_path, probe_ok=True, changed=[])
    assert status == TaskStatus.COMPLETE_WITH_ERRORS   # NOT promoted
    assert "probe" not in calls                        # never even built
    assert "pipeline" not in calls                     # nothing landed


def test_complete_with_red_post_pipeline_build_preserves_session(monkeypatch, tmp_path):
    """Aristotle COMPLETE is not success if Marathon's own `lake build` is red.

    Return FAILED while preserving the current project for project.ask(), so
    the next attempt keeps Aristotle's server-side context and local partial
    commit instead of paying for a fresh session.
    """
    calls: dict = {}
    repo = tmp_path / "repo"
    (repo / "Chapter1").mkdir(parents=True)
    log_path = tmp_path / "refine-log.md"

    async def fake_run_to_completion(**kw):
        return None

    async def fake_download(project, path):
        return path

    monkeypatch.setattr(refine, "run_task_to_completion", fake_run_to_completion)
    monkeypatch.setattr(refine, "download_result", fake_download)
    monkeypatch.setattr(refine, "_writable_paths_for_attempt",
                        lambda repo_dir, expected, rej: [])
    monkeypatch.setattr(refine, "_extract_solution",
                        lambda tar, expected, repo_dir, log, additional_writable_paths:
                        (True, True, [], []))
    monkeypatch.setattr(refine, "save_refine_state", lambda p, s: None)

    def fake_pipeline(**kw):
        calls.setdefault("pipeline", []).append(kw)
        return {
            "build": BuildResult(ok=False, duration_seconds=1.0,
                                 log_tail="error: missing instance"),
            "push_ok": None,
            "pr_ok": None,
        }

    monkeypatch.setattr(refine, "run_post_pipeline", fake_pipeline)

    task = SimpleNamespace(status=TaskStatus.COMPLETE, agent_task_id="t-1")
    project = SimpleNamespace(project_id="p-1")
    state = RefineState(target_folder=str(repo / "Chapter1"))
    state.project_id = "p-1"

    status = asyncio.run(refine._run_refine_attempt(
        prompt=None,
        repo_dir=repo,
        tex_path=None,
        workdir_log=None,
        expected_path=PurePosixPath("Chapter1"),
        output_path_str="Chapter1",
        polling_interval=1,
        state=state,
        state_path=tmp_path / "state.json",
        attempt_idx=0,
        max_retries=1,
        pipeline_config=PipelineConfig(auto_build=True, build_timeout=60),
        iteration_idx=1,
        target_folder_name="Chapter1",
        existing_project=project,
        existing_task=task,
        existing_mode="reattach",
        refine_log_path=log_path,
    ))

    assert status == TaskStatus.FAILED
    assert state.status == "POST_PIPELINE_BUILD_FAILED"
    assert state.force_continuation is True
    assert "local lake build failed" in state.note
    assert len(calls.get("pipeline", [])) == 1
    assert "error: missing instance" in log_path.read_text()


def test_gate_blocked_commit_is_retryable_on_same_session(monkeypatch, tmp_path):
    status, calls = _drive(
        monkeypatch,
        tmp_path,
        probe_ok=True,
        pipeline_result={
            "build": BuildResult(ok=True),
            "push_ok": None,
            "pr_ok": None,
            "pr_blocked": "gate",
        },
    )
    state = calls["state"]
    assert status == TaskStatus.FAILED
    assert state.status == "POST_PIPELINE_PR_BLOCKED"
    assert state.force_continuation is True
    assert state.pending_publication is None


def test_force_continuation_overrides_terminal_complete_status(monkeypatch):
    project = SimpleNamespace(project_id="p-1")
    task = SimpleNamespace(
        status=TaskStatus.COMPLETE,
        agent_task_id="t-1",
        output_summary="partial result",
    )

    async def reattach(**kwargs):
        return project, task

    monkeypatch.setattr(refine, "reattach_project_and_task", reattach)
    state = RefineState(
        target_folder="Chapter1",
        project_id="p-1",
        agent_task_id="t-1",
        status="POST_PIPELINE_PR_BLOCKED",
        force_continuation=True,
    )
    got_project, got_task, mode = asyncio.run(
        refine._try_reattach_or_continue(state)
    )
    assert (got_project, got_task, mode) == (project, task, "continue")


def test_transient_project_ask_failure_preserves_continuation_intent(
    monkeypatch, tmp_path,
):
    async def fail_ask(project, prompt):
        raise AristotleAPIError("temporary transport failure", status_code=503)

    monkeypatch.setattr(refine, "continue_via_ask", fail_ask)
    state = RefineState(
        target_folder="Chapter1",
        project_id="p-1",
        agent_task_id="t-old",
        force_continuation=True,
    )
    result = asyncio.run(
        refine._submit_continuation_via_ask(
            project=SimpleNamespace(project_id="p-1"),
            prompt="fix the gate failure",
            state=state,
            state_path=tmp_path / "state.json",
            attempt_idx=0,
            max_retries=1,
        )
    )
    assert result is None
    assert state.status == "ASK_FAILED"
    assert state.force_continuation is True
    assert state.project_id == "p-1" and state.agent_task_id == "t-old"


def test_download_failure_reattaches_terminal_task_instead_of_resubmitting(
    monkeypatch,
):
    project = SimpleNamespace(project_id="p-1")
    task = SimpleNamespace(
        status=TaskStatus.COMPLETE,
        agent_task_id="t-1",
        output_summary=None,
    )

    async def reattach(**kwargs):
        return project, task

    monkeypatch.setattr(refine, "reattach_project_and_task", reattach)
    state = RefineState(
        target_folder="Chapter1",
        project_id="p-1",
        agent_task_id="t-1",
        status="DOWNLOAD_FAILED",
    )
    got_project, got_task, mode = asyncio.run(
        refine._try_reattach_or_continue(state)
    )
    assert (got_project, got_task, mode) == (project, task, "reattach")

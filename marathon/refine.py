"""The ``marathon refine`` subcommand.

For up to ``--max-iterations`` rounds:

1. Call Claude to review the current state of the target Lean folder
   (plus the rest of the repo's Lean files and the past refinement log).
   Claude's response is sent verbatim to Aristotle as the next prompt.
2. Build an Aristotle submission containing the entire ``--repo-dir`` repo
   (gitignore-filtered), the optional ``--tex`` reference file the user
   supplied, and ``marathon.md`` from the workdir if present.
3. Submit, retry on ``COMPLETE_WITH_ERRORS`` / ``FAILED`` up to
   ``--max-retries`` extra attempts (same machinery as ``skeleton``),
   reattach to in-flight projects on rerun.
4. Extract the response back into the target folder, overwriting in place.
5. Append the iteration's Claude response, project ID, and final status to
   ``<workdir>/marathon-refine-log.md``.

Claude is never given the ``.tex`` file — Marathon reads its bytes only to
include them in the Aristotle bundle.
"""

import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Optional

from aristotlelib import AristotleAPIError, Project, ProjectStatus

from marathon.claude_review import review_and_draft_prompt
from marathon.post_pipeline import (
    PipelineConfig,
    append_promptlog_url,
    run_post_pipeline,
)
from marathon.skeleton import (
    IN_FLIGHT_STATUS_VALUES,
    LOG_FILENAME,
    NON_RETRYABLE_FAILURE_STATUSES,
    RETRYABLE_STATUSES,
    _ensure_api_key,
    _extract_solution,
    _list_repo_files,
    _mask_key,
)
from marathon.state import (
    RefineState,
    compute_duration_seconds,
    format_duration,
    load_refine_state,
    now_iso,
    save_refine_state,
)

REFINE_STATE_FILENAME = "marathon-refine-state.json"
REFINE_LOG_FILENAME = "marathon-refine-log.md"

OUTPUT_REQUIREMENTS_TRAILER = """

---

## Output requirements (added by Marathon)

Place every Lean file you produce at the relative path `{output_path}/` in
your response. This path has multiple components; preserve each one as a
nested directory — do not flatten it. If the path is `Foo/Bar/Baz`, your
output should contain `Foo/Bar/Baz/<your-files>.lean`, not `Baz/<your-files>.lean`
and not `Foo-Bar-Baz/...`. Marathon extracts that directory tree back into
the user's repo at the same relative path. Do not modify or recreate files
outside `{output_path}/`.

Update `marathon.md` at the root of your response (a single top-level file).
Append a section for this refinement iteration recording naming conventions,
design choices, and notes for future iterations. Preserve all prior entries.
"""

SKELETON_OUTPUT_REQUIREMENTS_TRAILER = """

---

## Output requirements (added by Marathon, skeleton mode)

This is a **skeleton refinement** iteration: every theorem, lemma,
proposition, and corollary body must remain `sorry`. **Do not attempt to
prove anything**, even one-line tactic proofs you think will succeed. Your
job is to improve signatures, definitions, names, and structure — not to
fill in proofs. If existing code in the target folder contains non-`sorry`
proof bodies, revert them to `sorry`.

Place every Lean file you produce at the relative path `{output_path}/` in
your response. This path has multiple components; preserve each one as a
nested directory — do not flatten it. Marathon extracts that directory
tree back into the user's repo at the same relative path. Do not modify
or recreate files outside `{output_path}/`.

Update `marathon.md` at the root of your response (a single top-level
file). Append a section for this refinement iteration recording naming
conventions, design choices, and notes for future iterations. Preserve all
prior entries.
"""


def _build_refine_submission_dir(
    repo_dir: Path,
    tex_path: Optional[Path],
    workdir_log: Optional[Path],
    work_dir: Path,
) -> Path:
    """Stage the Aristotle submission tree."""
    staged = work_dir / "submission"
    staged.mkdir()

    for rel in _list_repo_files(repo_dir):
        src = repo_dir / rel
        if not src.is_file():
            continue
        dst = staged / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    if tex_path is not None and tex_path.is_file():
        shutil.copy2(tex_path, staged / tex_path.name)

    if workdir_log is not None and workdir_log.is_file():
        shutil.copy2(workdir_log, staged / LOG_FILENAME)

    return staged


async def _try_reattach(state: RefineState) -> Optional[Project]:
    """Reattach to a project from a previous run if it's still in flight, or
    if it terminated but Marathon failed to extract its output. Returns None
    when no reattach is applicable."""
    if not state.project_id:
        return None
    reason = None
    if state.status in IN_FLIGHT_STATUS_VALUES:
        reason = "in-flight"
    elif state.status == "OUTPUT_FOLDER_MISSING":
        reason = "previous extraction failure"
    if reason is None:
        return None
    try:
        candidate = await Project.from_id(state.project_id)
        await candidate.refresh()
        print(
            f"  reattaching ({reason}) to project "
            f"project_id={state.project_id}, status={candidate.status.value}"
        )
        return candidate
    except AristotleAPIError as e:
        print(
            f"  could not reattach to project_id={state.project_id} "
            f"(status {e.status_code}: {e}); will submit fresh instead"
        )
        return None


async def _submit_fresh_refine(
    prompt: str,
    repo_dir: Path,
    tex_path: Optional[Path],
    workdir_log: Optional[Path],
    state: RefineState,
    state_path: Path,
    attempt_idx: int,
    max_retries: int,
) -> Optional[Project]:
    state.attempts += 1
    state.output_path = None
    state.note = None

    label = "attempt" if attempt_idx == 0 else f"retry {attempt_idx}"
    print(f"  {label} ({attempt_idx + 1}/{max_retries + 1}) submitting to Aristotle")

    with tempfile.TemporaryDirectory(prefix="marathon-refine-stage-") as stage_tmp:
        staged = _build_refine_submission_dir(
            repo_dir=repo_dir,
            tex_path=tex_path,
            workdir_log=workdir_log,
            work_dir=Path(stage_tmp),
        )
        try:
            project = await Project.create_from_directory(prompt=prompt, project_dir=staged)
        except AristotleAPIError as e:
            state.status = "SUBMIT_FAILED"
            state.note = f"submit error (status {e.status_code}): {e}"
            save_refine_state(state_path, state)
            return None

    state.project_id = project.project_id
    state.status = project.status.value if project.status else "QUEUED"
    state.started_at = now_iso()
    save_refine_state(state_path, state)
    print(f"    submitted: project_id={project.project_id}")
    if append_promptlog_url(repo_dir, project.project_id):
        print(f"    PromptLog.md updated")
    return project


async def _run_refine_attempt(
    prompt: Optional[str],
    repo_dir: Path,
    tex_path: Optional[Path],
    workdir_log: Optional[Path],
    expected_path: PurePosixPath,
    output_path_str: str,
    polling_interval: int,
    state: RefineState,
    state_path: Path,
    attempt_idx: int,
    max_retries: int,
    pipeline_config: PipelineConfig,
    iteration_idx: int,
    target_folder_name: str,
    existing_project: Optional[Project] = None,
) -> Optional[ProjectStatus]:
    """Run one Aristotle attempt for the current iteration. Returns the
    terminal ``ProjectStatus``, or ``None`` for Marathon-level errors."""
    if existing_project is not None:
        project = existing_project
        state.output_path = None
        state.note = None
        save_refine_state(state_path, state)
        print(f"  reattached: continuing project_id={project.project_id}")
    else:
        if prompt is None:
            sys.exit("internal error: prompt required for fresh submit")
        project = await _submit_fresh_refine(
            prompt=prompt,
            repo_dir=repo_dir,
            tex_path=tex_path,
            workdir_log=workdir_log,
            state=state,
            state_path=state_path,
            attempt_idx=attempt_idx,
            max_retries=max_retries,
        )
        if project is None:
            return None

    with tempfile.TemporaryDirectory(prefix="marathon-refine-dl-") as dl_tmp:
        download_path = Path(dl_tmp) / "solution.tar.gz"
        try:
            result = await project.wait_for_completion(
                destination=str(download_path),
                polling_interval_seconds=polling_interval,
            )
        except AristotleAPIError as e:
            state.status = "POLL_FAILED"
            state.note = f"poll error (status {e.status_code}): {e}"
            save_refine_state(state_path, state)
            return None

        await project.refresh()
        state.status = project.status.value
        state.completed_at = now_iso()
        state.duration_seconds = compute_duration_seconds(
            state.started_at, state.completed_at
        )

        if result is not None:
            log_dest = workdir_log if workdir_log is not None else Path(dl_tmp) / "_unused.md"
            found, log_updated, unexpected = _extract_solution(
                Path(result), expected_path, repo_dir, log_dest
            )
            if found:
                state.output_path = str(repo_dir / Path(*expected_path.parts))
                notes: list[str] = []
                if not log_updated:
                    notes.append(f"warning: {LOG_FILENAME} not updated by Aristotle")
                if unexpected:
                    notes.append(
                        f"{len(unexpected)} unexpected top-level entries "
                        f"(mostly echoed input): {unexpected}"
                    )
                if notes:
                    state.note = "; ".join(notes)
            else:
                state.status = "OUTPUT_FOLDER_MISSING"
                state.note = (
                    f"expected path {output_path_str!r} not in solution tar; "
                    f"top-level entries: {unexpected}"
                )
                save_refine_state(state_path, state)
                return None

    save_refine_state(state_path, state)

    if (
        state.output_path is not None
        and project.status == ProjectStatus.COMPLETE
        and pipeline_config.has_any()
    ):
        run_post_pipeline(
            config=pipeline_config,
            repo_dir=repo_dir,
            target_path=Path(state.output_path),
            chapter_label=target_folder_name,
            iteration=iteration_idx,
            project_id=state.project_id,
        )

    return project.status


async def _run_iteration(
    iteration_idx: int,
    target_folder: Path,
    repo_dir: Path,
    tex_path: Optional[Path],
    workdir_log: Optional[Path],
    log_path: Path,
    expected_path: PurePosixPath,
    output_path_str: str,
    polling_interval: int,
    max_retries: int,
    max_iterations: int,
    skeleton_mode: bool,
    max_prompt_words: Optional[int],
    pipeline_config: PipelineConfig,
    target_folder_name: str,
    state: RefineState,
    state_path: Path,
    existing_project: Optional[Project],
) -> bool:
    """Run a single refinement iteration. Each attempt (other than a
    reattach to an in-flight project) gets its own Claude review against
    the current target-folder state. Returns True on success, False if
    the iteration failed permanently."""
    last_status: Optional[str] = None

    for attempt_idx in range(max_retries + 1):
        use_existing = existing_project is not None and attempt_idx == 0

        if use_existing:
            full_prompt: Optional[str] = None
        else:
            label = "attempt 1" if attempt_idx == 0 else f"retry {attempt_idx}"
            print(
                f"  iteration {iteration_idx} {label} "
                f"({attempt_idx + 1}/{max_retries + 1}): "
                "calling Claude for review + drafted prompt..."
            )

            marathon_md = workdir_log.read_text() if workdir_log and workdir_log.is_file() else None
            refine_log_text = log_path.read_text() if log_path.is_file() else ""

            claude_response = review_and_draft_prompt(
                target_folder=target_folder,
                repo_dir=repo_dir,
                marathon_md=marathon_md,
                refine_log=refine_log_text,
                iteration_idx=iteration_idx,
                max_iterations=max_iterations,
                skeleton_mode=skeleton_mode,
                max_prompt_words=max_prompt_words,
                attempt_idx=attempt_idx,
                max_retries=max_retries,
                previous_status=last_status,
            )

            print("\n--- Claude's drafted prompt (sent verbatim to Aristotle) ---")
            print(claude_response)
            print("--- end ---\n")
            _append_refine_log(log_path, iteration_idx, claude_response, attempt_idx=attempt_idx)

            trailer_template = (
                SKELETON_OUTPUT_REQUIREMENTS_TRAILER
                if skeleton_mode
                else OUTPUT_REQUIREMENTS_TRAILER
            )
            full_prompt = claude_response + trailer_template.format(
                output_path=output_path_str
            )

        status = await _run_refine_attempt(
            prompt=full_prompt,
            repo_dir=repo_dir,
            tex_path=tex_path,
            workdir_log=workdir_log,
            expected_path=expected_path,
            output_path_str=output_path_str,
            polling_interval=polling_interval,
            state=state,
            state_path=state_path,
            attempt_idx=attempt_idx,
            max_retries=max_retries,
            pipeline_config=pipeline_config,
            iteration_idx=iteration_idx,
            target_folder_name=target_folder_name,
            existing_project=existing_project if use_existing else None,
        )
        existing_project = None

        if status is None:
            return False

        if status == ProjectStatus.COMPLETE:
            return True

        if status in RETRYABLE_STATUSES:
            last_status = status.value
            if attempt_idx < max_retries:
                print(f"    {status.value} — will retry with a fresh Claude review")
                continue
            state.status = "RETRIES_EXHAUSTED"
            state.note = (
                f"reached max retries ({max_retries + 1} attempts); "
                f"last attempt status was {status.value}"
            )
            save_refine_state(state_path, state)
            return False

        if status in NON_RETRYABLE_FAILURE_STATUSES:
            state.note = f"terminal status {status.value} (not auto-retried)"
            save_refine_state(state_path, state)
            return False

        state.note = f"unexpected status {status.value}"
        save_refine_state(state_path, state)
        return False

    return False


def _append_refine_log(
    log_path: Path,
    iteration_idx: int,
    claude_response: str,
    attempt_idx: int = 0,
) -> None:
    with log_path.open("a") as f:
        if attempt_idx == 0:
            heading = f"## Iteration {iteration_idx} — Claude critique + drafted prompt"
        else:
            heading = (
                f"## Iteration {iteration_idx} attempt {attempt_idx + 1} (retry) — "
                f"Claude critique + drafted prompt"
            )
        f.write(f"\n\n{heading}\n\n")
        f.write(claude_response)
        f.write("\n")


async def refine_command(args) -> None:
    target_folder: Path = args.target.resolve()
    if not target_folder.is_dir():
        sys.exit(f"target folder not found: {target_folder}")

    repo_dir: Path = args.repo_dir.resolve()
    if not repo_dir.is_dir():
        sys.exit(f"--repo-dir not found: {repo_dir}")
    if not (repo_dir / ".git").exists():
        sys.exit(f"--repo-dir is not a git repo: {repo_dir}")

    workdir: Path = (args.workdir or Path.cwd()).resolve()
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        sys.exit(f"--workdir path exists but is not a directory: {workdir}")

    tex_path: Optional[Path] = None
    if args.tex is not None:
        tex_path = args.tex.resolve()
        if not tex_path.is_file():
            sys.exit(f"--tex file not found: {tex_path}")

    try:
        rel_to_repo = target_folder.relative_to(repo_dir)
    except ValueError:
        sys.exit(
            f"target folder ({target_folder}) must be inside --repo-dir ({repo_dir})"
        )
    expected_path = PurePosixPath(*rel_to_repo.parts)
    output_path_str = expected_path.as_posix()

    state_path = workdir / REFINE_STATE_FILENAME
    log_path = workdir / REFINE_LOG_FILENAME
    workdir_log = workdir / LOG_FILENAME  # marathon.md (may not exist)

    state = load_refine_state(state_path)
    if state is None:
        state = RefineState(target_folder=str(target_folder))
    elif state.target_folder != str(target_folder):
        sys.exit(
            f"existing {state_path} is for target {state.target_folder!r}, "
            f"not {str(target_folder)!r}. Move or delete that file before "
            "starting a new refine target."
        )

    api_key = _ensure_api_key()
    print(f"using Aristotle API key: {_mask_key(api_key)}")
    print(f"target folder:    {target_folder}")
    print(f"repo dir:         {repo_dir}")
    print(f"output path:      {output_path_str}")
    if tex_path is not None:
        print(f"tex file:         {tex_path}")
    print(f"workdir:          {workdir}")
    print(f"max iterations:   {args.max_iterations}")
    print(f"max retries/iter: {args.max_retries}")
    if args.skeleton:
        print("mode:             skeleton (no proofs; sorry-only)")
    if args.max_prompt_words is not None:
        print(f"max prompt words: {args.max_prompt_words}")

    pipeline_config = PipelineConfig(
        auto_build=args.auto_build,
        auto_commit=args.auto_commit,
        auto_push=args.auto_push,
        auto_rate=args.auto_rate,
        build_timeout=args.build_timeout,
        ratings_path=workdir / "marathon-ratings.jsonl",
        claude_in_loop=True,  # refine drafts each prompt via Claude
    )
    if pipeline_config.has_any():
        flags = [
            name for name, on in [
                ("auto-build", pipeline_config.auto_build),
                ("auto-commit", pipeline_config.auto_commit),
                ("auto-push", pipeline_config.auto_push),
                ("auto-rate", pipeline_config.auto_rate),
            ] if on
        ]
        print(f"post-extraction:  {', '.join(flags)}")

    if args.dry_run:
        print(
            "\n[dry-run] would loop up to "
            f"{args.max_iterations} times: each iteration calls Claude to "
            "draft a prompt, then submits to Aristotle. Skipping all calls."
        )
        return

    while state.iterations_completed < args.max_iterations:
        iteration_idx = state.iterations_completed + 1
        state.current_iteration_idx = iteration_idx
        save_refine_state(state_path, state)

        existing_project = await _try_reattach(state)

        if existing_project is None:
            print(f"\n=== iteration {iteration_idx}/{args.max_iterations} ===")
            # Reset state for a fresh iteration. Claude is called inside
            # _run_iteration (per attempt), not here.
            state.attempts = 0
            state.project_id = None
            state.status = None
            state.started_at = None
            state.completed_at = None
            state.duration_seconds = None
            state.output_path = None
            state.note = None
            save_refine_state(state_path, state)
        else:
            print(f"\n=== iteration {iteration_idx}/{args.max_iterations} (resumed) ===")

        ok = await _run_iteration(
            iteration_idx=iteration_idx,
            target_folder=target_folder,
            repo_dir=repo_dir,
            tex_path=tex_path,
            workdir_log=workdir_log,
            log_path=log_path,
            expected_path=expected_path,
            output_path_str=output_path_str,
            polling_interval=args.polling_interval,
            max_retries=args.max_retries,
            max_iterations=args.max_iterations,
            skeleton_mode=args.skeleton,
            max_prompt_words=args.max_prompt_words,
            pipeline_config=pipeline_config,
            target_folder_name=target_folder.name,
            state=state,
            state_path=state_path,
            existing_project=existing_project,
        )

        if not ok:
            msg = f"  iteration {iteration_idx} failed: {state.status}"
            if state.note:
                msg += f"  ({state.note})"
            print(msg)
            return

        state.iterations_completed = iteration_idx
        save_refine_state(state_path, state)
        duration = format_duration(state.duration_seconds)
        print(
            f"  iteration {iteration_idx} complete  duration={duration}  "
            f"output={state.output_path}"
        )
        if state.note:
            print(f"  note: {state.note}")

    print(
        f"\nrefine batch finished. {state.iterations_completed} "
        f"iteration(s) completed."
    )

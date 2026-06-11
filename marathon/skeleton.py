"""The ``marathon skeleton`` subcommand.

For each line of ``order.txt`` (in order), submit the named ``.tex`` file to
Aristotle bundled with:

- ``macros.sty`` from the input folder (if present),
- the shared ``marathon.md`` log (if present),
- the entire ``--repo-dir`` Lean project, filtered by ``.gitignore`` via
  ``git ls-files --cached --others --exclude-standard``.

Aristotle is instructed to place its output at
``<--output-base>/<output_folder>/`` within the bundle (e.g.
``GeometricAnalysis/LeeSM/Chapter12/``), and to update ``marathon.md`` at the
top level. Marathon extracts those paths into ``<repo-dir>/...`` and
``<input>/marathon.md`` respectively. Progress is checkpointed in
``<input>/marathon-state.json`` so re-runs resume.
"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Optional, Sequence

import aristotlelib
from aristotlelib import AgentTask, AristotleAPIError, Project, TaskStatus

from marathon.aristotle_runtime import (
    IN_FLIGHT_STATUS_VALUES,
    NON_RETRYABLE_FAILURE_STATUSES,
    RESUMABLE_SUCCESS_STATUS_VALUES,
    RETRYABLE_STATUSES,
    download_result,
    reattach_project_and_task,
    run_task_to_completion,
    submit_from_directory,
)
from marathon.order import OrderEntry, parse_order_file
from marathon.post_pipeline import (
    GATE_STATE_FILENAME,
    JURY_LOG_FILENAME,
    PipelineConfig,
    append_promptlog_url,
    run_post_pipeline,
)
from marathon.state import (
    ChapterState,
    RunState,
    compute_duration_seconds,
    format_duration,
    load_state,
    now_iso,
    save_state,
)

LOG_FILENAME = "marathon.md"
MACROS_FILENAME = "macros.sty"

# Re-exported for refine.py + future callers. Authoritative definitions
# live in marathon.aristotle_runtime; we just surface them here so the old
# import paths keep working.
__all__ = [
    "LOG_FILENAME",
    "MACROS_FILENAME",
    "RETRYABLE_STATUSES",
    "NON_RETRYABLE_FAILURE_STATUSES",
    "RESUMABLE_SUCCESS_STATUS_VALUES",
    "IN_FLIGHT_STATUS_VALUES",
]


def _read_prompt_template() -> str:
    prompt_path = Path(__file__).parent / "prompts" / "skeleton.md"
    if not prompt_path.is_file():
        sys.exit(f"prompt template missing: {prompt_path}")
    return prompt_path.read_text()


def _ensure_api_key() -> str:
    key = os.environ.get("ARISTOTLE_API_KEY")
    if not key:
        sys.exit(
            "ARISTOTLE_API_KEY not set. Add `export ARISTOTLE_API_KEY=arstl_...` "
            "to ~/.zshrc and re-source."
        )
    aristotlelib.set_api_key(key)
    return key


def _mask_key(key: str) -> str:
    if len(key) < 10:
        return "***"
    return f"{key[:6]}…{key[-4:]}"


def _validate_relpath(rel: str, label: str) -> PurePosixPath:
    """Reject absolute paths and paths containing '..'."""
    p = PurePosixPath(rel)
    if p.is_absolute():
        sys.exit(f"{label} must be a relative path, not absolute: {rel!r}")
    if any(part == ".." for part in p.parts):
        sys.exit(f"{label} must not contain '..': {rel!r}")
    if not p.parts:
        sys.exit(f"{label} must not be empty")
    return p


def _list_repo_files(repo_dir: Path) -> list[str]:
    """Return tracked + untracked-not-gitignored files in repo_dir, relative to repo root."""
    if not (repo_dir / ".git").exists():
        sys.exit(f"--repo-dir {repo_dir} is not a git repo (no .git directory found)")
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=str(repo_dir),
        capture_output=True,
        check=True,
    )
    return [p.decode("utf-8") for p in result.stdout.split(b"\0") if p]


def _build_submission_dir(
    folder: Path,
    entry: OrderEntry,
    repo_dir: Path,
    work_dir: Path,
) -> Path:
    """Stage the submission tree.

    Layout:
        <staged>/
          <chapter>.tex          (from input folder)
          macros.sty             (from input folder, if present)
          marathon.md            (from input folder, if present)
          <repo contents...>     (from repo_dir, filtered by .gitignore)
    """
    staged = work_dir / "submission"
    staged.mkdir()

    src_tex = folder / entry.input_file
    if not src_tex.is_file():
        sys.exit(f"missing input file: {src_tex}")
    shutil.copy2(src_tex, staged / entry.input_file)

    macros_src = folder / MACROS_FILENAME
    if macros_src.is_file():
        shutil.copy2(macros_src, staged / MACROS_FILENAME)

    log_src = folder / LOG_FILENAME
    if log_src.is_file():
        shutil.copy2(log_src, staged / LOG_FILENAME)

    for rel in _list_repo_files(repo_dir):
        src = repo_dir / rel
        if not src.is_file():
            continue
        dst = staged / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    return staged


def _atomic_replace_dir(new_dir: Path, target_dir: Path) -> None:
    """Atomically swap ``new_dir`` into place at ``target_dir``.

    Both paths must be on the same filesystem (callers stage ``new_dir``
    next to ``target_dir``, never in ``/tmp``, so the swap is a cheap
    rename rather than a cross-device copy). POSIX ``rename`` can't
    replace a non-empty directory, so the swap is two renames: the old
    folder is moved aside into a unique holding directory, the new one
    renamed into place, then the old copy is deleted. If the second
    rename fails the old folder is restored — there is no window where
    the destination is missing *and* unrecoverable.
    """
    if not target_dir.exists():
        os.replace(new_dir, target_dir)
        return
    # mkdtemp gives us a collision-free holding spot beside the target
    # (crash leftovers from a previous run can't shadow the rename).
    holding = Path(tempfile.mkdtemp(prefix=".marathon-old-", dir=target_dir.parent))
    old_moved = holding / target_dir.name
    try:
        os.replace(target_dir, old_moved)
    except OSError:
        shutil.rmtree(holding, ignore_errors=True)  # holding is still empty
        raise
    try:
        os.replace(new_dir, target_dir)
    except OSError:
        # Restore the previous contents before propagating: better to
        # keep the stale chapter than to lose it.
        try:
            os.replace(old_moved, target_dir)
        except OSError:
            # Double fault: swap-in AND restore both failed. The holding
            # dir now holds the ONLY surviving copy of the previous
            # contents — deleting it here would destroy them, so leave
            # it in place and tell the operator where it is.
            print(
                f"  WARN: could not restore {target_dir} after a failed "
                f"swap; previous contents preserved at {old_moved}"
            )
            raise
        shutil.rmtree(holding, ignore_errors=True)
        raise
    shutil.rmtree(holding, ignore_errors=True)


def _extract_solution(
    tar_path: Path,
    expected_path: PurePosixPath,
    repo_dir: Path,
    log_dest: Path,
    additional_writable_paths: Optional[Sequence[PurePosixPath]] = None,
) -> tuple[bool, bool, list[str], list[str]]:
    """Extract ``expected_path/`` from the tar into ``repo_dir/expected_path/``,
    and a top-level ``marathon.md`` into ``log_dest``.

    If the tar wraps everything in a single top-level directory (Aristotle
    has been observed using ``submission_aristotle/`` for this), that wrapper
    is detected and stripped before matching ``expected_path``. The
    ``marathon.md`` lookup also moves to the same level.

    **Primary chapter** (``expected_path``) gets wipe-and-replace
    semantics: the chapter's new contents fully replace
    ``repo_dir/expected_path``. This propagates Aristotle's deletes for
    the chapter that was the focal subject of the refine call. The
    replacement is *validate-then-swap*: members are extracted into a
    temp dir next to the destination, and only if at least one file
    actually landed under ``expected_path`` is the temp dir atomically
    swapped into place (via :func:`_atomic_replace_dir`). A malformed
    or folder-less result tar therefore leaves the previous chapter
    output untouched — previously the destination was ``rmtree``'d
    *before* the tar was inspected, so a bad tar destroyed the prior
    attempt's output with nothing to replace it.

    **Additional writable paths** (``additional_writable_paths``) get
    *overlay-extract* semantics: tar members under these paths are
    written to disk, but the existing repo files outside the tar
    member set are kept. This supports cross-chapter refactors
    (where Aristotle's reject-notes told it to also edit a sibling
    chapter, e.g. moving a block of declarations) and vendor-file
    backports — both workflows are project-supported per
    ``.marathon/referee.md``'s rubric items, but previously broke
    silently because the extractor scope was hard-coded to one
    chapter. See ``/tmp/marathon-refine-chapter-scope-bug.md`` for
    the failure mode this addresses.

    Returns ``(folder_found, log_updated, sorted_unexpected_top_levels,
    sorted_cross_chapter_writes)`` where ``cross_chapter_writes`` is
    the list of *repo-relative POSIX paths of files extracted into
    additional writable paths* — surfaced to the caller so it can
    distinguish "echoed input" (the existing unexpected-top warning)
    from "Aristotle actually wrote outside the primary chapter and
    we accepted it." This avoids the previous silent-loss failure
    mode where cross-chapter writes were thrown away and reported
    only as a generic "unexpected top-level entries" note.
    """
    expected_parts = tuple(expected_path.parts)
    extra_paths_parts: list[tuple[str, ...]] = []
    for p in additional_writable_paths or ():
        # Skip the primary expected_path if it shows up in extras (a
        # caller bug; would otherwise double-wipe).
        pp = tuple(p.parts)
        if pp and pp != expected_parts:
            extra_paths_parts.append(pp)

    found = False
    log_updated = False
    unexpected_top: set[str] = set()
    cross_chapter_writes: set[str] = set()
    primary_files_extracted = 0

    # Stage the primary chapter's new contents in a temp dir NEXT TO the
    # destination — same filesystem, so the final swap is an atomic
    # rename, never a copy through /tmp. The previous chapter output is
    # not touched until the staged copy has been validated.
    target_dir = repo_dir / Path(*expected_parts)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir: Optional[Path] = Path(
        tempfile.mkdtemp(prefix=".marathon-extract-", dir=target_dir.parent)
    )

    try:
        with tarfile.open(tar_path, "r:*") as tf:
            members = tf.getmembers()

            # Determine whether to strip a wrapper directory. Try the tar root
            # first; if the expected path doesn't match anywhere, see if exactly
            # one top-level directory contains everything and try stripping that.
            candidate_prefixes: list[tuple[str, ...]] = [()]
            top_levels = sorted({
                tuple(Path(m.name).parts[:1])[0]
                for m in members
                if m.name and Path(m.name).parts
            })
            if (
                len(top_levels) == 1
                and (not expected_parts or top_levels[0] != expected_parts[0])
            ):
                candidate_prefixes.append((top_levels[0],))

            chosen_prefix: tuple[str, ...] = ()
            for prefix in candidate_prefixes:
                full = prefix + expected_parts
                n = len(full)
                for m in members:
                    parts = tuple(Path(m.name).parts)
                    if len(parts) >= n and parts[:n] == full:
                        chosen_prefix = prefix
                        break
                else:
                    continue
                break
            else:
                chosen_prefix = ()  # no match anywhere; will leave found=False

            prefix_skip = len(chosen_prefix)
            full_expected = chosen_prefix + expected_parts
            full_extras = [chosen_prefix + ep for ep in extra_paths_parts]

            for m in members:
                parts = tuple(Path(m.name).parts)
                if not parts:
                    continue
                if any(p == ".." for p in parts) or Path(m.name).is_absolute():
                    continue

                # Member under expected output path (after stripping wrapper)?
                # Goes to the staging dir, not the live repo — swapped in
                # below only after validation.
                if (
                    len(parts) >= len(full_expected)
                    and parts[: len(full_expected)] == full_expected
                ):
                    found = True
                    if m.isfile():
                        f = tf.extractfile(m)
                        if f is None:
                            continue
                        rel_parts = parts[len(full_expected):]
                        if not rel_parts:
                            # The expected *folder* path is a plain file in
                            # this tar — malformed; don't let it become the
                            # chapter "directory".
                            continue
                        out_path = staging_dir / Path(*rel_parts)
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        out_path.write_bytes(f.read())
                        primary_files_extracted += 1
                    continue

                # Member under any of the additional writable paths?
                # Overlay-extract (no wipe) so we preserve any files in the
                # path that Aristotle didn't echo. Cross-chapter deletes
                # are not propagated this way; that's an acceptable
                # tradeoff for safety — the common workflow is add /
                # modify in the cross-chapter scope, not delete.
                matched_extra: Optional[tuple[str, ...]] = None
                for full_extra in full_extras:
                    if (
                        len(parts) >= len(full_extra)
                        and parts[: len(full_extra)] == full_extra
                    ):
                        matched_extra = full_extra
                        break
                if matched_extra is not None:
                    if m.isfile():
                        f = tf.extractfile(m)
                        if f is None:
                            continue
                        inner_parts = parts[prefix_skip:]
                        out_path = repo_dir / Path(*inner_parts)
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        out_path.write_bytes(f.read())
                        cross_chapter_writes.add("/".join(inner_parts))
                    continue

                # marathon.md at the wrapper-stripped root (or true root).
                if (
                    len(parts) == prefix_skip + 1
                    and parts[:prefix_skip] == chosen_prefix
                    and parts[-1] == LOG_FILENAME
                    and m.isfile()
                ):
                    log_updated = True
                    f = tf.extractfile(m)
                    if f is not None:
                        log_dest.write_bytes(f.read())
                    continue

                # Otherwise: track top-level entry (after wrapper-strip) as "unexpected".
                if prefix_skip > 0 and parts[:prefix_skip] == chosen_prefix:
                    if len(parts) > prefix_skip:
                        unexpected_top.add(parts[prefix_skip])
                else:
                    unexpected_top.add(parts[0])

        # Validation: the chapter folder must actually contain files. A tar
        # that names the folder but ships nothing under it (or only the bare
        # directory entry) is treated the same as a missing folder, so the
        # caller's OUTPUT_FOLDER_MISSING path fires and the previous chapter
        # output survives untouched.
        if found and primary_files_extracted == 0:
            found = False

        if found:
            _atomic_replace_dir(staging_dir, target_dir)
            staging_dir = None  # consumed by the swap; nothing to clean up
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)

    return (
        found,
        log_updated,
        sorted(unexpected_top),
        sorted(cross_chapter_writes),
    )


def _build_targets_block(entry: OrderEntry) -> str:
    if not entry.instructions:
        return ""
    return (
        "\n\n## Per-chapter targets\n\n"
        f"{entry.instructions}\n\n"
        f"These targets narrow the scope of the task: only formalize material "
        f"needed to reach them, and ignore unrelated content in `{entry.input_file}`."
    )


def _build_retry_block(
    attempt_idx: int,
    max_retries: int,
    last_status: str | None,
    has_partial_output: bool,
    output_path_str: str,
) -> str:
    if attempt_idx == 0:
        return ""
    if has_partial_output:
        body = (
            f"The previous attempt ended with status `{last_status}` and produced "
            f"partial output at `{output_path_str}/`, which is bundled with this "
            f"submission. Continue from where it left off: preserve correct content, "
            f"replace remaining `sorry`s where you can, fix earlier mistakes, and "
            f"keep aiming for the targets above. Do not start over from scratch."
        )
    else:
        body = (
            f"The previous attempt ended with status `{last_status}` and produced "
            f"no extractable output. Try again, aiming for the targets above. "
            f"Be conservative: prefer correct partial progress over an ambitious "
            f"complete attempt that breaks."
        )
    return (
        f"\n\n## Continuation context\n\nThis is retry attempt {attempt_idx} "
        f"of up to {max_retries}. {body}"
    )


async def _submit_fresh(
    folder: Path,
    entry: OrderEntry,
    prompt_template: str,
    repo_dir: Path,
    output_path_str: str,
    chapter: ChapterState,
    state: RunState,
    state_path: Path,
    attempt_idx: int,
    max_retries: int,
) -> tuple[Project, AgentTask] | None:
    """Build the bundle, build the prompt, and submit a new Aristotle project.

    On success returns ``(project, agent_task)`` — the freshly-created
    project plus its first task (the thing we poll for status). On submit
    failure, mutates ``chapter`` to SUBMIT_FAILED and returns None.
    """
    has_partial = chapter.output_path is not None and chapter.status in {
        TaskStatus.COMPLETE_WITH_ERRORS.value,
    }
    targets_block = _build_targets_block(entry)
    retry_block = _build_retry_block(
        attempt_idx=attempt_idx,
        max_retries=max_retries,
        last_status=chapter.status,
        has_partial_output=has_partial,
        output_path_str=output_path_str,
    )
    prompt = (
        prompt_template
        .replace("{output_path}", output_path_str)
        .replace("{input_file}", entry.input_file)
        .replace("{additional_instructions}", targets_block)
        .replace("{retry_context}", retry_block)
    )

    chapter.attempts += 1
    chapter.output_path = None
    chapter.note = None

    label = "attempt" if attempt_idx == 0 else f"retry {attempt_idx}"
    print(f"  {label} ({attempt_idx + 1}/{max_retries + 1}) starting")

    with tempfile.TemporaryDirectory(prefix="marathon-stage-") as stage_tmp:
        staged = _build_submission_dir(folder, entry, repo_dir, Path(stage_tmp))
        try:
            project, task = await submit_from_directory(prompt=prompt, project_dir=staged)
        except AristotleAPIError as e:
            chapter.status = "SUBMIT_FAILED"
            chapter.note = f"submit error (status {e.status_code}): {e}"
            save_state(state_path, state)
            return None

    chapter.project_id = project.project_id
    chapter.agent_task_id = task.agent_task_id
    chapter.status = task.status.value if task.status else TaskStatus.QUEUED.value
    chapter.started_at = now_iso()
    save_state(state_path, state)
    print(f"    submitted: project_id={project.project_id} task_id={task.agent_task_id}")
    if append_promptlog_url(repo_dir, project.project_id):
        print(f"    PromptLog.md updated")
    return project, task


async def _run_one_attempt(
    folder: Path,
    entry: OrderEntry,
    prompt_template: str,
    repo_dir: Path,
    expected_path: PurePosixPath,
    output_path_str: str,
    polling_interval: int,
    chapter: ChapterState,
    state: RunState,
    state_path: Path,
    attempt_idx: int,
    max_retries: int,
    pipeline_config: PipelineConfig,
    existing_project: Project | None = None,
    existing_task: AgentTask | None = None,
) -> TaskStatus | None:
    """Run a single attempt. If ``existing_project``/``existing_task`` are
    provided, skip submission and poll that task (used for reattaching to a
    previous run's in-flight task). Mutates ``chapter``. Returns the terminal
    ``TaskStatus`` or ``None`` for Marathon-level errors (submit/poll/missing
    output folder)."""
    if existing_project is not None and existing_task is not None:
        project = existing_project
        task = existing_task
        chapter.output_path = None
        chapter.note = None
        save_state(state_path, state)
        print(
            f"  reattached: continuing project_id={project.project_id} "
            f"task_id={task.agent_task_id}"
        )
    else:
        submitted = await _submit_fresh(
            folder=folder,
            entry=entry,
            prompt_template=prompt_template,
            repo_dir=repo_dir,
            output_path_str=output_path_str,
            chapter=chapter,
            state=state,
            state_path=state_path,
            attempt_idx=attempt_idx,
            max_retries=max_retries,
        )
        if submitted is None:
            return None
        project, task = submitted

    try:
        await run_task_to_completion(
            task=task,
            project=project,
            polling_interval=polling_interval,
            watcher=None,
        )
    except AristotleAPIError as e:
        chapter.status = "POLL_FAILED"
        chapter.note = f"poll error (status {e.status_code}): {e}"
        save_state(state_path, state)
        return None

    chapter.status = task.status.value
    chapter.completed_at = now_iso()
    chapter.duration_seconds = compute_duration_seconds(
        chapter.started_at, chapter.completed_at
    )

    # Download the result only if the task produced output. ``get_files``
    # falls back to the input tarball if there's no result, which we don't
    # want; gate on a successful-or-partial status.
    if task.status in {
        TaskStatus.COMPLETE,
        TaskStatus.COMPLETE_WITH_ERRORS,
        TaskStatus.OUT_OF_BUDGET,
    }:
        with tempfile.TemporaryDirectory(prefix="marathon-dl-") as dl_tmp:
            download_path = Path(dl_tmp) / "solution.tar.gz"
            try:
                result_path = await download_result(project, download_path)
            except AristotleAPIError as e:
                chapter.status = "POLL_FAILED"
                chapter.note = f"download error (status {e.status_code}): {e}"
                save_state(state_path, state)
                return None

            log_dest = folder / LOG_FILENAME
            # Skeleton's normal flow has no cross-chapter writes: each
            # chapter is independent. Pass no additional writable paths
            # — keeps the skeleton extraction strictly scoped, matching
            # historical behavior.
            found, log_updated, unexpected, cross_writes = _extract_solution(
                Path(result_path), expected_path, repo_dir, log_dest,
            )
            if found:
                chapter.output_path = str(repo_dir / Path(*expected_path.parts))
                notes: list[str] = []
                if not log_updated:
                    notes.append(f"warning: {LOG_FILENAME} not updated by Aristotle")
                if unexpected:
                    notes.append(
                        f"{len(unexpected)} unexpected top-level entries "
                        f"(mostly echoed input): {unexpected}"
                    )
                if cross_writes:
                    # Should not happen in skeleton mode (no additional
                    # writable paths configured), but surfaced loudly if
                    # it does so behavior is visible.
                    notes.append(
                        f"{len(cross_writes)} cross-chapter writes "
                        "(unexpected in skeleton mode): " + ", ".join(cross_writes)
                    )
                if notes:
                    chapter.note = "; ".join(notes)
            else:
                chapter.status = "OUTPUT_FOLDER_MISSING"
                chapter.note = (
                    f"expected path {output_path_str!r} not in solution tar; "
                    f"top-level entries: {unexpected}"
                )
                save_state(state_path, state)
                return None

    save_state(state_path, state)

    # Post-extraction pipeline (no-op if no flags set).
    if (
        chapter.output_path is not None
        and task.status == TaskStatus.COMPLETE
        and pipeline_config.has_any()
    ):
        run_post_pipeline(
            config=pipeline_config,
            repo_dir=repo_dir,
            target_path=Path(chapter.output_path),
            chapter_label=entry.input_file,
            iteration=None,
            project_id=chapter.project_id,
        )

    return task.status


async def _run_chapter(
    folder: Path,
    entry: OrderEntry,
    prompt_template: str,
    repo_dir: Path,
    output_base: PurePosixPath,
    polling_interval: int,
    max_retries: int,
    pipeline_config: PipelineConfig,
    state: RunState,
    state_path: Path,
) -> ChapterState:
    chapter = state.find(entry.input_file)
    if chapter is None:
        chapter = ChapterState(input_file=entry.input_file, output_folder=entry.output_folder)
        state.chapters.append(chapter)

    output_folder_path = _validate_relpath(entry.output_folder, "order.txt output folder")
    expected_path = output_base / output_folder_path
    output_path_str = expected_path.as_posix()

    print(f"\n=== {entry.input_file} -> {output_path_str} ===")

    # If a previous Marathon run died with this chapter still in flight on
    # Aristotle's side, OR finished but couldn't extract the output, reattach
    # rather than submitting a duplicate. The "in flight" case keeps polling;
    # the "extraction failure" case re-extracts using the current code.
    existing_project: Project | None = None
    existing_task: AgentTask | None = None
    reattach_reason = None
    if chapter.project_id:
        if chapter.status in IN_FLIGHT_STATUS_VALUES:
            reattach_reason = "in-flight"
        elif chapter.status == "OUTPUT_FOLDER_MISSING":
            reattach_reason = "previous extraction failure"
    if reattach_reason is not None:
        project, task = await reattach_project_and_task(
            project_id=chapter.project_id,
            agent_task_id=chapter.agent_task_id,
        )
        if project is not None and task is not None:
            existing_project = project
            existing_task = task
            print(
                f"  reattaching ({reattach_reason}) to project "
                f"project_id={chapter.project_id} task_id={task.agent_task_id} "
                f"status={task.status.value}"
            )
        else:
            print(
                f"  could not reattach to project_id={chapter.project_id}; "
                "will submit fresh instead"
            )

    for attempt_idx in range(max_retries + 1):
        status = await _run_one_attempt(
            folder=folder,
            entry=entry,
            prompt_template=prompt_template,
            repo_dir=repo_dir,
            expected_path=expected_path,
            output_path_str=output_path_str,
            polling_interval=polling_interval,
            chapter=chapter,
            state=state,
            state_path=state_path,
            attempt_idx=attempt_idx,
            max_retries=max_retries,
            pipeline_config=pipeline_config,
            existing_project=existing_project if attempt_idx == 0 else None,
            existing_task=existing_task if attempt_idx == 0 else None,
        )
        existing_project = None
        existing_task = None

        # Marathon-level error (submit, poll, missing folder) — chapter.status
        # already records it. Don't retry: these are infrastructure problems,
        # not Aristotle progress problems.
        if status is None:
            return chapter

        if status == TaskStatus.COMPLETE:
            return chapter

        if status in RETRYABLE_STATUSES:
            if attempt_idx < max_retries:
                print(f"    {status.value} — will retry")
                continue
            chapter.status = "RETRIES_EXHAUSTED"
            chapter.note = (
                f"reached max retries ({max_retries + 1} attempts); "
                f"last attempt status was {status.value}"
            )
            save_state(state_path, state)
            return chapter

        if status in NON_RETRYABLE_FAILURE_STATUSES:
            chapter.note = f"terminal status {status.value} (not auto-retried)"
            save_state(state_path, state)
            return chapter

        # Unknown / unexpected status — record and bail out without retrying.
        chapter.note = f"unexpected status {status.value}"
        save_state(state_path, state)
        return chapter

    return chapter


async def skeleton_command(args) -> None:
    folder: Path = args.folder.resolve()
    if not folder.is_dir():
        sys.exit(f"folder not found: {folder}")

    repo_dir: Path = args.repo_dir.resolve()
    if not repo_dir.is_dir():
        sys.exit(f"--repo-dir not found: {repo_dir}")

    output_base = _validate_relpath(args.output_base, "--output-base")

    order_path = folder / "order.txt"
    state_path = folder / "marathon-state.json"

    entries = parse_order_file(order_path)
    if not entries:
        sys.exit("order.txt is empty")

    api_key = _ensure_api_key()
    print(f"using API key: {_mask_key(api_key)}")
    print(f"input folder:  {folder}")
    print(f"repo dir:      {repo_dir}")
    print(f"output base:   {output_base.as_posix()}")

    prompt_template = _read_prompt_template()
    state = load_state(state_path)

    pipeline_config = PipelineConfig(
        auto_build=args.auto_build,
        auto_commit=args.auto_commit,
        auto_push=args.auto_push,
        auto_rate=args.auto_rate,
        build_timeout=args.build_timeout,
        ratings_path=folder / "marathon-ratings.jsonl",
        # Machine gate (phase-2). skeleton is ALWAYS skeleton mode —
        # sorry bodies are this command's product, so theorem-body
        # sorry deltas must not read as regressions. The snapshot and
        # jury jsonl live in the input folder, beside the other
        # per-input-folder state (marathon-state.json, the ratings
        # jsonl).
        gate=getattr(args, "gate", "warn"),
        gate_override=getattr(args, "gate_override", None),
        gate_state_path=folder / GATE_STATE_FILENAME,
        skeleton_mode=True,
        jury=getattr(args, "jury", False),
        jury_log_path=folder / JURY_LOG_FILENAME,
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
        print(f"post-extraction pipeline: {', '.join(flags)}")

    for entry in entries:
        existing = state.find(entry.input_file)
        if existing and existing.status in RESUMABLE_SUCCESS_STATUS_VALUES:
            print(f"skipping (already {existing.status}): {entry.input_file}")
            continue

        chapter = await _run_chapter(
            folder=folder,
            entry=entry,
            prompt_template=prompt_template,
            repo_dir=repo_dir,
            output_base=output_base,
            polling_interval=args.polling_interval,
            max_retries=args.max_retries,
            pipeline_config=pipeline_config,
            state=state,
            state_path=state_path,
        )

        status = chapter.status
        if status in RESUMABLE_SUCCESS_STATUS_VALUES:
            duration = format_duration(chapter.duration_seconds)
            print(
                f"  done: {status}  duration={duration}  "
                f"output={chapter.output_path}"
            )
            if chapter.note:
                print(f"  note: {chapter.note}")
            continue

        msg = f"  failed: {status}"
        if chapter.note:
            msg += f"  ({chapter.note})"
        print(msg)
        if not args.continue_on_error:
            sys.exit(
                f"aborting batch after {entry.input_file} "
                "(re-run with --continue-on-error to skip and continue)"
            )

    print("\nbatch finished.")

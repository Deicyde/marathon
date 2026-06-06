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

from aristotlelib import AgentTask, AristotleAPIError, Project, TaskStatus

from marathon.aristotle_runtime import (
    CONTINUABLE_STATUS_VALUES,
    CONTINUABLE_STATUSES,
    IN_FLIGHT_STATUS_VALUES,
    IN_FLIGHT_STATUSES,
    NON_RETRYABLE_FAILURE_STATUSES,
    RETRYABLE_STATUSES,
    continue_via_ask,
    download_result,
    reattach_project_and_task,
    run_task_to_completion,
    submit_from_directory,
)
from marathon.claude_review import review_and_draft_prompt
from marathon.post_pipeline import (
    PipelineConfig,
    append_promptlog_url,
    run_post_pipeline,
)
from marathon.skeleton import (
    LOG_FILENAME,
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
RATINGS_FILENAME = "marathon-ratings.jsonl"


def _load_additional_writable_paths(
    repo_dir: Path, expected_path: PurePosixPath
) -> list[PurePosixPath]:
    """Compute the cross-chapter / vendor writable whitelist for the
    extractor. Returns a list of repo-relative POSIX paths the
    extractor may write into (in addition to the primary
    ``expected_path``).

    Sources:

    * Every registered chapter path under
      ``.marathon/review/config.toml``'s ``[[chapters]]`` block,
      EXCEPT the primary chapter ``expected_path`` (which the
      extractor already handles separately with wipe-and-replace).
    * Every entry of the top-level ``extra_writable_paths`` config
      array (typically vendor directories such as ``Mathlib_4_30/``).

    Gracefully returns ``[]`` when the consumer repo isn't using the
    review workflow (no ``config.toml``) — preserves the historical
    chapter-scoped extractor behavior in that case.
    """
    try:
        from marathon.review.config import load_config
    except ImportError:
        return []
    config_path = repo_dir / ".marathon" / "review" / "config.toml"
    if not config_path.is_file():
        return []
    try:
        cfg = load_config(repo_dir=repo_dir)
    except SystemExit:
        return []

    expected_parts = tuple(expected_path.parts)
    out: list[PurePosixPath] = []
    seen: set[tuple[str, ...]] = {expected_parts}
    for chap in cfg.chapters:
        chap_path = cfg.target_path(chap)
        try:
            rel = chap_path.relative_to(cfg.repo_dir)
        except ValueError:
            continue
        ppp = PurePosixPath(*rel.parts)
        parts = tuple(ppp.parts)
        if parts in seen:
            continue
        seen.add(parts)
        out.append(ppp)
    for p in cfg.extra_writable_paths:
        ppp = PurePosixPath(*p.parts)
        parts = tuple(ppp.parts)
        if parts in seen:
            continue
        seen.add(parts)
        out.append(ppp)
    return out


def _format_reject_as_aristotle_prompt(
    pending_rejections_md: str,
    focus_issue: int,
    skeleton_mode: bool,
) -> str:
    """Format a single rejected sub-issue's notes as a direct Aristotle
    prompt, bypassing Claude-in-loop.

    The notes that the human typed into ``marathon review reject N --notes
    NOTES_FILE`` are already file-and-declaration-level instructions for
    Aristotle. For focused single-issue rejections, Claude-in-loop's
    "review and draft" pass adds noise rather than value — it tends to
    scan the target folder, notice unrelated structural opportunities,
    and override the explicit reject ask with its own picks. Skipping
    Claude entirely on these iterations keeps the contract intact.

    The returned text is sent verbatim to Aristotle (modulo the standard
    output-requirements trailer appended by the caller). A short
    contextual lead is prepended so Aristotle knows the scope is exact.
    """
    lead = (
        f"You are addressing a focused rejection of sub-issue #{focus_issue}.\n\n"
        "The human reviewer typed the change request below. Execute it "
        "exactly as written — same file(s), same declarations, same "
        "structural change. Do NOT introduce other refactors, even if you "
        "notice clean structural opportunities in adjacent files. Do NOT "
        "touch files outside those named in the request. If the request "
        "has unavoidable downstream consumer edits, include only those "
        "minimal consumer fixes.\n\n"
    )
    if skeleton_mode:
        lead += (
            "Skeleton mode: every proof body stays ``by sorry``. Only "
            "signatures, definitions, and structural shape change this "
            "iteration.\n\n"
        )
    lead += "---\n\n# Reject notes\n\n"
    return lead + pending_rejections_md.strip() + "\n"


def _load_pending_rejections_md(
    repo_dir: Path,
    target_folder: Path,
    focus_issue: Optional[int] = None,
) -> Optional[str]:
    """Load the pending-rejection queue for the chapter ``target_folder``
    belongs to, rendered as Markdown for Hermes's prompt context. Returns
    ``None`` if there's no review config, no pending rejections, or the
    target folder doesn't map to any registered chapter.

    When ``focus_issue`` is supplied, the rendered block is restricted
    to that single rejected issue — used by the refine daemon for
    one-rejection-per-iteration dispatch (see
    ``--review-rejection N`` on ``marathon refine``).

    Gracefully no-ops when the consumer repo isn't using the review
    workflow (`.marathon/review/config.toml` absent), so callers that
    don't care about the queue don't break.
    """
    try:
        from marathon.review.config import load_config
        from marathon.review.state import render_pending_rejections_md
    except ImportError:
        return None
    config_path = repo_dir / ".marathon" / "review" / "config.toml"
    if not config_path.is_file():
        return None
    try:
        cfg = load_config(repo_dir=repo_dir)
    except SystemExit:
        # load_config sys.exits on missing required fields; treat as
        # "no review config available" rather than crashing refine.
        return None
    # Map target_folder back to a chapter via the template.
    chapter: Optional[int] = None
    target_resolved = target_folder.resolve()
    for cand_chapter in cfg.chapters.keys():
        if cfg.target_path(cand_chapter).resolve() == target_resolved:
            chapter = cand_chapter
            break
    # If we can't identify the chapter, fall back to project-wide pending
    # rejections rather than dropping them entirely.
    return render_pending_rejections_md(cfg, chapter, focus_issue=focus_issue)


def _read_latest_rating_note(workdir: Path) -> Optional[str]:
    """Return the most-recent rating's `notes` paragraph from the workdir's
    ratings log, or None if the file is missing/empty/malformed.

    Used to feed the previous iteration's auto-rater diagnosis back into
    the next iteration's Claude review (closes the rater→reviewer loop).
    """
    import json
    path = workdir / RATINGS_FILENAME
    if not path.is_file():
        return None
    try:
        last_line = ""
        for line in path.read_text().splitlines():
            if line.strip():
                last_line = line
        if not last_line:
            return None
        data = json.loads(last_line)
        rating = data.get("rating") or {}
        notes = rating.get("notes")
        return notes if isinstance(notes, str) and notes.strip() else None
    except (OSError, ValueError):
        return None


def _read_latest_rating_summary(workdir: Path) -> Optional[dict]:
    """Return the latest rating's full record (with build status, struct
    score, project_id, notes) from the workdir's ratings log."""
    import json
    path = workdir / RATINGS_FILENAME
    if not path.is_file():
        return None
    try:
        last_line = ""
        for line in path.read_text().splitlines():
            if line.strip():
                last_line = line
        if not last_line:
            return None
        return json.loads(last_line)
    except (OSError, ValueError):
        return None


def _trim_marathon_md_tail(text: str, max_chars: int = 4_000) -> str:
    """Return the last ``max_chars`` of a marathon.md (preferring whole
    section blocks). Aristotle's marathon.md grows monotonically across
    iterations; we want the tail (most recent design notes), not the head.
    """
    if len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    # Try to start at the next section header to avoid mid-paragraph cuts.
    idx = tail.find("\n## ")
    if 0 < idx < max_chars - 200:
        return tail[idx + 1:]
    return "... (earlier marathon.md content trimmed)\n" + tail


def _collect_sibling_chapter_context(workdir: Path) -> Optional[str]:
    """Aggregate marathon.md tails + latest rater notes from sibling
    chapter workdirs (subdirs of ``workdir.parent`` other than ``workdir``
    that look like marathon refine workdirs).

    Returns a markdown block to splice into the next Hermes prompt, or
    None when no siblings are found. Scoped to the immediate parent so
    cross-batch directories (e.g. ``may7-r1/`` vs ``may9-r1/``) don't
    bleed into each other.
    """
    import json
    parent = workdir.parent
    if not parent.is_dir():
        return None
    self_resolved = workdir.resolve()
    siblings: list[Path] = []
    for entry in sorted(parent.iterdir()):
        if not entry.is_dir():
            continue
        try:
            if entry.resolve() == self_resolved:
                continue
        except OSError:
            continue
        if (entry / REFINE_STATE_FILENAME).is_file():
            siblings.append(entry)
    if not siblings:
        return None

    blocks: list[str] = []
    for sib in siblings:
        # Pull state for chapter label + status line.
        state_path = sib / REFINE_STATE_FILENAME
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            continue
        target = state.get("target_folder") or ""
        chap_label = Path(target).name if target else sib.name

        status = state.get("status") or "?"
        iters = state.get("iterations_completed")
        cur = state.get("current_iteration_idx")

        # Pull last rating summary.
        rating = _read_latest_rating_summary(sib) or {}
        rating_data = rating.get("rating") or {}
        build = rating.get("build") or {}
        if build.get("ok"):
            build_status = "OK"
        elif build.get("timed_out"):
            build_status = "TIMEOUT"
        elif build:
            build_status = "FAIL"
        else:
            build_status = "—"
        struct = rating_data.get("structural_focus")
        struct = struct if struct is not None else "—"
        last_iter = rating.get("iteration") or "—"

        # Pull marathon.md tail.
        marathon_md_path = sib / LOG_FILENAME  # marathon.md
        marathon_md_section: Optional[str] = None
        if marathon_md_path.is_file():
            try:
                marathon_md_section = _trim_marathon_md_tail(marathon_md_path.read_text())
            except OSError:
                marathon_md_section = None

        notes = rating_data.get("notes") if isinstance(rating_data.get("notes"), str) else None

        block_lines = [
            f"## {chap_label} — status: {status}, iterations: {iters}/{cur}, "
            f"last build: {build_status}, last struct: {struct} (rated iter {last_iter})"
        ]
        if marathon_md_section:
            block_lines.append("### marathon.md (tail)")
            block_lines.append(marathon_md_section.strip())
        if notes:
            block_lines.append("### Last auto-rater diagnosis")
            block_lines.append(notes.strip())
        blocks.append("\n\n".join(block_lines))

    if not blocks:
        return None
    return "\n\n---\n\n".join(blocks)

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
    referee_path: Optional[Path] = None,
) -> Path:
    """Stage the Aristotle submission tree.

    ``referee_path``, if given, is excluded from the bundle: those notes
    are reviewer-only direction for Claude and should not reach Aristotle.
    """
    staged = work_dir / "submission"
    staged.mkdir()

    referee_resolved = referee_path.resolve() if referee_path else None

    for rel in _list_repo_files(repo_dir):
        src = repo_dir / rel
        if not src.is_file():
            continue
        if referee_resolved is not None and src.resolve() == referee_resolved:
            continue
        dst = staged / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    if tex_path is not None and tex_path.is_file():
        shutil.copy2(tex_path, staged / tex_path.name)

    if workdir_log is not None and workdir_log.is_file():
        shutil.copy2(workdir_log, staged / LOG_FILENAME)

    return staged


async def _try_reattach_or_continue(
    state: RefineState,
    *,
    continue_on_review: bool = True,
) -> tuple[Optional[Project], Optional[AgentTask], Optional[str]]:
    """Decide what to do given the state's recorded prior project/task.

    Returns ``(project, task, mode)`` where mode is one of:

    * ``"reattach"`` — prior task is still QUEUED/IN_PROGRESS, or it
      terminated but Marathon's extraction failed (OUTPUT_FOLDER_MISSING).
      Caller should poll/re-extract the existing task; no new prompt.
    * ``"continue"`` — prior task is in a ``CONTINUABLE_STATUSES`` state
      (Aristotle "Review Suggested" / "Out of Budget"); the server-side
      session is preserved. Caller should call Hermes in
      ``continuation_mode=True`` and dispatch via ``project.ask(...)``
      instead of fresh-submitting. Suppressed when
      ``continue_on_review=False``.
    * ``None`` — no reattach or continuation applies; caller should
      submit a fresh project.
    """
    if not state.project_id:
        return None, None, None

    project, task = await reattach_project_and_task(
        project_id=state.project_id,
        agent_task_id=state.agent_task_id,
    )
    if project is None or task is None:
        print(
            f"  could not reattach to project_id={state.project_id}; "
            "will submit fresh instead"
        )
        return None, None, None

    if state.status in IN_FLIGHT_STATUS_VALUES or task.status in IN_FLIGHT_STATUSES:
        reason = "in-flight"
        print(
            f"  reattaching ({reason}) to project "
            f"project_id={state.project_id} task_id={task.agent_task_id} "
            f"status={task.status.value}"
        )
        return project, task, "reattach"

    if state.status == "OUTPUT_FOLDER_MISSING":
        # Prior task terminated but extraction failed; we want to re-extract,
        # not continue. Treat as reattach so the caller re-runs extraction
        # against the existing terminal task.
        print(
            f"  reattaching (previous extraction failure) to project "
            f"project_id={state.project_id} task_id={task.agent_task_id} "
            f"status={task.status.value}"
        )
        return project, task, "reattach"

    if continue_on_review and task.status in CONTINUABLE_STATUSES:
        print(
            f"  continuing project_id={state.project_id} via project.ask() — "
            f"prior task {task.agent_task_id} ended {task.status.value} "
            "(session preserved server-side; will dispatch Hermes' "
            "continuation prompt via ask())"
        )
        return project, task, "continue"

    return None, None, None


async def _submit_fresh_refine(
    prompt: str,
    repo_dir: Path,
    tex_path: Optional[Path],
    workdir_log: Optional[Path],
    state: RefineState,
    state_path: Path,
    attempt_idx: int,
    max_retries: int,
    referee_path: Optional[Path] = None,
) -> Optional[tuple[Project, AgentTask]]:
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
            referee_path=referee_path,
        )
        try:
            project, task = await submit_from_directory(prompt=prompt, project_dir=staged)
        except AristotleAPIError as e:
            state.status = "SUBMIT_FAILED"
            state.note = f"submit error (status {e.status_code}): {e}"
            save_refine_state(state_path, state)
            return None

    state.project_id = project.project_id
    state.agent_task_id = task.agent_task_id
    state.status = task.status.value if task.status else TaskStatus.QUEUED.value
    state.started_at = now_iso()
    save_refine_state(state_path, state)
    print(f"    submitted: project_id={project.project_id} task_id={task.agent_task_id}")
    if append_promptlog_url(repo_dir, project.project_id):
        print(f"    PromptLog.md updated")
    return project, task


async def _submit_continuation_via_ask(
    project: Project,
    prompt: str,
    state: RefineState,
    state_path: Path,
    attempt_idx: int,
    max_retries: int,
) -> Optional[AgentTask]:
    """Send a continuation prompt to ``project`` via ``project.ask(...)``.

    Used when the previous task left the project in a ``CONTINUABLE_STATUSES``
    state. Updates ``state.agent_task_id``/``status``/``started_at`` for the
    new task and persists. Returns the new task, or ``None`` if ``ask()``
    fails (in which case the caller should fall back to fresh submit).
    """
    state.attempts += 1
    state.output_path = None
    state.note = None

    label = "attempt" if attempt_idx == 0 else f"retry {attempt_idx}"
    print(
        f"  {label} ({attempt_idx + 1}/{max_retries + 1}) "
        f"continuing project_id={project.project_id} via project.ask()"
    )

    try:
        task = await continue_via_ask(project, prompt)
    except AristotleAPIError as e:
        state.status = "ASK_FAILED"
        state.note = f"project.ask error (status {e.status_code}): {e}"
        save_refine_state(state_path, state)
        return None

    state.agent_task_id = task.agent_task_id
    state.status = task.status.value if task.status else TaskStatus.QUEUED.value
    state.started_at = now_iso()
    state.completed_at = None
    state.duration_seconds = None
    save_refine_state(state_path, state)
    print(f"    new task: task_id={task.agent_task_id}")
    return task


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
    existing_task: Optional[AgentTask] = None,
    existing_mode: Optional[str] = None,
    referee_path: Optional[Path] = None,
    watcher_factory=None,
) -> Optional[TaskStatus]:
    """Run one Aristotle attempt for the current iteration. Returns the
    terminal ``TaskStatus``, or ``None`` for Marathon-level errors.

    ``existing_mode`` is one of ``"reattach"`` / ``"continue"`` / ``None``:

    * ``"reattach"`` — poll ``existing_task`` directly (no new prompt,
      no submission). Used for tasks that died mid-flight or whose
      extraction we need to redo.
    * ``"continue"`` — dispatch ``prompt`` via ``project.ask()`` on
      ``existing_project`` to spawn a new continuation task on the same
      preserved Aristotle session (the SDK's intended path for
      ``COMPLETE_WITH_ERRORS`` / ``OUT_OF_BUDGET``).
    * ``None`` — submit a fresh project from the staging bundle.

    ``watcher_factory``, if provided, is called as ``watcher_factory(state,
    iteration_idx)`` to construct an :class:`EventWatcher` for this
    attempt; the watcher polls the event stream alongside the status loop
    (used by the Hermes live-steering flow).
    """
    if existing_mode == "reattach" and existing_project is not None and existing_task is not None:
        project = existing_project
        task = existing_task
        state.output_path = None
        state.note = None
        save_refine_state(state_path, state)
        print(
            f"  reattached: polling project_id={project.project_id} "
            f"task_id={task.agent_task_id}"
        )
    elif existing_mode == "continue" and existing_project is not None:
        if prompt is None:
            sys.exit("internal error: prompt required for continuation submit")
        project = existing_project
        new_task = await _submit_continuation_via_ask(
            project=project,
            prompt=prompt,
            state=state,
            state_path=state_path,
            attempt_idx=attempt_idx,
            max_retries=max_retries,
        )
        if new_task is None:
            return None
        task = new_task
    else:
        if prompt is None:
            sys.exit("internal error: prompt required for fresh submit")
        submitted = await _submit_fresh_refine(
            prompt=prompt,
            repo_dir=repo_dir,
            tex_path=tex_path,
            workdir_log=workdir_log,
            state=state,
            state_path=state_path,
            attempt_idx=attempt_idx,
            max_retries=max_retries,
            referee_path=referee_path,
        )
        if submitted is None:
            return None
        project, task = submitted

    watcher = watcher_factory(state, iteration_idx) if watcher_factory else None

    try:
        await run_task_to_completion(
            task=task,
            project=project,
            polling_interval=polling_interval,
            watcher=watcher,
        )
    except AristotleAPIError as e:
        state.status = "POLL_FAILED"
        state.note = f"poll error (status {e.status_code}): {e}"
        save_refine_state(state_path, state)
        return None

    state.status = task.status.value
    state.completed_at = now_iso()
    state.duration_seconds = compute_duration_seconds(
        state.started_at, state.completed_at
    )

    if task.status in {
        TaskStatus.COMPLETE,
        TaskStatus.COMPLETE_WITH_ERRORS,
        TaskStatus.OUT_OF_BUDGET,
    }:
        # Cross-chapter writes are captured inside the with-block below
        # but consumed by ``run_post_pipeline`` further down (outside
        # the block), so hoist to outer scope to preserve.
        cross_writes_for_pipeline: list[str] = []

        with tempfile.TemporaryDirectory(prefix="marathon-refine-dl-") as dl_tmp:
            download_path = Path(dl_tmp) / "solution.tar.gz"
            try:
                result_path = await download_result(project, download_path)
            except AristotleAPIError as e:
                state.status = "POLL_FAILED"
                state.note = f"download error (status {e.status_code}): {e}"
                save_refine_state(state_path, state)
                return None

            log_dest = workdir_log if workdir_log is not None else Path(dl_tmp) / "_unused.md"
            # Compute the cross-chapter / vendor writable whitelist
            # from .marathon/review/config.toml (every registered
            # chapter folder other than the primary one, plus any
            # `extra_writable_paths` entries). Gracefully no-ops when
            # the project isn't using the review workflow.
            additional_writable = _load_additional_writable_paths(
                repo_dir, expected_path,
            )
            found, log_updated, unexpected, cross_writes = _extract_solution(
                Path(result_path), expected_path, repo_dir, log_dest,
                additional_writable_paths=additional_writable,
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
                # Capture cross-chapter writes for the post-pipeline
                # commit/PR (consumed outside the with-block below).
                cross_writes_for_pipeline = list(cross_writes)
                if cross_writes:
                    # Cross-chapter writes are a *positive* event when
                    # the reject-notes asked for cross-chapter work
                    # (the bug-report scenario the widening was added
                    # for). Surface them loudly so the iteration log
                    # actually shows what landed beyond the primary
                    # chapter.
                    notes.append(
                        f"{len(cross_writes)} cross-chapter write(s) "
                        f"into additional writable paths: {cross_writes}"
                    )
                    print(
                        f"  cross-chapter writes accepted: "
                        f"{len(cross_writes)} file(s) outside the primary "
                        f"chapter scope:",
                        flush=True,
                    )
                    for w in cross_writes:
                        print(f"    {w}", flush=True)
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
        and task.status == TaskStatus.COMPLETE
        and pipeline_config.has_any()
    ):
        run_post_pipeline(
            config=pipeline_config,
            repo_dir=repo_dir,
            target_path=Path(state.output_path),
            chapter_label=target_folder_name,
            iteration=iteration_idx,
            project_id=state.project_id,
            extra_paths_to_stage=cross_writes_for_pipeline or None,
        )

    return task.status


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
    existing_task: Optional[AgentTask],
    existing_mode: Optional[str],
    workdir: Path,
    referee_path: Optional[Path] = None,
    cross_chapter: bool = True,
    watcher_factory=None,
    continue_on_review: bool = True,
    review_rejection: Optional[int] = None,
    focus_directive: Optional[str] = None,
    prefetched_pending_rejections_md: Optional[str] = None,
) -> bool:
    """Run a single refinement iteration. Each attempt (other than a pure
    ``reattach`` reentry) gets its own Claude review against the current
    target-folder state. Returns True on success, False if the iteration
    failed permanently.

    ``prefetched_pending_rejections_md`` lets the caller hand in the
    rendered pending-rejections context loaded BEFORE this iteration's
    branch switch. Necessary when ``--auto-pr`` is on, since
    ``prepare_auto_pr_branch`` does ``git checkout -B branch
    origin/main`` and wipes the local ``state.json`` rejection record;
    reading state.json inside the iteration would then return None and
    the focused-rejection bypass would silently fall through to
    Claude-in-loop. When this argument is None, the iteration loads
    fresh (autonomous-run path, no branch switch).

    ``existing_mode`` is the mode handed in from :func:`_try_reattach_or_continue`:

    * ``"reattach"`` — the prior task is mid-flight or its extraction
      failed; poll/re-extract without re-calling Hermes.
    * ``"continue"`` — the prior task ended in ``CONTINUABLE_STATUSES``;
      Hermes drafts a *continuation* prompt that we dispatch via
      ``project.ask()``.
    * ``None`` — start fresh.
    """
    last_status: Optional[str] = None
    previous_output_summary: Optional[str] = None
    if existing_task is not None and existing_mode == "continue":
        previous_output_summary = existing_task.output_summary
        last_status = existing_task.status.value if existing_task.status else None

    for attempt_idx in range(max_retries + 1):
        # Pick the mode for THIS attempt.
        #   - attempt 0: honors the caller-supplied existing_mode.
        #   - attempts 1+ (retries): if the previous attempt's last_status
        #     is CONTINUABLE and continue_on_review is on, switch to
        #     continuation via ask(); otherwise fresh.
        if attempt_idx == 0:
            attempt_mode = existing_mode
            attempt_project: Optional[Project] = existing_project
            attempt_task: Optional[AgentTask] = existing_task
        else:
            existing_project = None
            existing_task = None
            if (
                continue_on_review
                and last_status in CONTINUABLE_STATUS_VALUES
                and state.project_id is not None
            ):
                # The previous attempt's project_id is still our project; reattach
                # to it explicitly to get a Project handle (the task object is
                # already terminal so we don't reuse it).
                proj, _prev_task = await reattach_project_and_task(
                    project_id=state.project_id,
                    agent_task_id=state.agent_task_id,
                )
                if proj is not None and _prev_task is not None:
                    attempt_mode = "continue"
                    attempt_project = proj
                    attempt_task = _prev_task
                    previous_output_summary = _prev_task.output_summary
                else:
                    attempt_mode = None
                    attempt_project = None
                    attempt_task = None
            else:
                attempt_mode = None
                attempt_project = None
                attempt_task = None

        if attempt_mode == "reattach":
            full_prompt: Optional[str] = None
        else:
            label = "attempt 1" if attempt_idx == 0 else f"retry {attempt_idx}"
            label_suffix = " (continuation)" if attempt_mode == "continue" else ""
            print(
                f"  iteration {iteration_idx} {label}{label_suffix} "
                f"({attempt_idx + 1}/{max_retries + 1}): "
                "calling Claude for review + drafted prompt..."
            )

            marathon_md = workdir_log.read_text() if workdir_log and workdir_log.is_file() else None
            refine_log_text = log_path.read_text() if log_path.is_file() else ""
            referee_md = (
                referee_path.read_text()
                if referee_path and referee_path.is_file()
                else None
            )
            # Per-iteration rejection queue. Prefer the prefetched value
            # passed in from the caller (necessary under ``--auto-pr``,
            # where the branch switch wipes ``state.json`` before this
            # iteration reads it). Fall back to a fresh load for
            # autonomous runs that don't switch branches.
            if prefetched_pending_rejections_md is not None:
                pending_rejections_md = prefetched_pending_rejections_md
            else:
                pending_rejections_md = _load_pending_rejections_md(
                    repo_dir, target_folder, focus_issue=review_rejection,
                )
            # When the daemon is dispatching a focused single-issue
            # rejection, suppress referee.md ENTIRELY so Claude-in-loop
            # can't second-guess the human's specific reject ask by
            # scanning the failure-mode catalog in the user header.
            # The rubric (system prompt) still carries style/idiom
            # guidance; the project-specific priorities are not
            # load-bearing for a one-rejection iteration.
            referee_md_for_prompt = (
                None if review_rejection is not None else referee_md
            )
            # Re-read the latest rater note on every attempt (including
            # retries) so a fresh Claude review sees the latest available
            # diagnosis even if a retry follows a partial pipeline run.
            previous_rating_note = _read_latest_rating_note(workdir)
            cross_chapter_md = (
                _collect_sibling_chapter_context(workdir) if cross_chapter else None
            )

            # Bypass Claude-in-loop for focused single-issue rejections:
            # the human's reject notes ARE the Aristotle prompt. Past
            # iterations under Claude-in-loop have repeatedly overridden
            # the explicit reject ask with project-wide structural picks
            # — even after muting referee.md and tightening the
            # rejection-queue preamble. The target file's visible content
            # alone is enough to nudge Claude toward "while we're at it"
            # refactors. For focused rejections, the simplest fix is to
            # skip the review-and-draft pass entirely and dispatch the
            # human's notes verbatim. The reject notes that the human
            # types into ``marathon review reject --notes`` are already
            # file-and-declaration-level Aristotle instructions.
            if review_rejection is not None and pending_rejections_md:
                claude_response = _format_reject_as_aristotle_prompt(
                    pending_rejections_md,
                    review_rejection,
                    skeleton_mode,
                )
            else:
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
                    referee_md=referee_md_for_prompt,
                    pending_rejections_md=pending_rejections_md,
                    previous_rating_note=previous_rating_note,
                    cross_chapter_md=cross_chapter_md,
                    continuation_mode=(attempt_mode == "continue"),
                    previous_output_summary=previous_output_summary,
                    focus_directive=focus_directive,
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
            existing_project=attempt_project,
            existing_task=attempt_task,
            existing_mode=attempt_mode,
            referee_path=referee_path,
            watcher_factory=watcher_factory,
        )

        if status is None:
            return False

        if status == TaskStatus.COMPLETE:
            return True

        if status in RETRYABLE_STATUSES:
            last_status = status.value
            if attempt_idx < max_retries:
                if continue_on_review and status in CONTINUABLE_STATUSES:
                    print(
                        f"    {status.value} — will retry via project.ask() "
                        "continuation (session preserved)"
                    )
                else:
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

    referee_path: Optional[Path] = None
    if args.referee is not None:
        referee_path = args.referee.resolve()
        if not referee_path.is_file():
            sys.exit(f"--referee file not found: {referee_path}")
    else:
        candidate = repo_dir / ".marathon" / "referee.md"
        if candidate.is_file():
            referee_path = candidate.resolve()

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
    if referee_path is not None:
        print(f"referee notes:    {referee_path}")
    print(f"workdir:          {workdir}")
    if not args.no_cross_chapter:
        sibling_count = sum(
            1 for entry in workdir.parent.iterdir()
            if entry.is_dir()
            and entry.resolve() != workdir.resolve()
            and (entry / REFINE_STATE_FILENAME).is_file()
        ) if workdir.parent.is_dir() else 0
        if sibling_count:
            print(f"cross-chapter:    auto ({sibling_count} sibling(s) under {workdir.parent})")
        else:
            print(f"cross-chapter:    auto (no siblings detected)")
    else:
        print(f"cross-chapter:    disabled")
    print(f"max iterations:   {args.max_iterations}")
    print(f"max retries/iter: {args.max_retries}")
    if args.skeleton:
        print("mode:             skeleton (no proofs; sorry-only)")
    if args.max_prompt_words is not None:
        print(f"max prompt words: {args.max_prompt_words}")

    # Determine sub-issue this iteration addresses (used by --auto-pr to
    # name the dedicated marathon branch deterministically per-issue and
    # link the PR back to the tracking issue).
    review_issue_num: Optional[int] = (
        int(args.review_rejection)
        if getattr(args, "review_rejection", None) is not None
        else None
    )

    pipeline_config = PipelineConfig(
        auto_build=args.auto_build,
        auto_commit=args.auto_commit,
        auto_push=args.auto_push,
        auto_rate=args.auto_rate,
        build_timeout=args.build_timeout,
        ratings_path=workdir / "marathon-ratings.jsonl",
        claude_in_loop=True,  # refine drafts each prompt via Claude
        referee_path=referee_path,
        audit_verified=getattr(args, "audit_verified", False),
        audit_workdir=workdir,
        update_formalization=getattr(args, "update_formalization", True),
        formalization_models=["claude-opus-4-7", "Aristotle"],
        formalization_framework="Marathon",
        auto_pr=getattr(args, "auto_pr", False),
        auto_pr_repo=getattr(args, "auto_pr_repo", None),
        auto_pr_review_issue=review_issue_num,
        auto_pr_base=getattr(args, "auto_pr_base", "main"),
    )

    # Load the pending-rejections context BEFORE any branch switch.
    # ``prepare_auto_pr_branch`` (below, when ``--auto-pr`` is on) does
    # ``git checkout -B branch origin/main`` and wipes the local
    # ``state.json`` rejection record. Reading state.json from inside
    # the iteration would then return None and the focused-rejection
    # bypass would silently fall through to Claude-in-loop. Capture the
    # rejection notes here while state.json still reflects them, then
    # thread the value into the iteration via
    # ``prefetched_pending_rejections_md``.
    prefetched_pending_rejections_md = (
        _load_pending_rejections_md(
            repo_dir, target_folder, focus_issue=review_issue_num,
        )
        if review_issue_num is not None
        else None
    )

    # When --auto-pr is set, prepare the dedicated marathon branch
    # BEFORE the iteration runs so the auto-commit lands on the right
    # branch. Refuses on a dirty working tree; fail-fast so the human
    # doesn't lose uncommitted work.
    if pipeline_config.auto_pr:
        from marathon.post_pipeline import prepare_auto_pr_branch
        # ``chapter_label`` isn't bound in this function — the analog
        # is ``target_folder.name`` (e.g., "Chapter14"). The inner
        # iteration loop uses ``target_folder_name`` for the same
        # purpose (cf. run_post_pipeline calls in _run_iteration).
        ok, branch_name, branch_msg = prepare_auto_pr_branch(
            repo_dir=Path(args.repo_dir).resolve(),
            chapter_label=target_folder.name,
            issue_num=review_issue_num,
            base=pipeline_config.auto_pr_base,
        )
        if not ok:
            print(f"  auto-pr: {branch_msg}", flush=True)
            print("  refusing to run iteration on the wrong branch.")
            return
        print(f"  auto-pr: {branch_msg}")
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

    # Construct the Hermes live-steering watcher factory if requested. The
    # factory is called once per attempt so the watcher can pick up the
    # current iteration index for its decision log. Built BEFORE the
    # dry-run early-exit so the dry-run summary surfaces the flag.
    watcher_factory = None
    if getattr(args, "live_steering", False):
        from marathon.hermes_watcher import build_watcher_factory
        watcher_factory = build_watcher_factory(
            workdir=workdir,
            target_folder=target_folder,
            repo_dir=repo_dir,
            referee_path=referee_path,
            skeleton_mode=args.skeleton,
        )
        print("live-steering:   on (Hermes will inspect each EDITING_FILE event)")

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

        continue_on_review = not getattr(args, "no_continue_on_review", False)
        existing_project, existing_task, existing_mode = await _try_reattach_or_continue(
            state, continue_on_review=continue_on_review,
        )

        if existing_mode is None:
            print(f"\n=== iteration {iteration_idx}/{args.max_iterations} ===")
            # Reset state for a fresh iteration. Claude is called inside
            # _run_iteration (per attempt), not here.
            state.attempts = 0
            state.project_id = None
            state.agent_task_id = None
            state.status = None
            state.started_at = None
            state.completed_at = None
            state.duration_seconds = None
            state.output_path = None
            state.note = None
            save_refine_state(state_path, state)
        elif existing_mode == "continue":
            print(
                f"\n=== iteration {iteration_idx}/{args.max_iterations} "
                "(continuation via project.ask) ==="
            )
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
            existing_task=existing_task,
            existing_mode=existing_mode,
            workdir=workdir,
            referee_path=referee_path,
            cross_chapter=not args.no_cross_chapter,
            watcher_factory=watcher_factory,
            continue_on_review=continue_on_review,
            review_rejection=getattr(args, "review_rejection", None),
            focus_directive=getattr(args, "focus_directive", None),
            prefetched_pending_rejections_md=prefetched_pending_rejections_md,
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

        # Auto-referee trigger: after every Nth iteration of this refine
        # invocation, run the referee agent to refresh referee.md.
        if (
            args.auto_referee_every > 0
            and iteration_idx % args.auto_referee_every == 0
        ):
            from marathon.referee import update_referee
            ref_path = (
                referee_path
                if referee_path is not None
                else (repo_dir / ".marathon" / "referee.md")
            )
            print(
                f"\n  auto-referee: triggered after iteration "
                f"{iteration_idx} (every {args.auto_referee_every}); "
                "scanning repo + sibling workdirs..."
            )
            ref_result = update_referee(
                repo_dir=repo_dir,
                referee_path=ref_path,
                workdirs_parent=workdir.parent if workdir.parent.is_dir() else None,
                auto_commit=True,
                # Mirror --auto-push: when the user has it on for chapter
                # commits, push the auto-referee commits too so the next
                # chapter's bundle sees the refreshed referee.md remotely.
                auto_push=bool(args.auto_push),
                write_to_proposed_only=False,
            )
            if ref_result.error:
                print(f"  auto-referee: ERROR — {ref_result.error}")
            elif ref_result.ok:
                tail_info = (
                    f"  auto-referee: wrote {ref_result.output_path.name}"
                )
                if ref_result.diff_summary:
                    tail_info += f"  ({ref_result.diff_summary})"
                if ref_result.commit_sha:
                    tail_info += f"  commit={ref_result.commit_sha}"
                if ref_result.pushed is True:
                    tail_info += "  push=ok"
                elif ref_result.pushed is False:
                    tail_info += f"  push=FAIL({ref_result.push_message})"
                print(tail_info)

    print(
        f"\nrefine batch finished. {state.iterations_completed} "
        f"iteration(s) completed."
    )

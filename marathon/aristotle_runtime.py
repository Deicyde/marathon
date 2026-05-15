"""Marathon's wrapper around the aristotlelib 2.x SDK.

The SDK split Marathon's old end-to-end ``Project.wait_for_completion`` into
three steps:

1. ``project = await Project.create_from_directory(...)`` — submit a project,
   which schedules its first ``AgentTask``.
2. ``task = (await project.get_tasks(limit=1))[0]`` — pick up the task we
   need to wait on. ``await task.wait_for_completion(...)`` (or our own
   polling loop) blocks until the task reaches a terminal ``TaskStatus``.
3. ``path = await project.get_files(destination=...)`` — download the
   result tarball (or the input if no result yet).

This module collapses those three steps back into the shape Marathon's
older code expects, and adds two things the old SDK didn't offer:

* A unified reattach helper (``reattach_project_and_task``) that gives both
  the ``Project`` and the latest ``AgentTask`` back in one call.
* A pluggable event watcher contract (``EventWatcher``) so callers (the
  Hermes live-steering watcher; future TUIs) can subscribe to the event
  stream concurrently with polling.

The vocabulary of terminal/retryable statuses in SDK 2.x is the same as the
old ``ProjectStatus`` vocabulary, just relocated onto ``TaskStatus``. The
string ``.value`` of each enum entry is unchanged, which keeps
``marathon-state.json`` files from prior runs readable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional, Protocol

from aristotlelib import (
    AgentTask,
    AristotleAPIError,
    Event,
    Project,
    TaskStatus,
)

# --- Status classification ---------------------------------------------------
# These mirror the old skeleton.py constants but on ``TaskStatus``. Stored
# state still uses ``TaskStatus.value`` strings, so old state files load
# without migration.

RETRYABLE_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.COMPLETE_WITH_ERRORS,
    TaskStatus.FAILED,
})

NON_RETRYABLE_FAILURE_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.OUT_OF_BUDGET,
    TaskStatus.CANCELED,
})

# Statuses meaning the task is still running on Aristotle's side.
IN_FLIGHT_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.QUEUED,
    TaskStatus.IN_PROGRESS,
})

# Same as above but as ``str`` so callers comparing against
# ``state.status`` (which is a stored ``TaskStatus.value``) don't have to
# convert on every call.
IN_FLIGHT_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in IN_FLIGHT_STATUSES)

TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = (
    RETRYABLE_STATUSES
    | NON_RETRYABLE_FAILURE_STATUSES
    | frozenset({TaskStatus.COMPLETE})
)

# Statuses that mean "this chapter is done; skip on re-run."
RESUMABLE_SUCCESS_STATUS_VALUES: frozenset[str] = frozenset({TaskStatus.COMPLETE.value})


# --- Watcher contract --------------------------------------------------------


class EventWatcher(Protocol):
    """Anything that consumes a task's event stream while the task runs.

    Implementations should poll ``task.get_events`` themselves and react to
    new events. The ``stop`` event is set by the polling loop when the task
    reaches a terminal status, so the watcher can drain final events and
    exit cleanly.
    """

    async def run(
        self,
        task: AgentTask,
        project: Project,
        stop: asyncio.Event,
    ) -> None:
        ...


# --- Submission / reattach ---------------------------------------------------


async def get_latest_task(project: Project) -> AgentTask:
    """Return the most recent ``AgentTask`` under ``project``.

    Raises ``RuntimeError`` if the project has no tasks at all (should not
    happen for a project we just submitted).
    """
    tasks, _ = await project.get_tasks(limit=1, newest_first=True)
    if not tasks:
        raise RuntimeError(
            f"project {project.project_id} has no AgentTask yet; "
            "this likely indicates an SDK or server bug"
        )
    return tasks[0]


async def submit_from_directory(
    prompt: str,
    project_dir: Path,
) -> tuple[Project, AgentTask]:
    """Submit a fresh project from a staging dir and pick up its first task.

    Wraps the two-call ``Project.create_from_directory`` → ``get_tasks(limit=1)``
    pattern. Raises ``AristotleAPIError`` if the submission fails.
    """
    project = await Project.create_from_directory(prompt=prompt, project_dir=project_dir)
    task = await get_latest_task(project)
    return project, task


async def reattach_project_and_task(
    project_id: str,
    agent_task_id: Optional[str] = None,
) -> tuple[Optional[Project], Optional[AgentTask]]:
    """Reattach to a prior project + latest task.

    Returns ``(None, None)`` if the project can't be loaded (API error /
    deleted). If the project loads but no task can be found, returns
    ``(project, None)`` so the caller can decide whether to start a new
    task via ``project.ask(...)``.

    If ``agent_task_id`` is provided, that specific task is loaded; otherwise
    the latest task under the project is used.
    """
    try:
        project = await Project.from_id(project_id)
        await project.refresh()
    except AristotleAPIError:
        return None, None

    task: Optional[AgentTask] = None
    try:
        if agent_task_id is not None:
            task = await AgentTask.from_id(agent_task_id)
            await task.refresh()
        else:
            task = await get_latest_task(project)
    except (AristotleAPIError, RuntimeError):
        task = None

    return project, task


# --- Polling -----------------------------------------------------------------


async def _poll_task_loop(
    task: AgentTask,
    polling_interval: int,
    stop: asyncio.Event,
) -> None:
    """Poll ``task.refresh`` until the task reaches a terminal status.

    Sets ``stop`` on exit so a concurrent watcher knows to drain. Honors
    ``stop`` being set externally (caller cancellation) by returning early.
    """
    try:
        while task.status in IN_FLIGHT_STATUSES:
            try:
                # Block on either the poll interval or an external stop signal,
                # so cancellation doesn't have to wait for the next refresh.
                await asyncio.wait_for(stop.wait(), timeout=polling_interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await task.refresh()
            except AristotleAPIError:
                # Surface to the caller via the normal terminal-status path:
                # leave task.status alone and try again on the next iteration.
                # Repeated failures will eventually exhaust caller timeouts.
                continue
    finally:
        stop.set()


async def run_task_to_completion(
    task: AgentTask,
    project: Project,
    *,
    polling_interval: int,
    watcher: Optional[EventWatcher] = None,
) -> AgentTask:
    """Block until ``task`` reaches a terminal ``TaskStatus``.

    If ``watcher`` is supplied, it runs concurrently with the polling loop;
    both share a single ``asyncio.Event`` that the polling loop sets when
    the task terminates. The watcher should respect that event and exit.

    Returns the (refreshed) task.
    """
    stop = asyncio.Event()

    if watcher is None:
        await _poll_task_loop(task, polling_interval, stop)
        return task

    poll_coro = _poll_task_loop(task, polling_interval, stop)
    watch_coro = watcher.run(task, project, stop)
    await asyncio.gather(poll_coro, watch_coro)
    return task


# --- Download ----------------------------------------------------------------


async def download_result(project: Project, destination: Path) -> Path:
    """Download the project's result tarball to ``destination``.

    ``Project.get_files`` refreshes the project before downloading and
    returns the resulting path (which is ``destination`` when supplied).
    """
    return await project.get_files(destination=destination)


# --- Event stream helper -----------------------------------------------------


async def fetch_new_events_since(
    task: AgentTask,
    seen_event_ids: set[str],
    limit_per_call: int = 50,
) -> list[Event]:
    """Return events under ``task`` whose IDs aren't already in
    ``seen_event_ids``, oldest-first.

    Updates ``seen_event_ids`` in place. Used by the Hermes watcher to walk
    the event stream incrementally without double-processing.

    Strategy: fetch the most recent batch newest-first, drop seen ones,
    reverse for chronological order. (Aristotle event IDs are stable, so
    set-based deduplication is reliable.)
    """
    events, _ = await task.get_events(limit=limit_per_call, newest_first=True)
    fresh: list[Event] = []
    for event in events:
        if event.event_id in seen_event_ids:
            continue
        fresh.append(event)
        seen_event_ids.add(event.event_id)
    fresh.reverse()
    return fresh

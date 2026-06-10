#!/usr/bin/env python3
"""Empirically measure how many Aristotle projects run concurrently.

*** WARNING — THIS SCRIPT SPENDS REAL ARISTOTLE COMPUTE BUDGET. ***

Every invocation submits K live projects (default 3) to Harmonic's
Aristotle service. Each one is a deliberately trivial prompt-only proof
request, but Aristotle still schedules a real session for each, and those
sessions bill against the account's budget. Do NOT run this casually, do
NOT run it in a loop, and do NOT wire it into CI. Requires
``ARISTOTLE_API_KEY`` in the environment.

Why this exists: the v2 Conductor (docs/marathon-v2-plan.md §3) dispatches
N concurrent ``marathon refine`` jobs, and §6 Open Question Q1 asks what
Aristotle's concurrent-session limit actually is — the dashboard docs
don't say. This probe answers Q1 empirically so the Conductor's
concurrency setting starts from a measured number instead of a guess.

What it does:

1. Submits K trivial prompt-only projects via ``Project.create`` (no tar
   upload), back to back. If the server rejects a submission with the
   "too many requests in progress" error, that rejection itself is the
   concurrency answer and is recorded.
2. Polls every submitted project's first ``AgentTask``, recording the
   first time each status (QUEUED / IN_PROGRESS / terminal) is observed.
3. Reports, per project, the QUEUED → IN_PROGRESS → terminal timeline,
   plus two concurrency measures:
   - **sampled**: max number of tasks simultaneously IN_PROGRESS at any
     single poll (lower bound; can miss overlap shorter than one poll),
   - **interval**: max overlap of the recorded [IN_PROGRESS, terminal)
     windows (upper bound; timestamps are first-*observed*, so each
     boundary is late by up to one poll interval).
4. On exit — normal completion, error, or Ctrl-C — cancels every
   still-running task so no budget keeps burning after the probe stops.
   Pass ``--keep`` to leave them running instead (e.g. to watch them in
   the dashboard).

Usage:
    ARISTOTLE_API_KEY=arstl_... uv run scripts/aristotle_concurrency_probe.py
    uv run scripts/aristotle_concurrency_probe.py --k 5 --poll-interval 10
    uv run scripts/aristotle_concurrency_probe.py --keep   # no auto-cancel
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aristotlelib
from aristotlelib import AgentTask, AristotleAPIError, Project, TaskStatus

# Deliberately trivial: a one-tactic arithmetic fact, no files attached.
# The goal is to occupy a scheduler slot for the shortest plausible time,
# not to produce useful output.
TRIVIAL_PROMPT = (
    "Prove the following single theorem in Lean 4 and nothing else: "
    "theorem probe_one_add_one : 1 + 1 = 2. "
    "This is a trivial infrastructure probe; do not expand scope."
)

IN_FLIGHT = frozenset({TaskStatus.QUEUED, TaskStatus.IN_PROGRESS})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ProbeRecord:
    """One submitted probe project and everything we observed about it."""

    index: int
    project: Project
    task: Optional[AgentTask] = None
    # status value -> (wall-clock ISO string, monotonic seconds) at first
    # observation. Monotonic times feed the interval-overlap math; ISO
    # strings feed the human-readable report.
    first_seen: dict[str, tuple[str, float]] = field(default_factory=dict)
    canceled_by_probe: bool = False

    def observe(self, status: TaskStatus) -> None:
        if status.value not in self.first_seen:
            self.first_seen[status.value] = (_now_iso(), time.monotonic())

    @property
    def is_terminal(self) -> bool:
        return self.task is not None and self.task.status not in IN_FLIGHT


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Aristotle's effective project concurrency. "
            "SPENDS REAL COMPUTE BUDGET — read the module docstring first."
        ),
    )
    parser.add_argument(
        "--k",
        type=int,
        default=3,
        help="number of trivial projects to submit (default: 3)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=15,
        help="seconds between status polls (default: 15)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="do NOT cancel still-running probe projects on exit",
    )
    args = parser.parse_args(argv)
    if args.k < 1:
        parser.error("--k must be >= 1")
    if args.poll_interval < 1:
        parser.error("--poll-interval must be >= 1")
    return args


async def _submit_all(k: int, records: list[ProbeRecord]) -> None:
    """Submit K prompt-only projects, appending to ``records`` as we go.

    ``records`` is mutated in place (not returned) so the caller's
    cleanup path can cancel whatever was submitted even if we die midway
    through the loop or get Ctrl-C'd.
    """
    for i in range(k):
        try:
            project = await Project.create(prompt=TRIVIAL_PROMPT)
        except AristotleAPIError as e:
            # A 429-style "too many requests in progress" here IS the
            # measurement: the server refused submission i while i
            # earlier projects were live. Record loudly and stop.
            print(
                f"[probe] submission {i + 1}/{k} REJECTED by server: {e}\n"
                f"[probe] => server-side cap reached with {len(records)} "
                "project(s) already live; that is the concurrency answer."
            )
            return
        rec = ProbeRecord(index=i, project=project)
        records.append(rec)
        print(
            f"[probe] submitted {i + 1}/{k}: project_id={project.project_id}"
        )
        try:
            tasks, _ = await project.get_tasks(limit=1, newest_first=True)
        except AristotleAPIError as e:
            print(f"[probe]   WARN could not fetch task list yet: {e}")
            tasks = []
        if tasks:
            rec.task = tasks[0]
            rec.observe(rec.task.status)


async def _poll_until_done(
    records: list[ProbeRecord], poll_interval: int
) -> int:
    """Poll every record's task until all are terminal.

    Returns the max number of tasks observed simultaneously IN_PROGRESS
    across all polls (the *sampled* concurrency measure).
    """
    max_sampled = 0
    while True:
        in_progress = 0
        pending = 0
        for rec in records:
            if rec.task is None:
                # Task didn't exist at submit time (rare lag) — retry.
                try:
                    tasks, _ = await rec.project.get_tasks(
                        limit=1, newest_first=True
                    )
                    if tasks:
                        rec.task = tasks[0]
                except AristotleAPIError:
                    pass
                if rec.task is None:
                    pending += 1
                    continue
            if rec.task.status in IN_FLIGHT:
                try:
                    await rec.task.refresh()
                except AristotleAPIError:
                    # Transient poll failure: keep the last-known status
                    # and try again next round.
                    pass
            rec.observe(rec.task.status)
            if rec.task.status == TaskStatus.IN_PROGRESS:
                in_progress += 1
            elif rec.task.status in IN_FLIGHT:
                pending += 1
        max_sampled = max(max_sampled, in_progress)
        statuses = ", ".join(
            f"#{rec.index}={rec.task.status.value if rec.task else 'NO_TASK'}"
            for rec in records
        )
        print(
            f"[probe] {_now_iso()} in_progress={in_progress} "
            f"max_sampled={max_sampled} [{statuses}]"
        )
        if in_progress == 0 and pending == 0:
            return max_sampled
        await asyncio.sleep(poll_interval)


def _interval_overlap_max(records: list[ProbeRecord]) -> int:
    """Max overlap of recorded [IN_PROGRESS, terminal) windows.

    Classic sweep over interval endpoints. Records that never reached
    IN_PROGRESS contribute nothing; records still running at probe exit
    (e.g. --keep + Ctrl-C) are treated as open-ended through "now".
    """
    events: list[tuple[float, int]] = []
    for rec in records:
        start = rec.first_seen.get(TaskStatus.IN_PROGRESS.value)
        if start is None:
            continue
        end_mono = time.monotonic()
        for status_value, (_, mono) in rec.first_seen.items():
            if status_value not in (
                TaskStatus.QUEUED.value,
                TaskStatus.IN_PROGRESS.value,
            ):
                end_mono = min(end_mono, mono)
        events.append((start[1], +1))
        events.append((end_mono, -1))
    events.sort()
    best = cur = 0
    for _, delta in events:
        cur += delta
        best = max(best, cur)
    return best


async def _cancel_remaining(records: list[ProbeRecord]) -> None:
    """Cancel every probe task still in flight (best-effort)."""
    for rec in records:
        task = rec.task
        if task is None:
            try:
                tasks, _ = await rec.project.get_tasks(limit=1, newest_first=True)
                task = tasks[0] if tasks else None
                rec.task = task
            except AristotleAPIError:
                task = None
        if task is None:
            print(
                f"[probe] WARN no task found to cancel for "
                f"project_id={rec.project.project_id}; check the dashboard."
            )
            continue
        try:
            await task.refresh()
        except AristotleAPIError:
            pass
        if task.status in IN_FLIGHT:
            try:
                await task.cancel()
                rec.canceled_by_probe = True
                print(
                    f"[probe] canceled project_id={rec.project.project_id} "
                    f"task_id={task.agent_task_id}"
                )
            except AristotleAPIError as e:
                print(
                    f"[probe] WARN cancel failed for "
                    f"project_id={rec.project.project_id}: {e} "
                    "— cancel it manually in the dashboard."
                )


def _print_report(
    records: list[ProbeRecord], k_requested: int, max_sampled: Optional[int]
) -> None:
    print("\n" + "=" * 72)
    print("[probe] REPORT")
    print(f"[probe] requested: {k_requested}  submitted: {len(records)}")
    for rec in records:
        timeline = " -> ".join(
            f"{status}@{iso}"
            for status, (iso, _) in sorted(
                rec.first_seen.items(), key=lambda kv: kv[1][1]
            )
        ) or "(nothing observed)"
        final = rec.task.status.value if rec.task else "NO_TASK"
        suffix = "  [canceled by probe]" if rec.canceled_by_probe else ""
        print(
            f"[probe]   #{rec.index} project_id={rec.project.project_id} "
            f"final={final}{suffix}"
        )
        print(f"[probe]      {timeline}")
    sampled_str = str(max_sampled) if max_sampled is not None else "n/a (interrupted)"
    print(f"[probe] max simultaneous IN_PROGRESS (sampled per poll): {sampled_str}")
    print(f"[probe] max simultaneous IN_PROGRESS (interval overlap):  "
          f"{_interval_overlap_max(records)}")
    print(
        "[probe] Record the larger number in docs/marathon-v2-plan.md "
        "(§6 Q1); the Conductor's starting concurrency should not exceed it."
    )
    print("=" * 72)


async def _run(args: argparse.Namespace, records: list[ProbeRecord]) -> int:
    await _submit_all(args.k, records)
    if not records:
        return 0
    return await _poll_until_done(records, args.poll_interval)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    key = os.environ.get("ARISTOTLE_API_KEY")
    if not key:
        print(
            "ARISTOTLE_API_KEY not set — refusing to run. (This probe "
            "spends real Aristotle compute budget; see module docstring.)",
            file=sys.stderr,
        )
        return 2
    aristotlelib.set_api_key(key)

    print(
        f"[probe] submitting {args.k} trivial project(s); THIS SPENDS REAL "
        "ARISTOTLE BUDGET. Ctrl-C cancels in-flight probe projects"
        + (" (disabled by --keep)." if args.keep else ".")
    )

    records: list[ProbeRecord] = []
    max_sampled: Optional[int] = None
    exit_code = 0
    try:
        max_sampled = asyncio.run(_run(args, records))
    except KeyboardInterrupt:
        print("\n[probe] interrupted.")
        exit_code = 130
    finally:
        # Cleanup runs in a FRESH event loop: the original one may have
        # died mid-cancellation on Ctrl-C, and awaiting inside a
        # cancelled task is unreliable. A new asyncio.run is boring and
        # dependable.
        if records and not args.keep:
            try:
                asyncio.run(_cancel_remaining(records))
            except KeyboardInterrupt:
                print(
                    "[probe] WARN cleanup interrupted — probe projects may "
                    "still be running; cancel them in the dashboard."
                )
                exit_code = 130
        elif records and args.keep:
            print("[probe] --keep: leaving probe projects running (BUDGET!).")
    if records:
        _print_report(records, args.k, max_sampled)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

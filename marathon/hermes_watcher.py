"""Hermes live-steering watcher.

Subscribes to an Aristotle ``AgentTask``'s event stream while the task is
running. On every ``EventType.EDITING_FILE`` event, asks Claude (the
"Hermes" reviewer role) whether Aristotle is going off-course; if so,
sends a steering prompt to the project via ``project.ask(...)``.

Design choices:

* **Filter to EDITING_FILE only.** Per the project decision, Hermes
  inspects edits and skips thinking/building/reading/etc. Each edit
  triggers exactly one Hermes call.
* **Opus 4.7 throughout.** No two-tier (Haiku→Opus) escalation; we
  always go straight to Opus, gated only by the type-filter above.
* **project.ask only.** Hermes never cancels or restarts tasks. The
  worst case is "no steer, let the iteration reviewer catch it."
* **Per-attempt watcher.** Each refine attempt gets a fresh watcher
  with a fresh seen-event set, so the in-memory state can't leak
  across reruns/retries.
* **Single-flight Claude calls.** Multiple edit events arriving in one
  poll batch are processed sequentially; we do not parallelize
  hermes_judge calls within a single watcher instance. (Cheap to
  enforce, avoids out-of-order steering.)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from aristotlelib import (
    AgentTask,
    AristotleAPIError,
    Event,
    EventType,
    Project,
    TaskStatus,
)

from marathon.aristotle_runtime import (
    IN_FLIGHT_STATUSES,
    EventWatcher,
    fetch_new_events_since,
)

# Same model as the iteration reviewer and auto-rater.
CLAUDE_MODEL = "claude-opus-4-7"

# Steering-log filename in the workdir.
STEERING_LOG_FILENAME = "marathon-steering-log.jsonl"

# How often the watcher polls for new events (seconds). Distinct from the
# refine status-poll interval; the event watcher polls faster because edits
# arrive in bursts and we want low-latency steering.
EVENT_POLL_INTERVAL = 5

# How many recent events to feed Hermes for context, on top of the
# specific edit it's judging.
RECENT_EVENTS_CONTEXT = 6

# How many past steering decisions Hermes sees, so it doesn't re-steer on
# the same issue. We pass titles + reasons, not full prompts.
STEERING_HISTORY_CONTEXT = 8


@dataclass
class SteeringDecision:
    """Parsed output of a single Hermes call."""

    steer: bool
    reason: str
    prompt: Optional[str] = None
    parse_error: Optional[str] = None
    raw_response: Optional[str] = None


def _ensure_claude_cli() -> str:
    path = shutil.which("claude")
    if not path:
        sys.exit(
            "claude (Claude Code CLI) not found on PATH; required by "
            "--live-steering. Install Claude Code and authenticate once "
            "interactively, then retry."
        )
    return path


def _read_hermes_rubric() -> str:
    path = Path(__file__).parent / "prompts" / "hermes_steer.md"
    if not path.is_file():
        sys.exit(f"Hermes rubric missing: {path}")
    return path.read_text()


def _truncate(text: Optional[str], limit: int) -> str:
    """Trim ``text`` to ``limit`` chars with a tail marker."""
    if text is None:
        return "(none)"
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} chars trimmed]"


def _event_to_md(event: Event) -> str:
    """Render an Event as a compact markdown block for Hermes' context."""
    et = event.event_type.name if event.event_type is not None else "UNKNOWN"
    head = f"- **{et}** ({event.created_at.isoformat()})"
    if event.file_path:
        head += f" — `{event.file_path}`"
    body_parts: list[str] = []
    if event.explanation:
        body_parts.append(f"  explanation: {event.explanation}")
    if event.content:
        body_parts.append(f"  content: {_truncate(event.content, 600)}")
    return head + ("\n" + "\n".join(body_parts) if body_parts else "")


def _parse_decision(raw: str) -> SteeringDecision:
    """Parse the JSON line Hermes emits.

    Hermes is instructed to emit a single-line JSON object, but reality
    sometimes adds whitespace / a code fence / a stray comment. Strip a
    leading/trailing fence, find the first ``{``, and parse from there.
    """
    text = raw.strip()
    # Strip a leading "```...\n" / trailing "\n```" fence if present.
    fence_match = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", text, flags=re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Extract the first {...} block. We tolerate trailing prose by using
    # ``raw_decode``, which returns the longest leading JSON value.
    start = text.find("{")
    if start < 0:
        return SteeringDecision(
            steer=False,
            reason="(parse error: no JSON object in Hermes output)",
            parse_error="no '{' in output",
            raw_response=raw,
        )
    decoder = json.JSONDecoder()
    try:
        data, _end = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as e:
        return SteeringDecision(
            steer=False,
            reason="(parse error: malformed JSON in Hermes output)",
            parse_error=str(e),
            raw_response=raw,
        )

    if not isinstance(data, dict):
        return SteeringDecision(
            steer=False,
            reason="(parse error: Hermes output was not a JSON object)",
            parse_error=f"top-level type was {type(data).__name__}",
            raw_response=raw,
        )

    steer = bool(data.get("steer", False))
    reason = str(data.get("reason") or "")
    prompt = data.get("prompt")
    if prompt is not None:
        prompt = str(prompt).strip()
        if not prompt:
            prompt = None
    return SteeringDecision(steer=steer, reason=reason, prompt=prompt, raw_response=raw)


class HermesWatcher:
    """The live-steering loop. One instance per refine attempt.

    Lifecycle: ``run(task, project, stop)`` is invoked concurrently with
    Marathon's status polling. Returns when ``stop`` is set OR when
    ``task.status`` is observed terminal (whichever comes first); the
    watcher drains a final event batch before exiting so the last edit
    in a run is always inspected.
    """

    def __init__(
        self,
        *,
        workdir: Path,
        target_folder: Path,
        repo_dir: Path,
        referee_path: Optional[Path],
        skeleton_mode: bool,
        iteration_idx: int,
        poll_interval: int = EVENT_POLL_INTERVAL,
    ) -> None:
        self.workdir = workdir
        self.target_folder = target_folder
        self.repo_dir = repo_dir
        self.referee_path = referee_path
        self.skeleton_mode = skeleton_mode
        self.iteration_idx = iteration_idx
        self.poll_interval = poll_interval

        self.steering_log_path = workdir / STEERING_LOG_FILENAME
        self.seen_event_ids: set[str] = set()
        self.recent_events: list[Event] = []
        self.steering_history: list[SteeringDecision] = []
        self._claude_path = _ensure_claude_cli()
        self._rubric = _read_hermes_rubric()

    # --- Watcher contract --------------------------------------------------

    async def run(
        self,
        task: AgentTask,
        project: Project,
        stop: asyncio.Event,
    ) -> None:
        print(
            f"  hermes-watcher: starting (iteration {self.iteration_idx}, "
            f"task {task.agent_task_id})"
        )

        # Seed the seen-set with whatever events already exist so we don't
        # re-judge events from a previous reattach. Hermes only steers on
        # NEW edits arriving after we start watching.
        try:
            preexisting = await fetch_new_events_since(task, self.seen_event_ids)
            # Keep the most recent N for context, but mark all as seen.
            self.recent_events = preexisting[-RECENT_EVENTS_CONTEXT:]
        except AristotleAPIError as e:
            print(f"  hermes-watcher: WARN seeding events failed: {e}; continuing")

        try:
            while not stop.is_set():
                # Poll for new events.
                try:
                    fresh = await fetch_new_events_since(task, self.seen_event_ids)
                except AristotleAPIError as e:
                    print(f"  hermes-watcher: WARN event fetch failed: {e}; will retry")
                    await self._sleep_or_stop(stop)
                    continue

                for event in fresh:
                    self.recent_events.append(event)
                    self.recent_events = self.recent_events[-RECENT_EVENTS_CONTEXT * 2:]
                    if event.event_type is EventType.EDITING_FILE:
                        await self._handle_edit(event, task, project)

                # If the polling loop has signaled stop while we were
                # processing, exit immediately. Otherwise, wait for the
                # next interval or a stop signal, whichever comes first.
                if stop.is_set():
                    break
                await self._sleep_or_stop(stop)

            # Drain once more in case events landed in the gap between
            # task termination and our last poll.
            try:
                tail = await fetch_new_events_since(task, self.seen_event_ids)
            except AristotleAPIError:
                tail = []
            for event in tail:
                self.recent_events.append(event)
                if event.event_type is EventType.EDITING_FILE:
                    # Task already terminated, so steering would either no-op
                    # or start a new task. We log but never ask() on drain.
                    decision = await self._judge_edit(event, task, project)
                    self._record_decision(event, decision, sent=False)
        finally:
            steered = sum(1 for d in self.steering_history if d.steer)
            print(
                f"  hermes-watcher: done. judged "
                f"{len(self.steering_history)} edit(s), steered {steered}"
            )

    async def _sleep_or_stop(self, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
        except asyncio.TimeoutError:
            return

    # --- Per-edit handling -------------------------------------------------

    async def _handle_edit(
        self,
        event: Event,
        task: AgentTask,
        project: Project,
    ) -> None:
        decision = await self._judge_edit(event, task, project)

        if not decision.steer or not decision.prompt:
            self._record_decision(event, decision, sent=False)
            return

        # Guard against steering a task that already finished. ``project.ask``
        # on an IDLE project would *start a new task*, which is not what we
        # want; we only want to redirect a running task.
        try:
            await task.refresh()
        except AristotleAPIError:
            pass
        if task.status not in IN_FLIGHT_STATUSES:
            decision.parse_error = (
                f"task terminal ({task.status.value}); skipping ask"
            )
            self._record_decision(event, decision, sent=False)
            return

        try:
            new_task = await project.ask(decision.prompt)
            self._record_decision(event, decision, sent=True, new_task_id=new_task.agent_task_id)
            print(
                f"  hermes-watcher: STEERED — {decision.reason}\n"
                f"    prompt: {_truncate(decision.prompt, 200)}"
            )
        except AristotleAPIError as e:
            decision.parse_error = f"project.ask failed (status {e.status_code}): {e}"
            self._record_decision(event, decision, sent=False)
            print(f"  hermes-watcher: ask() failed — {e}; decision logged")

    async def _judge_edit(
        self,
        event: Event,
        task: AgentTask,
        project: Project,
    ) -> SteeringDecision:
        prompt = self._build_hermes_prompt(event, task)
        try:
            raw = await asyncio.to_thread(self._invoke_claude, prompt)
        except Exception as e:  # noqa: BLE001 - we want to log unrelated failures
            return SteeringDecision(
                steer=False,
                reason=f"(hermes invocation failed: {e})",
                parse_error=repr(e),
            )
        return _parse_decision(raw)

    def _build_hermes_prompt(self, event: Event, task: AgentTask) -> str:
        target_rel = self._target_relpath()

        history_md: list[str] = []
        for prev in self.steering_history[-STEERING_HISTORY_CONTEXT:]:
            verdict = "STEERED" if prev.steer else "skipped"
            history_md.append(f"- {verdict}: {prev.reason}")
        history_block = "\n".join(history_md) if history_md else "(none yet)"

        # The specific edit under review.
        edit_md = _event_to_md(event)

        # Recent events (excluding the focal edit), for context. Don't
        # repeat the focal one.
        ctx_events = [e for e in self.recent_events if e.event_id != event.event_id]
        ctx_md_list = [_event_to_md(e) for e in ctx_events[-RECENT_EVENTS_CONTEXT:]]
        ctx_block = "\n".join(ctx_md_list) if ctx_md_list else "(none)"

        # Referee notes; trim to keep total prompt size bounded.
        referee_md = "(no referee.md available)"
        if self.referee_path and self.referee_path.is_file():
            try:
                referee_md = _truncate(self.referee_path.read_text(), 8_000)
            except OSError:
                referee_md = "(could not read referee.md)"

        # Task description (the original prompt that kicked off this task)
        # gives Hermes the goal so it knows what "on-course" means.
        task_desc = _truncate(task.description, 1_500) if task.description else "(unknown)"

        rubric = self._rubric.replace("{target_folder}", target_rel).replace(
            "{skeleton_mode}", "true" if self.skeleton_mode else "false"
        )

        return (
            f"{rubric}\n\n"
            f"---\n\n"
            f"## Task goal\n\n{task_desc}\n\n"
            f"## Target folder\n\n`{target_rel}/`\n\n"
            f"## Skeleton mode\n\n{self.skeleton_mode}\n\n"
            f"## Referee notes (project-level reviewer guidance)\n\n{referee_md}\n\n"
            f"## Recent events (most recent last)\n\n{ctx_block}\n\n"
            f"## Steering decisions so far this attempt\n\n{history_block}\n\n"
            f"## THE EDIT TO JUDGE\n\n{edit_md}\n\n"
            f"---\n\n"
            "Emit ONLY the single-line JSON object now (no markdown, no prose)."
        )

    def _invoke_claude(self, prompt: str) -> str:
        """Synchronous subprocess call to ``claude -p`` (Claude Code).

        Runs from the Marathon repo root so any cwd-local ``.claude/``
        settings still apply. ``ANTHROPIC_API_KEY`` is scrubbed so the
        CLI uses Max OAuth from the keychain (same as ``claude_review``).
        """
        cmd = [
            self._claude_path,
            "-p", prompt,
            "--model", CLAUDE_MODEL,
            "--tools", "",
            "--output-format", "text",
        ]
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, env=env,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or "(no output)"
            raise RuntimeError(f"claude exited {proc.returncode}: {err}")
        return proc.stdout

    # --- Persistence --------------------------------------------------------

    def _record_decision(
        self,
        event: Event,
        decision: SteeringDecision,
        *,
        sent: bool,
        new_task_id: Optional[str] = None,
    ) -> None:
        self.steering_history.append(decision)
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "iteration": self.iteration_idx,
            "agent_task_id": event.agent_task_id,
            "event_id": event.event_id,
            "event_type": event.event_type.name if event.event_type else None,
            "file_path": event.file_path,
            "explanation": event.explanation,
            "steer": decision.steer,
            "reason": decision.reason,
            "prompt": decision.prompt,
            "sent": sent,
            "new_task_id_after_ask": new_task_id,
            "parse_error": decision.parse_error,
        }
        try:
            with self.steering_log_path.open("a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            # Non-fatal — log to stdout and keep going.
            print(f"  hermes-watcher: WARN could not append steering log: {e}")

    def _target_relpath(self) -> str:
        try:
            return str(self.target_folder.relative_to(self.repo_dir))
        except ValueError:
            return str(self.target_folder)


def build_watcher_factory(
    *,
    workdir: Path,
    target_folder: Path,
    repo_dir: Path,
    referee_path: Optional[Path],
    skeleton_mode: bool,
):
    """Return a factory matching the ``watcher_factory`` signature expected
    by ``marathon.refine._run_refine_attempt``: ``(state, iteration_idx)``.

    Each refine attempt calls the factory to get a fresh watcher whose
    seen-event set starts empty.
    """

    def factory(_state, iteration_idx: int) -> EventWatcher:
        return HermesWatcher(
            workdir=workdir,
            target_folder=target_folder,
            repo_dir=repo_dir,
            referee_path=referee_path,
            skeleton_mode=skeleton_mode,
            iteration_idx=iteration_idx,
        )

    return factory

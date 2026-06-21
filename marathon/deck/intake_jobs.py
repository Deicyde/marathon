"""Thread-safe registry of in-flight headless chapter-intake jobs for the deck.

A chapter-intake (:func:`marathon.review.chapter_intake.run_chapter_intake`) is a
multi-minute single-shot Claude call, so the deck can't run it inside a request.
``POST /api/add-chapter`` kicks one off on a daemon thread and returns
immediately; the UI then polls ``/api/chapter-intake-status`` until the job is
``ready`` (drafted sub-issues to approve) or ``error``. One job per chapter;
starting a new one supersedes a finished/errored prior job (never a running one).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from marathon.review.chapter_intake import IntakeResult, run_chapter_intake


@dataclass
class _Job:
    chapter: int
    status: str  # "running" | "ready" | "error"
    result: Optional[IntakeResult] = None


def _snapshot(job: _Job) -> dict:
    """JSON-able view of a job for the deck (the frontend reads this verbatim)."""
    out: dict = {"chapter": job.chapter, "status": job.status}
    res = job.result
    if res is not None:
        out["error"] = res.error
        out["model"] = res.model
        out["drafts_path"] = str(res.drafts_path)
        out["subissues"] = [
            {"index": s.index, "title": s.title, "body": s.body}
            for s in res.subissues
        ]
    return out


class IntakeRegistry:
    """Per-deck registry of chapter-intake jobs. Inject ``runner`` in tests."""

    def __init__(self, runner=run_chapter_intake) -> None:
        self._jobs: dict[int, _Job] = {}
        self._lock = threading.Lock()
        self._runner = runner

    def start(self, cfg, chapter: int, informal_path: Optional[Path] = None) -> dict:
        """Start (or return the already-running) intake for ``chapter``.

        Returns the job snapshot. A second call while one is running is a no-op
        that returns the running snapshot — the UI's poll and a double-click are
        both safe."""
        with self._lock:
            existing = self._jobs.get(chapter)
            if existing is not None and existing.status == "running":
                return _snapshot(existing)
            job = _Job(chapter, "running")
            self._jobs[chapter] = job

        def _work() -> None:
            try:
                res: IntakeResult = self._runner(
                    cfg, chapter, informal_path=informal_path
                )
                status = "ready" if res.ok else "error"
            except Exception as exc:  # noqa: BLE001 — registry must never die
                res = IntakeResult(
                    chapter, Path(""), [], "", "(default)",
                    error=f"intake thread crashed: {exc}",
                )
                status = "error"
            with self._lock:
                self._jobs[chapter] = _Job(chapter, status, res)

        threading.Thread(
            target=_work, name=f"marathon-intake-c{chapter}", daemon=True
        ).start()
        with self._lock:
            return _snapshot(self._jobs[chapter])

    def status(self, chapter: int) -> Optional[dict]:
        """The chapter's job snapshot, or ``None`` if no intake was ever started."""
        with self._lock:
            job = self._jobs.get(chapter)
            return _snapshot(job) if job is not None else None

    def result(self, chapter: int) -> Optional[IntakeResult]:
        """The finished :class:`IntakeResult` for ``chapter`` (for the approve
        path), or ``None`` if absent / still running."""
        with self._lock:
            job = self._jobs.get(chapter)
            if job is None or job.status == "running":
                return None
            return job.result

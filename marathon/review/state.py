"""Per-issue review state for the rejection queue.

`marathon review reject 22 --notes …` records the rejection here (in a
project-wide JSON file under ``.marathon/review/state.json``) rather
than appending bullets to ``referee.md``. This decouples per-issue
review state from the chapter-wide rubric layer, fixing several
failure modes of the prior referee.md-as-queue design:

* **Auto-nesting.** The referee.md queue wrapped every reject under a
  fixed ``- **Review #N REJECTED**`` heading and indented notes by two
  spaces, producing double-indentation when notes themselves opened
  with a top-level bullet.
* **No dedup or update.** Each ``marathon review reject`` of the same
  issue appended *another* block; stale entries accumulated and
  ``marathon review verify`` never cleared the corresponding queue
  entry from referee.md.
* **Cross-issue contamination.** Entries for issue #M diluted Hermes's
  read when working on #N.
* **Stale top-level notes.** Per-issue rejections living in
  referee.md's user header never aged out when the issue flipped to
  verified.

The state file is project-wide (one file across all chapters); the
daemon's chapter filter is applied at query time via the chapter
registry in ``ReviewConfig``.

Schema (versioned for forward compatibility)::

    {
      "schema_version": 1,
      "issues": {
        "<issue_num>": {
          "status": "rejected" | "verified",
          "verdict_ts": "2026-05-15T20:52:04-04:00",
          "notes": "...markdown body..."   // present iff status=="rejected"
        }
      }
    }

Concurrent writes are not protected; the CLI's per-issue command
granularity makes overlap rare and the worst-case is a lost write
that the next ``reject``/``verify`` reapplies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from marathon.review.config import ReviewConfig


SCHEMA_VERSION = 1
STATE_RELPATH = Path(".marathon/review/state.json")


@dataclass
class IssueState:
    status: str                    # "rejected" or "verified"
    verdict_ts: str                # ISO 8601
    notes: Optional[str] = None    # markdown; only when status=="rejected"


@dataclass
class ReviewState:
    issues: dict[int, IssueState]

    def get(self, issue_num: int) -> Optional[IssueState]:
        return self.issues.get(issue_num)

    def to_json(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "issues": {
                str(num): {
                    "status": st.status,
                    "verdict_ts": st.verdict_ts,
                    **({"notes": st.notes} if st.notes is not None else {}),
                }
                for num, st in sorted(self.issues.items())
            },
        }


def state_path(cfg: ReviewConfig) -> Path:
    return cfg.repo_dir / STATE_RELPATH


def load_state(cfg: ReviewConfig) -> ReviewState:
    """Load state.json. Returns an empty state if the file doesn't exist
    or is unparseable (logged but non-fatal — callers can `record_*`
    against a fresh empty state)."""
    path = state_path(cfg)
    if not path.is_file():
        return ReviewState(issues={})
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"  warning: {path} unparseable ({e}); treating as empty state")
        return ReviewState(issues={})
    raw_issues = data.get("issues", {})
    issues: dict[int, IssueState] = {}
    for key, val in raw_issues.items():
        try:
            issues[int(key)] = IssueState(
                status=val["status"],
                verdict_ts=val["verdict_ts"],
                notes=val.get("notes"),
            )
        except (KeyError, ValueError) as e:
            print(f"  warning: {path} issue {key!r} malformed ({e}); skipping")
    return ReviewState(issues=issues)


def save_state(cfg: ReviewConfig, state: ReviewState) -> None:
    """Atomic-ish write: write to ``state.json.tmp`` then rename."""
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_json(), indent=2) + "\n")
    tmp.replace(path)


def _now_iso() -> str:
    # Local-timezone ISO 8601 with offset, matching the prior verdict-ts
    # style seen in issue comments. astimezone() picks the local TZ.
    return datetime.now().astimezone().isoformat(timespec="seconds")


def record_rejection(cfg: ReviewConfig, issue_num: int, notes: str) -> IssueState:
    """Record a rejection: status=rejected, notes set, timestamp now.
    Overwrites any prior state for this issue."""
    state = load_state(cfg)
    entry = IssueState(status="rejected", verdict_ts=_now_iso(), notes=notes.strip())
    state.issues[issue_num] = entry
    save_state(cfg, state)
    return entry


def record_verification(cfg: ReviewConfig, issue_num: int) -> IssueState:
    """Record a verification: status=verified, notes cleared. This is the
    canonical way to clear a prior rejection's queue entry."""
    state = load_state(cfg)
    entry = IssueState(status="verified", verdict_ts=_now_iso(), notes=None)
    state.issues[issue_num] = entry
    save_state(cfg, state)
    return entry


def pending_rejections(
    cfg: ReviewConfig, chapter: Optional[int] = None
) -> list[tuple[int, IssueState]]:
    """Return all currently-rejected issues (with their notes), filtered
    to ``chapter`` if given. Sorted by issue number.

    If ``chapter`` is supplied, only issues registered in that chapter's
    registry are returned. Unknown issues (not in any chapter registry)
    are returned only when ``chapter`` is ``None`` (project-wide query).
    """
    state = load_state(cfg)
    if chapter is None:
        candidates = state.issues.items()
    else:
        registry = cfg.chapter_registry(chapter)
        known = {num for num, _ in registry.entries}
        candidates = ((n, s) for n, s in state.issues.items() if n in known)
    out = [(num, st) for num, st in candidates if st.status == "rejected"]
    out.sort(key=lambda x: x[0])
    return out


def render_pending_rejections_md(
    cfg: ReviewConfig, chapter: Optional[int] = None
) -> Optional[str]:
    """Render the pending rejections for ``chapter`` (or all chapters
    when None) as a Markdown block suitable for the Hermes/Claude
    prompt. Returns ``None`` if there are no pending rejections.

    Format mirrors what users wrote into referee.md before — top-level
    bullets, no auto-wrapper — so Hermes can read it identically.
    """
    pending = pending_rejections(cfg, chapter)
    if not pending:
        return None
    parts: list[str] = []
    for num, st in pending:
        # If the user's notes already begin with `- **`, keep them as-is.
        # Otherwise wrap with a stub header so Hermes always sees a
        # bullet boundary.
        notes = (st.notes or "").rstrip()
        if notes.lstrip().startswith("- **"):
            parts.append(notes)
        else:
            parts.append(f"- **Review #{num} REJECTED ({st.verdict_ts})**\n\n{notes}")
    return "\n\n".join(parts)


def hash_pending(cfg: ReviewConfig, chapter: Optional[int] = None) -> str:
    """SHA256 of the pending-rejections content for ``chapter`` (or
    project-wide when None). Used by the daemon to detect new rejections
    landing while it is in its poll loop.

    Returns ``''`` (empty string) when there are no pending rejections,
    matching the empty-file convention used previously."""
    rendered = render_pending_rejections_md(cfg, chapter)
    if not rendered:
        return ""
    return hashlib.sha256(rendered.encode()).hexdigest()

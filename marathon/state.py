"""Persistent state for skeleton runs (``marathon-state.json``)."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class ChapterState:
    input_file: str
    output_folder: str
    project_id: Optional[str] = None
    status: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    output_path: Optional[str] = None
    note: Optional[str] = None
    attempts: int = 0


@dataclass
class RunState:
    chapters: list[ChapterState] = field(default_factory=list)

    def find(self, input_file: str) -> Optional[ChapterState]:
        for c in self.chapters:
            if c.input_file == input_file:
                return c
        return None


def load_state(path: Path) -> RunState:
    if not path.is_file():
        return RunState()
    raw = json.loads(path.read_text())
    chapters = [ChapterState(**c) for c in raw.get("chapters", [])]
    return RunState(chapters=chapters)


def save_state(path: Path, state: RunState) -> None:
    payload = {"chapters": [asdict(c) for c in state.chapters]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


@dataclass
class RefineState:
    """Persistent state for a single ``marathon refine`` target."""

    target_folder: str
    iterations_completed: int = 0
    current_iteration_idx: int = 0
    project_id: Optional[str] = None
    status: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    attempts: int = 0
    output_path: Optional[str] = None
    note: Optional[str] = None


def load_refine_state(path: Path) -> Optional[RefineState]:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text())
    return RefineState(**raw)


def save_refine_state(path: Path, state: RefineState) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    tmp.replace(path)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def compute_duration_seconds(
    started_at: Optional[str], completed_at: Optional[str]
) -> Optional[float]:
    if not started_at or not completed_at:
        return None
    return (
        datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)
    ).total_seconds()


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"

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


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

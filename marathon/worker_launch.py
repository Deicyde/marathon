"""Crash-safe trampoline for conductor-managed worker processes.

The conductor writes a durable ``status=launching`` record before spawning this
module.  Running in the worker's newly detached session, the trampoline records
its own PID/PGID atomically *before* it execs the potentially billed Marathon
command.  If that marker update fails, the command is never started.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


EXIT_MARKER_ERROR = 70
EXIT_EXEC_ERROR = 71
ACK_TIMEOUT_SECONDS = 120.0


def _publish_identity(marker: Path) -> None:
    data = json.loads(marker.read_text())
    if not isinstance(data, dict) or data.get("status") != "launching":
        raise ValueError("worker launch marker is missing or not in launching state")
    data["pid"] = os.getpid()
    data["pgid"] = os.getpgrp() if os.name != "nt" else os.getpid()
    # Phase 1: publish identity, but do not start the billed command.  The
    # conductor must durably change this to ``running`` as its ACK first.
    data["status"] = "ready"
    tmp = marker.with_name(f"{marker.name}.worker-{os.getpid()}")
    with tmp.open("w") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, marker)


def _wait_for_ack(marker: Path, pid: int) -> bool:
    deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            data = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.01)
            continue
        if (
            isinstance(data, dict)
            and data.get("pid") == pid
            and data.get("status") == "running"
        ):
            return True
        time.sleep(0.01)
    return False


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or "--" not in args:
        print("usage: python -m marathon.worker_launch MARKER -- COMMAND ...",
              file=sys.stderr)
        return 2
    split = args.index("--")
    if split != 1 or split == len(args) - 1:
        print("worker_launch requires exactly one marker path before --",
              file=sys.stderr)
        return 2
    marker = Path(args[0])
    command = args[split + 1:]
    try:
        _publish_identity(marker)
    except Exception as exc:  # noqa: BLE001 — fail closed before billed work
        print(f"worker launch marker update failed: {exc}", file=sys.stderr)
        return EXIT_MARKER_ERROR
    if not _wait_for_ack(marker, os.getpid()):
        print("worker launch acknowledgement timed out", file=sys.stderr)
        return EXIT_MARKER_ERROR
    try:
        if os.name == "nt":  # os.execv does not preserve PID on Windows
            return subprocess.run(command, check=False).returncode
        os.execv(command[0], command)
    except OSError as exc:
        print(f"worker exec failed: {exc}", file=sys.stderr)
        return EXIT_EXEC_ERROR


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())

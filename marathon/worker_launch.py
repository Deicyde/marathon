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


def _durable_replace(tmp: Path, marker: Path) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file.restype = ctypes.c_int
        if not move_file(str(tmp), str(marker), 0x1 | 0x8):
            raise OSError(ctypes.get_last_error(), "MoveFileExW failed")
        return
    os.replace(tmp, marker)
    directory_fd = os.open(marker.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _enter_windows_kill_on_close_job() -> object | None:
    """Put this trampoline and descendants in a kill-on-close Windows Job."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle, 9, ctypes.byref(info), ctypes.sizeof(info)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(
        handle, kernel32.GetCurrentProcess()
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "AssignProcessToJobObject failed")
    return handle


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
    _durable_replace(tmp, marker)


def _wait_for_ack(marker: Path, pid: int) -> bool:
    deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            data = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.01)
            continue
        nonce = data.get("launch_nonce") if isinstance(data, dict) else None
        if (
            isinstance(data, dict)
            and data.get("pid") == pid
            and data.get("status") == "running"
            and isinstance(nonce, str)
            and nonce
        ):
            ack = marker.with_name(f"{marker.name}.ack-{pid}-{nonce}")
            try:
                ack_value = ack.read_text().strip()
            except OSError:
                time.sleep(0.01)
                continue
            if ack_value != nonce:
                time.sleep(0.01)
                continue
            try:
                ack.unlink()
            except OSError:
                pass
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
    windows_job = None
    try:
        windows_job = _enter_windows_kill_on_close_job()
        _publish_identity(marker)
    except Exception as exc:  # noqa: BLE001 — fail closed before billed work
        print(f"worker launch marker update failed: {exc}", file=sys.stderr)
        return EXIT_MARKER_ERROR
    if not _wait_for_ack(marker, os.getpid()):
        print("worker launch acknowledgement timed out", file=sys.stderr)
        return EXIT_MARKER_ERROR
    try:
        if os.name == "nt":  # os.execv does not preserve PID on Windows
            try:
                return subprocess.run(command, check=False).returncode
            finally:
                if windows_job is not None:
                    import ctypes
                    ctypes.windll.kernel32.CloseHandle(windows_job)
        os.execv(command[0], command)
    except OSError as exc:
        print(f"worker exec failed: {exc}", file=sys.stderr)
        return EXIT_EXEC_ERROR


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())

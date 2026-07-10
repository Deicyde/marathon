from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from marathon.worker_launch import EXIT_MARKER_ERROR


def test_worker_trampoline_publishes_identity_before_exec(tmp_path):
    marker = tmp_path / "refine.pid"
    observed = tmp_path / "observed.json"
    marker.write_text(json.dumps({"status": "launching", "pid": None, "pgid": None}))
    script = (
        "from pathlib import Path; import sys; "
        "Path(sys.argv[2]).write_text(Path(sys.argv[1]).read_text())"
    )
    command = [
        sys.executable, "-m", "marathon.worker_launch", str(marker), "--",
        sys.executable, "-c", script, str(marker), str(observed),
    ]
    kwargs = {"start_new_session": True} if os.name != "nt" else {}

    proc = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs
    )
    deadline = time.monotonic() + 5
    ready = None
    while time.monotonic() < deadline:
        try:
            candidate = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.01)
            continue
        if candidate.get("status") == "ready":
            ready = candidate
            break
        time.sleep(0.01)
    assert ready is not None
    assert ready["pid"] == proc.pid
    ready["status"] = "running"  # parent ACK: billed command may now execute
    marker.write_text(json.dumps(ready))
    stdout, stderr = proc.communicate(timeout=5)

    assert proc.returncode == 0, stderr
    payload = json.loads(observed.read_text())
    assert payload["status"] == "running"
    assert isinstance(payload["pid"], int) and payload["pid"] > 0
    assert isinstance(payload["pgid"], int) and payload["pgid"] > 0


def test_worker_trampoline_fails_closed_when_marker_is_missing(tmp_path):
    marker = tmp_path / "missing.pid"
    billed_side_effect = tmp_path / "must-not-exist"
    command = [
        sys.executable, "-m", "marathon.worker_launch", str(marker), "--",
        sys.executable, "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
        str(billed_side_effect),
    ]

    proc = subprocess.run(command, capture_output=True, text=True)

    assert proc.returncode == EXIT_MARKER_ERROR
    assert not billed_side_effect.exists()


def test_worker_trampoline_never_starts_command_before_parent_ack(tmp_path):
    marker = tmp_path / "refine.pid"
    billed_side_effect = tmp_path / "must-not-exist"
    marker.write_text(json.dumps({"status": "launching", "pid": None, "pgid": None}))
    command = [
        sys.executable, "-m", "marathon.worker_launch", str(marker), "--",
        sys.executable, "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
        str(billed_side_effect),
    ]
    kwargs = {"start_new_session": True} if os.name != "nt" else {}
    proc = subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if json.loads(marker.read_text()).get("status") == "ready":
                break
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.01)
    else:
        proc.kill()
        raise AssertionError("trampoline did not publish ready state")

    proc.terminate()
    proc.wait(timeout=5)

    assert not billed_side_effect.exists()

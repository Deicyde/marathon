"""marathon.notify — best-effort operator notifications.

The conductor and deck are long-running background processes whose failures
previously surfaced only in logs and dashboards the operator had to poll.
This module pushes the handful of events that genuinely need a human —
a parked recovery, a stalled issue, a failed ship — to the macOS
Notification Center.

Deliberately minimal and safe:

* never raises — a notification failure must not affect the pipeline;
* fire-and-forget with a short timeout — never blocks a scheduler tick;
* off on non-darwin hosts and when ``MARATHON_NOTIFY=0``.
"""

from __future__ import annotations

import os
import subprocess
import sys

__all__ = ["notify"]


def _enabled() -> bool:
    flag = os.environ.get("MARATHON_NOTIFY")
    if flag is not None:
        return flag.strip().lower() not in {"", "0", "false", "no", "off"}
    # Default: on for the operator's macOS host, but never inside a test run —
    # exercising failure paths under pytest must not spam real banners.
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False
    return sys.platform == "darwin"


def notify(title: str, message: str) -> bool:
    """Post one Notification Center banner. Returns whether it was sent.

    Text is passed through AppleScript string literals; escape the two
    characters that terminate them. Anything else (including newlines) is
    display-safe.
    """
    if not _enabled():
        return False

    def _escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{_escape(message[:400])}" '
        f'with title "{_escape(title[:100])}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return True
    except Exception:  # noqa: BLE001 — notifications must never break the caller
        return False

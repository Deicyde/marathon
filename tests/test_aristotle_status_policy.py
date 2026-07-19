"""Offline invariants for Aristotle terminal/retry classification."""

from aristotlelib import TaskStatus

from marathon.aristotle_runtime import (
    CONTINUABLE_STATUSES,
    RETRYABLE_STATUSES,
    TERMINAL_TASK_STATUSES,
)


def test_out_of_budget_continues_immediately_in_same_refine_process():
    """Out-of-budget is terminal but resumable through ``project.ask()``.

    The refine attempt loop only advances to another attempt for statuses in
    ``RETRYABLE_STATUSES``.  These three memberships jointly prevent an
    out-of-budget task from falling into the generic "unexpected status" exit
    and consuming a conductor-level retry before continuation.
    """
    assert TaskStatus.OUT_OF_BUDGET in TERMINAL_TASK_STATUSES
    assert TaskStatus.OUT_OF_BUDGET in RETRYABLE_STATUSES
    assert TaskStatus.OUT_OF_BUDGET in CONTINUABLE_STATUSES

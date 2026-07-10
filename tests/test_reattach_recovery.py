"""Saved Aristotle IDs are never discarded on transient reattach failures."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from aristotlelib import AristotleAPIError

import marathon.aristotle_runtime as runtime


def test_transient_project_reattach_raises_retry_signal(monkeypatch):
    class FakeProject:
        @classmethod
        async def from_id(cls, project_id):
            raise AristotleAPIError("temporary outage", status_code=503)

    monkeypatch.setattr(runtime, "Project", FakeProject)
    with pytest.raises(runtime.ReattachTransientError):
        __import__("asyncio").run(
            runtime.reattach_project_and_task("p-saved", "t-saved")
        )


def test_confirmed_missing_project_allows_absent_result(monkeypatch):
    class FakeProject:
        @classmethod
        async def from_id(cls, project_id):
            raise AristotleAPIError("not found", status_code=404)

    monkeypatch.setattr(runtime, "Project", FakeProject)
    assert __import__("asyncio").run(
        runtime.reattach_project_and_task("p-deleted", "t-deleted")
    ) == (None, None)


def test_transient_task_reattach_does_not_look_absent(monkeypatch):
    project = SimpleNamespace(project_id="p-saved")

    async def refresh_project():
        return None

    project.refresh = refresh_project

    class FakeProject:
        @classmethod
        async def from_id(cls, project_id):
            return project

    class FakeTask:
        @classmethod
        async def from_id(cls, task_id):
            raise AristotleAPIError("gateway timeout", status_code=502)

    monkeypatch.setattr(runtime, "Project", FakeProject)
    monkeypatch.setattr(runtime, "AgentTask", FakeTask)
    with pytest.raises(runtime.ReattachTransientError):
        __import__("asyncio").run(
            runtime.reattach_project_and_task("p-saved", "t-saved")
        )

"""Tests for the Scheduler MCP server tools."""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# Build a mock claude_agent_sdk with a working @tool decorator
def _make_sdk_mock() -> MagicMock:
    sdk = MagicMock()
    sdk.SdkMcpTool = MagicMock

    def _tool(name: str, description: str, schema: Any) -> Any:
        def decorator(fn: Any) -> Any:
            wrapper = MagicMock()
            wrapper.handler = fn
            wrapper.__name__ = fn.__name__
            return wrapper
        return decorator

    sdk.tool = _tool
    return sdk


# Force our mock with a working @tool decorator (must override, not setdefault,
# because another test module may have already set a plain MagicMock).
sys.modules["claude_agent_sdk"] = _make_sdk_mock()
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())

# Force reimport so the @tool decorator from our mock is applied
import importlib  # noqa: E402
sys.modules.pop("src.mcp.scheduler_server", None)

from src.mcp import scheduler_server  # noqa: E402

importlib.reload(scheduler_server)

# Access the underlying async handlers via .handler attribute
_list_tasks = scheduler_server.scheduler_list_tasks.handler
_get_task = scheduler_server.scheduler_get_task.handler
_add_task = scheduler_server.scheduler_add_task.handler
_update_task = scheduler_server.scheduler_update_task.handler
_remove_task = scheduler_server.scheduler_remove_task.handler
_pause_task = scheduler_server.scheduler_pause_task.handler
_resume_task = scheduler_server.scheduler_resume_task.handler
_trigger_task = scheduler_server.scheduler_trigger_task.handler
_reload = scheduler_server.scheduler_reload.handler


def _parse_text(result: dict[str, Any]) -> str:
    """Extract text content from a tool result."""
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


def _make_scheduler() -> MagicMock:
    return MagicMock()


@pytest.fixture(autouse=True)
def _reset_scheduler():
    scheduler_server._scheduler = None
    yield
    scheduler_server._scheduler = None


# ---------------------------------------------------------------------------
# TestSchedulerNotInitialized
# ---------------------------------------------------------------------------
class TestSchedulerNotInitialized:
    async def test_list_tasks_error(self):
        result = await _list_tasks({})
        assert _is_error(result)
        assert "Scheduler not initialized" in _parse_text(result)

    async def test_get_task_error(self):
        result = await _get_task({"task_id": "foo"})
        assert _is_error(result)
        assert "Scheduler not initialized" in _parse_text(result)

    async def test_add_task_error(self):
        result = await _add_task({"id": "t", "name": "T", "prompt": "P"})
        assert _is_error(result)
        assert "Scheduler not initialized" in _parse_text(result)

    async def test_update_task_error(self):
        result = await _update_task({"task_id": "foo"})
        assert _is_error(result)
        assert "Scheduler not initialized" in _parse_text(result)

    async def test_remove_task_error(self):
        result = await _remove_task({"task_id": "foo"})
        assert _is_error(result)
        assert "Scheduler not initialized" in _parse_text(result)

    async def test_pause_task_error(self):
        result = await _pause_task({"task_id": "foo"})
        assert _is_error(result)
        assert "Scheduler not initialized" in _parse_text(result)

    async def test_resume_task_error(self):
        result = await _resume_task({"task_id": "foo"})
        assert _is_error(result)
        assert "Scheduler not initialized" in _parse_text(result)

    async def test_trigger_task_error(self):
        result = await _trigger_task({"task_id": "foo"})
        assert _is_error(result)
        assert "Scheduler not initialized" in _parse_text(result)

    async def test_reload_error(self):
        result = await _reload({})
        assert _is_error(result)
        assert "Scheduler not initialized" in _parse_text(result)


# ---------------------------------------------------------------------------
# TestListTasks
# ---------------------------------------------------------------------------
class TestListTasks:
    async def test_returns_task_list(self):
        sched = _make_scheduler()
        sched.list_tasks.return_value = [
            {"id": "t1", "name": "Task 1", "enabled": True},
            {"id": "t2", "name": "Task 2", "enabled": False},
        ]
        scheduler_server._scheduler = sched

        result = await _list_tasks({})
        assert not _is_error(result)
        data = json.loads(_parse_text(result))
        assert len(data) == 2
        assert data[0]["id"] == "t1"


# ---------------------------------------------------------------------------
# TestGetTask
# ---------------------------------------------------------------------------
class TestGetTask:
    async def test_returns_task_details(self):
        sched = _make_scheduler()
        sched.get_task.return_value = {"id": "t1", "name": "Task 1", "prompt": "Do X"}
        scheduler_server._scheduler = sched

        result = await _get_task({"task_id": "t1"})
        assert not _is_error(result)
        data = json.loads(_parse_text(result))
        assert data["id"] == "t1"
        assert data["prompt"] == "Do X"

    async def test_not_found(self):
        sched = _make_scheduler()
        sched.get_task.return_value = None
        scheduler_server._scheduler = sched

        result = await _get_task({"task_id": "missing"})
        assert _is_error(result)
        assert "not found" in _parse_text(result).lower()


# ---------------------------------------------------------------------------
# TestAddTask
# ---------------------------------------------------------------------------
class TestAddTask:
    async def test_adds_task(self):
        sched = _make_scheduler()
        mock_task = MagicMock()
        mock_task.id = "new_task"
        sched.add_task.return_value = mock_task
        scheduler_server._scheduler = sched

        result = await _add_task({"id": "new_task", "name": "New", "prompt": "Do Y"})
        assert not _is_error(result)
        assert "new_task" in _parse_text(result)
        assert "created" in _parse_text(result).lower()


# ---------------------------------------------------------------------------
# TestUpdateTask
# ---------------------------------------------------------------------------
class TestUpdateTask:
    async def test_updates_task(self):
        sched = _make_scheduler()
        sched.update_task.return_value = True
        scheduler_server._scheduler = sched

        result = await _update_task({"task_id": "t1", "name": "Updated"})
        assert not _is_error(result)
        assert "updated" in _parse_text(result).lower()

    async def test_not_found(self):
        sched = _make_scheduler()
        sched.update_task.return_value = False
        scheduler_server._scheduler = sched

        result = await _update_task({"task_id": "missing", "name": "X"})
        assert _is_error(result)
        assert "not found" in _parse_text(result).lower()


# ---------------------------------------------------------------------------
# TestRemoveTask
# ---------------------------------------------------------------------------
class TestRemoveTask:
    async def test_removes_task(self):
        sched = _make_scheduler()
        sched.remove_task.return_value = True
        scheduler_server._scheduler = sched

        result = await _remove_task({"task_id": "t1"})
        assert not _is_error(result)
        assert "removed" in _parse_text(result).lower()

    async def test_not_found(self):
        sched = _make_scheduler()
        sched.remove_task.return_value = False
        scheduler_server._scheduler = sched

        result = await _remove_task({"task_id": "missing"})
        assert _is_error(result)
        assert "not found" in _parse_text(result).lower()


# ---------------------------------------------------------------------------
# TestPauseTask
# ---------------------------------------------------------------------------
class TestPauseTask:
    async def test_pauses_task(self):
        sched = _make_scheduler()
        sched.pause_task.return_value = True
        scheduler_server._scheduler = sched

        result = await _pause_task({"task_id": "t1"})
        assert not _is_error(result)
        assert "paused" in _parse_text(result).lower()


# ---------------------------------------------------------------------------
# TestResumeTask
# ---------------------------------------------------------------------------
class TestResumeTask:
    async def test_resumes_task(self):
        sched = _make_scheduler()
        sched.resume_task.return_value = True
        scheduler_server._scheduler = sched

        result = await _resume_task({"task_id": "t1"})
        assert not _is_error(result)
        assert "resumed" in _parse_text(result).lower()


# ---------------------------------------------------------------------------
# TestTriggerTask
# ---------------------------------------------------------------------------
class TestTriggerTask:
    async def test_triggers_task(self):
        sched = _make_scheduler()
        sched.trigger_task = AsyncMock(return_value=True)
        scheduler_server._scheduler = sched

        result = await _trigger_task({"task_id": "t1"})
        assert not _is_error(result)
        assert "triggered" in _parse_text(result).lower()


# ---------------------------------------------------------------------------
# TestReload
# ---------------------------------------------------------------------------
class TestReload:
    async def test_reloads_tasks(self):
        sched = _make_scheduler()
        sched.reload_tasks.return_value = 5
        scheduler_server._scheduler = sched

        result = await _reload({})
        assert not _is_error(result)
        assert "5" in _parse_text(result)
        assert "reloaded" in _parse_text(result).lower()

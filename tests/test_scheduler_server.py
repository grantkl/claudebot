"""Tests for the Scheduler MCP server tools."""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
            wrapper.input_schema = schema
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

        with patch.object(scheduler_server, "_requester", return_value="U1"):
            result = await _add_task({"id": "new_task", "name": "New", "prompt": "Do Y"})
        assert not _is_error(result)
        assert sched.add_task.call_args.args[0]["created_by"] == "U1"
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


# ---------------------------------------------------------------------------
# Ownership enforcement (real TaskScheduler, requester from the session)
# ---------------------------------------------------------------------------
import yaml  # noqa: E402

from src.scheduler import TaskScheduler  # noqa: E402


def _real_scheduler(tmp_path, tasks):
    cfg = MagicMock()
    cfg.scheduler_concurrency = 3
    cfg.scheduler_timezone = "US/Pacific"
    cfg.superuser_ids = {"U_SUPER"}
    cfg.authorized_user_ids = {"U_SUPER", "U_AUTH", "U_AUTH2"}
    manager = AsyncMock()
    manager.registered_mcp_server_names = {
        "sonos", "homekit", "gmail", "calendar", "scheduler", "stocks",
        "web_search", "memory", "credit_card", "shopping_list",
    }
    tasks_file = tmp_path / "tasks.yaml"
    tasks_file.write_text(yaml.dump({"tasks": tasks}))
    return TaskScheduler(cfg, manager, "xoxb", str(tasks_file), str(tmp_path / "state.json"))


def _task(task_id, created_by=None, servers=None):
    t = {
        "id": task_id, "name": task_id, "prompt": "p",
        "interval_seconds": 300, "mcp_servers": servers or ["sonos"],
    }
    if created_by is not None:
        t["created_by"] = created_by
    return t


def _as_user(user_id):
    return patch.object(scheduler_server, "_requester", return_value=user_id, create=True)


class TestOwnership:
    async def test_add_task_owner_comes_from_requester_not_args(self, tmp_path):
        sched = _real_scheduler(tmp_path, [])
        scheduler_server._scheduler = sched
        with _as_user("U_AUTH"):
            result = await _add_task({
                "id": "mine", "name": "Mine", "prompt": "p",
                "mcp_servers": ["sonos"], "created_by": "U_SUPER",
            })
        assert not _is_error(result), _parse_text(result)
        assert sched._tasks["mine"].created_by == "U_AUTH"

    async def test_add_task_spoofed_superuser_cannot_unlock_gmail(self, tmp_path):
        sched = _real_scheduler(tmp_path, [])
        scheduler_server._scheduler = sched
        with _as_user("U_AUTH"):
            result = await _add_task({
                "id": "evil", "name": "Evil", "prompt": "read email",
                "mcp_servers": ["gmail"], "created_by": "U_SUPER",
            })
        assert _is_error(result)
        assert "evil" not in sched._tasks

    async def test_add_task_without_requester_is_refused(self, tmp_path):
        sched = _real_scheduler(tmp_path, [])
        scheduler_server._scheduler = sched
        with _as_user(None):
            result = await _add_task({"id": "x", "name": "X", "prompt": "p"})
        assert _is_error(result)
        assert "x" not in sched._tasks

    async def test_add_task_cannot_overwrite_existing_task(self, tmp_path):
        sched = _real_scheduler(tmp_path, [_task("digest", created_by="U_SUPER", servers=["gmail"])])
        scheduler_server._scheduler = sched
        with _as_user("U_AUTH"):
            result = await _add_task({"id": "digest", "name": "Hijack", "prompt": "p"})
        assert _is_error(result)
        assert sched._tasks["digest"].created_by == "U_SUPER"
        assert sched._tasks["digest"].name == "digest"

    @pytest.mark.parametrize("owner", ["U_SUPER", "U_AUTH2", None])
    async def test_non_owner_cannot_manage_task(self, tmp_path, owner):
        sched = _real_scheduler(tmp_path, [_task("t", created_by=owner)])
        scheduler_server._scheduler = sched
        with _as_user("U_AUTH"), patch.object(sched, "_execute_with_semaphore", AsyncMock()) as run:
            results = [
                await _update_task({"task_id": "t", "prompt": "pwned"}),
                await _pause_task({"task_id": "t"}),
                await _resume_task({"task_id": "t"}),
                await _trigger_task({"task_id": "t"}),
                await _remove_task({"task_id": "t"}),
            ]
        assert all(_is_error(r) for r in results), [_parse_text(r) for r in results]
        assert "t" in sched._tasks
        assert sched._tasks["t"].prompt == "p"
        assert "t" not in sched._state or not sched._state["t"].paused
        run.assert_not_called()

    async def test_owner_can_manage_own_task(self, tmp_path):
        sched = _real_scheduler(tmp_path, [_task("t", created_by="U_AUTH")])
        scheduler_server._scheduler = sched
        with _as_user("U_AUTH"):
            assert not _is_error(await _update_task({"task_id": "t", "prompt": "new"}))
            assert sched._tasks["t"].prompt == "new"
            assert not _is_error(await _pause_task({"task_id": "t"}))
            assert not _is_error(await _resume_task({"task_id": "t"}))
            assert not _is_error(await _remove_task({"task_id": "t"}))
        assert "t" not in sched._tasks

    @pytest.mark.parametrize("owner", ["U_AUTH", None])
    async def test_superuser_can_manage_any_task(self, tmp_path, owner):
        sched = _real_scheduler(tmp_path, [_task("t", created_by=owner)])
        scheduler_server._scheduler = sched
        with _as_user("U_SUPER"):
            assert not _is_error(await _pause_task({"task_id": "t"}))
            assert not _is_error(await _remove_task({"task_id": "t"}))
        assert "t" not in sched._tasks

    async def test_update_cannot_change_owner_or_id(self, tmp_path):
        sched = _real_scheduler(tmp_path, [_task("t", created_by="U_AUTH")])
        scheduler_server._scheduler = sched
        with _as_user("U_AUTH"):
            r1 = await _update_task({"task_id": "t", "created_by": "U_SUPER"})
            r2 = await _update_task({"task_id": "t", "id": "other"})
        assert _is_error(r1)
        assert _is_error(r2)
        assert sched._tasks["t"].created_by == "U_AUTH"
        assert sched._tasks["t"].id == "t"

    async def test_update_revalidates_mcp_servers_against_owner_tier(self, tmp_path):
        sched = _real_scheduler(tmp_path, [_task("t", created_by="U_AUTH")])
        scheduler_server._scheduler = sched
        with _as_user("U_AUTH"):
            result = await _update_task({"task_id": "t", "mcp_servers": ["gmail"]})
        assert _is_error(result)
        assert sched._tasks["t"].mcp_servers == ["sonos"]

    async def test_superuser_update_still_validated_against_owner_tier(self, tmp_path):
        # A superuser editing an authorized user's task must not grant it
        # servers its owner's tier can't use (it runs as the owner).
        sched = _real_scheduler(tmp_path, [_task("t", created_by="U_AUTH")])
        scheduler_server._scheduler = sched
        with _as_user("U_SUPER"):
            result = await _update_task({"task_id": "t", "mcp_servers": ["gmail"]})
        assert _is_error(result)
        assert sched._tasks["t"].mcp_servers == ["sonos"]

    def test_tool_schemas_do_not_accept_created_by(self):
        for t in (scheduler_server.scheduler_add_task, scheduler_server.scheduler_update_task):
            assert "created_by" not in t.input_schema["properties"]
            assert "created_by" not in t.input_schema.get("required", [])

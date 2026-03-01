"""Tests for src.scheduler module."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import yaml

# Mock external modules before importing scheduler
sys.modules.setdefault("claude_agent_sdk", MagicMock())
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())

from src.scheduler import NOTHING_TO_REPORT, TaskDefinition, TaskScheduler, TaskState  # noqa: E402


def _make_config():
    cfg = MagicMock()
    cfg.scheduler_concurrency = 3
    cfg.scheduler_timezone = "US/Pacific"
    cfg.superuser_ids = {"U_SUPER"}
    return cfg


def _make_tasks_yaml(tmp_path, tasks):
    """Write tasks to a YAML file and return the path."""
    tasks_file = tmp_path / "tasks.yaml"
    tasks_file.write_text(yaml.dump({"tasks": tasks}))
    return str(tasks_file)


def _sample_task_data():
    return {
        "id": "test_task",
        "name": "Test Task",
        "prompt": "Do something",
        "interval_seconds": 300,
        "mcp_servers": ["gmail"],
        "output": "dm",
        "model": "sonnet",
        "enabled": True,
    }


def _make_scheduler(tmp_path, tasks=None):
    """Create a scheduler with optional tasks written to a YAML file."""
    config = _make_config()
    claude_manager = AsyncMock()
    state_file = str(tmp_path / "state.json")
    if tasks is not None:
        tasks_file = _make_tasks_yaml(tmp_path, tasks)
    else:
        tasks_file = str(tmp_path / "nonexistent.yaml")
    return TaskScheduler(config, claude_manager, "xoxb-test", tasks_file, state_file)


# ---------------------------------------------------------------------------
# TestTaskLoading
# ---------------------------------------------------------------------------
class TestTaskLoading:
    def test_loads_tasks_from_yaml(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        assert "test_task" in scheduler._tasks
        task = scheduler._tasks["test_task"]
        assert task.name == "Test Task"
        assert task.prompt == "Do something"
        assert task.interval_seconds == 300

    def test_missing_yaml_file(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=None)
        assert scheduler._tasks == {}

    def test_empty_yaml(self, tmp_path):
        tasks_file = tmp_path / "tasks.yaml"
        tasks_file.write_text(yaml.dump({"other_key": "value"}))
        config = _make_config()
        claude_manager = AsyncMock()
        state_file = str(tmp_path / "state.json")
        scheduler = TaskScheduler(config, claude_manager, "xoxb-test", str(tasks_file), state_file)
        assert scheduler._tasks == {}


# ---------------------------------------------------------------------------
# TestNextRunComputation
# ---------------------------------------------------------------------------
class TestNextRunComputation:
    def test_cron_next_run(self, tmp_path):
        tasks = [{**_sample_task_data(), "cron": "0 7 * * *", "interval_seconds": None}]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        task = scheduler._tasks["test_task"]
        state = TaskState()
        next_run = scheduler._compute_next_run(task, state)
        assert next_run is not None
        assert next_run > time.time() - 1  # should be in the future (or very close)

    def test_interval_first_run(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        task = scheduler._tasks["test_task"]
        state = TaskState()
        next_run = scheduler._compute_next_run(task, state)
        assert next_run == 0.0

    def test_interval_subsequent_run(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        task = scheduler._tasks["test_task"]
        last_run_str = "2026-03-01T10:00:00-0800"
        state = TaskState(last_run_time=last_run_str)
        next_run = scheduler._compute_next_run(task, state)
        last_dt = datetime.fromisoformat(last_run_str)
        expected = last_dt.timestamp() + 300
        assert next_run == expected

    def test_no_schedule(self, tmp_path):
        task_data = _sample_task_data()
        task_data["interval_seconds"] = None
        task_data.pop("interval_seconds", None)
        tasks = [{
            "id": "no_sched",
            "name": "No Schedule",
            "prompt": "Nothing",
        }]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        task = scheduler._tasks["no_sched"]
        state = TaskState()
        next_run = scheduler._compute_next_run(task, state)
        assert next_run is None


# ---------------------------------------------------------------------------
# TestTaskExecution
# ---------------------------------------------------------------------------
class TestTaskExecution:
    async def test_successful_execution_sends_dm(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(return_value="Summary of results")

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value = mock_client_instance

            task = scheduler._tasks["test_task"]
            await scheduler._execute_task(task)

            mock_client_instance.chat_postMessage.assert_called_once()
            call_kwargs = mock_client_instance.chat_postMessage.call_args.kwargs
            assert call_kwargs["channel"] == "U_SUPER"
            assert "Summary of results" in call_kwargs["text"]

    async def test_nothing_to_report_suppresses_dm(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(
            return_value=f"All clear. {NOTHING_TO_REPORT}"
        )

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value = mock_client_instance

            task = scheduler._tasks["test_task"]
            await scheduler._execute_task(task)

            mock_client_instance.chat_postMessage.assert_not_called()

    async def test_silent_output_suppresses_dm(self, tmp_path):
        task_data = _sample_task_data()
        task_data["output"] = "silent"
        tasks = [task_data]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(return_value="Some results")

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value = mock_client_instance

            task = scheduler._tasks["test_task"]
            await scheduler._execute_task(task)

            mock_client_instance.chat_postMessage.assert_not_called()

    async def test_session_lifecycle(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(return_value=NOTHING_TO_REPORT)
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient"):
            task = scheduler._tasks["test_task"]
            await scheduler._execute_task(task)

        scheduler._claude_manager.remove_session.assert_called_once()
        thread_ts = scheduler._claude_manager.remove_session.call_args[0][0]
        assert thread_ts.startswith("scheduler-test_task-")

    async def test_scheduler_stripped_from_mcp_servers(self, tmp_path):
        task_data = _sample_task_data()
        task_data["mcp_servers"] = ["gmail", "scheduler"]
        tasks = [task_data]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(return_value=NOTHING_TO_REPORT)
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient"):
            task = scheduler._tasks["test_task"]
            await scheduler._execute_task(task)

        call_kwargs = scheduler._claude_manager.send_message.call_args.kwargs
        assert call_kwargs["mcp_server_names"] == {"gmail"}

    async def test_superuser_privileges(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(return_value=NOTHING_TO_REPORT)
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient"):
            task = scheduler._tasks["test_task"]
            await scheduler._execute_task(task)

        call_kwargs = scheduler._claude_manager.send_message.call_args.kwargs
        assert call_kwargs["authorized"] is True
        assert call_kwargs["superuser"] is True


# ---------------------------------------------------------------------------
# TestErrorHandling
# ---------------------------------------------------------------------------
class TestErrorHandling:
    async def test_failure_increments_counter(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient"):
            task = scheduler._tasks["test_task"]
            await scheduler._execute_task(task)

        state = scheduler._state["test_task"]
        assert state.consecutive_failures == 1

    async def test_circuit_breaker_pauses_after_five(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value = mock_client_instance

            task = scheduler._tasks["test_task"]
            for _ in range(5):
                await scheduler._execute_task(task)

        state = scheduler._state["test_task"]
        assert state.paused is True
        assert state.consecutive_failures == 5
        # Notification DM should have been sent on the 5th failure
        assert mock_client_instance.chat_postMessage.call_count >= 1
        last_call_text = mock_client_instance.chat_postMessage.call_args.kwargs["text"]
        assert "paused" in last_call_text.lower()

    async def test_session_removed_on_failure(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient"):
            task = scheduler._tasks["test_task"]
            await scheduler._execute_task(task)

        scheduler._claude_manager.remove_session.assert_called_once()


# ---------------------------------------------------------------------------
# TestTaskManagement
# ---------------------------------------------------------------------------
class TestTaskManagement:
    def test_add_task(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=[])
        new_task = scheduler.add_task(_sample_task_data())
        assert new_task.id == "test_task"
        assert "test_task" in scheduler._tasks

    def test_remove_task(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=[_sample_task_data()])
        assert scheduler.remove_task("test_task") is True
        assert "test_task" not in scheduler._tasks

    def test_remove_nonexistent_task_returns_false(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=[])
        assert scheduler.remove_task("nonexistent") is False

    def test_update_task(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=[_sample_task_data()])
        result = scheduler.update_task("test_task", {"name": "Updated Name"})
        assert result is True
        assert scheduler._tasks["test_task"].name == "Updated Name"

    def test_pause_task(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=[_sample_task_data()])
        assert scheduler.pause_task("test_task") is True
        state = scheduler._state["test_task"]
        assert state.paused is True

    def test_resume_task_resets_failures(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=[_sample_task_data()])
        scheduler._state["test_task"] = TaskState(paused=True, consecutive_failures=3)
        assert scheduler.resume_task("test_task") is True
        state = scheduler._state["test_task"]
        assert state.paused is False
        assert state.consecutive_failures == 0

    def test_list_tasks(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=[_sample_task_data()])
        result = scheduler.list_tasks()
        assert len(result) == 1
        assert result[0]["id"] == "test_task"
        assert result[0]["name"] == "Test Task"


# ---------------------------------------------------------------------------
# TestStatePersistence
# ---------------------------------------------------------------------------
class TestStatePersistence:
    def test_save_and_load_state(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)

        # Modify state
        scheduler._state["test_task"] = TaskState(
            last_run_time="2026-03-01T10:00:00-0800",
            consecutive_failures=2,
            paused=True,
        )
        scheduler._save_state()

        # Create new scheduler that loads the same state file
        config = _make_config()
        claude_manager = AsyncMock()
        tasks_file = _make_tasks_yaml(tmp_path, tasks)
        state_file = str(tmp_path / "state.json")
        scheduler2 = TaskScheduler(config, claude_manager, "xoxb-test", tasks_file, state_file)

        state = scheduler2._state["test_task"]
        assert state.last_run_time == "2026-03-01T10:00:00-0800"
        assert state.consecutive_failures == 2
        assert state.paused is True

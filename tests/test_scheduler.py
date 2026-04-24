"""Tests for src.scheduler module."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import yaml

# Mock external modules before importing scheduler
sys.modules.setdefault("claude_agent_sdk", MagicMock())
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())

# Mock slack_sdk so that patch("slack_sdk.web.async_client.AsyncWebClient") works
# without requiring aiohttp to be installed.
_mock_slack_sdk = MagicMock()
sys.modules.setdefault("slack_sdk", _mock_slack_sdk)
sys.modules.setdefault("slack_sdk.web", _mock_slack_sdk.web)
sys.modules.setdefault("slack_sdk.web.async_client", _mock_slack_sdk.web.async_client)

from src.scheduler import NOTHING_TO_REPORT, TaskDefinition, TaskScheduler, TaskState  # noqa: E402


def _mock_result(text):
    """Create a mock SendMessageResult."""
    r = MagicMock()
    r.text = text
    return r


def _make_config(**overrides):
    cfg = MagicMock()
    cfg.scheduler_concurrency = 3
    cfg.scheduler_timezone = "US/Pacific"
    cfg.superuser_ids = overrides.get("superuser_ids", {"U_SUPER"})
    cfg.authorized_user_ids = overrides.get("authorized_user_ids", set())
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


def _make_scheduler(tmp_path, tasks=None, **config_overrides):
    """Create a scheduler with optional tasks written to a YAML file."""
    config = _make_config(**config_overrides)
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
        # Stateless tasks schedule for the next future cron match
        assert next_run > time.time()

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
        last_run_str = "2026-03-01T10:00:00-08:00"
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
        scheduler._claude_manager.send_message = AsyncMock(return_value=_mock_result("Summary of results"))

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
        scheduler._claude_manager.send_message = AsyncMock(return_value=_mock_result("Some results"))

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value = mock_client_instance

            task = scheduler._tasks["test_task"]
            await scheduler._execute_task(task)

            mock_client_instance.chat_postMessage.assert_not_called()

    async def test_session_lifecycle(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(return_value=_mock_result(NOTHING_TO_REPORT))
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
        scheduler._claude_manager.send_message = AsyncMock(return_value=_mock_result(NOTHING_TO_REPORT))
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient"):
            task = scheduler._tasks["test_task"]
            await scheduler._execute_task(task)

        call_kwargs = scheduler._claude_manager.send_message.call_args.kwargs
        assert call_kwargs["mcp_server_names"] == {"gmail"}

    async def test_superuser_privileges(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(return_value=_mock_result(NOTHING_TO_REPORT))
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
            last_run_time="2026-03-01T10:00:00-08:00",
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
        assert state.last_run_time == "2026-03-01T10:00:00-08:00"
        assert state.consecutive_failures == 2
        assert state.paused is True


# ---------------------------------------------------------------------------
# TestTaskOwnership
# ---------------------------------------------------------------------------
class TestTaskOwnership:
    def test_created_by_loads_from_yaml(self, tmp_path):
        task_data = {**_sample_task_data(), "created_by": "U_OWNER"}
        scheduler = _make_scheduler(tmp_path, tasks=[task_data])
        assert scheduler._tasks["test_task"].created_by == "U_OWNER"

    def test_created_by_saves_to_yaml(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=[], superuser_ids={"U_OWNER"})
        task_data = {**_sample_task_data(), "created_by": "U_OWNER"}
        scheduler.add_task(task_data)
        with open(scheduler._tasks_file) as f:
            saved = yaml.safe_load(f)
        assert saved["tasks"][0]["created_by"] == "U_OWNER"

    def test_created_by_none_not_written_to_yaml(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=[])
        task_data = _sample_task_data()
        # Ensure no created_by key
        task_data.pop("created_by", None)
        scheduler.add_task(task_data)
        with open(scheduler._tasks_file) as f:
            saved = yaml.safe_load(f)
        assert "created_by" not in saved["tasks"][0]

    async def test_send_dm_targets_owner(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, tasks=[])
        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value = mock_client_instance
            await scheduler._send_dm("test message", "Test Task", user_id="UOWNER")
        mock_client_instance.chat_postMessage.assert_called_once()
        call_kwargs = mock_client_instance.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "UOWNER"

    async def test_send_dm_broadcasts_without_owner(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path, tasks=[], superuser_ids={"U_S1", "U_S2"},
        )
        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value = mock_client_instance
            await scheduler._send_dm("test message", "Test Task")
        assert mock_client_instance.chat_postMessage.call_count == 2
        channels = {
            c.kwargs["channel"]
            for c in mock_client_instance.chat_postMessage.call_args_list
        }
        assert channels == {"U_S1", "U_S2"}

    async def test_execute_task_superuser_creator(self, tmp_path):
        task_data = {**_sample_task_data(), "created_by": "U_SUPER"}
        scheduler = _make_scheduler(
            tmp_path, tasks=[task_data], superuser_ids={"U_SUPER"},
        )
        scheduler._claude_manager.send_message = AsyncMock(return_value=_mock_result(NOTHING_TO_REPORT))
        scheduler._claude_manager.remove_session = AsyncMock()
        with patch("slack_sdk.web.async_client.AsyncWebClient"):
            await scheduler._execute_task(scheduler._tasks["test_task"])
        call_kwargs = scheduler._claude_manager.send_message.call_args.kwargs
        assert call_kwargs["superuser"] is True
        assert call_kwargs["authorized"] is True

    async def test_execute_task_authorized_creator(self, tmp_path):
        task_data = {**_sample_task_data(), "created_by": "U_AUTH", "mcp_servers": ["sonos"]}
        scheduler = _make_scheduler(
            tmp_path, tasks=[task_data],
            superuser_ids={"U_SUPER"}, authorized_user_ids={"U_AUTH"},
        )
        scheduler._claude_manager.send_message = AsyncMock(return_value=_mock_result(NOTHING_TO_REPORT))
        scheduler._claude_manager.remove_session = AsyncMock()
        with patch("slack_sdk.web.async_client.AsyncWebClient"):
            await scheduler._execute_task(scheduler._tasks["test_task"])
        call_kwargs = scheduler._claude_manager.send_message.call_args.kwargs
        assert call_kwargs["authorized"] is True
        assert call_kwargs["superuser"] is False

    async def test_execute_task_legacy_no_creator(self, tmp_path):
        task_data = _sample_task_data()
        task_data.pop("created_by", None)
        scheduler = _make_scheduler(tmp_path, tasks=[task_data])
        scheduler._claude_manager.send_message = AsyncMock(return_value=_mock_result(NOTHING_TO_REPORT))
        scheduler._claude_manager.remove_session = AsyncMock()
        with patch("slack_sdk.web.async_client.AsyncWebClient"):
            await scheduler._execute_task(scheduler._tasks["test_task"])
        call_kwargs = scheduler._claude_manager.send_message.call_args.kwargs
        assert call_kwargs["superuser"] is True
        assert call_kwargs["authorized"] is True

    def test_validate_mcp_rejects_gmail_for_authorized(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path, tasks=[],
            superuser_ids={"U_SUPER"}, authorized_user_ids={"U_AUTH"},
        )
        with pytest.raises(ValueError, match="not allowed"):
            scheduler.add_task({
                "id": "bad_task",
                "name": "Bad Task",
                "prompt": "Do something",
                "mcp_servers": ["gmail"],
                "created_by": "U_AUTH",
            })

    def test_validate_mcp_allows_gmail_for_superuser(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path, tasks=[],
            superuser_ids={"U_SUPER"}, authorized_user_ids={"U_AUTH"},
        )
        task = scheduler.add_task({
            "id": "good_task",
            "name": "Good Task",
            "prompt": "Do something",
            "mcp_servers": ["gmail"],
            "created_by": "U_SUPER",
        })
        assert task.id == "good_task"
        assert task.created_by == "U_SUPER"

    async def test_circuit_breaker_dms_owner(self, tmp_path):
        task_data = {**_sample_task_data(), "created_by": "UOWNER"}
        scheduler = _make_scheduler(tmp_path, tasks=[task_data])
        scheduler._claude_manager.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        scheduler._claude_manager.remove_session = AsyncMock()
        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client_instance = AsyncMock()
            MockClient.return_value = mock_client_instance
            task = scheduler._tasks["test_task"]
            for _ in range(5):
                await scheduler._execute_task(task)
        # Circuit breaker DM should go to the owner
        last_call_kwargs = mock_client_instance.chat_postMessage.call_args.kwargs
        assert last_call_kwargs["channel"] == "UOWNER"
        assert "paused" in last_call_kwargs["text"].lower()

    def test_list_tasks_includes_created_by(self, tmp_path):
        task_data = {**_sample_task_data(), "created_by": "U_OWNER"}
        scheduler = _make_scheduler(tmp_path, tasks=[task_data])
        result = scheduler.list_tasks()
        assert result[0]["created_by"] == "U_OWNER"

    def test_get_task_includes_created_by(self, tmp_path):
        task_data = {**_sample_task_data(), "created_by": "U_OWNER"}
        scheduler = _make_scheduler(tmp_path, tasks=[task_data])
        result = scheduler.get_task("test_task")
        assert result["created_by"] == "U_OWNER"


# ---------------------------------------------------------------------------
# TestParseRateLimitReset
# ---------------------------------------------------------------------------
class TestParseRateLimitReset:
    def test_parses_5am_utc(self):
        result = TaskScheduler._parse_rate_limit_reset(
            "You've hit your limit · resets 5am (UTC)"
        )
        assert result is not None
        assert result.tzinfo == timezone.utc
        assert result.hour == 5
        assert result.minute == 0

    def test_parses_11pm_utc(self):
        result = TaskScheduler._parse_rate_limit_reset(
            "You've hit your limit · resets 11pm (UTC)"
        )
        assert result is not None
        assert result.hour == 23

    def test_parses_12am_utc(self):
        result = TaskScheduler._parse_rate_limit_reset(
            "You've hit your limit · resets 12am (UTC)"
        )
        assert result is not None
        assert result.hour == 0

    def test_parses_12pm_utc(self):
        result = TaskScheduler._parse_rate_limit_reset(
            "You've hit your limit · resets 12pm (UTC)"
        )
        assert result is not None
        assert result.hour == 12

    def test_returns_none_for_unrelated_error(self):
        result = TaskScheduler._parse_rate_limit_reset("Connection timeout")
        assert result is None

    def test_returns_none_for_partial_match(self):
        result = TaskScheduler._parse_rate_limit_reset(
            "You've hit your limit but no reset time"
        )
        assert result is None

    def test_reset_time_in_past_rolls_to_tomorrow(self):
        # Force "now" to be after the reset hour so the result should be tomorrow
        now = datetime.now(timezone.utc)
        # Use 1 hour in the past
        past_hour = (now.hour - 1) % 24
        ampm = "am" if past_hour < 12 else "pm"
        display_hour = past_hour if past_hour <= 12 else past_hour - 12
        if display_hour == 0:
            display_hour = 12
        msg = f"resets {display_hour}{ampm} (UTC)"
        result = TaskScheduler._parse_rate_limit_reset(msg)
        assert result is not None
        assert result > now

    def test_reset_time_in_future_stays_today(self):
        now = datetime.now(timezone.utc)
        # Use 2 hours in the future
        future_hour = (now.hour + 2) % 24
        ampm = "am" if future_hour < 12 else "pm"
        display_hour = future_hour if future_hour <= 12 else future_hour - 12
        if display_hour == 0:
            display_hour = 12
        msg = f"resets {display_hour}{ampm} (UTC)"
        result = TaskScheduler._parse_rate_limit_reset(msg)
        assert result is not None
        # Should be today (within ~24 hours)
        assert result - now < timedelta(hours=24)


# ---------------------------------------------------------------------------
# TestRateLimitPauseResume
# ---------------------------------------------------------------------------
class TestRateLimitPauseResume:
    async def test_rate_limit_pauses_all_active_tasks(self, tmp_path):
        task1 = {**_sample_task_data(), "id": "task1", "name": "Task 1"}
        task2 = {**_sample_task_data(), "id": "task2", "name": "Task 2"}
        scheduler = _make_scheduler(tmp_path, tasks=[task1, task2])

        reset_utc = datetime.now(timezone.utc) + timedelta(hours=2)
        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await scheduler._pause_all_for_rate_limit(reset_utc)

        assert scheduler._state["task1"].paused is True
        assert scheduler._state["task1"].rate_limit_paused is True
        assert scheduler._state["task2"].paused is True
        assert scheduler._state["task2"].rate_limit_paused is True

        # Clean up the resume task
        if scheduler._rate_limit_resume_task:
            scheduler._rate_limit_resume_task.cancel()

    async def test_rate_limit_skips_manually_paused_tasks(self, tmp_path):
        task1 = {**_sample_task_data(), "id": "task1", "name": "Task 1"}
        task2 = {**_sample_task_data(), "id": "task2", "name": "Task 2"}
        scheduler = _make_scheduler(tmp_path, tasks=[task1, task2])

        # Manually pause task1
        scheduler.pause_task("task1")

        reset_utc = datetime.now(timezone.utc) + timedelta(hours=2)
        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await scheduler._pause_all_for_rate_limit(reset_utc)

        # task1 was already paused — should NOT be marked rate_limit_paused
        assert scheduler._state["task1"].paused is True
        assert scheduler._state["task1"].rate_limit_paused is False
        # task2 should be rate-limit paused
        assert scheduler._state["task2"].paused is True
        assert scheduler._state["task2"].rate_limit_paused is True

        if scheduler._rate_limit_resume_task:
            scheduler._rate_limit_resume_task.cancel()

    async def test_rate_limit_skips_disabled_tasks(self, tmp_path):
        task1 = {**_sample_task_data(), "id": "task1", "name": "Task 1", "enabled": False}
        task2 = {**_sample_task_data(), "id": "task2", "name": "Task 2"}
        scheduler = _make_scheduler(tmp_path, tasks=[task1, task2])

        reset_utc = datetime.now(timezone.utc) + timedelta(hours=2)
        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await scheduler._pause_all_for_rate_limit(reset_utc)

        # task1 is disabled — should not be touched
        assert scheduler._state.get("task1", TaskState()).rate_limit_paused is False
        # task2 should be paused
        assert scheduler._state["task2"].rate_limit_paused is True

        if scheduler._rate_limit_resume_task:
            scheduler._rate_limit_resume_task.cancel()

    async def test_rate_limit_sends_dm_alert(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)

        reset_utc = datetime.now(timezone.utc) + timedelta(hours=2)
        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value = mock_client
            await scheduler._pause_all_for_rate_limit(reset_utc)

        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "rate limit" in text.lower()

        if scheduler._rate_limit_resume_task:
            scheduler._rate_limit_resume_task.cancel()

    async def test_rate_limit_schedules_resume_task(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)

        reset_utc = datetime.now(timezone.utc) + timedelta(hours=2)
        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await scheduler._pause_all_for_rate_limit(reset_utc)

        assert scheduler._rate_limit_resume_task is not None
        assert not scheduler._rate_limit_resume_task.done()

        scheduler._rate_limit_resume_task.cancel()

    async def test_rate_limit_in_response_body_triggers_pause(self, tmp_path):
        """Claude CLI returns plan-limit text as a successful response, not an
        exception. The scheduler must still detect it and pause."""
        task1 = {**_sample_task_data(), "id": "task1", "name": "Task 1"}
        task2 = {**_sample_task_data(), "id": "task2", "name": "Task 2"}
        scheduler = _make_scheduler(tmp_path, tasks=[task1, task2])
        scheduler._claude_manager.send_message = AsyncMock(
            return_value=_mock_result("You've hit your limit · resets 5am (UTC)")
        )
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value = mock_client
            await scheduler._execute_task(scheduler._tasks["task1"])

        assert scheduler._state["task1"].rate_limit_paused is True
        assert scheduler._state["task2"].rate_limit_paused is True
        # The rate-limit text must NOT have been DM'd as a task result —
        # only the single rate-limit alert should go out.
        dm_texts = [c.kwargs["text"] for c in mock_client.chat_postMessage.call_args_list]
        assert not any("hit your limit" in t.lower() for t in dm_texts)
        assert any("rate limit" in t.lower() for t in dm_texts)

        if scheduler._rate_limit_resume_task:
            scheduler._rate_limit_resume_task.cancel()

    async def test_duplicate_pause_is_idempotent(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)

        reset_utc = datetime.now(timezone.utc) + timedelta(hours=2)
        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await scheduler._pause_all_for_rate_limit(reset_utc)
            first_task = scheduler._rate_limit_resume_task
            # Second call should be a no-op
            await scheduler._pause_all_for_rate_limit(reset_utc)
            assert scheduler._rate_limit_resume_task is first_task

        scheduler._rate_limit_resume_task.cancel()

    async def test_resume_after_clears_rate_limit_paused(self, tmp_path):
        task1 = {**_sample_task_data(), "id": "task1", "name": "Task 1"}
        task2 = {**_sample_task_data(), "id": "task2", "name": "Task 2"}
        scheduler = _make_scheduler(tmp_path, tasks=[task1, task2])

        # Manually set rate_limit_paused state
        scheduler._state["task1"] = TaskState(paused=True, rate_limit_paused=True)
        scheduler._state["task2"] = TaskState(paused=True, rate_limit_paused=True)

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            # Use 0 sleep to resume immediately
            await scheduler._rate_limit_resume_after(0)

        assert scheduler._state["task1"].paused is False
        assert scheduler._state["task1"].rate_limit_paused is False
        assert scheduler._state["task1"].consecutive_failures == 0
        assert scheduler._state["task2"].paused is False
        assert scheduler._state["task2"].rate_limit_paused is False

    async def test_resume_does_not_unset_manual_pause(self, tmp_path):
        task1 = {**_sample_task_data(), "id": "task1", "name": "Task 1"}
        task2 = {**_sample_task_data(), "id": "task2", "name": "Task 2"}
        scheduler = _make_scheduler(tmp_path, tasks=[task1, task2])

        # task1 manually paused, task2 rate-limit paused
        scheduler._state["task1"] = TaskState(paused=True, rate_limit_paused=False)
        scheduler._state["task2"] = TaskState(paused=True, rate_limit_paused=True)

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await scheduler._rate_limit_resume_after(0)

        # task1 should still be paused (manual)
        assert scheduler._state["task1"].paused is True
        # task2 should be resumed
        assert scheduler._state["task2"].paused is False

    async def test_resume_sends_dm(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._state["test_task"] = TaskState(paused=True, rate_limit_paused=True)

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value = mock_client
            await scheduler._rate_limit_resume_after(0)

        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "resumed" in text.lower()

    async def test_manual_resume_clears_rate_limit_paused(self, tmp_path):
        """resume_task() must clear rate_limit_paused so the pending auto-resume
        timer doesn't later spuriously announce the task as resumed."""
        task1 = {**_sample_task_data(), "id": "task1", "name": "Task 1"}
        scheduler = _make_scheduler(tmp_path, tasks=[task1])

        # Simulate rate-limit paused state
        scheduler._state["task1"] = TaskState(paused=True, rate_limit_paused=True)

        scheduler.resume_task("task1")

        assert scheduler._state["task1"].paused is False
        assert scheduler._state["task1"].rate_limit_paused is False

    async def test_resume_after_does_not_announce_already_resumed(self, tmp_path):
        """If the user manually resumed a rate-limit-paused task before the
        auto-resume fires, the auto-resume must NOT announce it as 'resumed' —
        it's already running. The stale flag should be cleared silently."""
        task1 = {**_sample_task_data(), "id": "task1", "name": "Task 1"}
        task2 = {**_sample_task_data(), "id": "task2", "name": "Task 2"}
        scheduler = _make_scheduler(tmp_path, tasks=[task1, task2])

        # task1: user already manually resumed (paused=False) but the flag
        # wasn't cleared — simulates the pre-fix bug scenario, or any path
        # that leaves the flag stale.
        scheduler._state["task1"] = TaskState(paused=False, rate_limit_paused=True)
        # task2: genuinely still rate-limit paused
        scheduler._state["task2"] = TaskState(paused=True, rate_limit_paused=True)

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value = mock_client
            await scheduler._rate_limit_resume_after(0)

        # task1's stale flag should be cleared
        assert scheduler._state["task1"].rate_limit_paused is False
        assert scheduler._state["task1"].paused is False
        # task2 should have been resumed
        assert scheduler._state["task2"].paused is False
        assert scheduler._state["task2"].rate_limit_paused is False

        # DM should only mention task2 — not task1
        mock_client.chat_postMessage.assert_called_once()
        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "Task 2" in text
        assert "Task 1" not in text
        assert "1 tasks" in text  # count reflects only genuinely resumed

    async def test_resume_after_no_dm_if_all_already_resumed(self, tmp_path):
        """If every rate-limit-paused task was already manually resumed,
        no DM should be sent at all."""
        task1 = {**_sample_task_data(), "id": "task1", "name": "Task 1"}
        scheduler = _make_scheduler(tmp_path, tasks=[task1])

        # Stale flag only; user already resumed
        scheduler._state["task1"] = TaskState(paused=False, rate_limit_paused=True)

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value = mock_client
            await scheduler._rate_limit_resume_after(0)

        assert scheduler._state["task1"].rate_limit_paused is False
        mock_client.chat_postMessage.assert_not_called()


# ---------------------------------------------------------------------------
# TestRateLimitInExecuteTask
# ---------------------------------------------------------------------------
class TestRateLimitInExecuteTask:
    async def test_rate_limit_error_pauses_all_tasks(self, tmp_path):
        task1 = {**_sample_task_data(), "id": "task1", "name": "Task 1"}
        task2 = {**_sample_task_data(), "id": "task2", "name": "Task 2"}
        scheduler = _make_scheduler(tmp_path, tasks=[task1, task2])
        scheduler._claude_manager.send_message = AsyncMock(
            side_effect=RuntimeError("You've hit your limit · resets 5am (UTC)")
        )
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await scheduler._execute_task(scheduler._tasks["task1"])

        # Both tasks should be rate-limit paused
        assert scheduler._state["task1"].rate_limit_paused is True
        assert scheduler._state["task2"].rate_limit_paused is True

        if scheduler._rate_limit_resume_task:
            scheduler._rate_limit_resume_task.cancel()

    async def test_rate_limit_error_does_not_increment_failures(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(
            side_effect=RuntimeError("You've hit your limit · resets 5am (UTC)")
        )
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await scheduler._execute_task(scheduler._tasks["test_task"])

        state = scheduler._state["test_task"]
        assert state.consecutive_failures == 0

        if scheduler._rate_limit_resume_task:
            scheduler._rate_limit_resume_task.cancel()

    async def test_unparseable_rate_limit_uses_fallback(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(
            side_effect=RuntimeError("You've hit your limit")
        )
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await scheduler._execute_task(scheduler._tasks["test_task"])

        # Should still be rate-limit paused (fallback 1-hour)
        assert scheduler._state["test_task"].rate_limit_paused is True
        assert scheduler._rate_limit_resume_task is not None

        scheduler._rate_limit_resume_task.cancel()

    async def test_non_rate_limit_error_increments_failures(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._claude_manager.send_message = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )
        scheduler._claude_manager.remove_session = AsyncMock()

        with patch("slack_sdk.web.async_client.AsyncWebClient"):
            await scheduler._execute_task(scheduler._tasks["test_task"])

        state = scheduler._state["test_task"]
        assert state.consecutive_failures == 1
        assert state.rate_limit_paused is False


# ---------------------------------------------------------------------------
# TestRateLimitStatePersistence
# ---------------------------------------------------------------------------
class TestRateLimitStatePersistence:
    def test_rate_limit_paused_persisted_in_state(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._state["test_task"] = TaskState(
            paused=True, rate_limit_paused=True
        )
        scheduler._save_state()

        # Load state in a new scheduler
        config = _make_config()
        tasks_file = _make_tasks_yaml(tmp_path, tasks)
        state_file = str(tmp_path / "state.json")
        scheduler2 = TaskScheduler(
            config, AsyncMock(), "xoxb-test", tasks_file, state_file
        )

        state = scheduler2._state["test_task"]
        assert state.paused is True
        assert state.rate_limit_paused is True

    def test_list_tasks_includes_rate_limit_paused(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._state["test_task"] = TaskState(
            paused=True, rate_limit_paused=True
        )
        result = scheduler.list_tasks()
        assert result[0]["rate_limit_paused"] is True

    def test_get_task_includes_rate_limit_paused(self, tmp_path):
        tasks = [_sample_task_data()]
        scheduler = _make_scheduler(tmp_path, tasks=tasks)
        scheduler._state["test_task"] = TaskState(
            paused=True, rate_limit_paused=True
        )
        result = scheduler.get_task("test_task")
        assert result["rate_limit_paused"] is True

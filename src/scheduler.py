"""Autonomous task scheduler for running background tasks on cron/interval schedules."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml
from croniter import croniter

if TYPE_CHECKING:
    from .claude_client import ClaudeManager
    from .config import Config

logger = logging.getLogger(__name__)

NOTHING_TO_REPORT = "NOTHING_TO_REPORT"

SUPERUSER_MCP_SERVERS = {"sonos", "homekit", "gmail", "flights", "flight_watch", "seats_aero", "playwright", "stocks", "web_search"}
AUTHORIZED_MCP_SERVERS = {"sonos", "homekit", "flights", "flight_watch", "scheduler", "stocks", "web_search"}


@dataclass
class TaskDefinition:
    id: str
    name: str
    prompt: str
    cron: str | None = None
    interval_seconds: int | None = None
    mcp_servers: list[str] = field(default_factory=list)
    output: str = "dm"  # "dm" or "silent"
    model: str = "sonnet"
    enabled: bool = True
    run_once: bool = False
    created_by: str | None = None


@dataclass
class TaskState:
    last_run_time: str | None = None  # ISO 8601
    consecutive_failures: int = 0
    paused: bool = False


class TaskScheduler:
    def __init__(
        self,
        config: Config,
        claude_manager: ClaudeManager,
        slack_token: str,
        tasks_file: str,
        state_file: str,
    ) -> None:
        self._config = config
        self._claude_manager = claude_manager
        self._slack_token = slack_token
        self._tasks_file = tasks_file
        self._state_file = state_file
        self._tasks: dict[str, TaskDefinition] = {}
        self._state: dict[str, TaskState] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(config.scheduler_concurrency)
        self._in_flight: set[asyncio.Task[None]] = set()
        self._executing_ids: set[str] = set()
        self._tz = ZoneInfo(config.scheduler_timezone)

        self._load_tasks()
        self._load_state()

    def validate_task_mcp_servers(self, task_data: dict[str, Any]) -> None:
        """Validate that the task creator is allowed to use the requested MCP servers."""
        created_by = task_data.get("created_by")
        if created_by is None:
            return
        if created_by in self._config.superuser_ids:
            allowed = SUPERUSER_MCP_SERVERS
        elif created_by in self._config.authorized_user_ids:
            allowed = AUTHORIZED_MCP_SERVERS
        else:
            allowed: set[str] = set()
        disallowed = [s for s in task_data.get("mcp_servers", []) if s not in allowed]
        if disallowed:
            raise ValueError(
                f"User {created_by} is not allowed to use MCP servers: {', '.join(disallowed)}"
            )

    def _load_tasks(self) -> None:
        """Load task definitions from YAML file."""
        path = Path(self._tasks_file)
        if not path.exists():
            logger.warning("Tasks file not found: %s", self._tasks_file)
            return
        with open(path) as f:
            data = yaml.safe_load(f)
        if not data or "tasks" not in data:
            logger.warning("No tasks found in %s", self._tasks_file)
            return
        self._tasks = {}
        for task_data in data["tasks"]:
            task = TaskDefinition(
                id=task_data["id"],
                name=task_data["name"],
                prompt=task_data["prompt"],
                cron=task_data.get("cron"),
                interval_seconds=task_data.get("interval_seconds"),
                mcp_servers=task_data.get("mcp_servers", []),
                output=task_data.get("output", "dm"),
                model=task_data.get("model", "sonnet"),
                enabled=task_data.get("enabled", True),
                run_once=task_data.get("run_once", False),
                created_by=task_data.get("created_by"),
            )
            self._tasks[task.id] = task
        logger.info("Loaded %d tasks from %s", len(self._tasks), self._tasks_file)

    def _load_state(self) -> None:
        """Load task state from JSON file."""
        path = Path(self._state_file)
        if not path.exists():
            return
        try:
            with open(path) as f:
                data = json.load(f)
            for task_id, state_data in data.items():
                self._state[task_id] = TaskState(
                    last_run_time=state_data.get("last_run_time"),
                    consecutive_failures=state_data.get("consecutive_failures", 0),
                    paused=state_data.get("paused", False),
                )
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to load scheduler state from %s", self._state_file)

    def _save_state(self) -> None:
        """Save task state to JSON file."""
        path = Path(self._state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for task_id, state in self._state.items():
            data[task_id] = {
                "last_run_time": state.last_run_time,
                "consecutive_failures": state.consecutive_failures,
                "paused": state.paused,
            }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    async def start(self) -> None:
        """Start the scheduler background loop."""
        self._loop_task = asyncio.create_task(self._scheduler_loop())
        logger.info("Task scheduler started with %d tasks", len(self._tasks))

    async def stop(self) -> None:
        """Stop the scheduler and wait for in-flight tasks."""
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)
        self._save_state()

    async def _scheduler_loop(self) -> None:
        """Main loop that checks for tasks to execute every 15 seconds."""
        while True:
            try:
                await asyncio.sleep(15)
                now = time.time()
                for task_id, task in self._tasks.items():
                    if not task.enabled:
                        continue
                    state = self._state.get(task_id, TaskState())
                    if state.paused:
                        continue
                    if task_id in self._executing_ids:
                        continue
                    next_run = self._compute_next_run(task, state)
                    if next_run is not None and next_run <= now:
                        self._executing_ids.add(task_id)
                        t = asyncio.create_task(self._execute_with_semaphore(task))
                        self._in_flight.add(t)
                        t.add_done_callback(self._in_flight.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in scheduler loop")

    async def _execute_with_semaphore(self, task: TaskDefinition) -> None:
        """Execute a task with concurrency limiting."""
        async with self._semaphore:
            await self._execute_task(task)

    async def _execute_task(self, task: TaskDefinition) -> None:
        """Execute a single scheduled task."""
        task_id = task.id
        state = self._state.setdefault(task_id, TaskState())
        thread_ts = f"scheduler-{task_id}-{int(time.time())}"

        prompt = (
            f"{task.prompt}\n\n"
            f"If there is nothing meaningful to report, respond with exactly: {NOTHING_TO_REPORT}"
        )

        # Strip "scheduler" from MCP server names to prevent recursion
        mcp_server_names = {s for s in task.mcp_servers if s != "scheduler"}

        # Determine privileges based on task creator
        if task.created_by in self._config.superuser_ids:
            authorized, superuser = True, True
        elif task.created_by in self._config.authorized_user_ids:
            authorized, superuser = True, False
        elif task.created_by is None:
            # Legacy tasks (no creator) get full privileges for backward compat
            authorized, superuser = True, True
        else:
            authorized, superuser = False, False

        logger.info("Executing scheduled task: %s (%s)", task.name, task_id)

        try:
            response = await self._claude_manager.send_message(
                thread_ts,
                prompt,
                model=task.model,
                mcp_server_names=mcp_server_names,
                authorized=authorized,
                superuser=superuser,
            )

            state.last_run_time = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            state.consecutive_failures = 0

            # Send DM if response is actionable
            if task.output == "dm" and NOTHING_TO_REPORT not in response:
                await self._send_dm(response, task.name, user_id=task.created_by)

            # Auto-disable one-time tasks after successful execution
            if task.run_once:
                task.enabled = False
                self._save_tasks()
                logger.info("One-time task %s auto-disabled after execution", task_id)

            logger.info("Task %s completed successfully", task_id)
        except Exception:
            logger.exception("Task %s failed", task_id)
            state.consecutive_failures += 1
            if state.consecutive_failures >= 5:
                state.paused = True
                logger.warning(
                    "Task %s paused after %d consecutive failures",
                    task_id,
                    state.consecutive_failures,
                )
                await self._send_dm(
                    f"Scheduled task *{task.name}* has been automatically paused "
                    f"after {state.consecutive_failures} consecutive failures.",
                    "Scheduler Alert",
                    user_id=task.created_by,
                )
        finally:
            self._executing_ids.discard(task_id)
            await self._claude_manager.remove_session(thread_ts)
            self._save_state()

    async def _send_dm(self, text: str, task_name: str, user_id: str | None = None) -> None:
        """Send a DM to a specific user, or broadcast to all superusers if user_id is None."""
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=self._slack_token)
        message = f"*[{task_name}]*\n{text}"
        recipients = [user_id] if user_id else list(self._config.superuser_ids)
        for recipient in recipients:
            try:
                await client.chat_postMessage(channel=recipient, text=message)
            except Exception:
                logger.exception("Failed to send DM to %s", recipient)

    def _compute_next_run(
        self, task: TaskDefinition, state: TaskState
    ) -> float | None:
        """Compute the next run time for a task."""
        from datetime import datetime

        if task.cron:
            if state.last_run_time:
                base = datetime.fromisoformat(state.last_run_time)
                cron = croniter(task.cron, base.astimezone(self._tz))
                next_dt = cron.get_next(datetime)
                return next_dt.timestamp()
            else:
                # Task has never run — schedule for the next future match.
                now = datetime.now(tz=self._tz)
                cron = croniter(task.cron, now)
                next_dt = cron.get_next(datetime)
                return next_dt.timestamp()
        elif task.interval_seconds:
            if state.last_run_time:
                last = datetime.fromisoformat(state.last_run_time)
                return last.timestamp() + task.interval_seconds
            else:
                # First run: run immediately
                return 0.0
        return None

    # --- Task management methods (called by MCP tools) ---

    def list_tasks(self) -> list[dict[str, Any]]:
        """List all tasks with their status and next run time."""
        from datetime import datetime

        result = []
        for task_id, task in self._tasks.items():
            state = self._state.get(task_id, TaskState())
            next_run = self._compute_next_run(task, state)
            next_run_str = (
                datetime.fromtimestamp(next_run, tz=self._tz).isoformat()
                if next_run is not None
                else None
            )
            result.append({
                "id": task.id,
                "name": task.name,
                "enabled": task.enabled,
                "paused": state.paused,
                "schedule": task.cron or f"every {task.interval_seconds}s",
                "next_run": next_run_str,
                "last_run": state.last_run_time,
                "consecutive_failures": state.consecutive_failures,
                "model": task.model,
                "mcp_servers": task.mcp_servers,
                "output": task.output,
                "run_once": task.run_once,
                "created_by": task.created_by,
            })
        return result

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get full task details including prompt."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        state = self._state.get(task_id, TaskState())
        return {
            "id": task.id,
            "name": task.name,
            "prompt": task.prompt,
            "cron": task.cron,
            "interval_seconds": task.interval_seconds,
            "mcp_servers": task.mcp_servers,
            "output": task.output,
            "model": task.model,
            "enabled": task.enabled,
            "run_once": task.run_once,
            "created_by": task.created_by,
            "paused": state.paused,
            "last_run": state.last_run_time,
            "consecutive_failures": state.consecutive_failures,
        }

    def add_task(self, task_data: dict[str, Any]) -> TaskDefinition:
        """Add a new task and save to YAML."""
        self.validate_task_mcp_servers(task_data)
        task = TaskDefinition(
            id=task_data["id"],
            name=task_data["name"],
            prompt=task_data["prompt"],
            cron=task_data.get("cron"),
            interval_seconds=task_data.get("interval_seconds"),
            mcp_servers=task_data.get("mcp_servers", []),
            output=task_data.get("output", "dm"),
            model=task_data.get("model", "sonnet"),
            enabled=task_data.get("enabled", True),
            run_once=task_data.get("run_once", False),
            created_by=task_data.get("created_by"),
        )
        self._tasks[task.id] = task
        self._save_tasks()
        return task

    def remove_task(self, task_id: str) -> bool:
        """Remove a task."""
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        self._state.pop(task_id, None)
        self._save_tasks()
        self._save_state()
        return True

    def update_task(self, task_id: str, updates: dict[str, Any]) -> bool:
        """Update an existing task's fields."""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        for key, value in updates.items():
            if hasattr(task, key) and key != "id":
                setattr(task, key, value)
        self._save_tasks()
        return True

    def pause_task(self, task_id: str) -> bool:
        """Pause a task."""
        if task_id not in self._tasks:
            return False
        state = self._state.setdefault(task_id, TaskState())
        state.paused = True
        self._save_state()
        return True

    def resume_task(self, task_id: str) -> bool:
        """Resume a paused task."""
        if task_id not in self._tasks:
            return False
        state = self._state.setdefault(task_id, TaskState())
        state.paused = False
        state.consecutive_failures = 0
        self._save_state()
        return True

    async def trigger_task(self, task_id: str) -> bool:
        """Immediately trigger a task execution."""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        t = asyncio.create_task(self._execute_with_semaphore(task))
        self._in_flight.add(t)
        t.add_done_callback(self._in_flight.discard)
        return True

    def reload_tasks(self) -> int:
        """Reload tasks from YAML file. Returns count of loaded tasks."""
        self._load_tasks()
        return len(self._tasks)

    def _save_tasks(self) -> None:
        """Save current tasks back to YAML file."""
        path = Path(self._tasks_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tasks_data = []
        for task in self._tasks.values():
            task_dict: dict[str, Any] = {
                "id": task.id,
                "name": task.name,
                "prompt": task.prompt,
            }
            if task.cron:
                task_dict["cron"] = task.cron
            if task.interval_seconds:
                task_dict["interval_seconds"] = task.interval_seconds
            task_dict["mcp_servers"] = task.mcp_servers
            task_dict["output"] = task.output
            task_dict["model"] = task.model
            task_dict["enabled"] = task.enabled
            if task.run_once:
                task_dict["run_once"] = True
            if task.created_by:
                task_dict["created_by"] = task.created_by
            tasks_data.append(task_dict)
        with open(path, "w") as f:
            yaml.dump({"tasks": tasks_data}, f, default_flow_style=False, sort_keys=False)

"""Scheduler MCP server tools for managing autonomous tasks."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import SdkMcpTool, tool

if TYPE_CHECKING:
    from ..scheduler import TaskScheduler

logger = logging.getLogger(__name__)

# Lazy binding — set from main.py after scheduler is created
_scheduler: TaskScheduler | None = None


def set_scheduler(scheduler: TaskScheduler) -> None:
    """Set the scheduler instance for MCP tools to use."""
    global _scheduler
    _scheduler = scheduler


def _get_scheduler() -> TaskScheduler:
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized")
    return _scheduler


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


@tool(
    "scheduler_list_tasks",
    "List all scheduled tasks with their status, schedule, and next run time.",
    {"type": "object", "properties": {}},
)
async def scheduler_list_tasks(args: dict[str, Any]) -> dict[str, Any]:
    try:
        scheduler = _get_scheduler()
        tasks = scheduler.list_tasks()
        return _text(json.dumps(tasks, indent=2))
    except Exception as e:
        return _error(f"Failed to list tasks: {e}")


@tool(
    "scheduler_get_task",
    "Get full details of a scheduled task including its prompt.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task ID to retrieve."},
        },
        "required": ["task_id"],
    },
)
async def scheduler_get_task(args: dict[str, Any]) -> dict[str, Any]:
    try:
        scheduler = _get_scheduler()
        task = scheduler.get_task(args["task_id"])
        if task is None:
            return _error(f"Task not found: {args['task_id']}")
        return _text(json.dumps(task, indent=2))
    except Exception as e:
        return _error(f"Failed to get task: {e}")


@tool(
    "scheduler_add_task",
    "Create a new scheduled task.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Unique task ID (e.g., 'hourly_email_check')."},
            "name": {"type": "string", "description": "Human-readable task name."},
            "prompt": {"type": "string", "description": "The prompt to send to Claude when the task runs."},
            "cron": {"type": "string", "description": "Cron expression (e.g., '0 7 * * *'). Mutually exclusive with interval_seconds."},
            "interval_seconds": {"type": "integer", "description": "Polling interval in seconds. Mutually exclusive with cron."},
            "mcp_servers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "MCP servers to make available (e.g., ['gmail', 'homekit']).",
            },
            "output": {"type": "string", "enum": ["dm", "silent"], "description": "Output mode: 'dm' sends results as Slack DM, 'silent' discards output."},
            "model": {"type": "string", "description": "Claude model to use (default: sonnet)."},
        },
        "required": ["id", "name", "prompt"],
    },
)
async def scheduler_add_task(args: dict[str, Any]) -> dict[str, Any]:
    try:
        scheduler = _get_scheduler()
        task = scheduler.add_task(args)
        return _text(f"Task '{task.id}' created successfully.")
    except Exception as e:
        return _error(f"Failed to add task: {e}")


@tool(
    "scheduler_update_task",
    "Update fields of an existing scheduled task.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task ID to update."},
            "name": {"type": "string", "description": "New task name."},
            "prompt": {"type": "string", "description": "New prompt."},
            "cron": {"type": "string", "description": "New cron expression."},
            "interval_seconds": {"type": "integer", "description": "New interval in seconds."},
            "mcp_servers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New MCP server list.",
            },
            "output": {"type": "string", "enum": ["dm", "silent"]},
            "model": {"type": "string"},
            "enabled": {"type": "boolean"},
        },
        "required": ["task_id"],
    },
)
async def scheduler_update_task(args: dict[str, Any]) -> dict[str, Any]:
    try:
        scheduler = _get_scheduler()
        task_id = args.pop("task_id")
        if not scheduler.update_task(task_id, args):
            return _error(f"Task not found: {task_id}")
        return _text(f"Task '{task_id}' updated successfully.")
    except Exception as e:
        return _error(f"Failed to update task: {e}")


@tool(
    "scheduler_remove_task",
    "Remove a scheduled task.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task ID to remove."},
        },
        "required": ["task_id"],
    },
)
async def scheduler_remove_task(args: dict[str, Any]) -> dict[str, Any]:
    try:
        scheduler = _get_scheduler()
        if not scheduler.remove_task(args["task_id"]):
            return _error(f"Task not found: {args['task_id']}")
        return _text(f"Task '{args['task_id']}' removed successfully.")
    except Exception as e:
        return _error(f"Failed to remove task: {e}")


@tool(
    "scheduler_pause_task",
    "Pause a scheduled task so it won't run until resumed.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task ID to pause."},
        },
        "required": ["task_id"],
    },
)
async def scheduler_pause_task(args: dict[str, Any]) -> dict[str, Any]:
    try:
        scheduler = _get_scheduler()
        if not scheduler.pause_task(args["task_id"]):
            return _error(f"Task not found: {args['task_id']}")
        return _text(f"Task '{args['task_id']}' paused.")
    except Exception as e:
        return _error(f"Failed to pause task: {e}")


@tool(
    "scheduler_resume_task",
    "Resume a paused scheduled task and reset its failure counter.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task ID to resume."},
        },
        "required": ["task_id"],
    },
)
async def scheduler_resume_task(args: dict[str, Any]) -> dict[str, Any]:
    try:
        scheduler = _get_scheduler()
        if not scheduler.resume_task(args["task_id"]):
            return _error(f"Task not found: {args['task_id']}")
        return _text(f"Task '{args['task_id']}' resumed.")
    except Exception as e:
        return _error(f"Failed to resume task: {e}")


@tool(
    "scheduler_trigger_task",
    "Immediately execute a scheduled task regardless of its schedule.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task ID to trigger."},
        },
        "required": ["task_id"],
    },
)
async def scheduler_trigger_task(args: dict[str, Any]) -> dict[str, Any]:
    try:
        scheduler = _get_scheduler()
        if not await scheduler.trigger_task(args["task_id"]):
            return _error(f"Task not found: {args['task_id']}")
        return _text(f"Task '{args['task_id']}' triggered for immediate execution.")
    except Exception as e:
        return _error(f"Failed to trigger task: {e}")


@tool(
    "scheduler_reload",
    "Reload task definitions from the YAML configuration file.",
    {"type": "object", "properties": {}},
)
async def scheduler_reload(args: dict[str, Any]) -> dict[str, Any]:
    try:
        scheduler = _get_scheduler()
        count = scheduler.reload_tasks()
        return _text(f"Reloaded {count} tasks from configuration.")
    except Exception as e:
        return _error(f"Failed to reload tasks: {e}")


SCHEDULER_TOOLS: list[SdkMcpTool] = [
    scheduler_list_tasks,
    scheduler_get_task,
    scheduler_add_task,
    scheduler_update_task,
    scheduler_remove_task,
    scheduler_pause_task,
    scheduler_resume_task,
    scheduler_trigger_task,
    scheduler_reload,
]

"""Deploy MCP server tools for triggering container rebuilds."""

from __future__ import annotations

import asyncio  # noqa: F401  (referenced by tests via patch)
import json
import logging
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

TRIGGER_FILE = "/app/data/deploy.trigger"


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


@tool(
    "trigger_deploy",
    "Queue a rebuild and deploy of the bot container (fire-and-forget). Writes a trigger file and returns immediately; the deploy runs asynchronously and the user is DM'd with the outcome when it completes. WARNING: the bot will go offline briefly during the rebuild.",
    {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Optional reason for the deploy.",
            },
        },
    },
)
async def trigger_deploy(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from src.claude_client import _current_user_id

        reason = args.get("reason", "")
        trigger_data: dict[str, Any] = {
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        user_id = _current_user_id.get()
        if user_id is not None:
            trigger_data["user_id"] = user_id

        with open(TRIGGER_FILE, "w") as f:
            json.dump(trigger_data, f)

        return _text(
            "Deploy queued. You'll get a DM with the result when it completes."
        )
    except Exception as e:
        return _error(f"Failed to trigger deploy: {e}")


DEPLOY_TOOLS: list[SdkMcpTool] = [
    trigger_deploy,
]

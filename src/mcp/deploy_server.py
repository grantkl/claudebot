"""Deploy MCP server tools for triggering container rebuilds."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

TRIGGER_FILE = "/app/data/deploy.trigger"
RESULT_FILE = "/app/data/deploy.result"
POLL_INTERVAL = 2
POLL_TIMEOUT = 600


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


@tool(
    "trigger_deploy",
    "Trigger a rebuild and deploy of the bot container. WARNING: The bot will go offline briefly during the rebuild. A trigger file is written and the tool polls for a result file to confirm completion.",
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
        reason = args.get("reason", "")
        trigger_data = {
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        with open(TRIGGER_FILE, "w") as f:
            json.dump(trigger_data, f)

        elapsed = 0
        while elapsed < POLL_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            if os.path.exists(RESULT_FILE):
                with open(RESULT_FILE) as f:
                    content = f.read()
                # Clean up files
                for path in (TRIGGER_FILE, RESULT_FILE):
                    if os.path.exists(path):
                        os.remove(path)
                return _text(content)

        # Timeout — clean up trigger file
        if os.path.exists(TRIGGER_FILE):
            os.remove(TRIGGER_FILE)
        return _error("Deploy timeout: no result after {0}s.".format(POLL_TIMEOUT))
    except Exception as e:
        return _error(f"Failed to trigger deploy: {e}")


DEPLOY_TOOLS: list[SdkMcpTool] = [
    trigger_deploy,
]

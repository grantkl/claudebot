"""MCP server factory for ClaudeBot."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_agent_sdk import McpServerConfig


def build_mcp_servers() -> dict[str, McpServerConfig]:
    """Build all in-process MCP server configurations."""
    from claude_agent_sdk import create_sdk_mcp_server

    from .sonos_server import SONOS_TOOLS

    servers: dict[str, McpServerConfig] = {
        "sonos": create_sdk_mcp_server(name="sonos", version="1.0.0", tools=SONOS_TOOLS),
    }

    homeclaw_url = os.environ.get("HOMECLAW_MCP_URL")
    if homeclaw_url:
        servers["homekit"] = {"type": "http", "url": homeclaw_url}
    else:
        from .homekit_server import HOMEKIT_TOOLS

        servers["homekit"] = create_sdk_mcp_server(name="homekit", version="1.0.0", tools=HOMEKIT_TOOLS)

    gmail_creds = os.environ.get("GMAIL_CREDENTIALS_FILE")
    gmail_token = os.environ.get("GMAIL_TOKEN_FILE")
    if gmail_creds and gmail_token:
        from .gmail_server import GMAIL_TOOLS

        servers["gmail"] = create_sdk_mcp_server(name="gmail", version="1.0.0", tools=GMAIL_TOOLS)

    return servers

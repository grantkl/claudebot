"""MCP server factory for ClaudeBot."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_agent_sdk import McpSdkServerConfig


def build_mcp_servers() -> dict[str, McpSdkServerConfig]:
    """Build all in-process MCP server configurations."""
    from claude_agent_sdk import create_sdk_mcp_server

    from .homekit_server import HOMEKIT_TOOLS
    from .sonos_server import SONOS_TOOLS

    return {
        "sonos": create_sdk_mcp_server(name="sonos", version="1.0.0", tools=SONOS_TOOLS),
        "homekit": create_sdk_mcp_server(name="homekit", version="1.0.0", tools=HOMEKIT_TOOLS),
    }

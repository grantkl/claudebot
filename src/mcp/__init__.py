"""MCP server factory for ClaudeBot."""

from __future__ import annotations

import os
import subprocess as _subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_agent_sdk import McpServerConfig


def _resolve_playwright_path() -> bool:
    """Pre-install / verify the Playwright MCP package is available."""
    try:
        result = _subprocess.run(
            ["npx", "--yes", "@playwright/mcp@latest", "--help"],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except (FileNotFoundError, _subprocess.TimeoutExpired):
        return False


def _resolve_amadeus_path() -> str | None:
    """Resolve the path to the amadeus MCP server's stdio entry point."""
    try:
        env = {**os.environ, "NODE_PATH": "/usr/lib/node_modules"}
        result = _subprocess.run(
            ["node", "-e", "console.log(require.resolve('@privilegemendes/amadeus-mcp-server/dist/index.js'))"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, _subprocess.TimeoutExpired):
        pass
    return None


def build_mcp_servers() -> dict[str, McpServerConfig]:
    """Build all MCP server configurations."""
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

    scheduler_enabled = os.environ.get("SCHEDULER_ENABLED", "").lower() in ("1", "true", "yes")
    if scheduler_enabled:
        from .scheduler_server import SCHEDULER_TOOLS
        servers["scheduler"] = create_sdk_mcp_server(
            name="scheduler", version="1.0.0", tools=SCHEDULER_TOOLS
        )

    gmail_creds = os.environ.get("GMAIL_CREDENTIALS_FILE")
    gmail_token = os.environ.get("GMAIL_TOKEN_FILE")
    if gmail_creds and gmail_token:
        from .gmail_server import GMAIL_TOOLS

        servers["gmail"] = create_sdk_mcp_server(name="gmail", version="1.0.0", tools=GMAIL_TOOLS)

    flights_enabled = os.environ.get("FLIGHTS_ENABLED", "").lower() in ("1", "true", "yes")
    if flights_enabled:
        amadeus_path = _resolve_amadeus_path()
        if amadeus_path:
            servers["flights"] = {
                "type": "stdio",
                "command": "node",
                "args": [amadeus_path],
                "env": {
                    "AMADEUS_CLIENT_ID": os.environ.get("AMADEUS_CLIENT_ID", ""),
                    "AMADEUS_CLIENT_SECRET": os.environ.get("AMADEUS_CLIENT_SECRET", ""),
                },
            }

        from .flight_watch_server import FLIGHT_WATCH_TOOLS
        servers["flight_watch"] = create_sdk_mcp_server(
            name="flight_watch", version="1.0.0", tools=FLIGHT_WATCH_TOOLS
        )

    seats_aero_key = os.environ.get("SEATS_AERO_API_KEY", "")
    if seats_aero_key:
        from .seats_aero_server import SEATS_AERO_TOOLS
        servers["seats_aero"] = create_sdk_mcp_server(
            name="seats_aero", version="1.0.0", tools=SEATS_AERO_TOOLS
        )

    playwright_enabled = os.environ.get("PLAYWRIGHT_ENABLED", "").lower() in ("1", "true", "yes")
    if playwright_enabled:
        if _resolve_playwright_path():
            servers["playwright"] = {
                "type": "stdio",
                "command": "npx",
                "args": ["--yes", "@playwright/mcp@latest", "--headless", "--browser", "chromium"],
            }

    stocks_enabled = os.environ.get("STOCKS_ENABLED", "").lower() in ("1", "true", "yes")
    if stocks_enabled:
        from .stocks_server import STOCKS_TOOLS
        servers["stocks"] = create_sdk_mcp_server(name="stocks", version="1.0.0", tools=STOCKS_TOOLS)

    brave_api_key = os.environ.get("BRAVE_API_KEY", "")
    if brave_api_key:
        servers["web_search"] = {
            "type": "stdio",
            "command": "npx",
            "args": ["--yes", "@modelcontextprotocol/server-brave-search"],
            "env": {"BRAVE_API_KEY": brave_api_key},
        }

    return servers

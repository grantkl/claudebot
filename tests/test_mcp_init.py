"""Tests for src.mcp module (build_mcp_servers)."""

import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock SDK before importing source modules
sys.modules.setdefault("claude_agent_sdk", MagicMock())

# Mock dependent MCP server modules to avoid import-time side effects
sys.modules.setdefault("src.mcp.sonos_server", MagicMock())
sys.modules.setdefault("src.mcp.homekit_server", MagicMock())
sys.modules.setdefault("src.mcp.gmail_server", MagicMock())
sys.modules.setdefault("src.mcp.flight_watch_server", MagicMock())

from src.mcp import _resolve_amadeus_path, _resolve_playwright_path, build_mcp_servers  # noqa: E402


class TestBuildMcpServersFlights:
    @patch("src.mcp._resolve_amadeus_path", return_value="/usr/lib/node_modules/@privilegemendes/amadeus-mcp-server/dist/index.js")
    @patch.dict("os.environ", {"FLIGHTS_ENABLED": "true", "AMADEUS_CLIENT_ID": "test-id", "AMADEUS_CLIENT_SECRET": "test-secret"}, clear=False)
    def test_flights_enabled_with_amadeus_installed(self, mock_resolve):
        servers = build_mcp_servers()
        assert "flights" in servers
        assert servers["flights"]["type"] == "stdio"
        assert servers["flights"]["command"] == "node"
        assert servers["flights"]["args"] == ["/usr/lib/node_modules/@privilegemendes/amadeus-mcp-server/dist/index.js"]
        assert servers["flights"]["env"]["AMADEUS_CLIENT_ID"] == "test-id"
        assert servers["flights"]["env"]["AMADEUS_CLIENT_SECRET"] == "test-secret"

    @patch("src.mcp._resolve_amadeus_path", return_value="/some/path/index.js")
    @patch.dict("os.environ", {"FLIGHTS_ENABLED": "false"}, clear=False)
    def test_flights_disabled(self, mock_resolve):
        servers = build_mcp_servers()
        assert "flights" not in servers

    @patch("src.mcp._resolve_amadeus_path", return_value=None)
    @patch.dict("os.environ", {"FLIGHTS_ENABLED": "true"}, clear=False)
    def test_flights_enabled_but_amadeus_not_installed(self, mock_resolve):
        servers = build_mcp_servers()
        assert "flights" not in servers

    @patch.dict("os.environ", {}, clear=False)
    def test_flights_not_set_in_env(self):
        # Remove FLIGHTS_ENABLED if present
        import os
        os.environ.pop("FLIGHTS_ENABLED", None)
        servers = build_mcp_servers()
        assert "flights" not in servers


class TestBuildMcpServersFlightWatch:
    @patch("src.mcp._resolve_amadeus_path", return_value="/some/path/index.js")
    @patch.dict("os.environ", {"FLIGHTS_ENABLED": "true", "AMADEUS_CLIENT_ID": "id", "AMADEUS_CLIENT_SECRET": "secret"}, clear=False)
    def test_flight_watch_registered_when_flights_enabled(self, mock_resolve):
        servers = build_mcp_servers()
        assert "flight_watch" in servers

    @patch("src.mcp._resolve_amadeus_path", return_value="/some/path/index.js")
    @patch.dict("os.environ", {"FLIGHTS_ENABLED": "false"}, clear=False)
    def test_flight_watch_not_registered_when_flights_disabled(self, mock_resolve):
        servers = build_mcp_servers()
        assert "flight_watch" not in servers


class TestResolveAmadeusPath:
    @patch("src.mcp._subprocess.run")
    def test_returns_path_on_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="/path/to/index.js\n")
        result = _resolve_amadeus_path()
        assert result == "/path/to/index.js"

    @patch("src.mcp._subprocess.run")
    def test_returns_none_on_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = _resolve_amadeus_path()
        assert result is None

    @patch("src.mcp._subprocess.run", side_effect=FileNotFoundError)
    def test_returns_none_when_node_not_found(self, mock_run):
        result = _resolve_amadeus_path()
        assert result is None

    @patch("src.mcp._subprocess.run")
    def test_returns_none_on_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="node", timeout=10)
        result = _resolve_amadeus_path()
        assert result is None


class TestBuildMcpServersPlaywright:
    @patch("src.mcp._resolve_playwright_path", return_value="@playwright/mcp@latest")
    @patch.dict("os.environ", {"PLAYWRIGHT_ENABLED": "true"}, clear=False)
    def test_playwright_enabled_and_available(self, mock_resolve):
        servers = build_mcp_servers()
        assert "playwright" in servers
        assert servers["playwright"]["type"] == "stdio"
        assert servers["playwright"]["command"] == "npx"
        assert servers["playwright"]["args"] == ["--yes", "@playwright/mcp@latest", "--headless"]

    @patch.dict("os.environ", {"PLAYWRIGHT_ENABLED": "false"}, clear=False)
    def test_playwright_disabled(self):
        servers = build_mcp_servers()
        assert "playwright" not in servers

    @patch("src.mcp._resolve_playwright_path", return_value=None)
    @patch.dict("os.environ", {"PLAYWRIGHT_ENABLED": "true"}, clear=False)
    def test_playwright_npx_not_found(self, mock_resolve):
        servers = build_mcp_servers()
        assert "playwright" not in servers


class TestResolvePlaywrightPath:
    @patch("src.mcp._subprocess.run")
    def test_returns_path_on_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result = _resolve_playwright_path()
        assert result is True

    @patch("src.mcp._subprocess.run")
    def test_returns_none_on_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        result = _resolve_playwright_path()
        assert result is False

    @patch("src.mcp._subprocess.run", side_effect=FileNotFoundError)
    def test_returns_none_when_npx_not_found(self, mock_run):
        result = _resolve_playwright_path()
        assert result is False

    @patch("src.mcp._subprocess.run")
    def test_returns_none_on_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="npx", timeout=30)
        result = _resolve_playwright_path()
        assert result is False

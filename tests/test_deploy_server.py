"""Tests for the Deploy MCP server tools."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# Build a mock claude_agent_sdk with a working @tool decorator
def _make_sdk_mock() -> MagicMock:
    sdk = MagicMock()
    sdk.SdkMcpTool = MagicMock

    def _tool(name: str, description: str, schema: Any) -> Any:
        def decorator(fn: Any) -> Any:
            wrapper = MagicMock()
            wrapper.handler = fn
            wrapper.__name__ = fn.__name__
            return wrapper
        return decorator

    sdk.tool = _tool
    return sdk


sys.modules.setdefault("claude_agent_sdk", _make_sdk_mock())

from src.mcp import deploy_server
from src.mcp.deploy_server import DEPLOY_TOOLS

# Access the underlying async handler via .handler attribute
_trigger_deploy = deploy_server.trigger_deploy.handler


def _parse_text(result: dict[str, Any]) -> str:
    """Extract text content from a tool result."""
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


def _read_trigger(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# trigger_deploy — fire-and-forget behavior
# ---------------------------------------------------------------------------
class TestTriggerDeployFireAndForget:
    async def test_writes_trigger_file_with_reason_and_timestamp(
        self, tmp_path: Path
    ) -> None:
        """trigger_deploy should write a JSON trigger file with reason + timestamp."""
        trigger_path = tmp_path / "deploy.trigger"

        with patch.object(deploy_server, "TRIGGER_FILE", str(trigger_path)):
            result = await asyncio.wait_for(
                _trigger_deploy({"reason": "merge PR #123"}), timeout=2.0
            )

        assert not _is_error(result)
        assert trigger_path.exists()
        data = _read_trigger(trigger_path)
        assert data["reason"] == "merge PR #123"
        assert "timestamp" in data

    async def test_returns_immediately_no_polling(self, tmp_path: Path) -> None:
        """trigger_deploy must NOT poll for a result file. Should return quickly."""
        trigger_path = tmp_path / "deploy.trigger"

        with patch.object(deploy_server, "TRIGGER_FILE", str(trigger_path)):
            # asyncio.wait_for raises TimeoutError if the call doesn't return
            # within the timeout. Fire-and-forget should be near-instant.
            result = await asyncio.wait_for(
                _trigger_deploy({"reason": "fast"}), timeout=1.0
            )

        assert not _is_error(result)

    async def test_does_not_call_asyncio_sleep(self, tmp_path: Path) -> None:
        """Fire-and-forget should not poll, so asyncio.sleep should never be called."""
        trigger_path = tmp_path / "deploy.trigger"

        with (
            patch.object(deploy_server, "TRIGGER_FILE", str(trigger_path)),
            patch("src.mcp.deploy_server.asyncio.sleep") as mock_sleep,
        ):
            await asyncio.wait_for(
                _trigger_deploy({"reason": "no poll"}), timeout=2.0
            )

        mock_sleep.assert_not_called()

    async def test_return_text_indicates_queued(self, tmp_path: Path) -> None:
        """The success message should indicate the deploy was queued/started,
        not that it completed."""
        trigger_path = tmp_path / "deploy.trigger"

        with patch.object(deploy_server, "TRIGGER_FILE", str(trigger_path)):
            result = await asyncio.wait_for(
                _trigger_deploy({"reason": "test"}), timeout=2.0
            )

        assert not _is_error(result)
        text = _parse_text(result).lower()
        # Caller-facing confirmation language
        assert "queued" in text or "started" in text or "triggered" in text

    async def test_user_id_written_when_contextvar_set(self, tmp_path: Path) -> None:
        """When _current_user_id contextvar is set, it should be written to trigger file."""
        from src.claude_client import _current_user_id

        trigger_path = tmp_path / "deploy.trigger"

        token = _current_user_id.set("U123")
        try:
            with patch.object(deploy_server, "TRIGGER_FILE", str(trigger_path)):
                result = await asyncio.wait_for(
                    _trigger_deploy({"reason": "with user"}), timeout=2.0
                )
        finally:
            _current_user_id.reset(token)

        assert not _is_error(result)
        data = _read_trigger(trigger_path)
        assert data["user_id"] == "U123"

    async def test_user_id_omitted_when_contextvar_unset(self, tmp_path: Path) -> None:
        """When _current_user_id is None, the trigger file should NOT include user_id
        (or include it as null). Recommended: omit the key entirely."""
        from src.claude_client import _current_user_id

        trigger_path = tmp_path / "deploy.trigger"

        # Ensure contextvar is unset
        assert _current_user_id.get() is None

        with patch.object(deploy_server, "TRIGGER_FILE", str(trigger_path)):
            result = await asyncio.wait_for(
                _trigger_deploy({"reason": "no user"}), timeout=2.0
            )

        assert not _is_error(result)
        data = _read_trigger(trigger_path)
        # user_id key omitted, OR present-but-null. Either is acceptable.
        assert data.get("user_id") is None
        assert data["reason"] == "no user"
        assert "timestamp" in data

    async def test_default_reason(self, tmp_path: Path) -> None:
        """Calling without a reason should still succeed."""
        trigger_path = tmp_path / "deploy.trigger"

        with patch.object(deploy_server, "TRIGGER_FILE", str(trigger_path)):
            result = await asyncio.wait_for(_trigger_deploy({}), timeout=2.0)

        assert not _is_error(result)
        assert trigger_path.exists()

    async def test_error_when_write_fails(self, tmp_path: Path) -> None:
        """If writing the trigger file fails, return an error response."""
        trigger_path = tmp_path / "deploy.trigger"

        with (
            patch.object(deploy_server, "TRIGGER_FILE", str(trigger_path)),
            patch("builtins.open", side_effect=PermissionError("Access denied")),
        ):
            result = await _trigger_deploy({"reason": "boom"})

        assert _is_error(result)

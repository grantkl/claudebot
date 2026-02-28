"""Tests for src.claude_client module."""

import asyncio
import sys
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Build mock SDK module before importing claude_client
@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeAssistantMessage:
    content: list
    role: str = "assistant"


@dataclass
class _FakeResultMessage:
    stop_reason: str = "end_turn"


class _FakeClaudeAgentOptions:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeClaudeSDKClient:
    """A minimal async-capable stub for ClaudeSDKClient."""

    def __init__(self, options=None):
        self.options = options
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.query = AsyncMock()
        self._responses: list = []

    def set_responses(self, responses):
        self._responses = responses

    async def receive_response(self):
        for r in self._responses:
            yield r


_mock_sdk = MagicMock()
_mock_sdk.ClaudeSDKClient = _FakeClaudeSDKClient
_mock_sdk.ClaudeAgentOptions = _FakeClaudeAgentOptions
_mock_sdk.AssistantMessage = _FakeAssistantMessage
_mock_sdk.TextBlock = _FakeTextBlock
_mock_sdk.ResultMessage = _FakeResultMessage

# Temporarily inject mock SDK for import, then restore only the claude_agent_sdk
# entry so that src.claude_client remains cached with mock types while other test
# modules can import the real claude_agent_sdk.
_orig_sdk = sys.modules.get("claude_agent_sdk")
sys.modules["claude_agent_sdk"] = _mock_sdk

from src.claude_client import ClaudeManager, SessionEntry  # noqa: E402

if _orig_sdk is not None:
    sys.modules["claude_agent_sdk"] = _orig_sdk
else:
    del sys.modules["claude_agent_sdk"]


def _make_config(**overrides):
    cfg = MagicMock()
    cfg.claude_model = overrides.get("claude_model", "test-model")
    cfg.claude_system_prompt = overrides.get("claude_system_prompt", "test prompt")
    cfg.session_ttl_seconds = overrides.get("session_ttl_seconds", 3600)
    cfg.enable_mcp = overrides.get("enable_mcp", False)
    return cfg


class TestClaudeManager:
    @pytest.mark.asyncio
    async def test_session_created_on_first_message(self):
        config = _make_config()
        manager = ClaudeManager(config)
        # Patch internal ClaudeSDKClient creation
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            result = await manager.send_message("t1", "hello")
        assert result == "hi"
        assert "t1" in manager._sessions

    @pytest.mark.asyncio
    async def test_send_message_returns_collected_text(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[
                _FakeTextBlock(text="part1"),
                _FakeTextBlock(text="part2"),
            ]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            result = await manager.send_message("t1", "hello")
        assert result == "part1part2"

    @pytest.mark.asyncio
    async def test_session_reuse_for_same_thread(self):
        config = _make_config()
        manager = ClaudeManager(config)

        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="r1")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "msg1")

        # The session now exists; send_message should reuse it
        entry = manager._sessions["t1"]
        # Re-set responses on the existing client
        entry.client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="r2")]),
            _FakeResultMessage(),
        ])
        result = await manager.send_message("t1", "msg2")
        assert result == "r2"
        # Still the same client object
        assert manager._sessions["t1"].client is entry.client

    @pytest.mark.asyncio
    async def test_error_handling_removes_broken_session(self):
        config = _make_config()
        manager = ClaudeManager(config)

        fake_client = _FakeClaudeSDKClient()
        fake_client.query = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            with pytest.raises(RuntimeError, match="boom"):
                await manager.send_message("t1", "hello")

        assert "t1" not in manager._sessions

    @pytest.mark.asyncio
    async def test_cleanup_evicts_old_sessions(self):
        config = _make_config(session_ttl_seconds=10)
        manager = ClaudeManager(config)

        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="ok")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "hi")

        # Artificially age the session
        manager._sessions["t1"].last_accessed = time.time() - 20

        # Run the internal cleanup logic directly
        now = time.time()
        expired = [
            ts
            for ts, entry in manager._sessions.items()
            if now - entry.last_accessed > config.session_ttl_seconds
        ]
        for ts in expired:
            await manager._remove_session(ts)

        assert "t1" not in manager._sessions

    @pytest.mark.asyncio
    async def test_start_creates_cleanup_task(self):
        config = _make_config()
        manager = ClaudeManager(config)
        await manager.start()
        assert manager._cleanup_task is not None
        assert not manager._cleanup_task.done()
        # Clean up
        await manager.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_cleanup_and_removes_sessions(self):
        config = _make_config()
        manager = ClaudeManager(config)
        await manager.start()

        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="ok")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "hi")

        await manager.stop()
        assert manager._cleanup_task.cancelled() or manager._cleanup_task.done()
        assert len(manager._sessions) == 0

    @pytest.mark.asyncio
    async def test_model_param_used_for_new_session(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", model="opus")
        # Verify the options passed to ClaudeSDKClient used "opus"
        call_kwargs = mock_cls.call_args
        assert call_kwargs[1]["options"].model == "opus" or call_kwargs.kwargs["options"].model == "opus"

    @pytest.mark.asyncio
    async def test_model_param_ignored_for_existing_session(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="r1")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "msg1", model="opus")

        # Reuse session with different model - should keep original
        entry = manager._sessions["t1"]
        entry.client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="r2")]),
            _FakeResultMessage(),
        ])
        result = await manager.send_message("t1", "msg2", model="haiku")
        assert result == "r2"
        # Original client kept (no new ClaudeSDKClient created)
        assert manager._sessions["t1"].client is entry.client

    @pytest.mark.asyncio
    async def test_none_model_falls_back_to_config(self):
        config = _make_config(claude_model="sonnet")
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello")
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert options.model == "sonnet"

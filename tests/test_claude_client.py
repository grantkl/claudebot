"""Tests for src.claude_client module."""

import asyncio
import base64
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

# Force reimport so claude_client picks up the proper fake types even if another
# test module (e.g. test_slack_app) already imported it with a generic MagicMock.
sys.modules.pop("src.claude_client", None)

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
    async def test_has_session_returns_true_for_existing(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "hello")
        assert manager.has_session("t1") is True

    @pytest.mark.asyncio
    async def test_has_session_returns_false_for_missing(self):
        config = _make_config()
        manager = ClaudeManager(config)
        assert manager.has_session("nonexistent") is False

    @pytest.mark.asyncio
    async def test_thread_context_prepended_to_first_message(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "hello", thread_context="[Previous context]")
        fake_client.query.assert_called_once_with("[Previous context]\n\nhello")

    @pytest.mark.asyncio
    async def test_thread_context_ignored_for_existing_session(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="r1")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "msg1")

        # Re-set responses for second message
        entry = manager._sessions["t1"]
        entry.client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="r2")]),
            _FakeResultMessage(),
        ])
        await manager.send_message("t1", "msg2", thread_context="[context]")
        # The second query call should have just "msg2", not prepended with context
        assert entry.client.query.call_args_list[1].args[0] == "msg2"

    @pytest.mark.asyncio
    async def test_thread_context_none_sends_text_only(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "hello", thread_context=None)
        fake_client.query.assert_called_once_with("hello")

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

    @pytest.mark.asyncio
    async def test_images_uses_async_iterable_query_path(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="got it")]),
            _FakeResultMessage(),
        ])
        images = [("image/png", b"\x89PNG")]
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            result = await manager.send_message("t1", "describe this", images=images)
        assert result == "got it"
        # query() should have received an async iterable, not a string
        arg = fake_client.query.call_args[0][0]
        assert not isinstance(arg, str)
        items = [item async for item in arg]
        assert len(items) == 1
        assert items[0]["type"] == "user"

    @pytest.mark.asyncio
    async def test_image_content_blocks_correctly_structured(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="ok")]),
            _FakeResultMessage(),
        ])
        raw_bytes = b"\x89PNG\r\n"
        images = [("image/png", raw_bytes)]
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "look", images=images)
        arg = fake_client.query.call_args[0][0]
        items = [item async for item in arg]
        content = items[0]["message"]["content"]
        # First block is text
        assert content[0] == {"type": "text", "text": "look"}
        # Second block is the image
        img_block = content[1]
        assert img_block["type"] == "image"
        assert img_block["source"]["type"] == "base64"
        assert img_block["source"]["media_type"] == "image/png"
        assert img_block["source"]["data"] == base64.b64encode(raw_bytes).decode()

    @pytest.mark.asyncio
    async def test_no_images_uses_string_query_path(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "hello", images=None)
        # query() should have received a plain string
        fake_client.query.assert_called_once_with("hello")

    @pytest.mark.asyncio
    async def test_multiple_images_all_included(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="seen")]),
            _FakeResultMessage(),
        ])
        images = [
            ("image/png", b"img1"),
            ("image/jpeg", b"img2"),
            ("image/gif", b"img3"),
        ]
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "multi", images=images)
        arg = fake_client.query.call_args[0][0]
        items = [item async for item in arg]
        content = items[0]["message"]["content"]
        # 1 text block + 3 image blocks
        assert len(content) == 4
        assert content[0]["type"] == "text"
        for i, (mt, raw) in enumerate(images, start=1):
            assert content[i]["type"] == "image"
            assert content[i]["source"]["media_type"] == mt
            assert content[i]["source"]["data"] == base64.b64encode(raw).decode()

    @pytest.mark.asyncio
    async def test_images_with_thread_context(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="ok")]),
            _FakeResultMessage(),
        ])
        images = [("image/png", b"pic")]
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message(
                "t1", "describe", thread_context="[Previous context]", images=images
            )
        arg = fake_client.query.call_args[0][0]
        items = [item async for item in arg]
        content = items[0]["message"]["content"]
        # Thread context should be prepended in the text block
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "[Previous context]\n\ndescribe"
        # Image block still present
        assert content[1]["type"] == "image"

    @pytest.mark.asyncio
    async def test_disallowed_tools_passed_to_options(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", disallowed_tools=["Bash"])
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert options.disallowed_tools == ["Bash"]

    @pytest.mark.asyncio
    async def test_disallowed_tools_none_defaults_to_empty_list(self):
        config = _make_config()
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
        assert options.disallowed_tools == []

    @pytest.mark.asyncio
    async def test_authorized_flag_stored_on_session(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "hello", authorized=True)
        assert manager.is_authorized_session("t1") is True

    @pytest.mark.asyncio
    async def test_is_authorized_session_returns_none_for_missing(self):
        config = _make_config()
        manager = ClaudeManager(config)
        assert manager.is_authorized_session("nonexistent") is None

    @pytest.mark.asyncio
    async def test_unauthorized_session_flag(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "hello")
        assert manager.is_authorized_session("t1") is False

    @pytest.mark.asyncio
    async def test_superuser_flag_stored_on_session(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "hello", superuser=True)
        assert manager.is_superuser_session("t1") is True

    @pytest.mark.asyncio
    async def test_is_superuser_session_returns_false_by_default(self):
        config = _make_config()
        manager = ClaudeManager(config)
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client):
            await manager.send_message("t1", "hello")
        assert manager.is_superuser_session("t1") is False

    @pytest.mark.asyncio
    async def test_is_superuser_session_returns_none_for_missing(self):
        config = _make_config()
        manager = ClaudeManager(config)
        assert manager.is_superuser_session("nonexistent") is None

    @pytest.mark.asyncio
    async def test_mcp_server_names_filters_servers(self):
        config = _make_config()
        manager = ClaudeManager(config)
        manager._mcp_servers = {"sonos": "sonos_srv", "homekit": "hk_srv", "gmail": "gmail_srv"}
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", mcp_server_names={"sonos", "homekit"})
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert options.mcp_servers == {"sonos": "sonos_srv", "homekit": "hk_srv"}

    @pytest.mark.asyncio
    async def test_mcp_server_names_none_gives_no_servers(self):
        config = _make_config()
        manager = ClaudeManager(config)
        manager._mcp_servers = {"sonos": "sonos_srv", "homekit": "hk_srv"}
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", mcp_server_names=None)
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert options.mcp_servers == {}

    @pytest.mark.asyncio
    async def test_mcp_server_names_empty_set_gives_no_servers(self):
        config = _make_config()
        manager = ClaudeManager(config)
        manager._mcp_servers = {"sonos": "sonos_srv", "homekit": "hk_srv"}
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", mcp_server_names=set())
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert options.mcp_servers == {}

    @pytest.mark.asyncio
    async def test_gmail_system_prompt_included_when_gmail_in_mcp_server_names(self):
        config = _make_config()
        manager = ClaudeManager(config)
        manager._mcp_servers = {"gmail": "gmail_srv"}
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", mcp_server_names={"gmail"}, authorized=True)
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert "Gmail capabilities" in options.system_prompt
        assert "CANNOT send emails" in options.system_prompt

    @pytest.mark.asyncio
    async def test_flights_system_prompt_included_when_flights_in_mcp_server_names(self):
        config = _make_config()
        manager = ClaudeManager(config)
        manager._mcp_servers = {"flights": "flights_srv"}
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", mcp_server_names={"flights"})
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert "flight search capabilities" in options.system_prompt
        assert "Amadeus API" in options.system_prompt
        assert "search-flights" in options.system_prompt

    @pytest.mark.asyncio
    async def test_flight_watch_system_prompt_included(self):
        config = _make_config()
        manager = ClaudeManager(config)
        manager._mcp_servers = {"flight_watch": "fw_srv"}
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", mcp_server_names={"flight_watch"})
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert "flight price watch tools" in options.system_prompt
        assert "flight_watch_add" in options.system_prompt

    @pytest.mark.asyncio
    async def test_flights_prompt_not_included_without_flights(self):
        config = _make_config()
        manager = ClaudeManager(config)
        manager._mcp_servers = {"sonos": "sonos_srv"}
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", mcp_server_names={"sonos"})
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert "flight search" not in options.system_prompt

    @pytest.mark.asyncio
    async def test_partial_mcp_access_gets_hide_prompt(self):
        config = _make_config()
        manager = ClaudeManager(config)
        manager._mcp_servers = {"sonos": "sonos_srv", "gmail": "gmail_srv"}
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", mcp_server_names={"sonos"}, authorized=True)
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert "You have access to Gmail capabilities" not in options.system_prompt
        assert "You only have the tools explicitly provided to you" in options.system_prompt

    @pytest.mark.asyncio
    async def test_no_mcp_access_gets_hide_prompt(self):
        config = _make_config()
        manager = ClaudeManager(config)
        manager._mcp_servers = {"sonos": "sonos_srv"}
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", mcp_server_names=set(), authorized=False)
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert "You only have the tools explicitly provided to you" in options.system_prompt

    @pytest.mark.asyncio
    async def test_full_mcp_access_gets_no_hide_prompt(self):
        config = _make_config()
        manager = ClaudeManager(config)
        manager._mcp_servers = {"sonos": "sonos_srv", "homekit": "hk_srv", "gmail": "gmail_srv"}
        fake_client = _FakeClaudeSDKClient()
        fake_client.set_responses([
            _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
            _FakeResultMessage(),
        ])
        with patch("src.claude_client.ClaudeSDKClient", return_value=fake_client) as mock_cls:
            await manager.send_message("t1", "hello", mcp_server_names={"sonos", "homekit", "gmail"}, authorized=True, superuser=True)
        call_kwargs = mock_cls.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1]["options"]
        assert "You only have the tools explicitly provided" not in options.system_prompt
        assert "You have access to Gmail capabilities" in options.system_prompt

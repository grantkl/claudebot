"""Tests for src.slack_app module."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock slack_bolt before importing slack_app
_mock_bolt = MagicMock()
_mock_async_app_cls = MagicMock()


class _FakeAsyncApp:
    """A fake AsyncApp that captures registered handlers."""

    def __init__(self, **kwargs):
        self.token = kwargs.get("token")
        self._handlers = {}

    def event(self, event_type):
        def decorator(func):
            self._handlers[event_type] = func
            return func
        return decorator


sys.modules.setdefault("slack_bolt", _mock_bolt)
sys.modules.setdefault("slack_bolt.async_app", MagicMock())
sys.modules.setdefault("claude_agent_sdk", MagicMock())

from src.rate_limiter import RATE_LIMIT_MESSAGE as RL_MESSAGE  # noqa: E402
from src.slack_app import create_app  # noqa: E402


def _make_config(**overrides):
    cfg = MagicMock()
    cfg.slack_bot_token = overrides.get("slack_bot_token", "xoxb-test")
    cfg.authorized_user_ids = overrides.get("authorized_user_ids", {"U001"})
    return cfg


def _make_rate_limiter(allowed=True):
    rl = MagicMock()
    rl.check_and_record = MagicMock(return_value=allowed)
    return rl


def _make_event(**overrides):
    base = {
        "user": "U001",
        "text": "<@B001> hello",
        "channel": "C001",
        "ts": "1234567890.000001",
    }
    base.update(overrides)
    return base


class TestSlackApp:
    @pytest.mark.asyncio
    async def test_bot_messages_are_ignored(self):
        config = _make_config()
        claude_manager = AsyncMock()
        say = AsyncMock()
        client = AsyncMock()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        event = _make_event(bot_id="B999")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        say.assert_not_called()
        claude_manager.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_subtype_messages_are_ignored(self):
        config = _make_config()
        claude_manager = AsyncMock()
        say = AsyncMock()
        client = AsyncMock()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        event = _make_event(subtype="message_changed")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        say.assert_not_called()
        claude_manager.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_message_flow(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="Claude response")
        say = AsyncMock()
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "B001"})
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        # Reaction added
        client.reactions_add.assert_called_once_with(
            name="hourglass_flowing_sand",
            channel="C001",
            timestamp=event["ts"],
        )

        # Claude called with stripped text and sonnet model for authorized user
        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", model="sonnet", include_mcp=True
        )

        # Response posted
        say.assert_called_once_with(text="Claude response", thread_ts=event["ts"])

        # Reaction removed
        client.reactions_remove.assert_called_once_with(
            name="hourglass_flowing_sand",
            channel="C001",
            timestamp=event["ts"],
        )

    @pytest.mark.asyncio
    async def test_dm_handler_only_processes_im_channel_type(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="dm response")
        say = AsyncMock()
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "B001"})
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        # channel_type != im should be ignored by the "message" handler
        event_not_im = _make_event(channel_type="channel", text="<@B001> hello")
        handler = app._handlers["message"]
        await handler(event=event_not_im, say=say, client=client)
        say.assert_not_called()

        # channel_type == im should be processed
        event_im = _make_event(channel_type="im", text="<@B001> hello")
        await handler(event=event_im, say=say, client=client)
        say.assert_called()

    @pytest.mark.asyncio
    async def test_error_in_claude_sends_error_message(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        say = AsyncMock()
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "B001"})
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        # Error message posted
        say.assert_called_once_with(
            text="I encountered an error processing your request. Please try again.",
            thread_ts=event["ts"],
        )

        # Reaction still removed in finally block
        client.reactions_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_authorized_user_gets_sonnet_no_rate_limit(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        rate_limiter = _make_rate_limiter()
        say = AsyncMock()
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "B001"})
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", model="sonnet", include_mcp=True
        )
        rate_limiter.check_and_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_authorized_user_gets_haiku_with_rate_limit(self):
        config = _make_config(authorized_user_ids={"UOTHER"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        rate_limiter = _make_rate_limiter(allowed=True)
        say = AsyncMock()
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "B001"})
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        rate_limiter.check_and_record.assert_called_once_with("U001")
        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", model="haiku", include_mcp=False
        )

    @pytest.mark.asyncio
    async def test_non_authorized_rate_limited_user_rejected(self):
        config = _make_config(authorized_user_ids={"UOTHER"})
        claude_manager = AsyncMock()
        rate_limiter = _make_rate_limiter(allowed=False)
        say = AsyncMock()
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "B001"})

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        say.assert_called_once_with(
            text=RL_MESSAGE,
            thread_ts=event["ts"],
        )
        claude_manager.send_message.assert_not_called()

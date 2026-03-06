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
sys.modules.setdefault("httpx", MagicMock())

from src.rate_limiter import RATE_LIMIT_MESSAGE as RL_MESSAGE  # noqa: E402
from src.slack_app import create_app  # noqa: E402

# Non-superuser tiers block filesystem tools to prevent capability discovery
_NON_SUPERUSER_DISALLOWED = ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]


def _make_config(**overrides):
    cfg = MagicMock()
    cfg.slack_bot_token = overrides.get("slack_bot_token", "xoxb-test")
    cfg.authorized_user_ids = overrides.get("authorized_user_ids", {"U001"})
    cfg.superuser_ids = overrides.get("superuser_ids", set())
    return cfg


def _make_rate_limiter(allowed=True):
    rl = MagicMock()
    rl.check_and_record = MagicMock(return_value=allowed)
    return rl


def _make_client():
    client = AsyncMock()
    client.auth_test = AsyncMock(return_value={"user_id": "B001"})
    client.reactions_add = AsyncMock()
    client.reactions_remove = AsyncMock()
    client.users_info = AsyncMock(return_value={
        "user": {"profile": {"display_name": "TestUser", "real_name": "Test User"}}
    })
    client.files_upload_v2 = AsyncMock()
    return client


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
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

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
            event["ts"], "hello", thread_context=None,
            model="sonnet", mcp_server_names={"sonos", "homekit", "flights", "flight_watch", "scheduler", "stocks", "web_search"}, images=None,
            disallowed_tools=_NON_SUPERUSER_DISALLOWED, authorized=True, superuser=False,
            user_id="U001",
            user_name="TestUser",
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
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

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
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

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
        claude_manager.has_session = MagicMock(return_value=True)
        rate_limiter = _make_rate_limiter()
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", thread_context=None,
            model="sonnet", mcp_server_names={"sonos", "homekit", "flights", "flight_watch", "scheduler", "stocks", "web_search"}, images=None,
            disallowed_tools=_NON_SUPERUSER_DISALLOWED, authorized=True, superuser=False,
            user_id="U001",
            user_name="TestUser",
        )
        rate_limiter.check_and_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_authorized_user_gets_haiku_with_rate_limit(self):
        config = _make_config(authorized_user_ids={"UOTHER"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        rate_limiter = _make_rate_limiter(allowed=True)
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        rate_limiter.check_and_record.assert_called_once_with("U001")
        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", thread_context=None,
            model="haiku", mcp_server_names={"stocks", "web_search"}, images=None,
            disallowed_tools=_NON_SUPERUSER_DISALLOWED,
            authorized=False, superuser=False,
            user_id="U001",
            user_name="TestUser",
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

    # --- New tests ---

    @pytest.mark.asyncio
    async def test_file_share_subtype_not_filtered(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        event = _make_event(subtype="file_share", files=[])
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        claude_manager.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_message_changed_subtype_still_filtered(self):
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
    async def test_thread_history_hydration_on_cold_session(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.has_session = MagicMock(return_value=False)
        claude_manager.send_message = AsyncMock(return_value="response")
        say = AsyncMock()
        client = _make_client()
        client.conversations_replies = AsyncMock(return_value={
            "messages": [
                {"user": "U001", "text": "first msg"},
                {"user": "B001", "text": "response"},
                {"user": "U001", "text": "<@B001> follow up"},
            ]
        })

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        event = _make_event(
            user="U001", text="<@B001> follow up",
            thread_ts="parent_ts",
        )
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        client.conversations_replies.assert_called_once_with(
            channel="C001", ts="parent_ts",
        )
        call_args = claude_manager.send_message.call_args
        thread_ctx = call_args.kwargs.get("thread_context") or call_args[1].get("thread_context")
        assert thread_ctx is not None
        assert "first msg" in thread_ctx

    @pytest.mark.asyncio
    async def test_thread_history_not_fetched_for_existing_session(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.has_session = MagicMock(return_value=True)
        claude_manager.send_message = AsyncMock(return_value="response")
        say = AsyncMock()
        client = _make_client()
        client.conversations_replies = AsyncMock()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        event = _make_event(
            user="U001", text="<@B001> follow up",
            thread_ts="parent_ts",
        )
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        client.conversations_replies.assert_not_called()
        claude_manager.send_message.assert_called_once_with(
            "parent_ts", "follow up", thread_context=None,
            model="sonnet", mcp_server_names={"sonos", "homekit", "flights", "flight_watch", "scheduler", "stocks", "web_search"}, images=None,
            disallowed_tools=_NON_SUPERUSER_DISALLOWED, authorized=True, superuser=False,
            user_id="U001",
            user_name="TestUser",
        )

    @pytest.mark.asyncio
    async def test_file_download_and_content_inclusion(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

        mock_response = MagicMock()
        mock_response.text = '{"key": "value"}'
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp), \
             patch("src.slack_app.httpx.AsyncClient", return_value=mock_http_client):
            app = create_app(config, claude_manager, _make_rate_limiter())

            event = _make_event(
                user="U001", text="<@B001> check this file",
                files=[{
                    "name": "test.json",
                    "mimetype": "application/json",
                    "url_private": "https://files.slack.com/test.json",
                }],
            )
            handler = app._handlers["app_mention"]
            await handler(event=event, say=say, client=client)

        call_args = claude_manager.send_message.call_args
        sent_text = call_args[0][1]
        assert '{"key": "value"}' in sent_text

    # --- Image support tests ---

    @pytest.mark.asyncio
    async def test_image_file_downloaded_and_passed_to_send_message(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="I see an image")
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

        mock_response = MagicMock()
        mock_response.content = b"fake-png-bytes"
        mock_response.status_code = 200
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp), \
             patch("src.slack_app.httpx.AsyncClient", return_value=mock_http_client):
            app = create_app(config, claude_manager, _make_rate_limiter())

            event = _make_event(
                user="U001", text="<@B001> what is this",
                files=[{
                    "name": "photo.png",
                    "mimetype": "image/png",
                    "url_private": "https://files.slack.com/photo.png",
                }],
            )
            handler = app._handlers["app_mention"]
            await handler(event=event, say=say, client=client)

        call_kwargs = claude_manager.send_message.call_args.kwargs
        assert call_kwargs["images"] == [("image/png", b"fake-png-bytes")]

    @pytest.mark.asyncio
    async def test_multiple_image_files_all_forwarded(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="I see images")
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

        png_response = MagicMock()
        png_response.content = b"png-bytes"
        png_response.status_code = 200
        jpeg_response = MagicMock()
        jpeg_response.content = b"jpeg-bytes"
        jpeg_response.status_code = 200

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=[png_response, jpeg_response])
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp), \
             patch("src.slack_app.httpx.AsyncClient", return_value=mock_http_client):
            app = create_app(config, claude_manager, _make_rate_limiter())

            event = _make_event(
                user="U001", text="<@B001> compare these",
                files=[
                    {
                        "name": "a.png",
                        "mimetype": "image/png",
                        "url_private": "https://files.slack.com/a.png",
                    },
                    {
                        "name": "b.jpg",
                        "mimetype": "image/jpeg",
                        "url_private": "https://files.slack.com/b.jpg",
                    },
                ],
            )
            handler = app._handlers["app_mention"]
            await handler(event=event, say=say, client=client)

        call_kwargs = claude_manager.send_message.call_args.kwargs
        assert call_kwargs["images"] == [
            ("image/png", b"png-bytes"),
            ("image/jpeg", b"jpeg-bytes"),
        ]

    @pytest.mark.asyncio
    async def test_non_image_binary_file_gets_metadata_note(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        event = _make_event(
            user="U001", text="<@B001> check this",
            files=[{
                "name": "report.pdf",
                "mimetype": "application/pdf",
                "url_private": "https://files.slack.com/report.pdf",
            }],
        )
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        call_args = claude_manager.send_message.call_args
        sent_text = call_args[0][1]
        assert "[Attached file: report.pdf (application/pdf) - binary file, contents not included]" in sent_text
        # images should be None (empty list → None)
        assert call_args.kwargs["images"] is None

    @pytest.mark.asyncio
    async def test_mixed_text_and_image_files(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

        text_response = MagicMock()
        text_response.text = "file content here"
        text_response.status_code = 200
        image_response = MagicMock()
        image_response.content = b"gif-bytes"
        image_response.status_code = 200

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=[text_response, image_response])
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp), \
             patch("src.slack_app.httpx.AsyncClient", return_value=mock_http_client):
            app = create_app(config, claude_manager, _make_rate_limiter())

            event = _make_event(
                user="U001", text="<@B001> look at these",
                files=[
                    {
                        "name": "data.json",
                        "mimetype": "application/json",
                        "url_private": "https://files.slack.com/data.json",
                    },
                    {
                        "name": "diagram.gif",
                        "mimetype": "image/gif",
                        "url_private": "https://files.slack.com/diagram.gif",
                    },
                ],
            )
            handler = app._handlers["app_mention"]
            await handler(event=event, say=say, client=client)

        call_args = claude_manager.send_message.call_args
        sent_text = call_args[0][1]
        # Text file content should be in the message text
        assert "file content here" in sent_text
        # Image should be in images param
        assert call_args.kwargs["images"] == [("image/gif", b"gif-bytes")]

    @pytest.mark.asyncio
    async def test_no_files_sends_no_images(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        call_kwargs = claude_manager.send_message.call_args.kwargs
        assert call_kwargs["images"] is None

    @pytest.mark.asyncio
    async def test_supported_image_mimetypes(self):
        """All four supported image types (png, jpeg, gif, webp) are downloaded."""
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

        mimetypes = ["image/png", "image/jpeg", "image/gif", "image/webp"]
        responses = []
        for mt in mimetypes:
            r = MagicMock()
            r.content = f"{mt}-bytes".encode()
            r.status_code = 200
            responses.append(r)

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=responses)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)

        files = [
            {"name": f"img.{mt.split('/')[1]}", "mimetype": mt, "url_private": f"https://files.slack.com/img.{mt.split('/')[1]}"}
            for mt in mimetypes
        ]

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp), \
             patch("src.slack_app.httpx.AsyncClient", return_value=mock_http_client):
            app = create_app(config, claude_manager, _make_rate_limiter())

            event = _make_event(
                user="U001", text="<@B001> check all images",
                files=files,
            )
            handler = app._handlers["app_mention"]
            await handler(event=event, say=say, client=client)

        call_kwargs = claude_manager.send_message.call_args.kwargs
        images = call_kwargs["images"]
        assert len(images) == 4
        assert [mt for mt, _ in images] == mimetypes
        for mt, data in images:
            assert data == f"{mt}-bytes".encode()

    @pytest.mark.asyncio
    async def test_large_code_block_posted_as_file(self):
        config = _make_config(authorized_user_ids={"U001"})
        # Build a response with a code block that exceeds 50 lines
        large_code = "\n".join([f"line {i}" for i in range(60)])
        response_text = f"Here is the code:\n```python\n{large_code}\n```"
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value=response_text)
        claude_manager.has_session = MagicMock(return_value=True)
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, _make_rate_limiter())

        event = _make_event(user="U001", text="<@B001> show me code")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        client.files_upload_v2.assert_called_once()
        upload_kwargs = client.files_upload_v2.call_args.kwargs
        assert upload_kwargs["channel"] == "C001"
        assert upload_kwargs["filename"] == "code.python"

        # say should be called with placeholder text
        say_text = say.call_args.kwargs["text"]
        assert "[Code uploaded as file:" in say_text

    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_filesystem_tools_blocked(self):
        config = _make_config(authorized_user_ids={"UOTHER"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        rate_limiter = _make_rate_limiter(allowed=True)
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", thread_context=None,
            model="haiku", mcp_server_names={"stocks", "web_search"}, images=None,
            disallowed_tools=_NON_SUPERUSER_DISALLOWED,
            authorized=False, superuser=False,
            user_id="U001",
            user_name="TestUser",
        )

    @pytest.mark.asyncio
    async def test_authorized_non_superuser_gets_filesystem_tools_blocked(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        rate_limiter = _make_rate_limiter()
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", thread_context=None,
            model="sonnet", mcp_server_names={"sonos", "homekit", "flights", "flight_watch", "scheduler", "stocks", "web_search"}, images=None,
            disallowed_tools=_NON_SUPERUSER_DISALLOWED, authorized=True, superuser=False,
            user_id="U001",
            user_name="TestUser",
        )

    @pytest.mark.asyncio
    async def test_unauthorized_user_evicts_authorized_session(self):
        config = _make_config(authorized_user_ids={"UOTHER"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        claude_manager.is_authorized_session = MagicMock(return_value=True)
        claude_manager.remove_session = AsyncMock()
        rate_limiter = _make_rate_limiter(allowed=True)
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        claude_manager.remove_session.assert_called_once_with(event["ts"])
        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", thread_context=None,
            model="haiku", mcp_server_names={"stocks", "web_search"}, images=None,
            disallowed_tools=_NON_SUPERUSER_DISALLOWED,
            authorized=False, superuser=False,
            user_id="U001",
            user_name="TestUser",
        )

    @pytest.mark.asyncio
    async def test_authorized_user_does_not_evict_session(self):
        config = _make_config(authorized_user_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        claude_manager.is_authorized_session = MagicMock(return_value=True)
        claude_manager.is_superuser_session = MagicMock(return_value=False)
        claude_manager.remove_session = AsyncMock()
        rate_limiter = _make_rate_limiter()
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        claude_manager.remove_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_superuser_gets_opus_and_all_mcp_servers(self):
        config = _make_config(authorized_user_ids={"U001"}, superuser_ids={"U001"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        rate_limiter = _make_rate_limiter()
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", thread_context=None,
            model="opus", mcp_server_names={"sonos", "homekit", "gmail", "scheduler", "flights", "flight_watch", "seats_aero", "playwright", "stocks", "web_search"}, images=None,
            disallowed_tools=None, authorized=True, superuser=True,
            user_id="U001",
            user_name="TestUser",
        )
        rate_limiter.check_and_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_authorized_non_superuser_gets_sonnet_and_limited_mcp(self):
        config = _make_config(authorized_user_ids={"U001"}, superuser_ids={"UOTHER"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        rate_limiter = _make_rate_limiter()
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", thread_context=None,
            model="sonnet", mcp_server_names={"sonos", "homekit", "flights", "flight_watch", "scheduler", "stocks", "web_search"}, images=None,
            disallowed_tools=_NON_SUPERUSER_DISALLOWED, authorized=True, superuser=False,
            user_id="U001",
            user_name="TestUser",
        )

    @pytest.mark.asyncio
    async def test_non_superuser_evicts_superuser_session(self):
        config = _make_config(authorized_user_ids={"U001"}, superuser_ids={"UOTHER"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.has_session = MagicMock(return_value=True)
        claude_manager.is_authorized_session = MagicMock(return_value=True)
        claude_manager.is_superuser_session = MagicMock(return_value=True)
        claude_manager.remove_session = AsyncMock()
        rate_limiter = _make_rate_limiter()
        say = AsyncMock()
        client = _make_client()

        with patch("src.slack_app.AsyncApp", _FakeAsyncApp):
            app = create_app(config, claude_manager, rate_limiter)

        event = _make_event(user="U001", text="<@B001> hello")
        handler = app._handlers["app_mention"]
        await handler(event=event, say=say, client=client)

        claude_manager.remove_session.assert_called_once_with(event["ts"])
        claude_manager.send_message.assert_called_once_with(
            event["ts"], "hello", thread_context=None,
            model="sonnet", mcp_server_names={"sonos", "homekit", "flights", "flight_watch", "scheduler", "stocks", "web_search"}, images=None,
            disallowed_tools=_NON_SUPERUSER_DISALLOWED, authorized=True, superuser=False,
            user_id="U001",
            user_name="TestUser",
        )

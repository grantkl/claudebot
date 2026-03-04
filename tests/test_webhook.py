"""Tests for src.webhook module."""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock external modules before importing webhook
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())
sys.modules.setdefault("claude_agent_sdk", MagicMock())

# Mock aiohttp — provide real-enough fakes so the webhook module works
_mock_aiohttp = MagicMock()
sys.modules.setdefault("aiohttp", _mock_aiohttp)
sys.modules.setdefault("aiohttp.web", _mock_aiohttp.web)

# Mock slack_sdk for DM delivery
_mock_slack_sdk = MagicMock()
sys.modules.setdefault("slack_sdk", _mock_slack_sdk)
sys.modules.setdefault("slack_sdk.web", _mock_slack_sdk.web)
sys.modules.setdefault("slack_sdk.web.async_client", _mock_slack_sdk.web.async_client)

from src.webhook import (  # noqa: E402
    AUTHORIZED_MCP_SERVERS,
    FILESYSTEM_TOOLS,
    SUPERUSER_MCP_SERVERS,
    create_webhook_app,
)


def _make_config(**overrides):
    cfg = MagicMock()
    cfg.webhook_secret = overrides.get("webhook_secret", "test-secret")
    cfg.slack_bot_token = overrides.get("slack_bot_token", "xoxb-test")
    cfg.authorized_user_ids = overrides.get("authorized_user_ids", set())
    cfg.superuser_ids = overrides.get("superuser_ids", set())
    return cfg


def _make_request(headers=None, body=None, body_error=False):
    """Create a fake aiohttp Request with given headers and JSON body."""
    request = MagicMock()
    request.headers = headers or {}
    if body_error:
        request.json = AsyncMock(side_effect=ValueError("bad json"))
    else:
        request.json = AsyncMock(return_value=body or {})
    return request


async def _call_handler(config, claude_manager, request):
    """Extract and call the POST /webhook/signal handler directly."""
    app = create_webhook_app(config, claude_manager)
    # The handler is registered via app.router.add_post; grab it from the call
    add_post_call = app.router.add_post
    handler = add_post_call.call_args[0][1]
    return await handler(request)


# ---------------------------------------------------------------------------
# TestAuth
# ---------------------------------------------------------------------------
class TestAuth:
    @pytest.mark.asyncio
    async def test_missing_auth_header_returns_401(self):
        config = _make_config()
        claude_manager = AsyncMock()
        request = _make_request(headers={}, body={"text": "hi", "user_id": "U1", "notify": ["U1"]})

        resp = await _call_handler(config, claude_manager, request)

        # web.json_response is mocked, check it was called with status=401
        from aiohttp import web
        web.json_response.assert_called()
        call_args = web.json_response.call_args
        assert call_args[1]["status"] == 401
        assert call_args[0][0]["error"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_wrong_token_returns_401(self):
        config = _make_config()
        claude_manager = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer wrong-token"},
            body={"text": "hi", "user_id": "U1", "notify": ["U1"]},
        )

        resp = await _call_handler(config, claude_manager, request)

        from aiohttp import web
        call_args = web.json_response.call_args
        assert call_args[1]["status"] == 401
        assert call_args[0][0]["error"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_valid_token_accepted(self):
        config = _make_config()
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="ok")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "user_id": "U1", "notify": []},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            resp = await _call_handler(config, claude_manager, request)

        from aiohttp import web
        call_args = web.json_response.call_args
        assert call_args[0][0]["ok"] is True


# ---------------------------------------------------------------------------
# TestPayloadValidation
# ---------------------------------------------------------------------------
class TestPayloadValidation:
    @pytest.mark.asyncio
    async def test_missing_text_returns_400(self):
        config = _make_config()
        claude_manager = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"user_id": "U1", "notify": ["U1"]},
        )

        resp = await _call_handler(config, claude_manager, request)

        from aiohttp import web
        call_args = web.json_response.call_args
        assert call_args[1]["status"] == 400
        assert "missing required fields" in call_args[0][0]["error"]

    @pytest.mark.asyncio
    async def test_missing_user_id_returns_400(self):
        config = _make_config()
        claude_manager = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "notify": ["U1"]},
        )

        resp = await _call_handler(config, claude_manager, request)

        from aiohttp import web
        call_args = web.json_response.call_args
        assert call_args[1]["status"] == 400

    @pytest.mark.asyncio
    async def test_missing_notify_returns_400(self):
        config = _make_config()
        claude_manager = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "user_id": "U1"},
        )

        resp = await _call_handler(config, claude_manager, request)

        from aiohttp import web
        call_args = web.json_response.call_args
        assert call_args[1]["status"] == 400

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        config = _make_config()
        claude_manager = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body_error=True,
        )

        resp = await _call_handler(config, claude_manager, request)

        from aiohttp import web
        call_args = web.json_response.call_args
        assert call_args[1]["status"] == 400
        assert call_args[0][0]["error"] == "invalid JSON"


# ---------------------------------------------------------------------------
# TestTierLogic
# ---------------------------------------------------------------------------
class TestTierLogic:
    @pytest.mark.asyncio
    async def test_superuser_gets_full_mcp_and_opus(self):
        config = _make_config(superuser_ids={"U_SUPER"}, authorized_user_ids={"U_SUPER"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "do stuff", "user_id": "U_SUPER", "notify": []},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await _call_handler(config, claude_manager, request)

        call_kwargs = claude_manager.send_message.call_args.kwargs
        assert call_kwargs["mcp_server_names"] == SUPERUSER_MCP_SERVERS
        assert call_kwargs["disallowed_tools"] is None
        assert call_kwargs["model"] == "opus"
        assert call_kwargs["superuser"] is True
        assert call_kwargs["authorized"] is True

    @pytest.mark.asyncio
    async def test_authorized_gets_limited_mcp_and_sonnet(self):
        config = _make_config(authorized_user_ids={"U_AUTH"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "do stuff", "user_id": "U_AUTH", "notify": []},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await _call_handler(config, claude_manager, request)

        call_kwargs = claude_manager.send_message.call_args.kwargs
        assert call_kwargs["mcp_server_names"] == AUTHORIZED_MCP_SERVERS
        assert call_kwargs["disallowed_tools"] == FILESYSTEM_TOOLS
        assert call_kwargs["model"] == "sonnet"
        assert call_kwargs["superuser"] is False
        assert call_kwargs["authorized"] is True

    @pytest.mark.asyncio
    async def test_unknown_user_gets_empty_mcp_and_sonnet(self):
        config = _make_config()
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="response")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "do stuff", "user_id": "U_NOBODY", "notify": []},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await _call_handler(config, claude_manager, request)

        call_kwargs = claude_manager.send_message.call_args.kwargs
        assert call_kwargs["mcp_server_names"] == set()
        assert call_kwargs["disallowed_tools"] == FILESYSTEM_TOOLS
        assert call_kwargs["model"] == "sonnet"
        assert call_kwargs["superuser"] is False
        assert call_kwargs["authorized"] is False


# ---------------------------------------------------------------------------
# TestDMDelivery
# ---------------------------------------------------------------------------
class TestDMDelivery:
    @pytest.mark.asyncio
    async def test_notify_sends_dm_to_each_user(self):
        config = _make_config()
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="result text")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "user_id": "U1", "notify": ["U_A", "U_B"]},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            mock_instance = AsyncMock()
            MockClient.return_value = mock_instance
            await _call_handler(config, claude_manager, request)

        assert mock_instance.chat_postMessage.call_count == 2
        channels = {
            c.kwargs["channel"]
            for c in mock_instance.chat_postMessage.call_args_list
        }
        assert channels == {"U_A", "U_B"}
        for call in mock_instance.chat_postMessage.call_args_list:
            assert call.kwargs["text"] == "result text"

    @pytest.mark.asyncio
    async def test_empty_notify_sends_no_dms(self):
        config = _make_config()
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="result text")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "user_id": "U1", "notify": []},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            mock_instance = AsyncMock()
            MockClient.return_value = mock_instance
            await _call_handler(config, claude_manager, request)

        mock_instance.chat_postMessage.assert_not_called()


# ---------------------------------------------------------------------------
# TestSessionCleanup
# ---------------------------------------------------------------------------
class TestSessionCleanup:
    @pytest.mark.asyncio
    async def test_session_removed_after_success(self):
        config = _make_config()
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="ok")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "user_id": "U1", "notify": []},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await _call_handler(config, claude_manager, request)

        claude_manager.remove_session.assert_called_once()
        thread_ts = claude_manager.remove_session.call_args[0][0]
        assert thread_ts.startswith("webhook-")

    @pytest.mark.asyncio
    async def test_session_removed_after_error(self):
        config = _make_config()
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "user_id": "U1", "notify": []},
        )

        await _call_handler(config, claude_manager, request)

        claude_manager.remove_session.assert_called_once()
        from aiohttp import web
        call_args = web.json_response.call_args
        assert call_args[1]["status"] == 500


# ---------------------------------------------------------------------------
# TestModelOverride
# ---------------------------------------------------------------------------
class TestModelOverride:
    @pytest.mark.asyncio
    async def test_custom_model_passed_through(self):
        config = _make_config()
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="ok")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "user_id": "U1", "notify": [], "model": "haiku"},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await _call_handler(config, claude_manager, request)

        call_kwargs = claude_manager.send_message.call_args.kwargs
        assert call_kwargs["model"] == "haiku"

    @pytest.mark.asyncio
    async def test_superuser_default_model_is_opus(self):
        config = _make_config(superuser_ids={"U_SUPER"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="ok")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "user_id": "U_SUPER", "notify": []},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await _call_handler(config, claude_manager, request)

        call_kwargs = claude_manager.send_message.call_args.kwargs
        assert call_kwargs["model"] == "opus"

    @pytest.mark.asyncio
    async def test_superuser_model_override(self):
        config = _make_config(superuser_ids={"U_SUPER"})
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="ok")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "user_id": "U_SUPER", "notify": [], "model": "sonnet"},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await _call_handler(config, claude_manager, request)

        call_kwargs = claude_manager.send_message.call_args.kwargs
        assert call_kwargs["model"] == "sonnet"


# ---------------------------------------------------------------------------
# TestErrorHandling
# ---------------------------------------------------------------------------
class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_send_message_exception_returns_500(self):
        config = _make_config()
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "hi", "user_id": "U1", "notify": ["U1"]},
        )

        await _call_handler(config, claude_manager, request)

        from aiohttp import web
        call_args = web.json_response.call_args
        assert call_args[1]["status"] == 500
        assert call_args[0][0]["ok"] is False
        assert call_args[0][0]["error"] == "internal error"

    @pytest.mark.asyncio
    async def test_response_includes_claude_response_text(self):
        config = _make_config()
        claude_manager = AsyncMock()
        claude_manager.send_message = AsyncMock(return_value="Here is the answer")
        claude_manager.remove_session = AsyncMock()
        request = _make_request(
            headers={"Authorization": "Bearer test-secret"},
            body={"text": "question", "user_id": "U1", "notify": []},
        )

        with patch("src.webhook.AsyncWebClient") as MockClient:
            MockClient.return_value = AsyncMock()
            await _call_handler(config, claude_manager, request)

        from aiohttp import web
        call_args = web.json_response.call_args
        assert call_args[0][0]["response"] == "Here is the answer"

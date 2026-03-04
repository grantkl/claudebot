"""Webhook server for external signal ingestion."""

import logging
import time

from aiohttp import web

from slack_sdk.web.async_client import AsyncWebClient

from .authorized_users import is_authorized, is_superuser
from .claude_client import ClaudeManager
from .config import Config

logger = logging.getLogger(__name__)

SUPERUSER_MCP_SERVERS = {"sonos", "homekit", "gmail", "scheduler", "flights", "flight_watch", "seats_aero", "playwright"}
AUTHORIZED_MCP_SERVERS = {"sonos", "homekit", "flights", "flight_watch", "scheduler"}
FILESYSTEM_TOOLS = ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]


def create_webhook_app(config: Config, claude_manager: ClaudeManager) -> web.Application:
    """Create and return the aiohttp webhook application."""

    async def handle_signal(request: web.Request) -> web.Response:
        # Auth check
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != config.webhook_secret:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        # Parse body
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        text = body.get("text")
        user_id = body.get("user_id")
        notify = body.get("notify")
        model = body.get("model")

        if not text or not user_id:
            return web.json_response(
                {"ok": False, "error": "missing required fields: text, user_id"},
                status=400,
            )

        if notify is None:
            notify = [user_id]

        # Determine tier-based access
        superuser = is_superuser(user_id, config.superuser_ids)
        authorized = superuser or is_authorized(user_id, config.authorized_user_ids)

        if superuser:
            mcp_server_names = SUPERUSER_MCP_SERVERS
            disallowed_tools = None
            if model is None:
                model = "opus"
        elif authorized:
            mcp_server_names = AUTHORIZED_MCP_SERVERS
            disallowed_tools = FILESYSTEM_TOOLS
            if model is None:
                model = "sonnet"
        else:
            mcp_server_names = set()
            disallowed_tools = FILESYSTEM_TOOLS
            if model is None:
                model = "sonnet"

        thread_ts = f"webhook-{time.time()}"

        try:
            response = await claude_manager.send_message(
                thread_ts, text,
                model=model,
                mcp_server_names=mcp_server_names,
                disallowed_tools=disallowed_tools,
                authorized=authorized,
                superuser=superuser,
                user_id=user_id,
            )
        except Exception:
            logger.exception("Webhook signal processing failed")
            return web.json_response({"ok": False, "error": "internal error"}, status=500)
        finally:
            await claude_manager.remove_session(thread_ts)

        # Notify via Slack DM
        if notify:
            client = AsyncWebClient(token=config.slack_bot_token)
            for recipient in notify:
                try:
                    await client.chat_postMessage(channel=recipient, text=response)
                except Exception:
                    logger.exception("Failed to send webhook DM to %s", recipient)

        return web.json_response({"ok": True, "response": response})

    app = web.Application()
    app.router.add_post("/webhook/signal", handle_signal)
    return app

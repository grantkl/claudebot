"""Slack app event handlers and bot wiring."""

import logging
from typing import Any

from slack_bolt.async_app import AsyncApp

from .allowlist import REJECTION_MESSAGE, is_user_allowed
from .claude_client import ClaudeManager
from .config import Config
from .message_utils import format_error_message, split_message, strip_bot_mention

logger = logging.getLogger(__name__)


def create_app(config: Config, claude_manager: ClaudeManager) -> AsyncApp:
    app = AsyncApp(token=config.slack_bot_token)
    bot_info: dict[str, str | None] = {"id": None}

    async def _handle_message(event: dict[str, Any], say: Any, client: Any) -> None:
        if event.get("bot_id") or event.get("subtype"):
            return

        if bot_info["id"] is None:
            result = await client.auth_test()
            bot_info["id"] = result["user_id"]

        if not is_user_allowed(event["user"], config.allowed_user_ids):
            thread_ts: str = event.get("thread_ts") or event["ts"]
            await say(text=REJECTION_MESSAGE, thread_ts=thread_ts)
            return

        text: str = event.get("text", "")
        cleaned_text = strip_bot_mention(text, str(bot_info["id"]))
        if not cleaned_text:
            return

        thread_ts = event.get("thread_ts") or event["ts"]

        await client.reactions_add(
            name="hourglass_flowing_sand",
            channel=event["channel"],
            timestamp=event["ts"],
        )

        try:
            response = await claude_manager.send_message(thread_ts, cleaned_text)
            for chunk in split_message(response):
                await say(text=chunk, thread_ts=thread_ts)
        except Exception as exc:
            await say(text=format_error_message(exc), thread_ts=thread_ts)
            logger.exception("Error handling message in thread %s", thread_ts)
        finally:
            try:
                await client.reactions_remove(
                    name="hourglass_flowing_sand",
                    channel=event["channel"],
                    timestamp=event["ts"],
                )
            except Exception:
                pass

    @app.event("app_mention")
    async def handle_mention(event: dict[str, Any], say: Any, client: Any) -> None:
        await _handle_message(event, say, client)

    @app.event("message")
    async def handle_message(event: dict[str, Any], say: Any, client: Any) -> None:
        if event.get("channel_type") == "im":
            await _handle_message(event, say, client)

    return app

"""Slack app event handlers and bot wiring."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from slack_bolt.async_app import AsyncApp

from .authorized_users import is_authorized, is_superuser
from .claude_client import ClaudeManager
from .config import Config
from .message_utils import (
    extract_large_code_blocks,
    format_error_message,
    format_file_attachments,
    format_thread_context,
    split_message,
    strip_bot_mention,
)
from .rate_limiter import RATE_LIMIT_MESSAGE, RateLimiter

logger = logging.getLogger(__name__)


def create_app(config: Config, claude_manager: ClaudeManager, rate_limiter: RateLimiter) -> AsyncApp:
    app = AsyncApp(token=config.slack_bot_token)
    bot_info: dict[str, str | None] = {"id": None}

    _SKIP_SUBTYPES = {"message_changed", "message_deleted", "message_replied", "channel_join", "channel_leave"}

    async def _handle_message(event: dict[str, Any], say: Any, client: Any) -> None:
        if event.get("bot_id") or event.get("subtype") in _SKIP_SUBTYPES:
            return

        if bot_info["id"] is None:
            result = await client.auth_test()
            bot_info["id"] = result["user_id"]

        text: str = event.get("text", "")
        cleaned_text = strip_bot_mention(text, str(bot_info["id"]))
        if not cleaned_text:
            return

        thread_ts = event.get("thread_ts") or event["ts"]

        superuser = is_superuser(event["user"], config.superuser_ids)
        authorized = superuser or is_authorized(event["user"], config.authorized_user_ids)
        if not authorized:
            if not rate_limiter.check_and_record(event["user"]):
                await say(text=RATE_LIMIT_MESSAGE, thread_ts=thread_ts)
                return

        model = "opus" if superuser else ("sonnet" if authorized else "haiku")
        if superuser:
            disallowed_tools = None
        else:
            disallowed_tools = ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]

        if superuser:
            mcp_server_names: set[str] = {"sonos", "homekit", "gmail", "scheduler", "flights"}
        elif authorized:
            mcp_server_names = {"sonos", "homekit", "flights"}
        else:
            mcp_server_names = {"flights"}

        # Thread history hydration for cold sessions in existing threads
        thread_context: str | None = None
        if not claude_manager.has_session(thread_ts) and "thread_ts" in event:
            result = await client.conversations_replies(
                channel=event["channel"], ts=thread_ts
            )
            messages = result.get("messages", [])
            context_messages = messages[:-1]
            if context_messages:
                thread_context = format_thread_context(
                    context_messages, str(bot_info["id"])
                )

        # File attachment reading
        TEXT_MIMETYPES = {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-yaml",
            "application/x-python",
        }
        IMAGE_MIMETYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
        images: list[tuple[str, bytes]] = []
        files = event.get("files", [])
        if files:
            files_content: list[tuple[str, str, str]] = []
            async with httpx.AsyncClient() as http_client:
                for file in files:
                    mimetype = file.get("mimetype", "")
                    if mimetype.startswith("text/") or mimetype in TEXT_MIMETYPES:
                        resp = await http_client.get(
                            file["url_private"],
                            headers={
                                "Authorization": f"Bearer {config.slack_bot_token}"
                            },
                        )
                        files_content.append(
                            (file["name"], mimetype, resp.text)
                        )
                    elif mimetype in IMAGE_MIMETYPES:
                        resp = await http_client.get(
                            file["url_private"],
                            headers={
                                "Authorization": f"Bearer {config.slack_bot_token}"
                            },
                        )
                        images.append((mimetype, resp.content))
                    else:
                        cleaned_text += f"\n\n[Attached file: {file['name']} ({mimetype}) - binary file, contents not included]"
            if files_content:
                cleaned_text += "\n\n" + format_file_attachments(files_content)

        if not authorized and claude_manager.is_authorized_session(thread_ts):
            await claude_manager.remove_session(thread_ts)
        elif authorized and not superuser and claude_manager.is_superuser_session(thread_ts):
            await claude_manager.remove_session(thread_ts)

        await client.reactions_add(
            name="hourglass_flowing_sand",
            channel=event["channel"],
            timestamp=event["ts"],
        )

        try:
            response = await claude_manager.send_message(
                thread_ts, cleaned_text, thread_context=thread_context,
                model=model, mcp_server_names=mcp_server_names,
                images=images if images else None,
                disallowed_tools=disallowed_tools,
                authorized=authorized,
                superuser=superuser,
            )

            # Extract large code blocks and post as files
            modified_text, code_blocks = extract_large_code_blocks(response)
            for block in code_blocks:
                filename = block.filename or f"code.{block.language}"
                await client.files_upload_v2(
                    channel=event["channel"],
                    content=block.content,
                    filename=filename,
                    thread_ts=thread_ts,
                    title=filename,
                )
            post_text = modified_text if code_blocks else response

            for chunk in split_message(post_text):
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

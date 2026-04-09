"""Slack app event handlers and bot wiring."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from slack_bolt.async_app import AsyncApp

from .authorized_users import is_authorized, is_superuser
from .mcp.shopping_list_server import _get_store, build_shopping_list_blocks
from .claude_client import ClaudeManager
from .config import Config
from .message_utils import (
    extract_image_paths,
    extract_large_code_blocks,
    extract_pdf_text,
    format_error_message,
    format_file_attachments,
    format_thread_context,
    split_message,
    strip_bold_links,
    strip_bot_mention,
)
from .rate_limiter import RATE_LIMIT_MESSAGE, RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword → MCP server mapping for lazy tool loading.
#
# Instead of sending every MCP tool schema in every API request (which can
# push the payload past the 20 MB limit), we classify each message and only
# enable the servers whose tools are actually relevant.
# ---------------------------------------------------------------------------

_SERVER_KEYWORD_RULES: list[tuple[set[str], re.Pattern[str]]] = [
    ({"sonos"}, re.compile(
        r"\bsonos\b|\bmusic\b|\bspeaker\b|\bvolume\b|\bplay\b|\bpause\b"
        r"|\bresume\b|\bskip\b|\bqueue\b|\bsong\b|\balbum\b|\bplaylist\b"
        r"|\btrack\b|\bnow playing\b|\bapple music\b|\bfavorite\b",
        re.IGNORECASE,
    )),
    ({"homekit"}, re.compile(
        r"\bhomekit\b|\bhome kit\b|\blight\b|\blamp\b|\bswitch\b"
        r"|\bthermostat\b|\block\b|\bfan\b|\bblind\b|\bshade\b|\bsensor\b"
        r"|\btemperature\b|\bturn on\b|\bturn off\b|\bbrightness\b|\bdoor\b"
        r"|\bgarage\b|\bscene\b|\bautomation\b|\baccessor",
        re.IGNORECASE,
    )),
    ({"gmail"}, re.compile(
        r"\bemail\b|\bgmail\b|\binbox\b|\bunread\b|\bmail\b",
        re.IGNORECASE,
    )),
    ({"calendar"}, re.compile(
        r"\bcalendar\b|\bmeeting\b|\bappointment\b",
        re.IGNORECASE,
    )),
    ({"scheduler"}, re.compile(
        r"\bschedule\b|\btask\b|\bcron\b|\breminder\b|\brecurring\b|\bremind\b",
        re.IGNORECASE,
    )),
    ({"flights", "flight_watch", "google_flights", "seats_aero"}, re.compile(
        r"\bflights?\b|\bairport\b|\bairline\b|\bfly\b|\bflying\b|\bboarding\b",
        re.IGNORECASE,
    )),
    ({"seats_aero"}, re.compile(
        r"\baward\b|\bpoints\b|\bmiles\b",
        re.IGNORECASE,
    )),
    ({"stocks"}, re.compile(
        r"\bstocks?\b|\boptions?\b|\bticker\b|\bmarket\b"
        r"|\bputs?\b|\bcalls?\b|\bearnings\b|\$[A-Z]{1,5}\b",
        re.IGNORECASE,
    )),
    ({"web_search"}, re.compile(
        r"\bsearch\b|\blook up\b|\bgoogle\b|\bnews\b|\blatest\b",
        re.IGNORECASE,
    )),
    ({"shopping_list"}, re.compile(
        r"\bgrocery\b|\bshopping list\b|\brecipe\b|\bingredients?\b|\bcostco\b",
        re.IGNORECASE,
    )),
    ({"deploy"}, re.compile(
        r"\bdeploy\b|\brebuild\b",
        re.IGNORECASE,
    )),
]


def classify_needed_servers(text: str) -> set[str]:
    """Determine which MCP servers are relevant based on message content."""
    needed: set[str] = set()
    for servers, pattern in _SERVER_KEYWORD_RULES:
        if pattern.search(text):
            needed |= servers
    return needed


def create_app(config: Config, claude_manager: ClaudeManager, rate_limiter: RateLimiter) -> AsyncApp:
    app = AsyncApp(token=config.slack_bot_token)
    bot_info: dict[str, str | None] = {"id": None}
    user_names: dict[str, str] = {}

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

        # Determine the full set of servers this user is *allowed* to use
        # (authorization gate — unchanged from before).
        if superuser:
            mcp_server_names: set[str] = {"sonos", "homekit", "gmail", "calendar", "scheduler", "flights", "flight_watch", "google_flights", "seats_aero", "stocks", "web_search", "shopping_list"}
        elif authorized:
            mcp_server_names = {"sonos", "homekit", "flights", "flight_watch", "google_flights", "scheduler", "stocks", "web_search", "shopping_list"}
        else:
            mcp_server_names = {"stocks", "web_search"}

        # Lazy loading: only enable servers whose tools are relevant to
        # this particular message.  The full authorized set is still passed
        # so the session *has* every server available — but only the needed
        # subset is toggled on before each API call.
        needed_servers = classify_needed_servers(cleaned_text) & mcp_server_names

        # Thread history hydration for cold sessions in existing threads
        thread_context: str | None = None
        if not claude_manager.has_session(thread_ts) and "thread_ts" in event:
            result = await client.conversations_replies(
                channel=event["channel"], ts=thread_ts
            )
            messages = result.get("messages", [])
            logger.info(
                "Thread hydration for %s: conversations_replies returned %d messages from channel %s",
                thread_ts, len(messages), event["channel"],
            )
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
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {config.slack_bot_token}"},
                follow_redirects=True,
            ) as http_client:
                for file in files:
                    mimetype = file.get("mimetype", "")
                    url = file.get("url_private_download") or file["url_private"]
                    if mimetype.startswith("text/") or mimetype in TEXT_MIMETYPES:
                        resp = await http_client.get(url)
                        files_content.append(
                            (file["name"], mimetype, resp.text)
                        )
                    elif mimetype == "application/pdf":
                        resp = await http_client.get(url)
                        if resp.status_code == 200 and resp.content:
                            pdf_text = extract_pdf_text(resp.content, file["name"])
                            files_content.append(
                                (file["name"], "text/plain", pdf_text)
                            )
                        else:
                            logger.warning("Failed to download PDF %s: HTTP %d", file["name"], resp.status_code)
                            cleaned_text += f"\n\n[Attached PDF: {file['name']} - failed to download]"
                    elif mimetype in IMAGE_MIMETYPES:
                        resp = await http_client.get(url)
                        if resp.status_code == 200 and resp.content:
                            images.append((mimetype, resp.content))
                        else:
                            logger.warning("Failed to download image %s: HTTP %d", file["name"], resp.status_code)
                            cleaned_text += f"\n\n[Attached image: {file['name']} - failed to download]"
                    else:
                        cleaned_text += f"\n\n[Attached file: {file['name']} ({mimetype}) - binary file, contents not included]"
            if files_content:
                cleaned_text += "\n\n" + format_file_attachments(files_content)

        if not authorized and claude_manager.is_authorized_session(thread_ts):
            await claude_manager.remove_session(thread_ts)
        elif authorized and not superuser and claude_manager.is_superuser_session(thread_ts):
            await claude_manager.remove_session(thread_ts)

        user_id = event["user"]
        if user_id not in user_names:
            try:
                info = await client.users_info(user=user_id)
                profile = info["user"].get("profile", {})
                user_names[user_id] = profile.get("display_name") or profile.get("real_name") or user_id
            except Exception:
                user_names[user_id] = user_id

        await client.reactions_add(
            name="hourglass_flowing_sand",
            channel=event["channel"],
            timestamp=event["ts"],
        )

        try:
            result = await claude_manager.send_message(
                thread_ts, cleaned_text, thread_context=thread_context,
                model=model, mcp_server_names=mcp_server_names,
                needed_servers=needed_servers,
                images=images if images else None,
                disallowed_tools=disallowed_tools,
                authorized=authorized,
                superuser=superuser,
                user_id=user_id,
                user_name=user_names.get(user_id),
            )
            response = result.text

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

            # Extract and upload screenshot images
            modified_text2, image_files = extract_image_paths(post_text)
            for img in image_files:
                try:
                    with open(img.path, "rb") as f:
                        await client.files_upload_v2(
                            channel=event["channel"],
                            file=f,
                            filename=img.filename,
                            thread_ts=thread_ts,
                            title=img.filename,
                        )
                except FileNotFoundError:
                    logger.warning("Screenshot not found: %s", img.path)
            post_text = modified_text2 if image_files else post_text
            post_text = strip_bold_links(post_text)

            if result.used_shopping_list_view:
                store = _get_store()
                items = store.get_items()
                blocks = build_shopping_list_blocks(items)
                await say(text=post_text, blocks=blocks, thread_ts=thread_ts)
            else:
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

    @app.action(re.compile(r"shopping_list_check_item.*"))
    async def handle_shopping_list_check(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        action = body["actions"][0]
        selected_names = {opt["value"] for opt in action.get("selected_options", [])}
        # Determine which category's checkboxes these are from the action_id options
        all_names = {opt["value"] for opt in action.get("options", [])}
        # Check selected, uncheck deselected within this category
        store = _get_store()
        to_check = list(selected_names)
        to_uncheck = list(all_names - selected_names)
        if to_check:
            store.check(to_check)
        if to_uncheck:
            store.uncheck(to_uncheck)
        items = store.get_items()
        blocks = build_shopping_list_blocks(items)
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        await client.chat_update(channel=channel, ts=ts, text="Shopping List", blocks=blocks)

    @app.event("message")
    async def handle_message(event: dict[str, Any], say: Any, client: Any) -> None:
        if event.get("channel_type") == "im":
            await _handle_message(event, say, client)
            return
        thread_ts = event.get("thread_ts")
        if thread_ts:
            has_session = claude_manager.has_session(thread_ts)
            in_channel = event.get("channel") in config.auto_reply_channel_ids
            logger.info(
                "Thread reply in channel %s thread %s: has_session=%s in_auto_reply=%s auto_reply_channels=%s",
                event.get("channel"), thread_ts, has_session, in_channel, config.auto_reply_channel_ids,
            )
            if has_session and in_channel:
                await _handle_message(event, say, client)

    return app

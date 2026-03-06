"""Claude SDK client wrapper with per-thread session management."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class SessionEntry:
    client: ClaudeSDKClient
    last_accessed: float
    authorized: bool = False
    superuser: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ClaudeManager:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._sessions: dict[str, SessionEntry] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

        self._mcp_servers: dict[str, Any] = {}
        if self._config.enable_mcp:
            from .mcp import build_mcp_servers
            self._mcp_servers = build_mcp_servers()

    async def start(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        for thread_ts in list(self._sessions):
            await self._remove_session(thread_ts)

    def has_session(self, thread_ts: str) -> bool:
        return thread_ts in self._sessions

    def is_authorized_session(self, thread_ts: str) -> bool | None:
        entry = self._sessions.get(thread_ts)
        if entry is None:
            return None
        return entry.authorized

    def is_superuser_session(self, thread_ts: str) -> bool | None:
        entry = self._sessions.get(thread_ts)
        if entry is None:
            return None
        return entry.superuser

    async def remove_session(self, thread_ts: str) -> None:
        await self._remove_session(thread_ts)

    async def send_message(self, thread_ts: str, text: str, thread_context: str | None = None, model: str | None = None, mcp_server_names: set[str] | None = None, images: list[tuple[str, bytes]] | None = None, disallowed_tools: list[str] | None = None, authorized: bool = False, superuser: bool = False, user_id: str | None = None) -> str:
        is_new_session = thread_ts not in self._sessions
        if is_new_session:
            system_prompt = self._config.claude_system_prompt
            if mcp_server_names:
                mcp_servers = {k: v for k, v in self._mcp_servers.items() if k in mcp_server_names}
            else:
                mcp_servers = {}
            if mcp_servers:
                system_prompt += (
                    "\n\nCRITICAL RULE — NO FABRICATION: You MUST NEVER fabricate, invent,"
                    " or hallucinate data that should come from tool calls. Every piece of"
                    " data you report (emails, prices, device states, search results) MUST"
                    " come directly from a tool call response. If a tool call fails, returns"
                    " an error, or returns no results, report that honestly. Do NOT generate"
                    " plausible-looking fake data under any circumstances. When summarizing"
                    " tool results, use only the fields returned by the tool — do not"
                    " embellish, add details, or fill in gaps with guesses."
                    " Also: NEVER claim you received a message or request that was not"
                    " actually in your conversation input. Only respond to what was actually said."
                )
                if os.environ.get("HOMECLAW_MCP_URL"):
                    system_prompt += (
                        "\n\nYou have access to smart home capabilities via MCP tools."
                        " You can control Sonos speakers."
                        " You can also control HomeKit devices via HomeClaw, which provides"
                        " access to all HomeKit devices including Thread-based devices."
                        " HomeClaw supports rooms, scenes, and individual device control."
                    )
                else:
                    system_prompt += (
                        "\n\nYou have access to smart home capabilities via MCP tools."
                        " You can control Sonos speakers and HomeKit devices."
                    )
            if mcp_server_names and "gmail" in mcp_server_names:
                system_prompt += (
                    "\n\nYou have access to Gmail capabilities via MCP tools."
                    " You can list, search, and read emails, mark them as read,"
                    " and star/unstar emails for follow-up."
                    " You CANNOT send emails."
                    " When a user asks to be reminded about an email, star it and"
                    " create a run_once scheduler task that references the email subject/sender."
                )
            if mcp_server_names and "scheduler" in mcp_server_names:
                system_prompt += (
                    "\n\nYou have access to a task scheduler via MCP tools."
                    " You can list, add, update, remove, pause, resume, and trigger"
                    " scheduled autonomous tasks. All cron expressions are in US/Pacific time."
                    " When the user says a time like '5pm', use Pacific time in the cron."
                    " For one-time reminders, set run_once=true so the task auto-disables after firing."
                )
            if mcp_server_names and "scheduler" in mcp_server_names and user_id:
                system_prompt += (
                    f'\n\nThe current user\'s Slack ID is {user_id}. When creating'
                    f' tasks with scheduler_add_task, you MUST include'
                    f' "created_by": "{user_id}".'
                )
            if mcp_server_names and "flights" in mcp_server_names:
                system_prompt += (
                    "\n\nYou have access to flight search capabilities via MCP tools (powered by the Amadeus API)."
                    " Available tools: search-flights, search-airports, flight-price-analysis,"
                    " flight-inspiration (cheapest destinations), airport-routes (direct routes),"
                    " and nearest-airports. Dates must be future ISO 8601 (YYYY-MM-DD)."
                )
            if mcp_server_names and "flight_watch" in mcp_server_names:
                system_prompt += (
                    "\n\nYou have access to flight price watch tools via MCP."
                    " You can add watches for specific routes and dates (flight_watch_add),"
                    " or track a specific booked flight by including airline and flight_numbers."
                    " When a user says they booked a flight, use max_price as the price they paid."
                    " List active watches (flight_watch_list), view price history"
                    " (flight_watch_history), and remove watches (flight_watch_remove)."
                    " Price checks run automatically every 6 hours via the scheduler."
                )
            if mcp_server_names and "playwright" in mcp_server_names:
                system_prompt += (
                    "\n\nYou have access to browser automation via Playwright MCP tools."
                    " You can navigate to URLs, click elements, fill forms, take screenshots,"
                    " and interact with web pages. Use browser_snapshot (not screenshots) to"
                    " read page content and find elements to interact with. Always use"
                    " browser_close when done with a browsing session."
                    " When taking screenshots, always specify a filename parameter."
                    " Screenshots you take will be automatically uploaded to the Slack thread."
                )
            if mcp_server_names and "seats_aero" in mcp_server_names:
                system_prompt += (
                    "\n\nYou have access to award flight search via seats.aero MCP tools."
                    " Use award_search for cached multi-program availability searches"
                    " across 24 loyalty programs (flexible dates and routes)."
                    " Use award_trip_details to get flight segments,"
                    " times, and booking links for a specific result from a cached search."
                )
            if set(mcp_servers) != set(self._mcp_servers):
                system_prompt += (
                    "\n\nYou only have the tools explicitly provided to you."
                    " Do not mention, reference, or suggest any capabilities beyond"
                    " what your available tools provide. If asked about capabilities"
                    " you do not have, say you cannot help with that."
                )
            client = ClaudeSDKClient(
                options=ClaudeAgentOptions(
                    model=model or self._config.claude_model,
                    system_prompt=system_prompt,
                    permission_mode="bypassPermissions",
                    mcp_servers=mcp_servers,
                    disallowed_tools=disallowed_tools or [],
                )
            )
            await client.connect()
            self._sessions[thread_ts] = SessionEntry(
                client=client, last_accessed=time.time(), authorized=authorized,
                superuser=superuser,
            )

        query_text = text
        if is_new_session and thread_context is not None:
            query_text = thread_context + "\n\n[NEW MESSAGE — respond to this only:]\n" + text

        if is_new_session and thread_context is not None:
            logger.info(
                "Session %s hydrated with thread context (%d chars): %s",
                thread_ts, len(thread_context), thread_context[:500],
            )
        logger.info("Session %s query (%d chars): %s", thread_ts, len(query_text), query_text[:500])

        entry = self._sessions[thread_ts]
        async with entry.lock:
            entry.last_accessed = time.time()
            try:
                if images:
                    content: list[dict[str, Any]] = [{"type": "text", "text": query_text}]
                    for media_type, data in images:
                        content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.b64encode(data).decode(),
                            },
                        })

                    async def _single_message(msg: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
                        yield msg

                    await entry.client.query(_single_message({
                        "type": "user",
                        "message": {"role": "user", "content": content},
                        "parent_tool_use_id": None,
                        "session_id": "",
                    }))
                else:
                    await entry.client.query(query_text)
                response_parts: list[str] = []
                screenshot_paths: list[str] = []
                async for msg in entry.client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                response_parts.append(block.text)
                            elif isinstance(block, ToolUseBlock) and "screenshot" in block.name.lower():
                                fn = block.input.get("filename")
                                if fn:
                                    abs_path = os.path.abspath(fn)
                                    screenshot_paths.append(abs_path)
                    elif isinstance(msg, ResultMessage):
                        break
                text = "".join(response_parts)
                if screenshot_paths:
                    unique = list(dict.fromkeys(screenshot_paths))
                    new_paths = [p for p in unique if p not in text]
                    if new_paths:
                        text += "\n" + "\n".join(new_paths)
                return text
            except Exception:
                logger.exception(
                    "Error in Claude session for thread %s", thread_ts
                )
                await self._remove_session(thread_ts)
                raise

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                expired = [
                    ts
                    for ts, entry in self._sessions.items()
                    if now - entry.last_accessed > self._config.session_ttl_seconds
                ]
                for ts in expired:
                    logger.info("Evicting expired session for thread %s", ts)
                    await self._remove_session(ts)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in session cleanup loop")

    async def _remove_session(self, thread_ts: str) -> None:
        entry = self._sessions.pop(thread_ts, None)
        if entry is not None:
            try:
                await entry.client.disconnect()
            except Exception:
                logger.exception(
                    "Error disconnecting session for thread %s", thread_ts
                )

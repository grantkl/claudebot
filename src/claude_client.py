"""Claude SDK client wrapper with per-thread session management."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

@dataclass
class SendMessageResult:
    """Result from sending a message to Claude."""
    text: str
    used_shopping_list_view: bool = False


from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from .config import Config
from .error_utils import classify_error

logger = logging.getLogger(__name__)

# MCP servers that should never be toggled off.  They stay enabled for the
# entire session lifetime so their tools are always available.
_ALWAYS_ON_SERVERS: set[str] = {"memory"}


@dataclass
class SessionEntry:
    client: ClaudeSDKClient
    last_accessed: float
    authorized: bool = False
    superuser: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Tracks which MCP servers are currently toggled *on* for this session.
    enabled_servers: set[str] = field(default_factory=set)
    # The full set of MCP server names this session was created with.
    all_server_names: set[str] = field(default_factory=set)


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

    async def send_message(self, thread_ts: str, text: str, thread_context: str | None = None, model: str | None = None, mcp_server_names: set[str] | None = None, needed_servers: set[str] | None = None, images: list[tuple[str, bytes]] | None = None, disallowed_tools: list[str] | None = None, authorized: bool = False, superuser: bool = False, user_id: str | None = None, user_name: str | None = None, _retry: bool = True) -> SendMessageResult:
        is_new_session = thread_ts not in self._sessions
        if is_new_session:
            system_prompt = self._config.claude_system_prompt
            system_prompt += (
                " Your responses are posted as Slack messages."
                " Large code blocks (50+ lines) in your response are automatically"
                " extracted and uploaded as file snippets in the Slack thread."
                " You CAN and SHOULD include large code blocks when helpful — they"
                " will be uploaded to Slack for the user. Never tell the user you"
                " cannot upload or share files in Slack."
            )
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
            if os.environ.get("PLAYWRIGHT_ENABLED", "").lower() in ("1", "true", "yes"):
                system_prompt += (
                    "\n\nYou have access to browser automation via the Playwright CLI"
                    " (`playwright-cli`), available through the Bash tool."
                    " Key commands: `playwright-cli open` (launch browser),"
                    " `playwright-cli goto <url>` (navigate),"
                    " `playwright-cli snapshot` (get page accessibility tree with element refs),"
                    " `playwright-cli click <ref>` (click element),"
                    " `playwright-cli type <text>` (type into focused element),"
                    " `playwright-cli fill <ref> <text>` (fill a field),"
                    " `playwright-cli screenshot [filename]` (capture screenshot),"
                    " `playwright-cli close` (close browser)."
                    " Always use `playwright-cli snapshot` to read page content and find"
                    " element refs before interacting. Always close the browser when done."
                    " Screenshots you take will be automatically uploaded to the Slack thread."
                    " Run `playwright-cli --help` for the full command list."
                )
            if mcp_server_names and "seats_aero" in mcp_server_names:
                system_prompt += (
                    "\n\nYou have access to award flight search via seats.aero MCP tools."
                    " Use award_search for cached multi-program availability searches"
                    " across 24 loyalty programs (flexible dates and routes)."
                    " Use award_trip_details to get flight segments,"
                    " times, and booking links for a specific result from a cached search."
                )
            if mcp_server_names and "stocks" in mcp_server_names:
                system_prompt += (
                    "\n\nYou have access to stock market data via MCP tools."
                    " Available tools: stock_quote (current price/fundamentals),"
                    " options_expirations (available expiry dates),"
                    " options_chain (calls/puts with greeks for a given expiration),"
                    " stock_technicals (SMA, RSI, MACD, Bollinger Bands)."
                    " For options analysis, use this workflow: stock_quote first"
                    " (get current price), then stock_technicals (trend/momentum),"
                    " then options_chain (specific strikes). Be direct and"
                    " concise — the user wants actionable analysis, not disclaimers."
                )
            if mcp_server_names and "memory" in mcp_server_names:
                system_prompt += (
                    "\n\nYou have access to persistent memory via MCP tools."
                    " Use store_memory to save important facts, user preferences,"
                    " decisions, and context. Use retrieve_memory to semantically"
                    " search past memories before answering questions. Use search_by_tag"
                    " to find memories by category. Proactively check memory at the"
                    " start of conversations for relevant context about the user."
                )
                if user_id:
                    system_prompt += (
                        f"\n\nMEMORY SCOPING: All memories are per-user. When storing memories,"
                        f" ALWAYS include the tag '{user_id}' so memories are scoped to this user."
                        f" When retrieving or searching memories, ALWAYS filter by the tag"
                        f" '{user_id}' to only recall this user's memories."
                    )
            use_web_search = bool(mcp_server_names and "web_search" in mcp_server_names)
            if use_web_search:
                system_prompt += (
                    "\n\nYou have access to web search via the built-in WebSearch and"
                    " WebFetch tools. Use this for current news, earnings dates,"
                    " analyst ratings, macro events, and any real-time information"
                    " not available through other tools."
                )
            if set(mcp_servers) != set(self._mcp_servers):
                system_prompt += (
                    "\n\nYou only have the tools explicitly provided to you."
                    " Do not mention, reference, or suggest any capabilities beyond"
                    " what your available tools provide. If asked about capabilities"
                    " you do not have, say you cannot help with that."
                )
            allowed_tools: list[str] = []
            if use_web_search:
                allowed_tools.extend(["WebSearch", "WebFetch"])

            client = ClaudeSDKClient(
                options=ClaudeAgentOptions(
                    model=model or self._config.claude_model,
                    system_prompt=system_prompt,
                    permission_mode="bypassPermissions",
                    mcp_servers=mcp_servers,
                    allowed_tools=allowed_tools,
                    disallowed_tools=disallowed_tools or [],
                )
            )
            await client.connect()
            # Start with all MCP servers disabled — we selectively enable
            # only the servers needed for each message via toggle.
            # Always-on servers (e.g. memory) are never disabled so their
            # tools remain available throughout the session.
            server_names_in_session = set(mcp_servers.keys())
            always_on = server_names_in_session & _ALWAYS_ON_SERVERS
            for name in server_names_in_session - always_on:
                try:
                    await client.toggle_mcp_server(name, enabled=False)
                except Exception:
                    logger.warning("Failed to disable MCP server %s at session start", name)
            self._sessions[thread_ts] = SessionEntry(
                client=client, last_accessed=time.time(), authorized=authorized,
                superuser=superuser,
                all_server_names=server_names_in_session,
                enabled_servers=always_on,
            )

        query_text = text
        if is_new_session and thread_context is not None:
            query_text = thread_context + "\n\n[NEW MESSAGE — respond to this only:]\n" + text

        if is_new_session and thread_context is not None:
            logger.info(
                "Session %s hydrated with thread context (%d chars): %s",
                thread_ts, len(thread_context), thread_context[:500],
            )
        logger.info("Session %s user=%s query (%d chars): %s", thread_ts, user_name or user_id or "unknown", len(query_text), query_text[:500])

        entry = self._sessions[thread_ts]
        async with entry.lock:
            entry.last_accessed = time.time()

            # Sync enabled MCP servers to match what this message needs.
            want = (needed_servers or set()) & entry.all_server_names
            to_disable = entry.enabled_servers - want - _ALWAYS_ON_SERVERS
            to_enable = want - entry.enabled_servers
            for name in to_disable:
                try:
                    await entry.client.toggle_mcp_server(name, enabled=False)
                except Exception:
                    logger.warning("Failed to disable MCP server %s", name)
            for name in to_enable:
                try:
                    await entry.client.toggle_mcp_server(name, enabled=True)
                except Exception:
                    logger.warning("Failed to enable MCP server %s", name)
            entry.enabled_servers = want
            if want:
                logger.info("Session %s MCP servers enabled: %s", thread_ts, want)

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
                used_shopping_list_view = False
                async for msg in entry.client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                response_parts.append(block.text)
                            elif isinstance(block, ToolUseBlock):
                                if "screenshot" in block.name.lower():
                                    fn = block.input.get("filename")
                                    if fn:
                                        abs_path = os.path.abspath(fn)
                                        screenshot_paths.append(abs_path)
                                elif "shopping_list_view" in block.name:
                                    used_shopping_list_view = True
                    elif isinstance(msg, ResultMessage):
                        break
                text = "".join(response_parts)
                if screenshot_paths:
                    unique = list(dict.fromkeys(screenshot_paths))
                    new_paths = [p for p in unique if p not in text]
                    if new_paths:
                        text += "\n" + "\n".join(new_paths)
                return SendMessageResult(text=text, used_shopping_list_view=used_shopping_list_view)
            except Exception as exc:
                classified = classify_error(exc)
                await self._remove_session(thread_ts)
                if _retry:
                    logger.warning(
                        "Session error for thread %s [%s]: %s — retrying with fresh session",
                        thread_ts, classified.category, classified.log_message,
                    )
                    return await self.send_message(
                        thread_ts, text, thread_context=thread_context,
                        model=model, mcp_server_names=mcp_server_names,
                        needed_servers=needed_servers,
                        images=images, disallowed_tools=disallowed_tools,
                        authorized=authorized, superuser=superuser,
                        user_id=user_id, user_name=user_name, _retry=False,
                    )
                logger.exception(
                    "Error in Claude session for thread %s (retry exhausted) [%s]: %s",
                    thread_ts, classified.category, classified.log_message,
                )
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

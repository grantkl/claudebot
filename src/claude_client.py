"""Claude SDK client wrapper with per-thread session management."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class SessionEntry:
    client: ClaudeSDKClient
    last_accessed: float
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

    async def send_message(self, thread_ts: str, text: str, thread_context: str | None = None, model: str | None = None, include_mcp: bool = True) -> str:
        is_new_session = thread_ts not in self._sessions
        if is_new_session:
            system_prompt = self._config.claude_system_prompt
            mcp_servers = self._mcp_servers if include_mcp else {}
            if mcp_servers:
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
            client = ClaudeSDKClient(
                options=ClaudeAgentOptions(
                    model=model or self._config.claude_model,
                    system_prompt=system_prompt,
                    permission_mode="bypassPermissions",
                    mcp_servers=mcp_servers,
                )
            )
            await client.connect()
            self._sessions[thread_ts] = SessionEntry(
                client=client, last_accessed=time.time()
            )

        query_text = text
        if is_new_session and thread_context is not None:
            query_text = thread_context + "\n\n" + text

        entry = self._sessions[thread_ts]
        async with entry.lock:
            entry.last_accessed = time.time()
            try:
                await entry.client.query(query_text)
                response_parts: list[str] = []
                async for msg in entry.client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                response_parts.append(block.text)
                    elif isinstance(msg, ResultMessage):
                        break
                return "".join(response_parts)
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

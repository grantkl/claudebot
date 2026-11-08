"""Per-user memory MCP server.

A thin in-process proxy in front of the shared mcp-memory-service
(``MEMORY_MCP_URL``, streamable-HTTP JSON-RPC). Sessions never talk to the
shared server directly. Every call is scoped to the requesting user's tag (their
Slack user ID, the tag convention memories already use):

- memory_store always adds the requester's tag and refuses tags that name
  another scope (another Slack user ID or the system scope).
- memory_search / memory_list always filter with the requester's tag
  (``tag_match="all"``, so extra tags only narrow the result).
- memory_delete only deletes a memory (by hash) tagged to the requester.

Turns without a user (legacy ownerless scheduler tasks) use SYSTEM_SCOPE.
No other upstream tool is exposed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

SYSTEM_SCOPE = "scope-system"

# Tags that look like a Slack user ID (U…/W…, alphanumeric with a digit).
_USER_ID_TAG_RE = re.compile(r"^[uw](?=[a-z0-9]*\d)[a-z0-9]{6,}$")
_LIST_PAGE_SIZE = 100

_TAGS_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {"type": "array", "items": {"type": "string"}},
        {"type": "string", "description": "Comma-separated tags"},
    ],
}


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _requester() -> str | None:
    """Slack user ID of the user whose turn is calling the tool."""
    from ..claude_client import current_user_id

    return current_user_id()


def _split_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("["):
            try:
                raw = json.loads(stripped)
            except ValueError:
                raw = [stripped]
        else:
            raw = stripped.split(",")
    if not isinstance(raw, list):
        raise ValueError("tags must be a list of strings or a comma-separated string")
    return [str(t).strip() for t in raw if str(t).strip()]


def _scoped_tags(raw: Any, scope: str, *, for_store: bool = False) -> list[str]:
    """Return ``[scope, *extra_tags]`` with duplicates of *scope* removed.

    For stores, reject extra tags that name another scope so a user can't
    plant memories in someone else's (or the system) scope.
    """
    tags = [scope]
    seen = {scope.lower()}
    for tag in _split_tags(raw):
        low = tag.lower()
        if low in seen:
            continue
        if for_store and (low == SYSTEM_SCOPE or _USER_ID_TAG_RE.match(low)):
            raise ValueError(
                f"Tag '{tag}' looks like another user's ID and can't be used; "
                "memories are scoped to the current user automatically."
            )
        seen.add(low)
        tags.append(tag)
    return tags


def _parse_rpc_response(content_type: str, body: str) -> dict[str, Any]:
    """Turn a JSON-RPC tools/call response (JSON or SSE) into a tool result."""
    if "text/event-stream" in content_type:
        message: dict[str, Any] = {}
        for line in body.splitlines():
            if line.startswith("data:"):
                candidate = json.loads(line[5:].strip())
                if "result" in candidate or "error" in candidate:
                    message = candidate
    else:
        message = json.loads(body)
    if "error" in message:
        return _error(f"Memory service error: {message['error'].get('message', message['error'])}")
    result = message.get("result") or {}
    return {"content": result.get("content", []), "is_error": bool(result.get("isError"))}


async def _call_upstream(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool on the shared memory server (stateless streamable HTTP)."""
    import httpx

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                os.environ["MEMORY_MCP_URL"], json=payload,
                headers={"Accept": "application/json, text/event-stream"},
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Memory service call %s failed: %s", name, e)
        return _error(f"Memory service unavailable: {e}")
    return _parse_rpc_response(resp.headers.get("content-type", ""), resp.text)


def _scope() -> str:
    return _requester() or SYSTEM_SCOPE


@tool(
    "memory_store",
    "Store a memory for the current user. Tags (array or comma-separated string)"
    " categorize it; the user's scope tag is added automatically. Use"
    " conversation_id to bypass semantic deduplication for incremental notes.",
    {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The memory content to store."},
            "conversation_id": {"type": "string", "description": "Optional; skips semantic deduplication."},
            "metadata": {
                "type": "object",
                "properties": {
                    "tags": _TAGS_SCHEMA,
                    "type": {"type": "string", "description": "Optional memory type, e.g. note, observation, decision."},
                },
            },
        },
        "required": ["content"],
    },
)
async def memory_store(args: dict[str, Any]) -> dict[str, Any]:
    content = args.get("content")
    if not content:
        return _error("content is required")
    metadata = args.get("metadata") or {}
    try:
        tags = _scoped_tags(metadata.get("tags", args.get("tags")), _scope(), for_store=True)
    except ValueError as e:
        return _error(str(e))
    upstream_meta: dict[str, Any] = {"tags": tags}
    if metadata.get("type"):
        upstream_meta["type"] = metadata["type"]
    upstream: dict[str, Any] = {"content": content, "metadata": upstream_meta}
    if args.get("conversation_id"):
        upstream["conversation_id"] = args["conversation_id"]
    return await _call_upstream("memory_store", upstream)


_SEARCH_FIELDS = ("query", "mode", "time_expr", "after", "before", "limit")


@tool(
    "memory_search",
    "Search the current user's memories. Modes: semantic (default), exact"
    " (substring), hybrid, ranked. Optional tags narrow the results (a memory"
    " must have ALL given tags); results are always limited to the current user.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (required for semantic/exact modes)."},
            "mode": {"type": "string", "enum": ["semantic", "exact", "hybrid", "ranked"]},
            "tags": _TAGS_SCHEMA,
            "time_expr": {"type": "string", "description": "Natural language time filter, e.g. 'last week'."},
            "after": {"type": "string", "description": "Created after this date (YYYY-MM-DD)."},
            "before": {"type": "string", "description": "Created before this date (YYYY-MM-DD)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
)
async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
    upstream = {k: args[k] for k in _SEARCH_FIELDS if k in args}
    try:
        upstream["tags"] = _scoped_tags(args.get("tags"), _scope())
    except ValueError as e:
        return _error(str(e))
    upstream["tag_match"] = "all"
    return await _call_upstream("memory_search", upstream)


_LIST_FIELDS = ("page", "page_size", "memory_type")


@tool(
    "memory_list",
    "List the current user's memories with pagination. Optional tags narrow the"
    " results (a memory must have ALL given tags).",
    {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "minimum": 1},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
            "tags": _TAGS_SCHEMA,
            "memory_type": {"type": "string"},
        },
    },
)
async def memory_list(args: dict[str, Any]) -> dict[str, Any]:
    upstream = {k: args[k] for k in _LIST_FIELDS if k in args}
    try:
        upstream["tags"] = _scoped_tags(args.get("tags"), _scope())
    except ValueError as e:
        return _error(str(e))
    upstream["tag_match"] = "all"
    return await _call_upstream("memory_list", upstream)


async def _owns(content_hash: str, scope: str) -> bool:
    """True if *content_hash* is one of *scope*'s memories."""
    page = 1
    while True:
        result = await _call_upstream("memory_list", {
            "page": page, "page_size": _LIST_PAGE_SIZE, "tags": [scope], "tag_match": "all",
        })
        if result.get("is_error"):
            return False
        try:
            data = json.loads(result["content"][0]["text"])
        except (KeyError, IndexError, ValueError):
            return False
        if any(m.get("content_hash") == content_hash for m in data.get("memories", [])):
            return True
        if not data.get("has_more"):
            return False
        page += 1


@tool(
    "memory_delete",
    "Delete one of the current user's memories by its content hash.",
    {
        "type": "object",
        "properties": {
            "content_hash": {"type": "string", "description": "Hash of the memory to delete."},
        },
        "required": ["content_hash"],
    },
)
async def memory_delete(args: dict[str, Any]) -> dict[str, Any]:
    content_hash = args.get("content_hash")
    if not content_hash:
        return _error("content_hash is required")
    if not await _owns(content_hash, _scope()):
        return _error(f"No memory {content_hash} found for the current user.")
    return await _call_upstream("memory_delete", {"content_hash": content_hash})


MEMORY_TOOLS: list[SdkMcpTool] = [
    memory_store,
    memory_search,
    memory_list,
    memory_delete,
]

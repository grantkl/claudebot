"""Tests for the per-user memory proxy MCP server (src.mcp.memory_server)."""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_sdk_mock() -> MagicMock:
    sdk = MagicMock()
    sdk.SdkMcpTool = MagicMock

    def _tool(name: str, description: str, schema: Any) -> Any:
        def decorator(fn: Any) -> Any:
            wrapper = MagicMock()
            wrapper.handler = fn
            wrapper.name = name
            wrapper.input_schema = schema
            return wrapper
        return decorator

    sdk.tool = _tool
    return sdk


sys.modules["claude_agent_sdk"] = _make_sdk_mock()
sys.modules.pop("src.mcp.memory_server", None)

from src.mcp import memory_server  # noqa: E402

importlib.reload(memory_server)

_store = memory_server.memory_store.handler
_search = memory_server.memory_search.handler
_list = memory_server.memory_list.handler
_delete = memory_server.memory_delete.handler

ME = "U0AUTH12345"
SUPER = "U011E9TPW84"


def _ok(text: str = "ok") -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": False}


def _is_error(result: dict[str, Any]) -> bool:
    return bool(result.get("is_error"))


def _as(user_id):
    return patch.object(memory_server, "_requester", return_value=user_id)


@pytest.fixture
def upstream():
    mock = AsyncMock(return_value=_ok())
    with patch.object(memory_server, "_call_upstream", mock):
        yield mock


class TestToolSurface:
    def test_only_scoped_tools_are_exposed(self):
        names = {t.name for t in memory_server.MEMORY_TOOLS}
        assert names == {"memory_store", "memory_search", "memory_list", "memory_delete"}

    def test_delete_schema_only_takes_content_hash(self):
        schema = memory_server.memory_delete.input_schema
        assert set(schema["properties"]) == {"content_hash"}


class TestStore:
    async def test_forces_requester_tag(self, upstream):
        with _as(ME):
            r = await _store({"content": "likes tea", "metadata": {"tags": "pref,drinks", "type": "note"}})
        assert not _is_error(r)
        name, args = upstream.call_args.args
        assert name == "memory_store"
        assert args["content"] == "likes tea"
        assert args["metadata"]["tags"][0] == ME
        assert set(args["metadata"]["tags"]) == {ME, "pref", "drinks"}
        assert args["metadata"]["type"] == "note"

    async def test_no_tags_still_scoped(self, upstream):
        with _as(ME):
            await _store({"content": "x"})
        _, args = upstream.call_args.args
        assert args["metadata"]["tags"] == [ME]

    async def test_own_id_tag_is_deduplicated(self, upstream):
        with _as(SUPER):
            await _store({"content": "x", "metadata": {"tags": [SUPER.lower(), "trump_watch_state"]}})
        _, args = upstream.call_args.args
        assert args["metadata"]["tags"] == [SUPER, "trump_watch_state"]

    async def test_conversation_id_passed_through(self, upstream):
        with _as(ME):
            await _store({"content": "x", "conversation_id": "c1"})
        _, args = upstream.call_args.args
        assert args["conversation_id"] == "c1"

    @pytest.mark.parametrize("bad", [SUPER, SUPER.lower(), "W0ABCDEF12", memory_server.SYSTEM_SCOPE])
    async def test_rejects_other_scope_tags(self, upstream, bad):
        with _as(ME):
            r = await _store({"content": "poison", "metadata": {"tags": f"notes,{bad}"}})
        assert _is_error(r)
        upstream.assert_not_called()

    async def test_store_partition_not_forwarded(self, upstream):
        with _as(ME):
            await _store({"content": "x", "store": "docs"})
        _, args = upstream.call_args.args
        assert "store" not in args


class TestSearch:
    async def test_forces_scope_and_all_match(self, upstream):
        with _as(ME):
            await _search({
                "query": "q", "mode": "exact", "tags": ["trading"], "tag_match": "any",
                "limit": 5, "fallback": True, "include_beliefs": True,
                "store": "all", "entity": "x", "include_debug": True,
            })
        name, args = upstream.call_args.args
        assert name == "memory_search"
        assert args == {
            "query": "q", "mode": "exact", "tags": [ME, "trading"],
            "tag_match": "all", "limit": 5,
        }

    async def test_no_tags_scoped_to_requester(self, upstream):
        with _as(ME):
            await _search({"query": "q"})
        _, args = upstream.call_args.args
        assert args["tags"] == [ME]
        assert args["tag_match"] == "all"

    async def test_comma_string_tags(self, upstream):
        with _as(SUPER):
            await _search({"query": "last_seen_post_id", "mode": "exact",
                           "tags": f"{SUPER},trump_watch_state", "limit": 1})
        _, args = upstream.call_args.args
        assert args["tags"] == [SUPER, "trump_watch_state"]

    async def test_time_filters_forwarded(self, upstream):
        with _as(ME):
            await _search({"time_expr": "last week", "after": "2026-01-01", "before": "2026-02-01"})
        _, args = upstream.call_args.args
        assert args["time_expr"] == "last week"
        assert args["after"] == "2026-01-01"
        assert args["before"] == "2026-02-01"

    async def test_no_requester_uses_system_scope(self, upstream):
        with _as(None):
            await _search({"query": "q"})
        _, args = upstream.call_args.args
        assert args["tags"] == [memory_server.SYSTEM_SCOPE]


class TestList:
    async def test_forces_scope(self, upstream):
        with _as(ME):
            await _list({"page": 2, "page_size": 50, "tags": ["x"], "tag_match": "any", "store": "all"})
        name, args = upstream.call_args.args
        assert name == "memory_list"
        assert args == {"page": 2, "page_size": 50, "tags": [ME, "x"], "tag_match": "all"}


def _list_page(hashes, has_more=False):
    return _ok(json.dumps({
        "memories": [{"content_hash": h, "tags": ["x"]} for h in hashes],
        "has_more": has_more,
    }))


class TestDelete:
    async def test_deletes_owned_memory(self):
        calls: list = []

        async def fake(name, args):
            calls.append((name, args))
            if name == "memory_list":
                return _list_page(["aaa", "mine"]) if args["page"] == 2 else _list_page(["zzz"], has_more=True)
            return _ok("Deleted 1 memories")

        with _as(ME), patch.object(memory_server, "_call_upstream", side_effect=fake):
            r = await _delete({"content_hash": "mine"})
        assert not _is_error(r)
        list_calls = [a for n, a in calls if n == "memory_list"]
        assert all(a["tags"] == [ME] and a["tag_match"] == "all" for a in list_calls)
        assert ("memory_delete", {"content_hash": "mine"}) in calls

    async def test_refuses_memory_not_tagged_to_requester(self):
        calls: list = []

        async def fake(name, args):
            calls.append((name, args))
            if name == "memory_list":
                return _list_page(["aaa"])
            return _ok("Deleted 1 memories")

        with _as(ME), patch.object(memory_server, "_call_upstream", side_effect=fake):
            r = await _delete({"content_hash": "superuser-hash", "tags": [SUPER]})
        assert _is_error(r)
        assert not any(n == "memory_delete" for n, _ in calls)

    async def test_requires_content_hash(self, upstream):
        with _as(ME):
            r = await _delete({"tags": [ME]})
        assert _is_error(r)
        upstream.assert_not_called()


class TestUpstreamParsing:
    def test_parses_sse_response(self):
        body = (
            "event: message\n"
            'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"hi"}],"isError":false}}\n\n'
        )
        result = memory_server._parse_rpc_response("text/event-stream", body)
        assert result == {"content": [{"type": "text", "text": "hi"}], "is_error": False}

    def test_parses_json_response(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": "x"}], "isError": True}})
        result = memory_server._parse_rpc_response("application/json", body)
        assert result == {"content": [{"type": "text", "text": "x"}], "is_error": True}

    def test_rpc_error_becomes_tool_error(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}})
        result = memory_server._parse_rpc_response("application/json", body)
        assert result["is_error"] is True
        assert "bad" in result["content"][0]["text"]

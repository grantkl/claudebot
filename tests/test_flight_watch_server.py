"""Tests for the Flight Watch MCP server tools."""

from __future__ import annotations

import datetime
import json
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# Build a mock claude_agent_sdk with a working @tool decorator
def _make_sdk_mock() -> MagicMock:
    sdk = MagicMock()
    sdk.SdkMcpTool = MagicMock

    def _tool(name: str, description: str, schema: Any) -> Any:
        def decorator(fn: Any) -> Any:
            wrapper = MagicMock()
            wrapper.handler = fn
            wrapper.__name__ = fn.__name__
            return wrapper
        return decorator

    sdk.tool = _tool
    return sdk


# Force our mock with a working @tool decorator
sys.modules["claude_agent_sdk"] = _make_sdk_mock()
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())

import importlib  # noqa: E402
sys.modules.pop("src.mcp.flight_watch_server", None)

from src.mcp import flight_watch_server  # noqa: E402

importlib.reload(flight_watch_server)

# Access underlying async handlers
_add = flight_watch_server.flight_watch_add.handler
_list = flight_watch_server.flight_watch_list.handler
_remove = flight_watch_server.flight_watch_remove.handler
_record = flight_watch_server.flight_watch_record.handler
_history = flight_watch_server.flight_watch_history.handler


def _parse_text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


@pytest.fixture(autouse=True)
def _reset_store():
    flight_watch_server._store = None
    yield
    flight_watch_server._store = None


def _future_date(days: int = 30) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _past_date(days: int = 1) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# TestFlightWatchAdd
# ---------------------------------------------------------------------------
class TestFlightWatchAdd:
    @pytest.mark.asyncio
    async def test_add_valid_watch(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
        assert not _is_error(result)
        assert "watch_1" in _parse_text(result)
        assert "SEA" in _parse_text(result)
        assert "NRT" in _parse_text(result)

    @pytest.mark.asyncio
    async def test_add_with_return_date_and_max_price(self, tmp_path):
        f = str(tmp_path / "watches.json")
        dep = _future_date(30)
        ret = _future_date(45)
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _add({
                "origin": "sea",
                "destination": "nrt",
                "departure_date": dep,
                "return_date": ret,
                "max_price": 800,
            })
        assert not _is_error(result)
        # Verify data stored correctly
        with open(f) as fh:
            data = json.load(fh)
        watch = data["watches"]["watch_1"]
        assert watch["origin"] == "SEA"
        assert watch["destination"] == "NRT"
        assert watch["return_date"] == ret
        assert watch["max_price"] == 800
        assert watch["active"] is True

    @pytest.mark.asyncio
    async def test_rejects_past_departure_date(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _past_date(),
            })
        assert _is_error(result)
        assert "future" in _parse_text(result).lower()

    @pytest.mark.asyncio
    async def test_rejects_return_before_departure(self, tmp_path):
        f = str(tmp_path / "watches.json")
        dep = _future_date(30)
        ret = _future_date(15)
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": dep,
                "return_date": ret,
            })
        assert _is_error(result)
        assert "return date" in _parse_text(result).lower()

    @pytest.mark.asyncio
    async def test_add_with_airline_and_flight_numbers(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
                "airline": "Alaska",
                "flight_numbers": ["AS275", "AS276"],
            })
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        watch = data["watches"]["watch_1"]
        assert watch["airline"] == "Alaska"
        assert watch["flight_numbers"] == ["AS275", "AS276"]

    @pytest.mark.asyncio
    async def test_add_without_airline_defaults_to_none(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
        assert not _is_error(result)
        with open(f) as fh:
            data = json.load(fh)
        watch = data["watches"]["watch_1"]
        assert watch["airline"] is None
        assert watch["flight_numbers"] is None

    @pytest.mark.asyncio
    async def test_id_increments(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            r1 = await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
            r2 = await _add({
                "origin": "LAX",
                "destination": "LHR",
                "departure_date": _future_date(60),
            })
        assert "watch_1" in _parse_text(r1)
        assert "watch_2" in _parse_text(r2)


# ---------------------------------------------------------------------------
# TestFlightWatchList
# ---------------------------------------------------------------------------
class TestFlightWatchList:
    @pytest.mark.asyncio
    async def test_empty_list(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _list({})
        assert not _is_error(result)
        data = json.loads(_parse_text(result))
        assert data == []

    @pytest.mark.asyncio
    async def test_lists_active_watches(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
            result = await _list({})
        data = json.loads(_parse_text(result))
        assert len(data) == 1
        assert data[0]["id"] == "watch_1"
        assert data[0]["origin"] == "SEA"

    @pytest.mark.asyncio
    async def test_excludes_inactive(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
            # Manually deactivate watch
            store = flight_watch_server._get_store()
            store.watches["watch_1"]["active"] = False
            store._save()
            result = await _list({})
        data = json.loads(_parse_text(result))
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_include_inactive_flag(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
            store = flight_watch_server._get_store()
            store.watches["watch_1"]["active"] = False
            store._save()
            result = await _list({"include_inactive": True})
        data = json.loads(_parse_text(result))
        assert len(data) == 1
        assert data[0]["active"] is False

    @pytest.mark.asyncio
    async def test_auto_deactivates_past_watches(self, tmp_path):
        f = str(tmp_path / "watches.json")
        # Seed data with a past departure date directly
        past_data = {
            "watches": {
                "watch_1": {
                    "origin": "SEA",
                    "destination": "NRT",
                    "departure_date": _past_date(),
                    "return_date": None,
                    "max_price": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "active": True,
                    "price_history": [],
                }
            },
            "next_id": 2,
        }
        with open(f, "w") as fh:
            json.dump(past_data, fh)
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _list({})
        # Active watches only — past-date should be auto-deactivated
        data = json.loads(_parse_text(result))
        assert len(data) == 0
        # Verify it was deactivated in the file
        with open(f) as fh:
            saved = json.load(fh)
        assert saved["watches"]["watch_1"]["active"] is False

    @pytest.mark.asyncio
    async def test_list_includes_airline_and_flight_numbers(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
                "airline": "Alaska",
                "flight_numbers": ["AS275"],
            })
            result = await _list({})
        data = json.loads(_parse_text(result))
        assert len(data) == 1
        assert data[0]["airline"] == "Alaska"
        assert data[0]["flight_numbers"] == ["AS275"]

    @pytest.mark.asyncio
    async def test_list_shows_last_price(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
            await _record({
                "watch_id": "watch_1",
                "lowest_price": 750,
                "currency": "USD",
                "airline": "Alaska",
            })
            result = await _list({})
        data = json.loads(_parse_text(result))
        assert data[0]["last_price"] == 750
        assert data[0]["last_currency"] == "USD"


# ---------------------------------------------------------------------------
# TestFlightWatchRemove
# ---------------------------------------------------------------------------
class TestFlightWatchRemove:
    @pytest.mark.asyncio
    async def test_removes_watch(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
            result = await _remove({"watch_id": "watch_1"})
        assert not _is_error(result)
        assert "removed" in _parse_text(result).lower()
        with open(f) as fh:
            data = json.load(fh)
        assert "watch_1" not in data["watches"]

    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _remove({"watch_id": "watch_99"})
        assert _is_error(result)
        assert "not found" in _parse_text(result).lower()


# ---------------------------------------------------------------------------
# TestFlightWatchRecord
# ---------------------------------------------------------------------------
class TestFlightWatchRecord:
    @pytest.mark.asyncio
    async def test_records_price(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
            result = await _record({
                "watch_id": "watch_1",
                "lowest_price": 650,
                "currency": "USD",
                "airline": "Delta",
                "flight_numbers": "DL123",
                "details": "1 stop, 14h",
            })
        assert not _is_error(result)
        assert "$650" in _parse_text(result)
        with open(f) as fh:
            data = json.load(fh)
        hist = data["watches"]["watch_1"]["price_history"]
        assert len(hist) == 1
        assert hist[0]["lowest_price"] == 650
        assert hist[0]["airline"] == "Delta"
        assert hist[0]["flight_numbers"] == "DL123"

    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _record({
                "watch_id": "watch_99",
                "lowest_price": 100,
            })
        assert _is_error(result)
        assert "not found" in _parse_text(result).lower()

    @pytest.mark.asyncio
    async def test_caps_history_at_max(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
            # Record 55 prices — should be capped at 50
            for i in range(55):
                await _record({
                    "watch_id": "watch_1",
                    "lowest_price": 500 + i,
                })
        with open(f) as fh:
            data = json.load(fh)
        hist = data["watches"]["watch_1"]["price_history"]
        assert len(hist) == 50
        # Oldest entries should have been trimmed — first remaining is price 505
        assert hist[0]["lowest_price"] == 505
        assert hist[-1]["lowest_price"] == 554


# ---------------------------------------------------------------------------
# TestFlightWatchHistory
# ---------------------------------------------------------------------------
class TestFlightWatchHistory:
    @pytest.mark.asyncio
    async def test_empty_history(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
            result = await _history({"watch_id": "watch_1"})
        assert not _is_error(result)
        data = json.loads(_parse_text(result))
        assert data["watch_id"] == "watch_1"
        assert data["price_history"] == []

    @pytest.mark.asyncio
    async def test_returns_recorded_prices(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
            await _record({"watch_id": "watch_1", "lowest_price": 700})
            await _record({"watch_id": "watch_1", "lowest_price": 650})
            result = await _history({"watch_id": "watch_1"})
        data = json.loads(_parse_text(result))
        assert len(data["price_history"]) == 2
        assert data["price_history"][0]["lowest_price"] == 700
        assert data["price_history"][1]["lowest_price"] == 650

    @pytest.mark.asyncio
    async def test_history_includes_airline_and_flight_numbers(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
                "airline": "Alaska",
                "flight_numbers": ["AS275"],
            })
            result = await _history({"watch_id": "watch_1"})
        data = json.loads(_parse_text(result))
        assert data["airline"] == "Alaska"
        assert data["flight_numbers"] == ["AS275"]

    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _history({"watch_id": "watch_99"})
        assert _is_error(result)
        assert "not found" in _parse_text(result).lower()


# ---------------------------------------------------------------------------
# TestFlightWatchStore
# ---------------------------------------------------------------------------
class TestFlightWatchStore:
    @pytest.mark.asyncio
    async def test_persistence_round_trip(self, tmp_path):
        f = str(tmp_path / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
        # Reset the store to force re-read from disk
        flight_watch_server._store = None
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _list({})
        data = json.loads(_parse_text(result))
        assert len(data) == 1
        assert data[0]["origin"] == "SEA"

    @pytest.mark.asyncio
    async def test_deactivate_past_dates(self, tmp_path):
        f = str(tmp_path / "watches.json")
        past_data = {
            "watches": {
                "watch_1": {
                    "origin": "SEA",
                    "destination": "NRT",
                    "departure_date": _past_date(5),
                    "return_date": None,
                    "max_price": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "active": True,
                    "price_history": [],
                },
                "watch_2": {
                    "origin": "LAX",
                    "destination": "LHR",
                    "departure_date": _future_date(),
                    "return_date": None,
                    "max_price": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "active": True,
                    "price_history": [],
                },
            },
            "next_id": 3,
        }
        with open(f, "w") as fh:
            json.dump(past_data, fh)
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            store = flight_watch_server._get_store()
            store.deactivate_past_watches()
        assert store.watches["watch_1"]["active"] is False
        assert store.watches["watch_2"]["active"] is True

    @pytest.mark.asyncio
    async def test_creates_parent_directory(self, tmp_path):
        f = str(tmp_path / "subdir" / "watches.json")
        with patch.dict("os.environ", {"FLIGHT_WATCH_FILE": f}):
            result = await _add({
                "origin": "SEA",
                "destination": "NRT",
                "departure_date": _future_date(),
            })
        assert not _is_error(result)
        assert (tmp_path / "subdir" / "watches.json").exists()

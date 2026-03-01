"""Tests for the Skyscanner flights MCP server tools."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from enum import Enum
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


sys.modules.setdefault("claude_agent_sdk", _make_sdk_mock())


# Build mock skyscanner types that behave like the real ones
@dataclass(frozen=True)
class _MockAirport:
    title: str
    entity_id: str
    skyId: str


class _MockCabinClass(Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class _MockSpecialTypes(Enum):
    ANYTIME = "anytime"
    EVERYWHERE = "everywhere"


# Wire up mock skyscanner module hierarchy
_mock_skyscanner_types = MagicMock()
_mock_skyscanner_types.Airport = _MockAirport
_mock_skyscanner_types.CabinClass = _MockCabinClass
_mock_skyscanner_types.SpecialTypes = _MockSpecialTypes

_mock_skyscanner_skyscanner = MagicMock()
_mock_skyscanner_skyscanner.BannedWithCaptcha = type("BannedWithCaptcha", (Exception,), {})
_mock_skyscanner_skyscanner.AttemptsExhaustedIncompleteResponse = type(
    "AttemptsExhaustedIncompleteResponse", (Exception,), {}
)
_mock_skyscanner_skyscanner.GenericError = type("GenericError", (Exception,), {})

_mock_skyscanner_pkg = MagicMock()
_mock_skyscanner_pkg.SkyScanner = MagicMock
_mock_skyscanner_pkg.types = _mock_skyscanner_types
_mock_skyscanner_pkg.skyscanner = _mock_skyscanner_skyscanner

sys.modules.setdefault("skyscanner", _mock_skyscanner_pkg)
sys.modules.setdefault("skyscanner.types", _mock_skyscanner_types)
sys.modules.setdefault("skyscanner.skyscanner", _mock_skyscanner_skyscanner)

from src.mcp import flights_server  # noqa: E402

# Access the underlying async handlers via .handler attribute
_search_airports = flights_server.flights_search_airports.handler
_search_flights = flights_server.flights_search_flights.handler


def _parse_text(result: dict[str, Any]) -> str:
    """Extract text content from a tool result."""
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


# ---------------------------------------------------------------------------
# flights_search_airports
# ---------------------------------------------------------------------------
class TestSearchAirports:
    @patch.object(flights_server, "_scanner", None)
    @patch("src.mcp.flights_server._get_scanner")
    async def test_returns_airports(self, mock_get_scanner: MagicMock) -> None:
        scanner = MagicMock()
        mock_get_scanner.return_value = scanner

        airports = [
            _MockAirport(title="San Francisco International", entity_id="95673635", skyId="SFO"),
            _MockAirport(title="Oakland International", entity_id="95673636", skyId="OAK"),
        ]
        scanner.search_airports.return_value = airports

        result = await _search_airports({"query": "San Francisco"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert len(data) == 2
        assert data[0]["name"] == "San Francisco International"
        assert data[0]["code"] == "SFO"
        assert data[0]["entity_id"] == "95673635"

    @patch.object(flights_server, "_scanner", None)
    @patch("src.mcp.flights_server._get_scanner")
    async def test_empty_results(self, mock_get_scanner: MagicMock) -> None:
        scanner = MagicMock()
        mock_get_scanner.return_value = scanner
        scanner.search_airports.return_value = []

        result = await _search_airports({"query": "xyznonexistent"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data == []

    @patch.object(flights_server, "_scanner", None)
    @patch("src.mcp.flights_server._get_scanner")
    async def test_error_handling(self, mock_get_scanner: MagicMock) -> None:
        mock_get_scanner.side_effect = Exception("Connection failed")

        result = await _search_airports({"query": "London"})
        assert _is_error(result)
        assert "Failed to search airports" in _parse_text(result)


# ---------------------------------------------------------------------------
# flights_search_flights
# ---------------------------------------------------------------------------
class TestSearchFlights:
    @patch.object(flights_server, "_scanner", None)
    @patch("src.mcp.flights_server._get_scanner")
    async def test_returns_flights(self, mock_get_scanner: MagicMock) -> None:
        scanner = MagicMock()
        mock_get_scanner.return_value = scanner

        origin = _MockAirport(title="JFK", entity_id="1", skyId="JFK")
        dest = _MockAirport(title="LAX", entity_id="2", skyId="LAX")
        scanner.get_airport_by_code.side_effect = lambda code: origin if code == "JFK" else dest

        mock_response = MagicMock()
        mock_response.json = {
            "itineraries": {
                "buckets": [
                    {
                        "id": "Best",
                        "items": [
                            {
                                "id": "itin1",
                                "price": {"raw": 299.0, "formatted": "$299"},
                                "legs": [
                                    {
                                        "origin": {"name": "New York JFK"},
                                        "destination": {"name": "Los Angeles LAX"},
                                        "departure": "2026-06-15T08:00:00",
                                        "arrival": "2026-06-15T11:30:00",
                                        "durationInMinutes": 330,
                                        "stopCount": 0,
                                        "carriers": {"marketing": [{"name": "Delta"}]},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "id": "Cheapest",
                        "items": [
                            {
                                "id": "itin2",
                                "price": {"raw": 199.0, "formatted": "$199"},
                                "legs": [],
                            },
                        ],
                    },
                ],
            },
        }
        scanner.get_flight_prices.return_value = mock_response

        result = await _search_flights({
            "origin": "JFK",
            "destination": "LAX",
            "depart_date": "2026-06-15",
        })

        assert not _is_error(result)
        data = json.loads(_parse_text(result))
        assert len(data) == 2
        assert data[0]["bucket"] == "Best"
        assert data[0]["total_items"] == 1
        assert data[0]["top_items"][0]["price"] == "$299"
        assert data[1]["bucket"] == "Cheapest"

    @patch.object(flights_server, "_scanner", None)
    @patch("src.mcp.flights_server._get_scanner")
    async def test_invalid_date(self, mock_get_scanner: MagicMock) -> None:
        scanner = MagicMock()
        mock_get_scanner.return_value = scanner

        result = await _search_flights({
            "origin": "JFK",
            "destination": "LAX",
            "depart_date": "not-a-date",
        })
        assert _is_error(result)
        assert "Invalid departure date" in _parse_text(result)

    @patch.object(flights_server, "_scanner", None)
    @patch("src.mcp.flights_server._get_scanner")
    async def test_airport_not_found(self, mock_get_scanner: MagicMock) -> None:
        scanner = MagicMock()
        mock_get_scanner.return_value = scanner
        scanner.get_airport_by_code.side_effect = Exception("Airport not found")

        result = await _search_flights({
            "origin": "XYZ",
            "destination": "LAX",
            "depart_date": "2026-06-15",
        })
        assert _is_error(result)
        assert "Could not find airport" in _parse_text(result)

    @patch.object(flights_server, "_scanner", None)
    @patch("src.mcp.flights_server._get_scanner")
    async def test_captcha_error(self, mock_get_scanner: MagicMock) -> None:
        scanner = MagicMock()
        mock_get_scanner.return_value = scanner

        origin = _MockAirport(title="JFK", entity_id="1", skyId="JFK")
        dest = _MockAirport(title="LAX", entity_id="2", skyId="LAX")
        scanner.get_airport_by_code.side_effect = lambda code: origin if code == "JFK" else dest
        scanner.get_flight_prices.side_effect = _mock_skyscanner_skyscanner.BannedWithCaptcha(
            "Captcha required"
        )

        result = await _search_flights({
            "origin": "JFK",
            "destination": "LAX",
            "depart_date": "2026-06-15",
        })
        assert _is_error(result)
        assert "rate-limited" in _parse_text(result).lower()

    @patch.object(flights_server, "_scanner", None)
    @patch("src.mcp.flights_server._get_scanner")
    async def test_timeout_error(self, mock_get_scanner: MagicMock) -> None:
        scanner = MagicMock()
        mock_get_scanner.return_value = scanner

        origin = _MockAirport(title="JFK", entity_id="1", skyId="JFK")
        dest = _MockAirport(title="LAX", entity_id="2", skyId="LAX")
        scanner.get_airport_by_code.side_effect = lambda code: origin if code == "JFK" else dest
        scanner.get_flight_prices.side_effect = (
            _mock_skyscanner_skyscanner.AttemptsExhaustedIncompleteResponse("Timeout")
        )

        result = await _search_flights({
            "origin": "JFK",
            "destination": "LAX",
            "depart_date": "2026-06-15",
        })
        assert _is_error(result)
        assert "timed out" in _parse_text(result).lower()

    @patch.object(flights_server, "_scanner", None)
    @patch("src.mcp.flights_server._get_scanner")
    async def test_anytime_date(self, mock_get_scanner: MagicMock) -> None:
        scanner = MagicMock()
        mock_get_scanner.return_value = scanner

        origin = _MockAirport(title="JFK", entity_id="1", skyId="JFK")
        dest = _MockAirport(title="LAX", entity_id="2", skyId="LAX")
        scanner.get_airport_by_code.side_effect = lambda code: origin if code == "JFK" else dest

        mock_response = MagicMock()
        mock_response.json = {"itineraries": {"buckets": []}}
        scanner.get_flight_prices.return_value = mock_response

        result = await _search_flights({
            "origin": "JFK",
            "destination": "LAX",
            "depart_date": "anytime",
        })

        assert not _is_error(result)

    @patch.object(flights_server, "_scanner", None)
    @patch("src.mcp.flights_server._get_scanner")
    async def test_everywhere_destination(self, mock_get_scanner: MagicMock) -> None:
        scanner = MagicMock()
        mock_get_scanner.return_value = scanner

        origin = _MockAirport(title="JFK", entity_id="1", skyId="JFK")
        scanner.get_airport_by_code.return_value = origin

        mock_response = MagicMock()
        mock_response.json = {"itineraries": {"buckets": []}}
        scanner.get_flight_prices.return_value = mock_response

        result = await _search_flights({
            "origin": "JFK",
            "destination": "everywhere",
            "depart_date": "2026-06-15",
        })

        assert not _is_error(result)
        # Should not have called get_airport_by_code for destination
        assert scanner.get_airport_by_code.call_count == 1

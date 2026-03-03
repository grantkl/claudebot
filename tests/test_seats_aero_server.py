"""Tests for the seats.aero MCP server tools."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch



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

import importlib  # noqa: E402

sys.modules.pop("src.mcp.seats_aero_server", None)

from src.mcp import seats_aero_server  # noqa: E402

importlib.reload(seats_aero_server)

# Access underlying async handlers
_award_search = seats_aero_server.award_search.handler
_award_search_live = seats_aero_server.award_search_live.handler
_award_trip_details = seats_aero_server.award_trip_details.handler


def _parse_text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


def _mock_response(status_code: int = 200, json_data: Any = None, text: str = "") -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


def _make_client(method: str = "get", response: MagicMock = None, side_effect: Exception = None) -> AsyncMock:
    """Create a mock httpx.AsyncClient as async context manager."""
    mock_client = AsyncMock()
    mock_method = AsyncMock(return_value=response, side_effect=side_effect)
    setattr(mock_client, method, mock_method)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _sample_availability(count: int = 1, has_more: bool = False, cursor: str = None) -> dict[str, Any]:
    """Build a sample availability response."""
    results = []
    for i in range(count):
        results.append({
            "Route": "SEA-NRT",
            "Date": f"2026-06-{10 + i:02d}",
            "Source": "united",
            "Airlines": "NH",
            "Direct": i == 0,
            "JAvailable": True,
            "JMileageCost": "88000",
            "JRemaining": "2",
            "YAvailable": False,
            "WAvailable": False,
            "FAvailable": False,
        })
    data: dict[str, Any] = {"data": results}
    if has_more:
        data["hasMore"] = True
    if cursor:
        data["cursor"] = cursor
    return data


def _sample_trip() -> dict[str, Any]:
    return {
        "ID": "trip-abc-123",
        "Route": "SEA-NRT",
        "Date": "2026-06-10",
        "Source": "united",
        "Airlines": "NH",
        "JAvailable": True,
        "JMileageCost": "88000",
        "JRemaining": "2",
        "YAvailable": False,
        "WAvailable": False,
        "FAvailable": False,
        "Segments": [
            {
                "DepartureAirport": "SEA",
                "ArrivalAirport": "NRT",
                "DepartureTime": "2026-06-10T11:00:00",
                "ArrivalTime": "2026-06-11T14:00:00",
                "FlightNumber": "NH178",
                "Airline": "NH",
            }
        ],
        "BookingLink": "https://example.com/book",
    }


# ---------------------------------------------------------------------------
# TestAwardSearch
# ---------------------------------------------------------------------------
class TestAwardSearch:
    async def test_missing_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            result = await _award_search({"origin": "SEA", "destination": "NRT"})
        assert _is_error(result)
        assert "SEATS_AERO_API_KEY" in _parse_text(result)

    async def test_successful_search(self):
        client = _make_client("get", _mock_response(200, _sample_availability(2)))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search({"origin": "sea", "destination": "nrt"})

        assert not _is_error(result)
        text = _parse_text(result)
        assert "Found 2 result(s)" in text
        assert "SEA-NRT" in text
        assert "J:88000mi" in text

        # Verify uppercase conversion in params
        call_kwargs = client.get.call_args
        assert call_kwargs.kwargs["params"]["origin"] == "SEA"
        assert call_kwargs.kwargs["params"]["destination"] == "NRT"

    async def test_empty_results(self):
        client = _make_client("get", _mock_response(200, {"data": []}))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search({"origin": "SEA", "destination": "NRT"})

        assert not _is_error(result)
        assert "No award availability found" in _parse_text(result)

    async def test_pagination_with_cursor(self):
        data = _sample_availability(1, has_more=True, cursor="next-page-abc")
        client = _make_client("get", _mock_response(200, data))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search({
                    "origin": "SEA",
                    "destination": "NRT",
                    "cursor": "prev-cursor",
                })

        assert not _is_error(result)
        text = _parse_text(result)
        assert "next-page-abc" in text
        assert "More results available" in text

        # Verify cursor was passed in request
        call_kwargs = client.get.call_args
        assert call_kwargs.kwargs["params"]["cursor"] == "prev-cursor"

    async def test_optional_params_forwarded(self):
        client = _make_client("get", _mock_response(200, {"data": []}))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                await _award_search({
                    "origin": "SEA",
                    "destination": "NRT",
                    "cabin": "business",
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-30",
                    "take": 50,
                })

        params = client.get.call_args.kwargs["params"]
        assert params["cabin"] == "business"
        assert params["start_date"] == "2026-06-01"
        assert params["end_date"] == "2026-06-30"
        assert params["take"] == "50"

    async def test_take_capped_at_100(self):
        client = _make_client("get", _mock_response(200, {"data": []}))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                await _award_search({
                    "origin": "SEA",
                    "destination": "NRT",
                    "take": 500,
                })

        params = client.get.call_args.kwargs["params"]
        assert params["take"] == "100"

    async def test_http_401(self):
        client = _make_client("get", _mock_response(401))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "bad-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search({"origin": "SEA", "destination": "NRT"})

        assert _is_error(result)
        assert "Invalid API key" in _parse_text(result)

    async def test_http_429(self):
        client = _make_client("get", _mock_response(429))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search({"origin": "SEA", "destination": "NRT"})

        assert _is_error(result)
        assert "Rate limited" in _parse_text(result)

    async def test_http_500(self):
        client = _make_client("get", _mock_response(500, text="Internal Server Error"))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search({"origin": "SEA", "destination": "NRT"})

        assert _is_error(result)
        assert "HTTP 500" in _parse_text(result)

    async def test_connection_error(self):
        import httpx as real_httpx

        client = _make_client("get", side_effect=real_httpx.ConnectError("Connection refused"))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search({"origin": "SEA", "destination": "NRT"})

        assert _is_error(result)
        assert "Could not connect" in _parse_text(result)

    async def test_timeout_error(self):
        import httpx as real_httpx

        client = _make_client("get", side_effect=real_httpx.TimeoutException("timed out"))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search({"origin": "SEA", "destination": "NRT"})

        assert _is_error(result)
        assert "timed out" in _parse_text(result).lower()

    async def test_invalid_cabin(self):
        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            result = await _award_search({
                "origin": "SEA",
                "destination": "NRT",
                "cabin": "luxury",
            })
        assert _is_error(result)
        assert "Invalid cabin" in _parse_text(result)


# ---------------------------------------------------------------------------
# TestAwardSearchLive
# ---------------------------------------------------------------------------
class TestAwardSearchLive:
    async def test_missing_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            result = await _award_search_live({
                "origin": "SEA",
                "destination": "NRT",
                "date": "2026-06-10",
                "source": "united",
            })
        assert _is_error(result)
        assert "SEATS_AERO_API_KEY" in _parse_text(result)

    async def test_successful_live_search(self):
        client = _make_client("post", _mock_response(200, _sample_availability(1)))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search_live({
                    "origin": "sea",
                    "destination": "nrt",
                    "date": "2026-06-10",
                    "source": "united",
                })

        assert not _is_error(result)
        text = _parse_text(result)
        assert "Found 1 result(s)" in text
        assert "SEA-NRT" in text

        # Verify uppercase conversion and correct payload
        call_kwargs = client.post.call_args
        payload = call_kwargs.kwargs["json"]
        assert payload["origin"] == "SEA"
        assert payload["destination"] == "NRT"
        assert payload["source"] == "united"
        assert payload["date"] == "2026-06-10"

    async def test_invalid_source(self):
        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            result = await _award_search_live({
                "origin": "SEA",
                "destination": "NRT",
                "date": "2026-06-10",
                "source": "invalidprogram",
            })
        assert _is_error(result)
        assert "Invalid source" in _parse_text(result)
        assert "invalidprogram" in _parse_text(result)

    async def test_source_case_insensitive(self):
        client = _make_client("post", _mock_response(200, {"data": []}))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search_live({
                    "origin": "SEA",
                    "destination": "NRT",
                    "date": "2026-06-10",
                    "source": "United",
                })

        assert not _is_error(result)

    async def test_with_cabin(self):
        client = _make_client("post", _mock_response(200, {"data": []}))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search_live({
                    "origin": "SEA",
                    "destination": "NRT",
                    "date": "2026-06-10",
                    "source": "united",
                    "cabin": "business",
                })

        assert not _is_error(result)
        payload = client.post.call_args.kwargs["json"]
        assert payload["cabin"] == "business"

    async def test_invalid_cabin(self):
        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            result = await _award_search_live({
                "origin": "SEA",
                "destination": "NRT",
                "date": "2026-06-10",
                "source": "united",
                "cabin": "luxury",
            })
        assert _is_error(result)
        assert "Invalid cabin" in _parse_text(result)

    async def test_uses_60s_timeout(self):
        client = _make_client("post", _mock_response(200, {"data": []}))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client) as mock_cls:
                await _award_search_live({
                    "origin": "SEA",
                    "destination": "NRT",
                    "date": "2026-06-10",
                    "source": "united",
                })

        # The AsyncClient should be instantiated with timeout=60
        mock_cls.assert_called_once_with(timeout=60)

    async def test_http_401(self):
        client = _make_client("post", _mock_response(401))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search_live({
                    "origin": "SEA",
                    "destination": "NRT",
                    "date": "2026-06-10",
                    "source": "united",
                })

        assert _is_error(result)
        assert "Invalid API key" in _parse_text(result)

    async def test_http_429(self):
        client = _make_client("post", _mock_response(429))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search_live({
                    "origin": "SEA",
                    "destination": "NRT",
                    "date": "2026-06-10",
                    "source": "united",
                })

        assert _is_error(result)
        assert "Rate limited" in _parse_text(result)

    async def test_timeout_mentions_60s(self):
        import httpx as real_httpx

        client = _make_client("post", side_effect=real_httpx.TimeoutException("timeout"))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search_live({
                    "origin": "SEA",
                    "destination": "NRT",
                    "date": "2026-06-10",
                    "source": "united",
                })

        assert _is_error(result)
        text = _parse_text(result)
        assert "timed out" in text.lower()
        assert "60s" in text

    async def test_connection_error(self):
        import httpx as real_httpx

        client = _make_client("post", side_effect=real_httpx.ConnectError("refused"))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_search_live({
                    "origin": "SEA",
                    "destination": "NRT",
                    "date": "2026-06-10",
                    "source": "united",
                })

        assert _is_error(result)
        assert "Could not connect" in _parse_text(result)


# ---------------------------------------------------------------------------
# TestAwardTripDetails
# ---------------------------------------------------------------------------
class TestAwardTripDetails:
    async def test_missing_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            result = await _award_trip_details({"trip_id": "trip-123"})
        assert _is_error(result)
        assert "SEATS_AERO_API_KEY" in _parse_text(result)

    async def test_empty_trip_id(self):
        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            result = await _award_trip_details({"trip_id": ""})
        assert _is_error(result)
        assert "trip_id is required" in _parse_text(result)

    async def test_successful_trip_details(self):
        trip = _sample_trip()
        client = _make_client("get", _mock_response(200, trip))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_trip_details({"trip_id": "trip-abc-123"})

        assert not _is_error(result)
        text = _parse_text(result)
        assert "trip-abc-123" in text
        assert "SEA-NRT" in text
        assert "NH178" in text
        assert "88000 miles" in text
        assert "https://example.com/book" in text

        # Verify URL construction
        call_args = client.get.call_args
        assert "trip-abc-123" in call_args.args[0]

    async def test_http_404(self):
        client = _make_client("get", _mock_response(404, text="Not Found"))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_trip_details({"trip_id": "nonexistent"})

        assert _is_error(result)
        assert "HTTP 404" in _parse_text(result)

    async def test_http_401(self):
        client = _make_client("get", _mock_response(401))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_trip_details({"trip_id": "trip-123"})

        assert _is_error(result)
        assert "Invalid API key" in _parse_text(result)

    async def test_connection_error(self):
        import httpx as real_httpx

        client = _make_client("get", side_effect=real_httpx.ConnectError("refused"))

        with patch.dict("os.environ", {"SEATS_AERO_API_KEY": "test-key"}):
            with patch("src.mcp.seats_aero_server.httpx.AsyncClient", return_value=client):
                result = await _award_trip_details({"trip_id": "trip-123"})

        assert _is_error(result)
        assert "Could not connect" in _parse_text(result)


# ---------------------------------------------------------------------------
# TestFormatHelpers
# ---------------------------------------------------------------------------
class TestFormatAvailability:
    def test_empty_results(self):
        text = seats_aero_server._format_availability({"data": []})
        assert "No award availability found" in text

    def test_single_result(self):
        data = _sample_availability(1)
        text = seats_aero_server._format_availability(data)
        assert "Found 1 result(s)" in text
        assert "SEA-NRT" in text
        assert "Direct" in text
        assert "J:88000mi" in text
        assert "united" in text

    def test_connecting_flight(self):
        data = _sample_availability(2)
        text = seats_aero_server._format_availability(data)
        # Second result has Direct=False
        assert "Connecting" in text

    def test_has_more_with_cursor(self):
        data = _sample_availability(1, has_more=True, cursor="abc-cursor")
        text = seats_aero_server._format_availability(data)
        assert "More results available" in text
        assert "abc-cursor" in text

    def test_no_availability_in_cabins(self):
        data = {
            "data": [{
                "Route": "SEA-NRT",
                "Date": "2026-06-10",
                "Source": "united",
                "Airlines": "NH",
                "Direct": True,
                "YAvailable": False,
                "WAvailable": False,
                "JAvailable": False,
                "FAvailable": False,
            }]
        }
        text = seats_aero_server._format_availability(data)
        assert "No availability" in text


class TestFormatTrip:
    def test_empty_data(self):
        text = seats_aero_server._format_trip({})
        assert "No trip details found" in text

    def test_full_trip(self):
        text = seats_aero_server._format_trip(_sample_trip())
        assert "trip-abc-123" in text
        assert "SEA-NRT" in text
        assert "NH178" in text
        assert "Business" in text
        assert "88000 miles" in text
        assert "https://example.com/book" in text

    def test_trip_without_segments(self):
        trip = _sample_trip()
        del trip["Segments"]
        text = seats_aero_server._format_trip(trip)
        assert "trip-abc-123" in text
        assert "Segments:" not in text

    def test_trip_without_booking_link(self):
        trip = _sample_trip()
        del trip["BookingLink"]
        text = seats_aero_server._format_trip(trip)
        assert "Book:" not in text


# ---------------------------------------------------------------------------
# TestExport
# ---------------------------------------------------------------------------
class TestSeatsAeroTools:
    def test_exports_three_tools(self):
        tools = seats_aero_server.SEATS_AERO_TOOLS
        assert len(tools) == 3

    def test_tool_names(self):
        names = [t.__name__ for t in seats_aero_server.SEATS_AERO_TOOLS]
        assert "award_search" in names
        assert "award_search_live" in names
        assert "award_trip_details" in names


# ---------------------------------------------------------------------------
# TestValidConstants
# ---------------------------------------------------------------------------
class TestConstants:
    def test_valid_sources_contains_key_programs(self):
        assert "united" in seats_aero_server.VALID_SOURCES
        assert "aeroplan" in seats_aero_server.VALID_SOURCES
        assert "lifemiles" in seats_aero_server.VALID_SOURCES
        assert "delta" in seats_aero_server.VALID_SOURCES

    def test_valid_cabins(self):
        assert "economy" in seats_aero_server.VALID_CABINS
        assert "business" in seats_aero_server.VALID_CABINS
        assert "first" in seats_aero_server.VALID_CABINS
        assert "premiumeconomy" in seats_aero_server.VALID_CABINS

    def test_headers_uses_partner_authorization(self):
        headers = seats_aero_server._headers("my-key")
        assert headers["Partner-Authorization"] == "my-key"
        assert headers["Accept"] == "application/json"

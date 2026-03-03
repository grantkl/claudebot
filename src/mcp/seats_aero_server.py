"""Seats.aero MCP server tools for award flight availability search."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

BASE_URL = "https://seats.aero"

VALID_CABINS = ("economy", "premiumeconomy", "business", "first")


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _get_api_key() -> str | None:
    return os.environ.get("SEATS_AERO_API_KEY", "") or None


def _headers(api_key: str) -> dict[str, str]:
    return {"Partner-Authorization": api_key, "Accept": "application/json"}


async def _handle_response(resp: httpx.Response) -> dict[str, Any] | None:
    """Check response status and return error dict if not OK, else None."""
    if resp.status_code == 401:
        return _error("Invalid API key. Check SEATS_AERO_API_KEY.")
    if resp.status_code == 429:
        return _error("Rate limited by seats.aero. Try again later.")
    if resp.status_code >= 400:
        return _error(f"seats.aero API error (HTTP {resp.status_code}): {resp.text[:200]}")
    return None


def _format_availability(data: dict[str, Any]) -> str:
    """Format award search results compactly for Slack."""
    results = data.get("data", [])
    if not results:
        return "No award availability found for this search."

    cursor = data.get("cursor")
    has_more = data.get("hasMore", False)
    count = len(results)

    lines = [f"Found {count} result(s):"]
    for r in results:
        route = f"{r.get('Route', 'N/A')}"
        date = r.get("Date", "N/A")
        source = r.get("Source", "")
        airlines = r.get("Airlines", "")
        direct = "Direct" if r.get("Direct", False) else "Connecting"

        cabins = []
        for cabin, label in [("YAvailable", "Y"), ("WAvailable", "W"), ("JAvailable", "J"), ("FAvailable", "F")]:
            if r.get(cabin):
                miles_key = cabin[0] + "MileageCost"
                remaining_key = cabin[0] + "Remaining"
                miles = r.get(miles_key, "?")
                remaining = r.get(remaining_key, "?")
                if miles and str(miles) != "0":
                    cabins.append(f"{label}:{miles}mi({remaining}left)")

        cabin_str = " | ".join(cabins) if cabins else "No availability"
        lines.append(f"  {route} {date} [{direct}] {cabin_str} via {source} ({airlines})")

    if has_more and cursor:
        lines.append(f"\nMore results available. Use cursor: {cursor}")

    return "\n".join(lines)


def _format_trip(data: dict[str, Any]) -> str:
    """Format trip details compactly for Slack."""
    if not data:
        return "No trip details found."

    trip_id = data.get("ID", "N/A")
    route = data.get("Route", "N/A")
    date = data.get("Date", "N/A")
    source = data.get("Source", "")
    airlines = data.get("Airlines", "")

    lines = [f"Trip {trip_id}: {route} on {date} (via {source}, {airlines})"]

    cabins = []
    for cabin, label in [("YAvailable", "Economy"), ("WAvailable", "Premium Econ"), ("JAvailable", "Business"), ("FAvailable", "First")]:
        if data.get(cabin):
            miles_key = cabin[0] + "MileageCost"
            remaining_key = cabin[0] + "Remaining"
            miles = data.get(miles_key, "?")
            remaining = data.get(remaining_key, "?")
            if miles and str(miles) != "0":
                cabins.append(f"  {label}: {miles} miles ({remaining} seats left)")
    if cabins:
        lines.extend(cabins)

    # Segments / booking info
    segments = data.get("Segments", []) or data.get("segments", [])
    if segments:
        lines.append("Segments:")
        for seg in segments:
            dep = seg.get("DepartureAirport", seg.get("departure_airport", "?"))
            arr = seg.get("ArrivalAirport", seg.get("arrival_airport", "?"))
            dep_time = seg.get("DepartureTime", seg.get("departure_time", ""))
            arr_time = seg.get("ArrivalTime", seg.get("arrival_time", ""))
            flight = seg.get("FlightNumber", seg.get("flight_number", ""))
            airline = seg.get("Airline", seg.get("airline", ""))
            lines.append(f"  {airline} {flight}: {dep} {dep_time} -> {arr} {arr_time}")

    booking_link = data.get("BookingLink") or data.get("booking_link")
    if booking_link:
        lines.append(f"Book: {booking_link}")

    return "\n".join(lines)


@tool(
    "award_search",
    "Search seats.aero cached award availability. Returns recent award flight data for a route. Fast but may not reflect real-time availability.",
    {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "Origin airport IATA code (e.g., 'SEA')."},
            "destination": {"type": "string", "description": "Destination airport IATA code (e.g., 'NRT')."},
            "cabin": {
                "type": "string",
                "description": "Cabin class: economy, premiumeconomy, business, or first.",
                "enum": ["economy", "premiumeconomy", "business", "first"],
            },
            "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format."},
            "end_date": {"type": "string", "description": "End date in YYYY-MM-DD format."},
            "take": {"type": "integer", "description": "Number of results to return (default 500, max 1000)."},
            "cursor": {"type": "string", "description": "Pagination cursor from a previous search."},
        },
        "required": ["origin", "destination"],
    },
)
async def award_search(args: dict[str, Any]) -> dict[str, Any]:
    api_key = _get_api_key()
    if not api_key:
        return _error("SEATS_AERO_API_KEY is not configured.")

    params: dict[str, str] = {
        "origin_airport": args["origin"].upper(),
        "destination_airport": args["destination"].upper(),
    }
    if args.get("cabin"):
        cabin = args["cabin"].lower()
        if cabin not in VALID_CABINS:
            return _error(f"Invalid cabin. Must be one of: {', '.join(VALID_CABINS)}")
        params["cabins"] = cabin
    if args.get("start_date"):
        params["start_date"] = args["start_date"]
    if args.get("end_date"):
        params["end_date"] = args["end_date"]
    if args.get("take"):
        params["take"] = str(min(int(args["take"]), 1000))
    if args.get("cursor"):
        params["cursor"] = args["cursor"]

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{BASE_URL}/partnerapi/search",
                params=params,
                headers=_headers(api_key),
            )
        err = await _handle_response(resp)
        if err:
            return err
        data = resp.json()
        return _text(_format_availability(data))
    except httpx.ConnectError:
        return _error("Could not connect to seats.aero. Service may be down.")
    except httpx.TimeoutException:
        return _error("Request to seats.aero timed out.")
    except Exception as e:
        return _error(f"Award search failed: {e}")



@tool(
    "award_trip_details",
    "Get details for a specific award trip from seats.aero, including flight segments, times, and booking links.",
    {
        "type": "object",
        "properties": {
            "trip_id": {"type": "string", "description": "Trip ID from a previous award search result."},
        },
        "required": ["trip_id"],
    },
)
async def award_trip_details(args: dict[str, Any]) -> dict[str, Any]:
    api_key = _get_api_key()
    if not api_key:
        return _error("SEATS_AERO_API_KEY is not configured.")

    trip_id = args["trip_id"]
    if not trip_id:
        return _error("trip_id is required.")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{BASE_URL}/partnerapi/trips/{trip_id}",
                headers=_headers(api_key),
            )
        err = await _handle_response(resp)
        if err:
            return err
        data = resp.json()
        return _text(_format_trip(data))
    except httpx.ConnectError:
        return _error("Could not connect to seats.aero. Service may be down.")
    except httpx.TimeoutException:
        return _error("Request to seats.aero timed out.")
    except Exception as e:
        return _error(f"Failed to get trip details: {e}")


SEATS_AERO_TOOLS: list[SdkMcpTool] = [
    award_search,
    award_trip_details,
]

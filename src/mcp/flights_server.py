"""Skyscanner flight search MCP server tools."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
from functools import partial
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

_scanner: Any = None


def _get_scanner() -> Any:
    global _scanner
    if _scanner is not None:
        return _scanner
    from skyscanner import SkyScanner

    _scanner = SkyScanner(
        locale=os.environ.get("SKYSCANNER_LOCALE", "en-US"),
        currency=os.environ.get("SKYSCANNER_CURRENCY", "USD"),
        market=os.environ.get("SKYSCANNER_MARKET", "US"),
    )
    return _scanner


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


# ---------------------------------------------------------------------------
# 1. flights_search_airports
# ---------------------------------------------------------------------------
@tool(
    "flights_search_airports",
    "Search for airports by name or city. Returns a JSON list of matching airports with name, IATA code, and entity ID.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for airport name or city (e.g., 'London', 'JFK', 'San Francisco').",
            },
        },
        "required": ["query"],
    },
)
async def flights_search_airports(args: dict[str, Any]) -> dict[str, Any]:
    try:
        scanner = _get_scanner()
        query = args["query"]
        loop = asyncio.get_event_loop()
        airports = await loop.run_in_executor(None, partial(scanner.search_airports, query))
        results = [
            {"name": a.title, "code": a.skyId, "entity_id": a.entity_id}
            for a in airports
        ]
        return _text(json.dumps(results, indent=2))
    except Exception as e:
        return _error(f"Failed to search airports: {e}")


# ---------------------------------------------------------------------------
# 2. flights_search_flights
# ---------------------------------------------------------------------------
@tool(
    "flights_search_flights",
    "Search for flights between airports. Returns flight options grouped by Best, Cheapest, Fastest, and Direct buckets. Searches may take 10-30 seconds.",
    {
        "type": "object",
        "properties": {
            "origin": {
                "type": "string",
                "description": "Origin airport IATA code (e.g., 'JFK', 'SFO', 'LAX').",
            },
            "destination": {
                "type": "string",
                "description": "Destination airport IATA code (e.g., 'LHR', 'CDG'), or 'everywhere' for open destination search.",
            },
            "depart_date": {
                "type": "string",
                "description": "Departure date in ISO 8601 format (YYYY-MM-DD) or 'anytime' for flexible search.",
            },
            "return_date": {
                "type": "string",
                "description": "Return date in ISO 8601 format (YYYY-MM-DD) or 'anytime' for flexible search. Omit for one-way.",
            },
            "cabin_class": {
                "type": "string",
                "description": "Cabin class: economy, premium_economy, business, or first. Defaults to economy.",
                "enum": ["economy", "premium_economy", "business", "first"],
            },
            "adults": {
                "type": "integer",
                "description": "Number of adult passengers (1-8). Defaults to 1.",
                "minimum": 1,
                "maximum": 8,
            },
        },
        "required": ["origin", "destination", "depart_date"],
    },
)
async def flights_search_flights(args: dict[str, Any]) -> dict[str, Any]:
    from skyscanner.skyscanner import (
        AttemptsExhaustedIncompleteResponse,
        BannedWithCaptcha,
    )
    from skyscanner.types import CabinClass, SpecialTypes

    try:
        scanner = _get_scanner()
        loop = asyncio.get_event_loop()

        # Resolve origin airport
        try:
            origin_airport = await loop.run_in_executor(
                None, partial(scanner.get_airport_by_code, args["origin"])
            )
        except Exception:
            return _error(f"Could not find airport with code '{args['origin']}'. Try searching for airports first.")

        # Resolve destination
        dest_str = args["destination"].lower()
        if dest_str == "everywhere":
            destination_airport = SpecialTypes.EVERYWHERE
        else:
            try:
                destination_airport = await loop.run_in_executor(
                    None, partial(scanner.get_airport_by_code, args["destination"])
                )
            except Exception:
                return _error(f"Could not find airport with code '{args['destination']}'. Try searching for airports first.")

        # Parse departure date
        depart_str = args["depart_date"].lower()
        if depart_str == "anytime":
            depart_date = SpecialTypes.ANYTIME
        else:
            try:
                depart_date = datetime.datetime.strptime(args["depart_date"], "%Y-%m-%d")
            except ValueError:
                return _error(f"Invalid departure date format: '{args['depart_date']}'. Use YYYY-MM-DD or 'anytime'.")

        # Parse return date (optional)
        return_date_str = args.get("return_date")
        if return_date_str is None:
            return_date = None
        elif return_date_str.lower() == "anytime":
            return_date = SpecialTypes.ANYTIME
        else:
            try:
                return_date = datetime.datetime.strptime(return_date_str, "%Y-%m-%d")
            except ValueError:
                return _error(f"Invalid return date format: '{return_date_str}'. Use YYYY-MM-DD or 'anytime'.")

        # Map cabin class
        cabin_map = {
            "economy": CabinClass.ECONOMY,
            "premium_economy": CabinClass.PREMIUM_ECONOMY,
            "business": CabinClass.BUSINESS,
            "first": CabinClass.FIRST,
        }
        cabin_class = cabin_map.get(args.get("cabin_class", "economy"), CabinClass.ECONOMY)

        adults = args.get("adults", 1)

        # Search flights
        response = await loop.run_in_executor(
            None,
            partial(
                scanner.get_flight_prices,
                origin=origin_airport,
                destination=destination_airport,
                depart_date=depart_date,
                return_date=return_date,
                cabinClass=cabin_class,
                adults=adults,
                childAges=[],
            ),
        )

        # Parse response
        buckets = response.json.get("itineraries", {}).get("buckets", [])
        result: list[dict[str, Any]] = []
        for bucket in buckets:
            items = bucket.get("items", [])
            top_items: list[dict[str, Any]] = []
            for item in items[:5]:
                price_info = item.get("price", {})
                legs = item.get("legs", [])
                leg_summaries: list[dict[str, Any]] = []
                for leg in legs:
                    leg_summaries.append({
                        "origin": leg.get("origin", {}).get("name", ""),
                        "destination": leg.get("destination", {}).get("name", ""),
                        "departure": leg.get("departure", ""),
                        "arrival": leg.get("arrival", ""),
                        "duration": leg.get("durationInMinutes", 0),
                        "stops": leg.get("stopCount", 0),
                        "carriers": [c.get("name", "") for c in leg.get("carriers", {}).get("marketing", [])],
                    })
                top_items.append({
                    "price": price_info.get("formatted", price_info.get("raw", "N/A")),
                    "legs": leg_summaries,
                })
            result.append({
                "bucket": bucket.get("id", "Unknown"),
                "total_items": len(items),
                "top_items": top_items,
            })

        return _text(json.dumps(result, indent=2))

    except BannedWithCaptcha:
        return _error("Flight search is temporarily rate-limited by Skyscanner (CAPTCHA required). Please try again later.")
    except AttemptsExhaustedIncompleteResponse:
        return _error("Flight search timed out with incomplete results. Please try again with a more specific query.")
    except Exception as e:
        return _error(f"Failed to search flights: {e}")


# ---------------------------------------------------------------------------
# Export all tools
# ---------------------------------------------------------------------------
FLIGHTS_TOOLS: list[SdkMcpTool] = [
    flights_search_airports,
    flights_search_flights,
]

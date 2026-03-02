"""Flight watch MCP server tools for tracking flight prices."""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

MAX_HISTORY_ENTRIES = 50

_store: FlightWatchStore | None = None


class FlightWatchStore:
    """JSON-backed storage for flight price watches."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, Any] = {"watches": {}, "next_id": 1}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path) as f:
                self._data = json.load(f)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def watches(self) -> dict[str, Any]:
        return self._data["watches"]

    def add(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str | None = None,
        max_price: float | None = None,
        airline: str | None = None,
        flight_numbers: list[str] | None = None,
    ) -> str:
        watch_id = f"watch_{self._data['next_id']}"
        self._data["next_id"] += 1
        self._data["watches"][watch_id] = {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "departure_date": departure_date,
            "return_date": return_date,
            "max_price": max_price,
            "airline": airline,
            "flight_numbers": flight_numbers,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "active": True,
            "price_history": [],
        }
        self._save()
        return watch_id

    def remove(self, watch_id: str) -> bool:
        if watch_id not in self._data["watches"]:
            return False
        del self._data["watches"][watch_id]
        self._save()
        return True

    def record_price(
        self,
        watch_id: str,
        lowest_price: float,
        currency: str = "USD",
        airline: str | None = None,
        flight_numbers: str | None = None,
        details: str | None = None,
    ) -> bool:
        watch = self._data["watches"].get(watch_id)
        if watch is None:
            return False
        entry: dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "lowest_price": lowest_price,
            "currency": currency,
        }
        if airline is not None:
            entry["airline"] = airline
        if flight_numbers is not None:
            entry["flight_numbers"] = flight_numbers
        if details is not None:
            entry["details"] = details
        watch["price_history"].append(entry)
        if len(watch["price_history"]) > MAX_HISTORY_ENTRIES:
            watch["price_history"] = watch["price_history"][-MAX_HISTORY_ENTRIES:]
        self._save()
        return True

    def deactivate_past_watches(self) -> None:
        today = datetime.date.today()
        changed = False
        for watch in self._data["watches"].values():
            if watch["active"] and datetime.date.fromisoformat(watch["departure_date"]) < today:
                watch["active"] = False
                changed = True
        if changed:
            self._save()


def _get_store() -> FlightWatchStore:
    global _store
    if _store is None:
        path = os.environ.get("FLIGHT_WATCH_FILE", "data/flight_watches.json")
        _store = FlightWatchStore(path)
    return _store


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


@tool(
    "flight_watch_add",
    "Add a flight price watch for a route or a specific booked flight.",
    {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "Origin airport IATA code (e.g., 'SEA')."},
            "destination": {"type": "string", "description": "Destination airport IATA code (e.g., 'NRT')."},
            "departure_date": {"type": "string", "description": "Departure date in YYYY-MM-DD format."},
            "return_date": {"type": "string", "description": "Optional return date in YYYY-MM-DD format."},
            "max_price": {"type": "number", "description": "Optional maximum price threshold for alerts."},
            "airline": {"type": "string", "description": "Airline name to filter (e.g., 'Alaska'). For tracking a specific booked flight."},
            "flight_numbers": {"type": "array", "items": {"type": "string"}, "description": "Flight number(s) to track (e.g., ['AS275']). For tracking a specific booked flight."},
        },
        "required": ["origin", "destination", "departure_date"],
    },
)
async def flight_watch_add(args: dict[str, Any]) -> dict[str, Any]:
    try:
        departure = datetime.date.fromisoformat(args["departure_date"])
        if departure < datetime.date.today():
            return _error("Departure date must be in the future.")
        return_date = args.get("return_date")
        if return_date is not None:
            ret = datetime.date.fromisoformat(return_date)
            if ret < departure:
                return _error("Return date must be on or after departure date.")
        store = _get_store()
        watch_id = store.add(
            origin=args["origin"],
            destination=args["destination"],
            departure_date=args["departure_date"],
            return_date=return_date,
            max_price=args.get("max_price"),
            airline=args.get("airline"),
            flight_numbers=args.get("flight_numbers"),
        )
        return _text(f"Created flight watch {watch_id}: {args['origin']} -> {args['destination']} on {args['departure_date']}.")
    except ValueError as e:
        return _error(f"Invalid date format: {e}")
    except Exception as e:
        return _error(f"Failed to add flight watch: {e}")


@tool(
    "flight_watch_list",
    "List flight price watches. Shows active watches with last known price. Auto-deactivates watches with past departure dates.",
    {
        "type": "object",
        "properties": {
            "include_inactive": {"type": "boolean", "description": "Include inactive/expired watches. Default false."},
        },
    },
)
async def flight_watch_list(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_store()
        store.deactivate_past_watches()
        include_inactive = args.get("include_inactive", False)
        result = []
        for watch_id, watch in store.watches.items():
            if not include_inactive and not watch["active"]:
                continue
            entry: dict[str, Any] = {
                "id": watch_id,
                "origin": watch["origin"],
                "destination": watch["destination"],
                "departure_date": watch["departure_date"],
                "return_date": watch["return_date"],
                "max_price": watch["max_price"],
                "airline": watch.get("airline"),
                "flight_numbers": watch.get("flight_numbers"),
                "active": watch["active"],
            }
            if watch["price_history"]:
                last = watch["price_history"][-1]
                entry["last_price"] = last["lowest_price"]
                entry["last_currency"] = last.get("currency", "USD")
                entry["last_checked"] = last["timestamp"]
            result.append(entry)
        return _text(json.dumps(result, indent=2))
    except Exception as e:
        return _error(f"Failed to list watches: {e}")


@tool(
    "flight_watch_remove",
    "Remove a flight price watch.",
    {
        "type": "object",
        "properties": {
            "watch_id": {"type": "string", "description": "The watch ID to remove (e.g., 'watch_1')."},
        },
        "required": ["watch_id"],
    },
)
async def flight_watch_remove(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_store()
        if not store.remove(args["watch_id"]):
            return _error(f"Watch not found: {args['watch_id']}")
        return _text(f"Removed flight watch {args['watch_id']}.")
    except Exception as e:
        return _error(f"Failed to remove watch: {e}")


@tool(
    "flight_watch_record",
    "Record a price check result for a flight watch. Called by the scheduler after searching for flights.",
    {
        "type": "object",
        "properties": {
            "watch_id": {"type": "string", "description": "The watch ID to record a price for."},
            "lowest_price": {"type": "number", "description": "The lowest price found."},
            "currency": {"type": "string", "description": "Currency code (default USD)."},
            "airline": {"type": "string", "description": "Airline name for the lowest fare."},
            "flight_numbers": {"type": "string", "description": "Flight number(s) for the lowest fare."},
            "details": {"type": "string", "description": "Additional details (stops, duration, etc.)."},
        },
        "required": ["watch_id", "lowest_price"],
    },
)
async def flight_watch_record(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_store()
        if not store.record_price(
            watch_id=args["watch_id"],
            lowest_price=args["lowest_price"],
            currency=args.get("currency", "USD"),
            airline=args.get("airline"),
            flight_numbers=args.get("flight_numbers"),
            details=args.get("details"),
        ):
            return _error(f"Watch not found: {args['watch_id']}")
        return _text(f"Recorded price ${args['lowest_price']} for {args['watch_id']}.")
    except Exception as e:
        return _error(f"Failed to record price: {e}")


@tool(
    "flight_watch_history",
    "Get the full price history for a flight watch.",
    {
        "type": "object",
        "properties": {
            "watch_id": {"type": "string", "description": "The watch ID to get history for."},
        },
        "required": ["watch_id"],
    },
)
async def flight_watch_history(args: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _get_store()
        watch = store.watches.get(args["watch_id"])
        if watch is None:
            return _error(f"Watch not found: {args['watch_id']}")
        result = {
            "watch_id": args["watch_id"],
            "origin": watch["origin"],
            "destination": watch["destination"],
            "departure_date": watch["departure_date"],
            "return_date": watch["return_date"],
            "max_price": watch["max_price"],
            "airline": watch.get("airline"),
            "flight_numbers": watch.get("flight_numbers"),
            "active": watch["active"],
            "price_history": watch["price_history"],
        }
        return _text(json.dumps(result, indent=2))
    except Exception as e:
        return _error(f"Failed to get history: {e}")


FLIGHT_WATCH_TOOLS: list[SdkMcpTool] = [
    flight_watch_add,
    flight_watch_list,
    flight_watch_remove,
    flight_watch_record,
    flight_watch_history,
]

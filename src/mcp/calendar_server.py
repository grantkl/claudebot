"""Google Calendar MCP server tools for managing calendar events."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

# Module-level cached service
_calendar_service: Any = None


def _get_calendar_service() -> Any:
    """Load Calendar API credentials and return a cached service object.

    Auto-refreshes expired tokens and writes them back to disk.
    """
    global _calendar_service
    if _calendar_service is not None:
        return _calendar_service

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds_file = os.environ["GMAIL_CREDENTIALS_FILE"]
    token_file = os.environ["GMAIL_TOKEN_FILE"]

    creds = Credentials.from_authorized_user_file(
        token_file,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    _calendar_service = build("calendar", "v3", credentials=creds)
    return _calendar_service


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _is_all_day(value: str) -> bool:
    """Check if a datetime string looks like an all-day date (YYYY-MM-DD)."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _format_time(value: str) -> dict[str, str]:
    """Format a start/end value as a Calendar API time object."""
    if _is_all_day(value):
        return {"date": value}
    return {"dateTime": value, "timeZone": "UTC"}


# ---------------------------------------------------------------------------
# 1. calendar_list_events
# ---------------------------------------------------------------------------
@tool(
    "calendar_list_events",
    "List upcoming Google Calendar events. Returns a JSON list with id, summary, start, end, location, description, attendees, hangoutLink, and status.",
    {
        "type": "object",
        "properties": {
            "time_min": {
                "type": "string",
                "description": "Start of time range in ISO format (default: now).",
            },
            "time_max": {
                "type": "string",
                "description": "End of time range in ISO format (default: end of today).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of events to return (default 10).",
                "minimum": 1,
                "maximum": 250,
            },
            "calendar_id": {
                "type": "string",
                "description": "Calendar ID (default 'primary').",
            },
        },
    },
)
async def calendar_list_events(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_calendar_service()
        now = datetime.now(timezone.utc)
        time_min = args.get("time_min", now.isoformat())
        time_max_default = now.replace(hour=23, minute=59, second=59).isoformat()
        time_max = args.get("time_max", time_max_default)
        max_results = args.get("max_results", 10)
        calendar_id = args.get("calendar_id", "primary")

        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        items = response.get("items", [])
        results = []
        for event in items:
            results.append({
                "id": event.get("id", ""),
                "summary": event.get("summary", ""),
                "start": event.get("start", {}),
                "end": event.get("end", {}),
                "location": event.get("location", ""),
                "description": event.get("description", ""),
                "attendees": event.get("attendees", []),
                "hangoutLink": event.get("hangoutLink", ""),
                "status": event.get("status", ""),
            })

        return _text(json.dumps(results, indent=2))
    except Exception as e:
        return _error(f"Failed to list events: {e}")


# ---------------------------------------------------------------------------
# 2. calendar_get_event
# ---------------------------------------------------------------------------
@tool(
    "calendar_get_event",
    "Get a specific Google Calendar event by ID. Returns full event details.",
    {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "The calendar event ID.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Calendar ID (default 'primary').",
            },
        },
        "required": ["event_id"],
    },
)
async def calendar_get_event(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_calendar_service()
        event_id = args["event_id"]
        calendar_id = args.get("calendar_id", "primary")

        event = (
            service.events()
            .get(calendarId=calendar_id, eventId=event_id)
            .execute()
        )

        return _text(json.dumps(event, indent=2))
    except Exception as e:
        return _error(f"Failed to get event: {e}")


# ---------------------------------------------------------------------------
# 3. calendar_create_event
# ---------------------------------------------------------------------------
@tool(
    "calendar_create_event",
    "Create a new Google Calendar event. Returns the created event details.",
    {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Event title.",
            },
            "start": {
                "type": "string",
                "description": "Start time in ISO format (e.g., '2026-03-26T10:00:00-07:00') or date for all-day events (e.g., '2026-03-26').",
            },
            "end": {
                "type": "string",
                "description": "End time in ISO format or date for all-day events.",
            },
            "description": {
                "type": "string",
                "description": "Event description.",
            },
            "location": {
                "type": "string",
                "description": "Event location.",
            },
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of attendee email addresses.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Calendar ID (default 'primary').",
            },
        },
        "required": ["summary", "start", "end"],
    },
)
async def calendar_create_event(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_calendar_service()
        calendar_id = args.get("calendar_id", "primary")

        body: dict[str, Any] = {
            "summary": args["summary"],
            "start": _format_time(args["start"]),
            "end": _format_time(args["end"]),
        }

        if "description" in args:
            body["description"] = args["description"]
        if "location" in args:
            body["location"] = args["location"]
        if "attendees" in args:
            body["attendees"] = [{"email": email} for email in args["attendees"]]

        event = (
            service.events()
            .insert(calendarId=calendar_id, body=body)
            .execute()
        )

        return _text(json.dumps(event, indent=2))
    except Exception as e:
        return _error(f"Failed to create event: {e}")


# ---------------------------------------------------------------------------
# 4. calendar_update_event
# ---------------------------------------------------------------------------
@tool(
    "calendar_update_event",
    "Update an existing Google Calendar event. Only provided fields are changed.",
    {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "The calendar event ID to update.",
            },
            "summary": {
                "type": "string",
                "description": "New event title.",
            },
            "start": {
                "type": "string",
                "description": "New start time in ISO format.",
            },
            "end": {
                "type": "string",
                "description": "New end time in ISO format.",
            },
            "description": {
                "type": "string",
                "description": "New event description.",
            },
            "location": {
                "type": "string",
                "description": "New event location.",
            },
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New list of attendee email addresses.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Calendar ID (default 'primary').",
            },
        },
        "required": ["event_id"],
    },
)
async def calendar_update_event(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_calendar_service()
        event_id = args["event_id"]
        calendar_id = args.get("calendar_id", "primary")

        body: dict[str, Any] = {}
        if "summary" in args:
            body["summary"] = args["summary"]
        if "start" in args:
            body["start"] = _format_time(args["start"])
        if "end" in args:
            body["end"] = _format_time(args["end"])
        if "description" in args:
            body["description"] = args["description"]
        if "location" in args:
            body["location"] = args["location"]
        if "attendees" in args:
            body["attendees"] = [{"email": email} for email in args["attendees"]]

        event = (
            service.events()
            .patch(calendarId=calendar_id, eventId=event_id, body=body)
            .execute()
        )

        return _text(json.dumps(event, indent=2))
    except Exception as e:
        return _error(f"Failed to update event: {e}")


# ---------------------------------------------------------------------------
# 5. calendar_delete_event
# ---------------------------------------------------------------------------
@tool(
    "calendar_delete_event",
    "Delete a Google Calendar event by ID.",
    {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "The calendar event ID to delete.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Calendar ID (default 'primary').",
            },
        },
        "required": ["event_id"],
    },
)
async def calendar_delete_event(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_calendar_service()
        event_id = args["event_id"]
        calendar_id = args.get("calendar_id", "primary")

        service.events().delete(
            calendarId=calendar_id, eventId=event_id
        ).execute()

        return _text(f"Event {event_id} deleted.")
    except Exception as e:
        return _error(f"Failed to delete event: {e}")


# ---------------------------------------------------------------------------
# Export all tools
# ---------------------------------------------------------------------------
CALENDAR_TOOLS: list[SdkMcpTool] = [
    calendar_list_events,
    calendar_get_event,
    calendar_create_event,
    calendar_update_event,
    calendar_delete_event,
]

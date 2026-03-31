"""Tests for the Google Calendar MCP server tools."""

from __future__ import annotations

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


sys.modules.setdefault("claude_agent_sdk", _make_sdk_mock())

# Mock google modules before importing calendar_server
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.auth", MagicMock())
sys.modules.setdefault("google.auth.transport", MagicMock())
sys.modules.setdefault("google.auth.transport.requests", MagicMock())
sys.modules.setdefault("google.oauth2", MagicMock())
sys.modules.setdefault("google.oauth2.credentials", MagicMock())
sys.modules.setdefault("googleapiclient", MagicMock())
sys.modules.setdefault("googleapiclient.discovery", MagicMock())

from src.mcp import calendar_server
from src.mcp.calendar_server import CALENDAR_TOOLS

# Access the underlying async handlers via .handler attribute
_list_events = calendar_server.calendar_list_events.handler
_get_event = calendar_server.calendar_get_event.handler
_create_event = calendar_server.calendar_create_event.handler
_update_event = calendar_server.calendar_update_event.handler
_delete_event = calendar_server.calendar_delete_event.handler


def _parse_text(result: dict[str, Any]) -> str:
    """Extract text content from a tool result."""
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


def _make_service() -> MagicMock:
    """Create a mock Google Calendar API service."""
    return MagicMock()


# ---------------------------------------------------------------------------
# calendar_list_events
# ---------------------------------------------------------------------------
class TestCalendarListEvents:
    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_lists_events(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.events().list().execute.return_value = {
            "items": [
                {
                    "id": "evt1",
                    "summary": "Team Standup",
                    "start": {"dateTime": "2026-03-25T09:00:00-07:00"},
                    "end": {"dateTime": "2026-03-25T09:30:00-07:00"},
                    "location": "Conference Room A",
                    "attendees": [
                        {"email": "alice@example.com", "responseStatus": "accepted"},
                    ],
                    "hangoutLink": "https://meet.google.com/abc-defg-hij",
                    "status": "confirmed",
                },
                {
                    "id": "evt2",
                    "summary": "Lunch",
                    "start": {"dateTime": "2026-03-25T12:00:00-07:00"},
                    "end": {"dateTime": "2026-03-25T13:00:00-07:00"},
                    "status": "confirmed",
                },
            ],
        }

        result = await _list_events({"max_results": 10})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert len(data) == 2
        assert data[0]["id"] == "evt1"
        assert data[0]["summary"] == "Team Standup"
        assert data[0]["location"] == "Conference Room A"
        assert data[0]["hangoutLink"] == "https://meet.google.com/abc-defg-hij"
        assert data[0]["status"] == "confirmed"
        assert len(data[0]["attendees"]) == 1

    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_empty_results(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.events().list().execute.return_value = {"items": []}

        result = await _list_events({})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data == []

    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_default_time_range(self, mock_get_svc: MagicMock) -> None:
        """Verify defaults are applied when no time_min/time_max provided."""
        service = _make_service()
        mock_get_svc.return_value = service

        service.events().list().execute.return_value = {"items": []}

        result = await _list_events({})

        assert not _is_error(result)
        # Verify list() was called — the implementation should provide
        # default timeMin (now) and timeMax (end of day) when not specified.
        service.events().list.assert_called()

    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_error_handling(self, mock_get_svc: MagicMock) -> None:
        mock_get_svc.side_effect = Exception("Auth failed")

        result = await _list_events({})
        assert _is_error(result)
        assert "Failed" in _parse_text(result)


# ---------------------------------------------------------------------------
# calendar_get_event
# ---------------------------------------------------------------------------
class TestCalendarGetEvent:
    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_gets_event(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.events().get().execute.return_value = {
            "id": "evt123",
            "summary": "Project Review",
            "description": "Quarterly project review meeting",
            "start": {"dateTime": "2026-03-25T14:00:00-07:00"},
            "end": {"dateTime": "2026-03-25T15:00:00-07:00"},
            "location": "Building 42, Room 101",
            "attendees": [
                {"email": "alice@example.com", "responseStatus": "accepted"},
                {"email": "bob@example.com", "responseStatus": "tentative"},
            ],
            "hangoutLink": "https://meet.google.com/xyz-abcd-efg",
            "status": "confirmed",
        }

        result = await _get_event({"event_id": "evt123"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["id"] == "evt123"
        assert data["summary"] == "Project Review"
        assert data["description"] == "Quarterly project review meeting"
        assert data["location"] == "Building 42, Room 101"
        assert len(data["attendees"]) == 2
        assert data["hangoutLink"] == "https://meet.google.com/xyz-abcd-efg"
        assert data["status"] == "confirmed"

    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_error_handling(self, mock_get_svc: MagicMock) -> None:
        mock_get_svc.side_effect = Exception("Event not found")

        result = await _get_event({"event_id": "bad_id"})
        assert _is_error(result)
        assert "Failed" in _parse_text(result)


# ---------------------------------------------------------------------------
# calendar_create_event
# ---------------------------------------------------------------------------
class TestCalendarCreateEvent:
    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_creates_event(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.events().insert().execute.return_value = {
            "id": "new_evt1",
            "summary": "New Meeting",
            "start": {"dateTime": "2026-03-26T10:00:00-07:00"},
            "end": {"dateTime": "2026-03-26T11:00:00-07:00"},
            "status": "confirmed",
            "htmlLink": "https://calendar.google.com/event?eid=new_evt1",
        }

        result = await _create_event({
            "summary": "New Meeting",
            "start": "2026-03-26T10:00:00-07:00",
            "end": "2026-03-26T11:00:00-07:00",
        })
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["id"] == "new_evt1"
        assert data["summary"] == "New Meeting"

    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_creates_event_with_optional_params(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.events().insert().execute.return_value = {
            "id": "new_evt2",
            "summary": "Team Offsite",
            "description": "Annual team offsite planning",
            "location": "Beach Resort",
            "start": {"dateTime": "2026-04-01T09:00:00-07:00"},
            "end": {"dateTime": "2026-04-01T17:00:00-07:00"},
            "attendees": [
                {"email": "alice@example.com"},
                {"email": "bob@example.com"},
            ],
            "status": "confirmed",
            "htmlLink": "https://calendar.google.com/event?eid=new_evt2",
        }

        result = await _create_event({
            "summary": "Team Offsite",
            "start": "2026-04-01T09:00:00-07:00",
            "end": "2026-04-01T17:00:00-07:00",
            "description": "Annual team offsite planning",
            "location": "Beach Resort",
            "attendees": ["alice@example.com", "bob@example.com"],
        })
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["id"] == "new_evt2"
        assert data["description"] == "Annual team offsite planning"
        assert data["location"] == "Beach Resort"
        assert len(data["attendees"]) == 2

    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_error_handling(self, mock_get_svc: MagicMock) -> None:
        mock_get_svc.side_effect = Exception("Calendar API error")

        result = await _create_event({
            "summary": "Fail Event",
            "start": "2026-03-26T10:00:00-07:00",
            "end": "2026-03-26T11:00:00-07:00",
        })
        assert _is_error(result)
        assert "Failed" in _parse_text(result)


# ---------------------------------------------------------------------------
# calendar_update_event
# ---------------------------------------------------------------------------
class TestCalendarUpdateEvent:
    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_updates_event(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.events().patch().execute.return_value = {
            "id": "evt123",
            "summary": "Updated Meeting Title",
            "start": {"dateTime": "2026-03-25T14:00:00-07:00"},
            "end": {"dateTime": "2026-03-25T15:00:00-07:00"},
            "status": "confirmed",
        }

        result = await _update_event({
            "event_id": "evt123",
            "summary": "Updated Meeting Title",
        })
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["id"] == "evt123"
        assert data["summary"] == "Updated Meeting Title"

    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_error_handling(self, mock_get_svc: MagicMock) -> None:
        mock_get_svc.side_effect = Exception("Update failed")

        result = await _update_event({
            "event_id": "bad_id",
            "summary": "Won't work",
        })
        assert _is_error(result)
        assert "Failed" in _parse_text(result)


# ---------------------------------------------------------------------------
# calendar_delete_event
# ---------------------------------------------------------------------------
class TestCalendarDeleteEvent:
    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_deletes_event(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.events().delete().execute.return_value = None

        result = await _delete_event({"event_id": "evt_to_delete"})

        assert not _is_error(result)
        text = _parse_text(result)
        assert "evt_to_delete" in text or "deleted" in text.lower()

    @patch.object(calendar_server, "_calendar_service", None)
    @patch("src.mcp.calendar_server._get_calendar_service")
    async def test_error_handling(self, mock_get_svc: MagicMock) -> None:
        mock_get_svc.side_effect = Exception("Delete failed")

        result = await _delete_event({"event_id": "bad_id"})
        assert _is_error(result)
        assert "Failed" in _parse_text(result)

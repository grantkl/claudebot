"""Tests for the Gmail MCP server tools."""

from __future__ import annotations

import base64
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

# Mock google modules before importing gmail_server
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.auth", MagicMock())
sys.modules.setdefault("google.auth.transport", MagicMock())
sys.modules.setdefault("google.auth.transport.requests", MagicMock())
sys.modules.setdefault("google.oauth2", MagicMock())
sys.modules.setdefault("google.oauth2.credentials", MagicMock())
sys.modules.setdefault("googleapiclient", MagicMock())
sys.modules.setdefault("googleapiclient.discovery", MagicMock())

from src.mcp import gmail_server
from src.mcp.gmail_server import _extract_body

# Access the underlying async handlers via .handler attribute
_list_emails = gmail_server.gmail_list_emails.handler
_get_email = gmail_server.gmail_get_email.handler
_mark_as_read = gmail_server.gmail_mark_as_read.handler


def _parse_text(result: dict[str, Any]) -> str:
    """Extract text content from a tool result."""
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


def _b64(text: str) -> str:
    """Encode text to URL-safe base64."""
    return base64.urlsafe_b64encode(text.encode()).decode()


def _make_service() -> MagicMock:
    """Create a mock Gmail API service."""
    return MagicMock()


# ---------------------------------------------------------------------------
# _extract_body
# ---------------------------------------------------------------------------
class TestExtractBody:
    def test_plain_text(self) -> None:
        payload = {
            "mimeType": "text/plain",
            "body": {"data": _b64("Hello world")},
        }
        assert _extract_body(payload) == "Hello world"

    def test_html_only(self) -> None:
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64("<p>Hello</p>")},
        }
        result = _extract_body(payload)
        assert "[HTML content]" in result
        assert "<p>Hello</p>" in result

    def test_multipart_prefers_plain(self) -> None:
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64("Plain text body")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64("<p>HTML body</p>")},
                },
            ],
        }
        assert _extract_body(payload) == "Plain text body"

    def test_multipart_falls_back_to_html(self) -> None:
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64("<p>Only HTML</p>")},
                },
            ],
        }
        result = _extract_body(payload)
        assert "[HTML content]" in result
        assert "<p>Only HTML</p>" in result

    def test_nested_multipart(self) -> None:
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _b64("Nested plain")},
                        },
                        {
                            "mimeType": "text/html",
                            "body": {"data": _b64("<p>Nested HTML</p>")},
                        },
                    ],
                },
            ],
        }
        assert _extract_body(payload) == "Nested plain"

    def test_empty_payload(self) -> None:
        payload: dict[str, Any] = {"mimeType": "multipart/mixed", "parts": []}
        assert _extract_body(payload) == ""

    def test_no_body_data(self) -> None:
        payload = {"mimeType": "text/plain", "body": {}}
        assert _extract_body(payload) == ""


# ---------------------------------------------------------------------------
# gmail_list_emails
# ---------------------------------------------------------------------------
class TestGmailListEmails:
    @patch.object(gmail_server, "_gmail_service", None)
    @patch("src.mcp.gmail_server._get_gmail_service")
    async def test_lists_emails(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg1"}, {"id": "msg2"}],
        }

        def _get_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
            msg_id = kwargs.get("id", "")
            mock_resp = MagicMock()
            mock_resp.execute.return_value = {
                "id": msg_id,
                "threadId": f"thread_{msg_id}",
                "snippet": f"Snippet for {msg_id}",
                "labelIds": ["INBOX", "UNREAD"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "alice@example.com"},
                        {"name": "To", "value": "bob@example.com"},
                        {"name": "Subject", "value": f"Subject {msg_id}"},
                        {"name": "Date", "value": "Mon, 1 Mar 2026 10:00:00 -0500"},
                    ],
                },
            }
            return mock_resp

        service.users().messages().get.side_effect = _get_side_effect

        result = await _list_emails({"query": "is:unread", "max_results": 2})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert len(data) == 2
        assert data[0]["id"] == "msg1"
        assert data[0]["from"] == "alice@example.com"
        assert data[0]["subject"] == "Subject msg1"

    @patch.object(gmail_server, "_gmail_service", None)
    @patch("src.mcp.gmail_server._get_gmail_service")
    async def test_empty_results(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.users().messages().list().execute.return_value = {"messages": []}

        result = await _list_emails({})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data == []

    @patch.object(gmail_server, "_gmail_service", None)
    @patch("src.mcp.gmail_server._get_gmail_service")
    async def test_error_handling(self, mock_get_svc: MagicMock) -> None:
        mock_get_svc.side_effect = Exception("Auth failed")

        result = await _list_emails({"query": "is:inbox"})
        assert _is_error(result)
        assert "Failed to list emails" in _parse_text(result)


# ---------------------------------------------------------------------------
# gmail_get_email
# ---------------------------------------------------------------------------
class TestGmailGetEmail:
    @patch.object(gmail_server, "_gmail_service", None)
    @patch("src.mcp.gmail_server._get_gmail_service")
    async def test_gets_email(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.users().messages().get().execute.return_value = {
            "id": "msg123",
            "threadId": "thread123",
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "text/plain",
                "body": {"data": _b64("Hello from the email body")},
                "headers": [
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "receiver@example.com"},
                    {"name": "Subject", "value": "Test Email"},
                    {"name": "Date", "value": "Mon, 1 Mar 2026 12:00:00 -0500"},
                ],
            },
        }

        result = await _get_email({"message_id": "msg123"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["id"] == "msg123"
        assert data["from"] == "sender@example.com"
        assert data["subject"] == "Test Email"
        assert "Hello from the email body" in data["body"]

    @patch.object(gmail_server, "_gmail_service", None)
    @patch("src.mcp.gmail_server._get_gmail_service")
    async def test_error_handling(self, mock_get_svc: MagicMock) -> None:
        mock_get_svc.side_effect = Exception("Not found")

        result = await _get_email({"message_id": "bad_id"})
        assert _is_error(result)
        assert "Failed to get email" in _parse_text(result)


# ---------------------------------------------------------------------------
# gmail_mark_as_read
# ---------------------------------------------------------------------------
class TestGmailMarkAsRead:
    @patch.object(gmail_server, "_gmail_service", None)
    @patch("src.mcp.gmail_server._get_gmail_service")
    async def test_marks_as_read(self, mock_get_svc: MagicMock) -> None:
        service = _make_service()
        mock_get_svc.return_value = service

        service.users().messages().modify().execute.return_value = {}

        result = await _mark_as_read({"message_id": "msg456"})

        assert not _is_error(result)
        assert "msg456" in _parse_text(result)
        assert "marked as read" in _parse_text(result)

    @patch.object(gmail_server, "_gmail_service", None)
    @patch("src.mcp.gmail_server._get_gmail_service")
    async def test_error_handling(self, mock_get_svc: MagicMock) -> None:
        mock_get_svc.side_effect = Exception("Permission denied")

        result = await _mark_as_read({"message_id": "msg789"})
        assert _is_error(result)
        assert "Failed to mark as read" in _parse_text(result)

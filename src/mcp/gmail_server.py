"""Gmail MCP server tools for reading and managing Gmail messages."""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

# Module-level cached service
_gmail_service: Any = None


def _get_gmail_service() -> Any:
    """Load Gmail API credentials and return a cached service object.

    Auto-refreshes expired tokens and writes them back to disk.
    """
    global _gmail_service
    if _gmail_service is not None:
        return _gmail_service

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds_file = os.environ["GMAIL_CREDENTIALS_FILE"]
    token_file = os.environ["GMAIL_TOKEN_FILE"]

    creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
        token_file,
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    _gmail_service = build("gmail", "v1", credentials=creds)
    return _gmail_service


def _extract_body(payload: dict[str, Any]) -> str:
    """Recursively walk a MIME payload and extract the message body.

    Prefers text/plain; falls back to text/html with a note.
    """
    mime_type = payload.get("mimeType", "")

    # Simple single-part message
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return f"[HTML content]\n{html}"

    # Multipart: recurse into parts
    parts = payload.get("parts", [])
    plain_text = ""
    html_text = ""
    for part in parts:
        result = _extract_body(part)
        if result:
            part_mime = part.get("mimeType", "")
            if part_mime == "text/plain" or (not result.startswith("[HTML content]")):
                if not plain_text:
                    plain_text = result
            else:
                if not html_text:
                    html_text = result

    return plain_text or html_text or ""


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


# ---------------------------------------------------------------------------
# 1. gmail_list_emails
# ---------------------------------------------------------------------------
@tool(
    "gmail_list_emails",
    "List or search Gmail emails using Gmail search syntax. Returns a JSON list with id, threadId, from, to, subject, date, snippet, and labels for each message.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Gmail search query (e.g., 'is:unread', 'from:alice@example.com', 'subject:invoice'). Defaults to 'is:inbox'.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 10).",
                "minimum": 1,
                "maximum": 100,
            },
        },
    },
)
async def gmail_list_emails(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_gmail_service()
        query = args.get("query", "is:inbox")
        max_results = args.get("max_results", 10)

        response = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )

        messages = response.get("messages", [])
        if not messages:
            return _text("[]")

        results = []
        for msg_stub in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_stub["id"], format="metadata",
                     metadataHeaders=["From", "To", "Subject", "Date"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            results.append({
                "id": msg["id"],
                "threadId": msg.get("threadId", ""),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
                "labels": msg.get("labelIds", []),
            })

        return _text(json.dumps(results, indent=2))
    except Exception as e:
        return _error(f"Failed to list emails: {e}")


# ---------------------------------------------------------------------------
# 2. gmail_get_email
# ---------------------------------------------------------------------------
@tool(
    "gmail_get_email",
    "Get a specific Gmail email by message ID. Returns full headers and body text.",
    {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "The Gmail message ID.",
            },
        },
        "required": ["message_id"],
    },
)
async def gmail_get_email(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_gmail_service()
        message_id = args["message_id"]

        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        body = _extract_body(msg.get("payload", {}))

        result = {
            "id": msg["id"],
            "threadId": msg.get("threadId", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "labels": msg.get("labelIds", []),
            "body": body,
        }

        return _text(json.dumps(result, indent=2))
    except Exception as e:
        return _error(f"Failed to get email: {e}")


# ---------------------------------------------------------------------------
# 3. gmail_mark_as_read
# ---------------------------------------------------------------------------
@tool(
    "gmail_mark_as_read",
    "Mark a Gmail email as read by removing the UNREAD label.",
    {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "The Gmail message ID to mark as read.",
            },
        },
        "required": ["message_id"],
    },
)
async def gmail_mark_as_read(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_gmail_service()
        message_id = args["message_id"]

        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()

        return _text(f"Message {message_id} marked as read.")
    except Exception as e:
        return _error(f"Failed to mark as read: {e}")


# ---------------------------------------------------------------------------
# Export all tools
# ---------------------------------------------------------------------------
GMAIL_TOOLS: list[SdkMcpTool] = [  # type: ignore[type-arg]
    gmail_list_emails,
    gmail_get_email,
    gmail_mark_as_read,
]

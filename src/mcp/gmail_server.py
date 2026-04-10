"""Gmail MCP server tools for reading and managing Gmail messages."""

from __future__ import annotations

import base64
import json
import logging
import os
from html.parser import HTMLParser
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ..error_utils import classify_error

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

    creds = Credentials.from_authorized_user_file(
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


def _extract_html(payload: dict[str, Any]) -> str:
    """Recursively walk a MIME payload and return raw HTML if available."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        result = _extract_html(part)
        if result:
            return result
    return ""


class _LinkExtractor(HTMLParser):
    """Extract <a> tags with their href and visible text."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href and href.startswith("http"):
                self._current_href = href
                self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href is not None:
            text = " ".join(t for t in self._current_text if t)
            # Skip links where the text is just the URL itself or empty
            if text and not text.startswith("http"):
                self.links.append({"text": text, "url": self._current_href})
            self._current_href = None
            self._current_text = []


def _extract_links(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Extract named links from email HTML body."""
    html = _extract_html(payload)
    if not html:
        return []
    parser = _LinkExtractor()
    parser.feed(html)
    # Deduplicate by URL
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for link in parser.links:
        if link["url"] not in seen:
            seen.add(link["url"])
            unique.append(link)
    return unique


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
        classified = classify_error(e)
        logger.exception("gmail_list_emails failed [%s]: %s", classified.category, classified.log_message)
        return _error(f"Failed to list emails ({classified.category}): {classified.log_message}")


# ---------------------------------------------------------------------------
# 2. gmail_get_email
# ---------------------------------------------------------------------------
@tool(
    "gmail_get_email",
"Get a specific Gmail email by message ID. Returns full headers, body text, links, and slack_links (pre-formatted for Slack). When reporting emails, copy slack_links values verbatim — NEVER show raw URLs.",
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

        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        body = _extract_body(payload)
        links = _extract_links(payload)

        slack_links = [f"<{link['url']}|{link['text']}>" for link in links]

        result = {
            "id": msg["id"],
            "threadId": msg.get("threadId", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "labels": msg.get("labelIds", []),
            "body": body,
            "links": links,
            "slack_links": slack_links,
        }

        return _text(json.dumps(result, indent=2))
    except Exception as e:
        classified = classify_error(e)
        logger.exception("gmail_get_email failed [%s]: %s", classified.category, classified.log_message)
        return _error(f"Failed to get email ({classified.category}): {classified.log_message}")


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
        classified = classify_error(e)
        logger.exception("gmail_mark_as_read failed [%s]: %s", classified.category, classified.log_message)
        return _error(f"Failed to mark as read ({classified.category}): {classified.log_message}")


# ---------------------------------------------------------------------------
# 4. gmail_star_email
# ---------------------------------------------------------------------------
@tool(
    "gmail_star_email",
    "Star or unstar a Gmail email. Starred emails appear under the 'Starred' label in Gmail.",
    {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "The Gmail message ID to star or unstar.",
            },
            "star": {
                "type": "boolean",
                "description": "True to star, false to unstar. Defaults to true.",
            },
        },
        "required": ["message_id"],
    },
)
async def gmail_star_email(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_gmail_service()
        message_id = args["message_id"]
        star = args.get("star", True)

        if star:
            body = {"addLabelIds": ["STARRED"]}
        else:
            body = {"removeLabelIds": ["STARRED"]}

        service.users().messages().modify(
            userId="me",
            id=message_id,
            body=body,
        ).execute()

        action = "starred" if star else "unstarred"
        return _text(f"Message {message_id} {action}.")
    except Exception as e:
        classified = classify_error(e)
        logger.exception("gmail_star_email failed [%s]: %s", classified.category, classified.log_message)
        return _error(f"Failed to star email ({classified.category}): {classified.log_message}")


# ---------------------------------------------------------------------------
# 5. gmail_check_alerted — dedup helper for scheduled email alerts
# ---------------------------------------------------------------------------
_ALERTED_FILE_DEFAULT = "data/alerted_emails.json"
_MAX_ALERTED_IDS = 500


def _load_alerted() -> set[str]:
    path = os.environ.get("GMAIL_ALERTED_FILE", _ALERTED_FILE_DEFAULT)
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f))
    return set()


def _save_alerted(ids: set[str]) -> None:
    path = os.environ.get("GMAIL_ALERTED_FILE", _ALERTED_FILE_DEFAULT)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Keep only the most recent IDs to prevent unbounded growth
    trimmed = sorted(ids)[-_MAX_ALERTED_IDS:]
    with open(path, "w") as f:
        json.dump(trimmed, f)


@tool(
    "gmail_check_alerted",
    "Check which email IDs have already been alerted on, and mark new ones as alerted. Returns only the IDs that have NOT been alerted before (i.e., new emails to report).",
    {
        "type": "object",
        "properties": {
            "message_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of Gmail message IDs to check.",
            },
        },
        "required": ["message_ids"],
    },
)
async def gmail_check_alerted(args: dict[str, Any]) -> dict[str, Any]:
    try:
        message_ids = args["message_ids"]
        alerted = _load_alerted()
        new_ids = [mid for mid in message_ids if mid not in alerted]
        alerted.update(new_ids)
        _save_alerted(alerted)
        return _text(json.dumps(new_ids))
    except Exception as e:
        classified = classify_error(e)
        logger.exception("gmail_check_alerted failed [%s]: %s", classified.category, classified.log_message)
        return _error(f"Failed to check alerted emails ({classified.category}): {classified.log_message}")


# ---------------------------------------------------------------------------
# 6. gmail_list_labels
# ---------------------------------------------------------------------------
@tool(
    "gmail_list_labels",
    "List all Gmail labels. Returns a JSON list with id, name, and type for each label. Use this to find the label ID for a given label name before applying it to a message.",
    {
        "type": "object",
        "properties": {},
    },
)
async def gmail_list_labels(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_gmail_service()
        response = service.users().labels().list(userId="me").execute()
        labels = response.get("labels", [])
        results = [
            {
                "id": label["id"],
                "name": label["name"],
                "type": label.get("type", "user"),
            }
            for label in labels
        ]
        return _text(json.dumps(results, indent=2))
    except Exception as e:
        classified = classify_error(e)
        logger.exception("gmail_list_labels failed [%s]: %s", classified.category, classified.log_message)
        return _error(f"Failed to list labels ({classified.category}): {classified.log_message}")


# ---------------------------------------------------------------------------
# 7. gmail_create_label
# ---------------------------------------------------------------------------
@tool(
    "gmail_create_label",
    "Create a new Gmail label. Returns the created label's id and name. Use gmail_list_labels first to check if the label already exists.",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The name of the label to create (e.g., '2026 taxes').",
            },
        },
        "required": ["name"],
    },
)
async def gmail_create_label(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_gmail_service()
        label_name = args["name"]
        label_body = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = (
            service.users()
            .labels()
            .create(userId="me", body=label_body)
            .execute()
        )
        result = {"id": created["id"], "name": created["name"]}
        return _text(json.dumps(result, indent=2))
    except Exception as e:
        classified = classify_error(e)
        logger.exception("gmail_create_label failed [%s]: %s", classified.category, classified.log_message)
        return _error(f"Failed to create label ({classified.category}): {classified.log_message}")


# ---------------------------------------------------------------------------
# 8. gmail_modify_labels
# ---------------------------------------------------------------------------
@tool(
    "gmail_modify_labels",
    "Add or remove labels from a Gmail email. Use gmail_list_labels to find label IDs first.",
    {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "The Gmail message ID.",
            },
            "add_label_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Label IDs to add to the message.",
            },
            "remove_label_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Label IDs to remove from the message.",
            },
        },
        "required": ["message_id"],
    },
)
async def gmail_modify_labels(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = _get_gmail_service()
        message_id = args["message_id"]
        add_ids = args.get("add_label_ids", [])
        remove_ids = args.get("remove_label_ids", [])

        if not add_ids and not remove_ids:
            return _error("Must provide add_label_ids or remove_label_ids (or both).")

        body: dict[str, Any] = {}
        if add_ids:
            body["addLabelIds"] = add_ids
        if remove_ids:
            body["removeLabelIds"] = remove_ids

        service.users().messages().modify(
            userId="me",
            id=message_id,
            body=body,
        ).execute()

        parts = []
        if add_ids:
            parts.append(f"added labels {add_ids}")
        if remove_ids:
            parts.append(f"removed labels {remove_ids}")
        return _text(f"Message {message_id}: {', '.join(parts)}.")
    except Exception as e:
        classified = classify_error(e)
        logger.exception("gmail_modify_labels failed [%s]: %s", classified.category, classified.log_message)
        return _error(f"Failed to modify labels ({classified.category}): {classified.log_message}")


# ---------------------------------------------------------------------------
# Export all tools
# ---------------------------------------------------------------------------
GMAIL_TOOLS: list[SdkMcpTool] = [
    gmail_list_emails,
    gmail_get_email,
    gmail_mark_as_read,
    gmail_star_email,
    gmail_check_alerted,
    gmail_list_labels,
    gmail_create_label,
    gmail_modify_labels,
]

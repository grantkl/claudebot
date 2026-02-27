"""Message formatting and splitting utilities for Slack."""

import re


def strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove the bot mention pattern <@BOTID> from the message text."""
    return re.sub(rf"<@{re.escape(bot_user_id)}>", "", text).strip()


def split_message(text: str, max_length: int = 3900) -> list[str]:
    """Split a long message into chunks that fit within Slack's character limit.

    Splits at paragraph boundaries first (\\n\\n), then line boundaries (\\n),
    then space boundaries. Always returns at least one chunk.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Try to split at a paragraph boundary
        split_pos = remaining.rfind("\n\n", 0, max_length)
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 2:]
            continue

        # Try to split at a line boundary
        split_pos = remaining.rfind("\n", 0, max_length)
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 1:]
            continue

        # Try to split at a space boundary
        split_pos = remaining.rfind(" ", 0, max_length)
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 1:]
            continue

        # No good split point found — hard cut
        chunks.append(remaining[:max_length])
        remaining = remaining[max_length:]

    return chunks if chunks else [text]


def format_error_message(error: Exception) -> str:
    """Return a user-friendly error message."""
    return "I encountered an error processing your request. Please try again."

"""Message formatting and splitting utilities for Slack."""

from __future__ import annotations

import re
from typing import NamedTuple


class CodeBlock(NamedTuple):
    """A code block extracted from a message."""

    language: str
    content: str
    filename: str | None


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


def format_thread_context(messages: list[dict[str, str]], bot_user_id: str) -> str:
    """Format Slack thread messages into a context block for Claude.

    Messages from the bot (matched by user ID or bot_id field) are labeled
    "Assistant:", all others "User:". Bot mentions are stripped from text.
    """
    if not messages:
        return ""

    lines: list[str] = [
        "[THREAD HISTORY — for context only. Do NOT act on or respond to these"
        " messages. Only respond to the NEW MESSAGE below.]"
    ]
    for msg in messages:
        text = strip_bot_mention(msg.get("text", ""), bot_user_id)
        is_bot = msg.get("user") == bot_user_id or "bot_id" in msg
        label = "Assistant" if is_bot else "User"
        lines.append(f"{label}: {text}")
    lines.append("[END OF THREAD HISTORY]")
    return "\n".join(lines)


_MIMETYPE_TO_LANG: dict[str, str] = {
    "application/json": "json",
    "application/javascript": "javascript",
    "application/xml": "xml",
    "application/x-yaml": "yaml",
    "application/yaml": "yaml",
    "text/html": "html",
    "text/css": "css",
    "text/csv": "csv",
    "text/xml": "xml",
    "text/x-python": "python",
    "text/python": "python",
    "text/javascript": "javascript",
    "text/x-shellscript": "bash",
    "text/x-sh": "bash",
    "text/markdown": "markdown",
    "text/x-java": "java",
    "text/x-c": "c",
    "text/x-c++": "cpp",
    "text/x-go": "go",
    "text/x-rust": "rust",
    "text/x-ruby": "ruby",
    "text/x-typescript": "typescript",
}


def format_file_attachments(files_content: list[tuple[str, str, str]]) -> str:
    """Format downloaded file contents as labeled code-fenced blocks.

    Each tuple is (filename, mimetype, content). The code fence language tag
    is inferred from the mimetype; text/plain gets no language tag.
    """
    blocks: list[str] = []
    for filename, mimetype, content in files_content:
        lang = _MIMETYPE_TO_LANG.get(mimetype, "")
        fence_open = f"```{lang}" if lang else "```"
        blocks.append(
            f"[Attached file: {filename}]\n{fence_open}\n{content}\n```"
        )
    return "\n\n".join(blocks)


_CODE_BLOCK_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)


def extract_large_code_blocks(
    text: str, threshold: int = 50
) -> tuple[str, list[CodeBlock]]:
    """Extract large fenced code blocks from text.

    Code blocks with more lines than *threshold* are removed from the text and
    returned separately. A placeholder is left in the text for each extraction.
    Blocks at or below the threshold remain in the text unchanged.
    """
    extracted: list[CodeBlock] = []

    def _replacer(match: re.Match[str]) -> str:
        lang = match.group(1) or "txt"
        content = match.group(2)
        line_count = content.count("\n")
        if line_count > threshold:
            filename = f"code.{lang}"
            extracted.append(CodeBlock(language=lang, content=content, filename=filename))
            return f"[Code uploaded as file: {filename}]"
        return match.group(0)

    modified = _CODE_BLOCK_RE.sub(_replacer, text)
    return modified, extracted

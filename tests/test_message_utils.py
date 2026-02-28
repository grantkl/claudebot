"""Tests for src.message_utils module."""

from src.message_utils import (
    CodeBlock,
    extract_large_code_blocks,
    format_error_message,
    format_file_attachments,
    format_thread_context,
    split_message,
    strip_bot_mention,
)


class TestStripBotMention:
    def test_removes_bot_mention(self):
        assert strip_bot_mention("<@B001> hello", "B001") == "hello"

    def test_handles_text_without_mention(self):
        assert strip_bot_mention("hello world", "B001") == "hello world"

    def test_handles_multiple_mentions(self):
        result = strip_bot_mention("<@B001> hello <@B001> world", "B001")
        assert result == "hello  world"

    def test_strips_surrounding_whitespace(self):
        assert strip_bot_mention("  <@B001>  hi  ", "B001") == "hi"

    def test_does_not_remove_other_mentions(self):
        result = strip_bot_mention("<@B001> <@U999> hello", "B001")
        assert "<@U999>" in result


class TestSplitMessage:
    def test_short_text_returns_single_chunk(self):
        text = "Hello world"
        result = split_message(text)
        assert result == ["Hello world"]

    def test_exact_max_length_returns_single_chunk(self):
        text = "a" * 3900
        result = split_message(text)
        assert result == [text]

    def test_splits_at_paragraph_boundary(self):
        paragraph1 = "a" * 2000
        paragraph2 = "b" * 2000
        text = paragraph1 + "\n\n" + paragraph2
        result = split_message(text, max_length=3000)
        assert result[0] == paragraph1
        assert result[1] == paragraph2

    def test_splits_at_line_boundary_when_no_paragraphs(self):
        line1 = "a" * 2000
        line2 = "b" * 2000
        text = line1 + "\n" + line2
        result = split_message(text, max_length=3000)
        assert result[0] == line1
        assert result[1] == line2

    def test_splits_at_word_boundary_as_last_resort(self):
        word1 = "a" * 2000
        word2 = "b" * 2000
        text = word1 + " " + word2
        result = split_message(text, max_length=3000)
        assert result[0] == word1
        assert result[1] == word2

    def test_hard_cut_when_no_boundaries(self):
        text = "a" * 8000
        result = split_message(text, max_length=3000)
        assert len(result) == 3
        assert result[0] == "a" * 3000
        assert result[1] == "a" * 3000
        assert result[2] == "a" * 2000


class TestFormatErrorMessage:
    def test_returns_user_friendly_string(self):
        error = RuntimeError("something broke")
        result = format_error_message(error)
        assert result == "I encountered an error processing your request. Please try again."


class TestFormatThreadContext:
    def test_mixed_user_and_bot_messages(self):
        messages = [
            {"user": "U001", "text": "<@B001> How do I reset my password?"},
            {"user": "B001", "text": "You can reset it at settings."},
            {"user": "U001", "text": "What about 2FA?"},
        ]
        result = format_thread_context(messages, "B001")
        assert result == (
            "[Previous messages in this thread:]\n"
            "User: How do I reset my password?\n"
            "Assistant: You can reset it at settings.\n"
            "User: What about 2FA?"
        )

    def test_empty_messages_list(self):
        result = format_thread_context([], "B001")
        assert result == ""

    def test_bot_detected_by_user_id(self):
        messages = [
            {"user": "B001", "text": "I can help with that."},
        ]
        result = format_thread_context(messages, "B001")
        assert "Assistant: I can help with that." in result

    def test_bot_detected_by_bot_id_field(self):
        messages = [
            {"user": "USLACK", "bot_id": "BXYZ", "text": "Automated reply."},
        ]
        result = format_thread_context(messages, "B001")
        assert "Assistant: Automated reply." in result


class TestFormatFileAttachments:
    def test_single_json_file(self):
        files = [("config.json", "application/json", '{"key": "value"}')]
        result = format_file_attachments(files)
        assert result == (
            '[Attached file: config.json]\n```json\n{"key": "value"}\n```'
        )

    def test_multiple_files(self):
        files = [
            ("app.py", "text/x-python", "print('hi')"),
            ("data.json", "application/json", "{}"),
        ]
        result = format_file_attachments(files)
        parts = result.split("\n\n")
        assert len(parts) == 2
        assert "[Attached file: app.py]" in parts[0]
        assert "```python" in parts[0]
        assert "[Attached file: data.json]" in parts[1]
        assert "```json" in parts[1]

    def test_plain_text_file_no_language_tag(self):
        files = [("notes.txt", "text/plain", "some notes")]
        result = format_file_attachments(files)
        assert result == "[Attached file: notes.txt]\n```\nsome notes\n```"


class TestExtractLargeCodeBlocks:
    def test_block_above_threshold_extracted(self):
        code = "\n".join(f"line {i}" for i in range(60))
        text = f"Before\n```python\n{code}\n```\nAfter"
        modified, blocks = extract_large_code_blocks(text, threshold=50)
        assert "[Code uploaded as file: code.python]" in modified
        assert "After" in modified
        assert len(blocks) == 1
        assert blocks[0].language == "python"
        assert blocks[0].filename == "code.python"
        assert "line 0" in blocks[0].content

    def test_block_below_threshold_stays(self):
        code = "\n".join(f"line {i}" for i in range(10))
        text = f"Before\n```python\n{code}\n```\nAfter"
        modified, blocks = extract_large_code_blocks(text, threshold=50)
        assert blocks == []
        assert "```python" in modified
        assert "line 0" in modified

    def test_multiple_blocks_mixed(self):
        small_code = "\n".join(f"s{i}" for i in range(5))
        big_code = "\n".join(f"b{i}" for i in range(60))
        text = (
            f"Intro\n```js\n{small_code}\n```\n"
            f"Middle\n```go\n{big_code}\n```\nEnd"
        )
        modified, blocks = extract_large_code_blocks(text, threshold=50)
        assert "```js" in modified  # small block stays
        assert "[Code uploaded as file: code.go]" in modified
        assert len(blocks) == 1
        assert blocks[0].language == "go"

    def test_no_code_blocks_unchanged(self):
        text = "Just some plain text\nwith newlines."
        modified, blocks = extract_large_code_blocks(text)
        assert modified == text
        assert blocks == []

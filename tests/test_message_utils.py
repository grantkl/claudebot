"""Tests for src.message_utils module."""

from src.message_utils import format_error_message, split_message, strip_bot_mention


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

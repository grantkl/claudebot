"""Tests for src.allowlist module."""

from src.allowlist import REJECTION_MESSAGE, is_user_allowed


class TestIsUserAllowed:
    def test_allowed_user_returns_true(self):
        allowed = {"U001", "U002", "U003"}
        assert is_user_allowed("U001", allowed) is True

    def test_non_allowed_user_returns_false(self):
        allowed = {"U001", "U002", "U003"}
        assert is_user_allowed("U999", allowed) is False

    def test_empty_set_returns_false(self):
        assert is_user_allowed("U001", set()) is False


class TestRejectionMessage:
    def test_rejection_message_value(self):
        assert REJECTION_MESSAGE == "No Tokens for you!"

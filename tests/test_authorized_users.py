"""Tests for authorized user checking."""

from src.authorized_users import is_authorized, is_superuser


class TestIsAuthorized:
    def test_authorized_user(self) -> None:
        assert is_authorized("U111", {"U111", "U222"}) is True

    def test_unauthorized_user(self) -> None:
        assert is_authorized("U999", {"U111", "U222"}) is False

    def test_empty_authorized_set(self) -> None:
        assert is_authorized("U111", set()) is False


class TestIsSuperuser:
    def test_superuser_in_set(self) -> None:
        assert is_superuser("U111", {"U111", "U222"}) is True

    def test_not_superuser(self) -> None:
        assert is_superuser("U999", {"U111", "U222"}) is False

    def test_empty_superuser_set(self) -> None:
        assert is_superuser("U111", set()) is False

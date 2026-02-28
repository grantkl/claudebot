"""Tests for src.rate_limiter module."""

from unittest.mock import patch

from src.rate_limiter import RATE_LIMIT_MESSAGE, RateLimiter


class TestRateLimiter:
    def test_enabled_when_max_messages_positive(self):
        rl = RateLimiter(max_messages=5, window_seconds=60)
        assert rl.enabled is True

    def test_disabled_when_max_messages_zero(self):
        rl = RateLimiter(max_messages=0, window_seconds=60)
        assert rl.enabled is False

    def test_disabled_when_max_messages_negative(self):
        rl = RateLimiter(max_messages=-1, window_seconds=60)
        assert rl.enabled is False

    def test_always_allows_when_disabled(self):
        rl = RateLimiter(max_messages=0, window_seconds=60)
        for _ in range(100):
            assert rl.check_and_record("U001") is True

    def test_allows_up_to_max_messages(self):
        rl = RateLimiter(max_messages=3, window_seconds=60)
        assert rl.check_and_record("U001") is True
        assert rl.check_and_record("U001") is True
        assert rl.check_and_record("U001") is True

    def test_rejects_after_max_messages(self):
        rl = RateLimiter(max_messages=3, window_seconds=60)
        for _ in range(3):
            rl.check_and_record("U001")
        assert rl.check_and_record("U001") is False

    def test_rejected_messages_do_not_count(self):
        rl = RateLimiter(max_messages=2, window_seconds=60)
        assert rl.check_and_record("U001") is True
        assert rl.check_and_record("U001") is True
        assert rl.check_and_record("U001") is False
        assert rl.check_and_record("U001") is False
        # Still only 2 recorded timestamps
        assert len(rl._timestamps["U001"]) == 2

    def test_expired_timestamps_pruned(self):
        rl = RateLimiter(max_messages=2, window_seconds=60)
        base_time = 1000.0
        with patch("src.rate_limiter.time") as mock_time:
            mock_time.monotonic.return_value = base_time
            assert rl.check_and_record("U001") is True
            assert rl.check_and_record("U001") is True
            assert rl.check_and_record("U001") is False

            # Advance past window
            mock_time.monotonic.return_value = base_time + 61
            assert rl.check_and_record("U001") is True

    def test_independent_per_user_tracking(self):
        rl = RateLimiter(max_messages=1, window_seconds=60)
        assert rl.check_and_record("U001") is True
        assert rl.check_and_record("U001") is False
        # Different user should still be allowed
        assert rl.check_and_record("U002") is True

    def test_rate_limit_message_is_string(self):
        assert isinstance(RATE_LIMIT_MESSAGE, str)
        assert len(RATE_LIMIT_MESSAGE) > 0

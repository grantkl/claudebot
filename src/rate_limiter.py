"""Sliding-window per-user rate limiter."""

import time

RATE_LIMIT_MESSAGE = (
    "You've reached the message rate limit. Please try again later."
)


class RateLimiter:
    def __init__(self, max_messages: int, window_seconds: int) -> None:
        self._max_messages = max_messages
        self._window_seconds = window_seconds
        self._timestamps: dict[str, list[float]] = {}

    @property
    def enabled(self) -> bool:
        return self._max_messages > 0

    def check_and_record(self, user_id: str) -> bool:
        if not self.enabled:
            return True

        now = time.monotonic()
        cutoff = now - self._window_seconds

        timestamps = self._timestamps.get(user_id, [])
        timestamps = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= self._max_messages:
            self._timestamps[user_id] = timestamps
            return False

        timestamps.append(now)
        self._timestamps[user_id] = timestamps
        return True

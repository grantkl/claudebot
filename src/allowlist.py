"""User allowlist enforcement for the Slack bot."""

REJECTION_MESSAGE = "No Tokens for you!"


def is_user_allowed(user_id: str, allowed: set[str]) -> bool:
    """Check whether a user is in the allowed set. O(1) set lookup."""
    return user_id in allowed

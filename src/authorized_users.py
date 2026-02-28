"""Authorized user check for tiered access."""


def is_authorized(user_id: str, authorized: set[str]) -> bool:
    """Check whether a user is in the authorized set. O(1) set lookup."""
    return user_id in authorized

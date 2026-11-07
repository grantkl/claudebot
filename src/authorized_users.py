"""Authorized user check for tiered access."""

# Per-tier MCP server sets. Superusers get every registered server
# (ClaudeManager.registered_mcp_server_names). Shared by slack_app and the
# scheduler so the tiers can't drift apart.
AUTHORIZED_MCP_SERVERS: frozenset[str] = frozenset({
    "sonos", "homekit", "flights", "flight_watch", "google_flights", "scheduler",
    "stocks", "web_search", "shopping_list", "memory",
})
EVERYONE_MCP_SERVERS: frozenset[str] = frozenset({"stocks", "web_search"})

# Built-in tools blocked for every non-superuser tier.
FILESYSTEM_TOOLS: list[str] = ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]


def is_authorized(user_id: str, authorized: set[str]) -> bool:
    """Check whether a user is in the authorized set. O(1) set lookup."""
    return user_id in authorized


def is_superuser(user_id: str, superuser_ids: set[str]) -> bool:
    """Check whether a user is in the superuser set. O(1) set lookup."""
    return user_id in superuser_ids

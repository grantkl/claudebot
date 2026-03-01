# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests
pytest

# Run a single test file
pytest tests/test_slack_app.py

# Run a single test
pytest tests/test_slack_app.py::TestSlackApp::test_successful_message_flow

# Type checking (strict mode)
mypy src/

# Run the bot locally via Docker
docker compose up -d --build

# Restart after code changes
docker compose up -d --build

# View logs
docker compose logs -f claudebot
```

## Architecture

**Async Slack bot** using `slack-bolt` with per-thread Claude conversation sessions managed via the Claude Agent SDK.

### Message Flow

```
Slack Event (mention or DM)
  → slack_app._handle_message()
  → Three-tier auth check (is_superuser → is_authorized → everyone)
  → If not authorized: rate limit check
  → Session eviction if tier mismatch (lower-tier user in higher-tier thread)
  → ClaudeManager.send_message(thread_ts, text, model, mcp_server_names, disallowed_tools)
  → Response split at 3900 chars → Slack reply
```

### Tiered Access Model

Three tiers, determined by `SUPERUSER_IDS` and `AUTHORIZED_USER_IDS` env vars. Nobody is rejected outright.

| Tier | Model | MCP Servers | Blocked Tools | Rate Limited |
|---|---|---|---|---|
| Superuser | opus | sonos + homekit + gmail | None | No |
| Authorized | sonnet | sonos + homekit | Bash, Read, Edit, Write, Glob, Grep | No |
| Everyone else | haiku | None | Bash, Read, Edit, Write, Glob, Grep | Yes |

**Security:** Non-superuser tiers have filesystem tools blocked to prevent capability discovery (e.g., reading source code to find that Gmail MCP exists). When a session has fewer MCP servers than what's available globally, a generic system prompt instructs Claude not to mention or suggest unavailable capabilities. Session eviction prevents a lower-tier user from inheriting a higher-tier session in the same thread.

### Per-Thread Sessions

Claude sessions are keyed by Slack `thread_ts`, not by user. The same thread shares a single Claude conversation context. Sessions are auto-evicted after `SESSION_TTL_SECONDS` (default 1 hour) by a background cleanup loop. Each session has its own `asyncio.Lock` to prevent concurrent message handling.

### Docker Auth Proxy

In Docker, an nginx sidecar (`auth-proxy`) injects real credentials into outbound Anthropic API calls. The bot container only sees a dummy token and routes requests through `http://auth-proxy:8080`. Real tokens stay in `.env` on the host, never reaching the bot container.

### MCP Integrations

When `ENABLE_MCP=true`, MCP servers are built once at startup and selectively injected into Claude sessions based on user tier. Available servers:

- **Sonos** — always loaded; controls Sonos speakers via configured IPs or network discovery
- **HomeKit** — always loaded; controls HomeKit devices via pairing data from a JSON file (or HomeClaw HTTP bridge if `HOMECLAW_MCP_URL` is set)
- **Gmail** — conditionally loaded when both `GMAIL_CREDENTIALS_FILE` and `GMAIL_TOKEN_FILE` are set; read-only (list, search, read, mark-as-read — no send). Superuser-only. OAuth setup: `python scripts/gmail-auth.py`

## Testing Conventions

External modules (`slack_bolt`, `claude_agent_sdk`) must be mock-injected into `sys.modules` **before** importing source modules, due to import-time initialization. See `test_slack_app.py` and `test_claude_client.py` for the pattern:

```python
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())
sys.modules.setdefault("claude_agent_sdk", MagicMock())

from src.slack_app import create_app  # must come after mocks
```

Use `AsyncMock` for async methods, `MagicMock` for sync. Config is mocked via `_make_config()` helpers returning `MagicMock` with attributes set. Environment variable tests use `@patch.dict("os.environ", {...}, clear=True)`.

## Configuration

Required env vars: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`. All others are optional. See `.env.example` for the full list.

- `AUTHORIZED_USER_IDS` — comma-separated Slack user IDs for authorized (sonnet) access
- `SUPERUSER_IDS` — comma-separated Slack user IDs for superuser (opus + gmail) access; must also be in `AUTHORIZED_USER_IDS` or they'll be auto-promoted
- `GMAIL_CREDENTIALS_FILE` / `GMAIL_TOKEN_FILE` — paths to Google OAuth2 credentials and token files for Gmail MCP

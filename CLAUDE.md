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
  → Authorization check (is_authorized)
  → If not authorized: rate limit check → haiku model
  → If authorized: skip rate limit → sonnet model
  → ClaudeManager.send_message(thread_ts, text, model)
  → Response split at 3900 chars → Slack reply
```

### Tiered Access Model

Users in `AUTHORIZED_USER_IDS` get the sonnet model with no rate limiting. Everyone else gets haiku with sliding-window rate limiting. Nobody is rejected outright.

### Per-Thread Sessions

Claude sessions are keyed by Slack `thread_ts`, not by user. The same thread shares a single Claude conversation context. Sessions are auto-evicted after `SESSION_TTL_SECONDS` (default 1 hour) by a background cleanup loop. Each session has its own `asyncio.Lock` to prevent concurrent message handling.

### Docker Auth Proxy

In Docker, an nginx sidecar (`auth-proxy`) injects real credentials into outbound Anthropic API calls. The bot container only sees a dummy token and routes requests through `http://auth-proxy:8080`. Real tokens stay in `.env` on the host, never reaching the bot container.

### MCP Integrations

When `ENABLE_MCP=true`, HomeKit and Sonos MCP servers are built once at startup and injected into all Claude sessions. HomeKit uses pairing data from a JSON file; Sonos uses configured IPs or network discovery.

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

Required env vars: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`. All others are optional. See `.env.example` for the full list. `AUTHORIZED_USER_IDS` is a comma-separated list of Slack user IDs for tier-1 (sonnet) access.

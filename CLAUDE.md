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

# Type checking
ty check src/

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
| Superuser | opus | sonos + homekit + gmail + scheduler + flights + flight_watch + seats_aero + playwright | None | No |
| Authorized | sonnet | sonos + homekit + flights + flight_watch + scheduler | Bash, Read, Edit, Write, Glob, Grep | No |
| Everyone else | haiku | _(none)_ | Bash, Read, Edit, Write, Glob, Grep | Yes |

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
- **Scheduler** — conditionally loaded when `SCHEDULER_ENABLED=true`; manages autonomous background tasks (email digests, smart home routines, custom prompts) on cron schedules or polling intervals. Superuser-only. Tasks defined in `config/tasks.yaml`, state persisted in `data/scheduler_state.json`.
- **Flights** — conditionally loaded when `FLIGHTS_ENABLED=true`; subprocess stdio MCP server (`@privilegemendes/amadeus-mcp-server`) using the official Amadeus API. Available to superuser and authorized tiers (not free-tier users, since API calls have cost). Tools: search-flights, search-airports, flight-price-analysis, flight-inspiration, airport-routes, nearest-airports.
- **Flight Watch** — conditionally loaded when `FLIGHTS_ENABLED=true`; manages flight price watches with automatic periodic checks. Supports both route-based watches (origin/destination on flexible dates) and specific flight tracking by airline and flight number for booked itineraries. Superuser and authorized tiers. Tools: flight_watch_add, flight_watch_list, flight_watch_remove, flight_watch_record, flight_watch_history.
- **Seats Aero** — conditionally loaded when `SEATS_AERO_API_KEY` is set; searches award flight availability across 24 loyalty programs via the seats.aero Partner API. Independent of FLIGHTS_ENABLED. Superuser-only. Tools: award_search, award_trip_details.
- **Playwright** — conditionally loaded when `PLAYWRIGHT_ENABLED=true`; provides full browser automation via `@playwright/mcp` (subprocess stdio, runs headless via npx). Superuser-only. Enables navigating to URLs, clicking elements, filling forms, taking screenshots, and interacting with web pages.

### Autonomous Task Scheduler

When `SCHEDULER_ENABLED=true`, a background scheduler runs tasks on cron or interval schedules. Tasks execute via `ClaudeManager.send_message()` with full MCP server access and deliver results as Slack DMs to superusers.

**Task YAML schema** (`config/tasks.yaml`):
```yaml
tasks:
  - id: unique_task_id
    name: "Human-readable name"
    prompt: |
      The prompt sent to Claude when the task fires.
      End with NOTHING_TO_REPORT sentinel to suppress empty DMs.
    cron: "0 7 * * *"           # OR interval_seconds: 300
    mcp_servers: [gmail, homekit]
    output: dm                   # "dm" or "silent"
    model: sonnet                 # opus/sonnet/haiku
    enabled: true
    created_by: U12345          # Slack user ID of task owner (auto-set by MCP tool)
```

**Task ownership:** Tasks have an optional `created_by` field (Slack user ID) set automatically when created via MCP tools. The creator's tier determines which MCP servers the task can use (validated at creation time). Task result DMs go only to the task owner; tasks without an owner (legacy) broadcast to all superusers.

**Management:** Superusers can manage tasks via Slack conversation using scheduler MCP tools (`scheduler_list_tasks`, `scheduler_add_task`, `scheduler_update_task`, `scheduler_remove_task`, `scheduler_pause_task`, `scheduler_resume_task`, `scheduler_trigger_task`, `scheduler_reload`).

**Circuit breaker:** Tasks that fail 5 consecutive times are auto-paused with a DM notification.

### Webhook Server

When `WEBHOOK_ENABLED=true`, an optional HTTP webhook server starts on `WEBHOOK_PORT` (default `8081`) for external signal ingestion. External systems (e.g., monitoring alerts, CI/CD pipelines, IoT triggers) can send signals via `POST /webhook/signal` with Bearer token authentication (`WEBHOOK_SECRET`). The request body specifies a prompt and target Slack user IDs. The server reuses the existing tiered access model to determine which Claude model and MCP servers are available based on the target user's tier, then processes the prompt and DMs results to the specified users.

## Testing Conventions

External modules (`slack_bolt`, `claude_agent_sdk`) must be mock-injected into `sys.modules` **before** importing source modules, due to import-time initialization. See `test_slack_app.py` and `test_claude_client.py` for the pattern:

```python
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())
sys.modules.setdefault("claude_agent_sdk", MagicMock())

from src.slack_app import create_app  # must come after mocks
```

Use `AsyncMock` for async methods, `MagicMock` for sync. Config is mocked via `_make_config()` helpers returning `MagicMock` with attributes set. Environment variable tests use `@patch.dict("os.environ", {...}, clear=True)`.

## Slack App OAuth Scopes

The bot requires these OAuth scopes (configured at https://api.slack.com/apps under **OAuth & Permissions**). After changing scopes, reinstall the app to the workspace.

| Scope | Purpose |
|---|---|
| `app_mentions:read` | Receive @mention events |
| `channels:history` | Read messages in public channels (thread context hydration) |
| `chat:write` | Post replies |
| `files:read` | Download file attachments (images, text files) |
| `files:write` | Upload large code blocks as file snippets |
| `groups:history` | Read messages in private channels |
| `im:history` | Read DM messages |
| `im:write` | Open DM conversations (scheduler task result delivery) |
| `reactions:read` | Read reactions |
| `reactions:write` | Add/remove hourglass reaction while processing |

## Configuration

Required env vars: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`. All others are optional. See `.env.example` for the full list.

- `AUTHORIZED_USER_IDS` — comma-separated Slack user IDs for authorized (sonnet) access
- `SUPERUSER_IDS` — comma-separated Slack user IDs for superuser (opus + gmail) access; must also be in `AUTHORIZED_USER_IDS` or they'll be auto-promoted
- `GMAIL_CREDENTIALS_FILE` / `GMAIL_TOKEN_FILE` — paths to Google OAuth2 credentials and token files for Gmail MCP
- `SCHEDULER_ENABLED` — set to `true` to enable the autonomous task scheduler
- `SCHEDULER_TASKS_FILE` — path to tasks YAML file (default `config/tasks.yaml`)
- `SCHEDULER_STATE_FILE` — path to state JSON file (default `data/scheduler_state.json`)
- `SCHEDULER_CONCURRENCY` — max concurrent task executions (default `3`)
- `SCHEDULER_TIMEZONE` — timezone for cron schedules (default `US/Pacific`)
- `FLIGHTS_ENABLED` — set to `true` to enable the Amadeus flight search MCP
- `AMADEUS_CLIENT_ID` — Amadeus API client ID (from https://developers.amadeus.com/)
- `AMADEUS_CLIENT_SECRET` — Amadeus API client secret
- `FLIGHT_WATCH_FILE` — path to flight watch data file (default `data/flight_watches.json`)
- `SEATS_AERO_API_KEY` — seats.aero Partner API key for award flight availability search
- `PLAYWRIGHT_ENABLED` — set to `true` to enable the Playwright browser automation MCP
- `WEBHOOK_ENABLED` — set to `true` to enable the HTTP webhook server for external signal ingestion
- `WEBHOOK_PORT` — port for the webhook server (default `8081`)
- `WEBHOOK_SECRET` — Bearer token secret for authenticating webhook requests

# claudebot

Slack bot powered by the Claude Agent SDK with three-tier access control and MCP integrations for smart home, email, flights, stocks, web search, browser automation, and scheduled automation. Also exposes an HTTP webhook for external signal ingestion.

## Quick Start

```bash
cp .env.example .env   # fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN, and an auth token
docker compose up -d --build
docker compose logs -f claudebot
```

## Configuration

**Required:** `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and one of `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`.

**Access tiers** (set via env vars, nobody is rejected):

- **Superuser** (`SUPERUSER_IDS`) -- opus model, all MCP servers, no restrictions
- **Authorized** (`AUTHORIZED_USER_IDS`) -- sonnet model, Sonos/HomeKit/Flights/Flight Watch/Scheduler/Stocks/Web Search, filesystem tools blocked
- **Everyone else** -- haiku model, Stocks/Web Search, filesystem tools blocked, rate limited

**Feature flags:**

| Flag | Enables | Extra vars |
|------|---------|------------|
| `ENABLE_MCP=true` | All MCP integrations | `SONOS_SPEAKER_IPS`, `HOMEKIT_PAIRING_FILE` or `HOMECLAW_MCP_URL` |
| `GMAIL_CREDENTIALS_FILE` + `GMAIL_TOKEN_FILE` | Gmail access (superuser) | OAuth setup: `python scripts/gmail-auth.py` |
| `SCHEDULER_ENABLED=true` | Autonomous cron/interval tasks | `SCHEDULER_TASKS_FILE`, `SCHEDULER_TIMEZONE` |
| `FLIGHTS_ENABLED=true` | Flight search + price watches | `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` |
| `SEATS_AERO_API_KEY` | Award flight availability search (superuser) | Partner API key from seats.aero |
| `PLAYWRIGHT_ENABLED=true` | Browser automation (superuser) | Runs headless via npx |
| `STOCKS_ENABLED=true` | Stock quotes, options chains, technicals (all tiers) | No API key required |
| `BRAVE_API_KEY` | Web search via Brave Search API (all tiers) | Key from https://brave.com/search/api/ |
| `WEBHOOK_ENABLED=true` | HTTP webhook for external signals | `WEBHOOK_PORT`, `WEBHOOK_SECRET` |

## Features

- **Three-tier access control** — superuser / authorized / everyone with per-tier model, MCP server, and tool restrictions
- **Per-thread Claude sessions** — automatic TTL eviction, session-level locking, and tier-based session eviction
- **Sonos MCP** — control Sonos speakers via configured IPs or network discovery
- **HomeKit MCP** — control HomeKit devices via pairing data or HomeClaw HTTP bridge
- **Gmail MCP** — list, search, read, star/unstar, and mark emails as read (superuser-only, no send)
- **Flights MCP** — search flights, airports, price analysis, route inspiration via Amadeus API
- **Flight Watch MCP** — monitor flight prices with automatic periodic checks; track booked itineraries by airline and flight number
- **Seats Aero MCP** — search award flight availability across 24 loyalty programs (superuser-only)
- **Playwright MCP** — full browser automation: navigate, click, fill forms, take screenshots (superuser-only, headless)
- **Stocks MCP** — real-time stock quotes, options chains, and technical indicators via yfinance (all tiers)
- **Web Search MCP** — web search via Brave Search API for news, earnings, and current events (all tiers)
- **Scheduler** — autonomous cron/interval tasks with circuit breaker, task ownership, run-once support, and Slack DM delivery
- **Webhook server** — HTTP endpoint (`POST /webhook/signal`) for external systems to send signals for Claude analysis and Slack DM delivery, with Bearer token auth and tier-based access
- **File attachments** — reads text files and images from Slack messages, passes images to Claude as multimodal input
- **Large code blocks** — extracts large code blocks from responses and uploads them as Slack file snippets
- **Thread context hydration** — loads prior messages when joining an existing thread mid-conversation
- **Docker auth proxy** — nginx sidecar keeps real API tokens out of the bot container

## Webhook API

External systems can send signals for Claude analysis:

```bash
curl -X POST http://localhost:8081/webhook/signal \
  -H "Authorization: Bearer $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text": "Analyze this signal", "user_id": "U011E9TPW84"}'
```

| Field | Required | Description |
|-------|----------|-------------|
| `text` | Yes | Prompt sent to Claude |
| `user_id` | Yes | Slack user ID (determines model and MCP access) |
| `notify` | No | Slack user IDs to DM results (defaults to `[user_id]`) |
| `model` | No | Model override: `opus` / `sonnet` / `haiku` |

Returns `{"ok": true, "response": "..."}` on success.

## Development

```bash
pytest                          # run all tests
ty check src/                   # type checking
python scripts/gmail-auth.py    # one-time Gmail OAuth setup
```

## File Layout

```
src/                    # application code
  slack_app.py          #   Slack event handling, auth, message flow
  claude_client.py      #   Claude Agent SDK session management
  config.py             #   env var parsing and configuration
  scheduler.py          #   autonomous task scheduler
  webhook.py            #   HTTP webhook server for external signals
  mcp/                  #   MCP server implementations
    sonos_server.py     #     Sonos speaker control
    homekit_server.py   #     HomeKit device control
    gmail_server.py     #     Gmail read/star/mark-as-read
    scheduler_server.py #     scheduler task management tools
    flight_watch_server.py #  flight price watch management
    seats_aero_server.py #    award flight availability search
    stocks_server.py    #     stock quotes, options, technicals
config/tasks.yaml       # scheduler task definitions
data/                   # runtime state (scheduler, flight watches, alerted emails)
proxy/                  # nginx auth-proxy config
scripts/                # setup helpers (gmail-auth, homekit-pair, homeclaw-bridge)
tests/                  # pytest suite (mirrors src/ structure)
```

## See Also

See [CLAUDE.md](CLAUDE.md) for architecture details, message flow, testing conventions, and task YAML schema.

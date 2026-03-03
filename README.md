# claudebot

Slack bot powered by the Claude Agent SDK with three-tier access control and MCP integrations for smart home, email, flights, and scheduled automation.

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
- **Authorized** (`AUTHORIZED_USER_IDS`) -- sonnet model, Sonos/HomeKit/Flights/Flight Watch, filesystem tools blocked
- **Everyone else** -- haiku model, no MCP servers, filesystem tools blocked, rate limited

**Feature flags:**

| Flag | Enables | Extra vars |
|------|---------|------------|
| `ENABLE_MCP=true` | All MCP integrations | `SONOS_SPEAKER_IPS`, `HOMEKIT_PAIRING_FILE` or `HOMECLAW_MCP_URL` |
| `GMAIL_CREDENTIALS_FILE` + `GMAIL_TOKEN_FILE` | Gmail read-only access (superuser) | OAuth setup: `python scripts/gmail-auth.py` |
| `SCHEDULER_ENABLED=true` | Autonomous cron/interval tasks | `SCHEDULER_TASKS_FILE`, `SCHEDULER_TIMEZONE` |
| `FLIGHTS_ENABLED=true` | Flight search + price watches | `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` |

## Features

- Three-tier access control (superuser / authorized / everyone) with per-tier model and tool restrictions
- Per-thread Claude sessions with automatic TTL eviction and session-level locking
- MCP integrations: Sonos, HomeKit, Gmail (read-only), Amadeus flight search, flight price watches
- Autonomous task scheduler with cron/interval triggers, circuit breaker, and Slack DM delivery
- Docker auth proxy (nginx sidecar) keeps real API tokens out of the bot container
- Session eviction prevents lower-tier users from inheriting higher-tier thread sessions

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
  mcp/                  #   MCP server implementations (sonos, homekit, gmail, flights, scheduler)
config/tasks.yaml       # scheduler task definitions
data/                   # runtime state (scheduler, flight watches)
proxy/                  # nginx auth-proxy config
scripts/                # setup helpers (gmail-auth, homekit-pair, homeclaw-bridge)
tests/                  # pytest suite (mirrors src/ structure)
```

## See Also

See [CLAUDE.md](CLAUDE.md) for architecture details, message flow, testing conventions, and task YAML schema.

"""Configuration loading and validation for the Slack bot."""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    slack_bot_token: str
    slack_app_token: str
    authorized_user_ids: set[str]
    anthropic_api_key: str = ""
    claude_model: str = "sonnet"
    claude_system_prompt: str = (
        "You are a helpful AI assistant available through Slack."
    )
    session_ttl_seconds: int = 3600
    log_level: str = "INFO"
    enable_mcp: bool = False
    homekit_pairing_file: str = ""
    sonos_speaker_ips: list[str] = field(default_factory=list)
    rate_limit_messages: int = 0
    rate_limit_window_seconds: int = 3600
    superuser_ids: set[str] = field(default_factory=set)
    gmail_credentials_file: str = ""
    gmail_token_file: str = ""


def load_config() -> Config:
    """Load configuration from environment variables.

    Raises:
        ValueError: If any required environment variable is missing.
    """
    missing = []
    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not slack_bot_token:
        missing.append("SLACK_BOT_TOKEN")

    slack_app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not slack_app_token:
        missing.append("SLACK_APP_TOKEN")

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    authorized_raw = os.environ.get("AUTHORIZED_USER_IDS", "")
    authorized_user_ids = {
        uid.strip() for uid in authorized_raw.split(",") if uid.strip()
    }

    superuser_raw = os.environ.get("SUPERUSER_IDS", "")
    superuser_ids = {
        uid.strip() for uid in superuser_raw.split(",") if uid.strip()
    }

    return Config(
        slack_bot_token=slack_bot_token,
        slack_app_token=slack_app_token,
        anthropic_api_key=anthropic_api_key,
        authorized_user_ids=authorized_user_ids,
        claude_model=os.environ.get("CLAUDE_MODEL", "sonnet"),
        claude_system_prompt=os.environ.get(
            "CLAUDE_SYSTEM_PROMPT",
            "You are a helpful AI assistant available through Slack.",
        ),
        session_ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", "3600")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        enable_mcp=os.environ.get("ENABLE_MCP", "").lower() in ("1", "true", "yes"),
        homekit_pairing_file=os.environ.get("HOMEKIT_PAIRING_FILE", ""),
        sonos_speaker_ips=[
            ip.strip()
            for ip in os.environ.get("SONOS_SPEAKER_IPS", "").split(",")
            if ip.strip()
        ],
        rate_limit_messages=int(os.environ.get("RATE_LIMIT_MESSAGES", "0")),
        rate_limit_window_seconds=int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "3600")),
        superuser_ids=superuser_ids,
        gmail_credentials_file=os.environ.get("GMAIL_CREDENTIALS_FILE", ""),
        gmail_token_file=os.environ.get("GMAIL_TOKEN_FILE", ""),
    )

"""Configuration loading and validation for the Slack bot."""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    slack_bot_token: str
    slack_app_token: str
    allowed_user_ids: set[str]
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
    user_models: dict[str, str] = field(default_factory=dict)
    rate_limit_messages: int = 0
    rate_limit_window_seconds: int = 3600

    def get_model_for_user(self, user_id: str) -> str:
        return self.user_models.get(user_id, self.claude_model)


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

    allowed_raw = os.environ.get("ALLOWED_USER_IDS", "")
    if not allowed_raw:
        missing.append("ALLOWED_USER_IDS")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    allowed_user_ids = {
        uid.strip() for uid in allowed_raw.split(",") if uid.strip()
    }

    user_models: dict[str, str] = {}
    for entry in os.environ.get("USER_MODELS", "").split(","):
        entry = entry.strip()
        if ":" in entry:
            parts = entry.split(":", 1)
            uid = parts[0].strip()
            model = parts[1].strip()
            if uid and model:
                user_models[uid] = model

    return Config(
        slack_bot_token=slack_bot_token,
        slack_app_token=slack_app_token,
        anthropic_api_key=anthropic_api_key,
        allowed_user_ids=allowed_user_ids,
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
        user_models=user_models,
        rate_limit_messages=int(os.environ.get("RATE_LIMIT_MESSAGES", "0")),
        rate_limit_window_seconds=int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "3600")),
    )

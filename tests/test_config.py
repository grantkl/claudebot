"""Tests for src.config module."""

import pytest
from unittest.mock import patch

from src.config import Config, load_config


REQUIRED_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test-token",
    "SLACK_APP_TOKEN": "xapp-test-token",
}


class TestLoadConfig:
    @patch.dict(
        "os.environ",
        {**REQUIRED_ENV, "AUTHORIZED_USER_IDS": "U001,U002"},
        clear=True,
    )
    def test_valid_env_produces_correct_config(self):
        cfg = load_config()
        assert cfg.slack_bot_token == "xoxb-test-token"
        assert cfg.slack_app_token == "xapp-test-token"
        assert cfg.anthropic_api_key == ""
        assert cfg.authorized_user_ids == {"U001", "U002"}

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_all_required_vars_raises_value_error(self):
        with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
            load_config()

    @patch.dict(
        "os.environ",
        {k: v for k, v in REQUIRED_ENV.items() if k != "SLACK_APP_TOKEN"},
        clear=True,
    )
    def test_missing_single_required_var_raises_value_error(self):
        with pytest.raises(ValueError, match="SLACK_APP_TOKEN"):
            load_config()

    @patch.dict(
        "os.environ",
        {**REQUIRED_ENV, "AUTHORIZED_USER_IDS": "U001,U002"},
        clear=True,
    )
    def test_comma_separated_authorized_user_ids_parsed_into_set(self):
        cfg = load_config()
        assert isinstance(cfg.authorized_user_ids, set)
        assert cfg.authorized_user_ids == {"U001", "U002"}

    @patch.dict(
        "os.environ",
        {**REQUIRED_ENV, "AUTHORIZED_USER_IDS": " U001 , U002 , U003 "},
        clear=True,
    )
    def test_authorized_user_ids_strips_whitespace(self):
        cfg = load_config()
        assert cfg.authorized_user_ids == {"U001", "U002", "U003"}

    @patch.dict("os.environ", REQUIRED_ENV, clear=True)
    def test_default_claude_model(self):
        cfg = load_config()
        assert cfg.claude_model == "sonnet"

    @patch.dict("os.environ", REQUIRED_ENV, clear=True)
    def test_default_system_prompt(self):
        cfg = load_config()
        assert cfg.claude_system_prompt == (
            "You are a helpful AI assistant available through Slack."
        )

    @patch.dict("os.environ", REQUIRED_ENV, clear=True)
    def test_default_session_ttl(self):
        cfg = load_config()
        assert cfg.session_ttl_seconds == 3600

    @patch.dict("os.environ", REQUIRED_ENV, clear=True)
    def test_default_log_level(self):
        cfg = load_config()
        assert cfg.log_level == "INFO"

    @patch.dict(
        "os.environ",
        {
            **REQUIRED_ENV,
            "CLAUDE_MODEL": "claude-opus-4-20250514",
            "SESSION_TTL_SECONDS": "7200",
            "LOG_LEVEL": "DEBUG",
        },
        clear=True,
    )
    def test_optional_overrides(self):
        cfg = load_config()
        assert cfg.claude_model == "claude-opus-4-20250514"
        assert cfg.session_ttl_seconds == 7200
        assert cfg.log_level == "DEBUG"

    @patch.dict("os.environ", REQUIRED_ENV, clear=True)
    def test_default_rate_limit_messages_is_zero(self):
        cfg = load_config()
        assert cfg.rate_limit_messages == 0

    @patch.dict("os.environ", REQUIRED_ENV, clear=True)
    def test_default_rate_limit_window_seconds(self):
        cfg = load_config()
        assert cfg.rate_limit_window_seconds == 3600

    @patch.dict(
        "os.environ",
        {**REQUIRED_ENV, "RATE_LIMIT_MESSAGES": "20", "RATE_LIMIT_WINDOW_SECONDS": "120"},
        clear=True,
    )
    def test_rate_limit_overrides(self):
        cfg = load_config()
        assert cfg.rate_limit_messages == 20
        assert cfg.rate_limit_window_seconds == 120

    @patch.dict("os.environ", REQUIRED_ENV, clear=True)
    def test_missing_authorized_user_ids_results_in_empty_set(self):
        cfg = load_config()
        assert cfg.authorized_user_ids == set()

    @patch.dict(
        "os.environ",
        {**REQUIRED_ENV, "AUTHORIZED_USER_IDS": ""},
        clear=True,
    )
    def test_empty_authorized_user_ids_results_in_empty_set(self):
        cfg = load_config()
        assert cfg.authorized_user_ids == set()

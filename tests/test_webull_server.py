"""Tests for the Webull options MCP server tools.

These tests define the public interface for `src.mcp.webull_server`. The
module exposes two MCP tools — `options_buy` and `options_sell` — plus a
`WEBULL_TOOLS: list[SdkMcpTool]` export. Both tools POST to an external
HTTP API (configured via `OPTIONS_API_URL` + `OPTIONS_API_KEY`) and
return MCP-style results: `{"content": [{"type": "text", "text": ...}],
"is_error"?: bool}`.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# Install a fake claude_agent_sdk that preserves the decorated function
# as `.handler` on the resulting wrapper. Mirrors the pattern used by
# tests/test_seats_aero_server.py and tests/test_stocks_server.py.
class _FakeSdkMcpTool:
    def __init__(self, handler: Any, name: str, description: str, schema: Any) -> None:
        self.handler = handler
        self.name = name
        self.description = description
        self.schema = schema
        self.__name__ = name


def _fake_tool(name: str, description: str, schema: Any):  # noqa: ANN201
    def decorator(fn: Any) -> _FakeSdkMcpTool:
        return _FakeSdkMcpTool(fn, name, description, schema)
    return decorator


_mock_sdk = MagicMock()
_mock_sdk.tool = _fake_tool
_mock_sdk.SdkMcpTool = _FakeSdkMcpTool
sys.modules["claude_agent_sdk"] = _mock_sdk

import importlib  # noqa: E402

sys.modules.pop("src.mcp.webull_server", None)

from src.mcp import webull_server  # noqa: E402

importlib.reload(webull_server)

_options_buy = webull_server.options_buy.handler
_options_sell = webull_server.options_sell.handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


def _mock_response(status_code: int = 200, json_data: Any = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


def _make_client(response: MagicMock = None, side_effect: Exception = None) -> AsyncMock:
    """Mock httpx.AsyncClient as an async context manager exposing `.post`."""
    mock_client = AsyncMock()
    mock_post = AsyncMock(return_value=response, side_effect=side_effect)
    mock_client.post = mock_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _valid_buy_args(**overrides: Any) -> dict[str, Any]:
    args = {
        "ticker": "SPY",
        "strike": 500.0,
        "expiry": "2026-06-19",
        "option_type": "CALL",
        "qty": 1,
        "limit_price": 5.25,
    }
    args.update(overrides)
    return args


def _valid_sell_args(**overrides: Any) -> dict[str, Any]:
    args = {
        "ticker": "SPY",
        "strike": 500.0,
        "expiry": "2026-06-19",
        "option_type": "CALL",
        "qty": 1,
        "limit_price": 6.50,
    }
    args.update(overrides)
    return args


_ENV = {
    "OPTIONS_API_URL": "https://options.example.com",
    "OPTIONS_API_KEY": "test-secret-key-do-not-leak",
}


# ---------------------------------------------------------------------------
# options_buy — happy path + request-shape contracts
# ---------------------------------------------------------------------------
class TestOptionsBuyHappyPath:
    async def test_returns_order_id_and_submitted(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "ord-abc-123", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_buy(_valid_buy_args())

        assert not _is_error(result)
        text = _parse_text(result)
        assert "ord-abc-123" in text
        assert "submitted" in text.lower()

    async def test_request_body_shape(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "x", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                await _options_buy(_valid_buy_args(
                    ticker="SPY",
                    strike=500.0,
                    expiry="2026-06-19",
                    option_type="CALL",
                    qty=2,
                    limit_price=5.25,
                ))

        call = client.post.call_args
        body = call.kwargs.get("json")
        assert body is not None, "options_buy must POST a JSON body"
        assert body["ticker"] == "SPY"
        assert body["side"] == "BUY"
        assert body["qty"] == 2
        assert body["limit_price"] == 5.25
        contract = body["contract"]
        assert contract["strike"] == 500.0
        assert contract["expiry"] == "2026-06-19"
        assert contract["type"] == "CALL"

    async def test_sends_api_key_header(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "x", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                await _options_buy(_valid_buy_args())

        headers = client.post.call_args.kwargs.get("headers", {})
        assert headers.get("X-API-Key") == "test-secret-key-do-not-leak"

    async def test_ticker_uppercased(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "x", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                await _options_buy(_valid_buy_args(ticker="spy"))

        body = client.post.call_args.kwargs.get("json")
        assert body["ticker"] == "SPY"


# ---------------------------------------------------------------------------
# options_sell — happy path + request-shape contracts
# ---------------------------------------------------------------------------
class TestOptionsSellHappyPath:
    async def test_returns_order_id_and_submitted(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "ord-sell-789", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_sell(_valid_sell_args())

        assert not _is_error(result)
        text = _parse_text(result)
        assert "ord-sell-789" in text
        assert "submitted" in text.lower()

    async def test_request_body_uses_sell_side(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "x", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                await _options_sell(_valid_sell_args(
                    ticker="QQQ",
                    strike=420.0,
                    expiry="2026-07-17",
                    option_type="PUT",
                    qty=3,
                    limit_price=2.10,
                ))

        body = client.post.call_args.kwargs.get("json")
        assert body["ticker"] == "QQQ"
        assert body["side"] == "SELL"
        assert body["qty"] == 3
        assert body["limit_price"] == 2.10
        assert body["contract"]["strike"] == 420.0
        assert body["contract"]["expiry"] == "2026-07-17"
        assert body["contract"]["type"] == "PUT"

    async def test_sends_api_key_header(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "x", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                await _options_sell(_valid_sell_args())

        headers = client.post.call_args.kwargs.get("headers", {})
        assert headers.get("X-API-Key") == "test-secret-key-do-not-leak"

    async def test_ticker_uppercased(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "x", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                await _options_sell(_valid_sell_args(ticker="qqq"))

        body = client.post.call_args.kwargs.get("json")
        assert body["ticker"] == "QQQ"


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------
class TestHttpErrorHandling:
    async def test_401_invalid_api_key(self) -> None:
        client = _make_client(_mock_response(401, {"detail": "Unauthorized"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_buy(_valid_buy_args())

        assert _is_error(result)
        assert "Invalid API key" in _parse_text(result)

    async def test_502_includes_rejection_detail(self) -> None:
        client = _make_client(_mock_response(
            502,
            {"detail": "Webull rejected order: insufficient buying power"},
        ))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_buy(_valid_buy_args())

        assert _is_error(result)
        text = _parse_text(result)
        assert "Webull rejected order" in text
        assert "insufficient buying power" in text

    async def test_503_endpoint_not_configured(self) -> None:
        client = _make_client(_mock_response(503, {"detail": "broker not configured"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_buy(_valid_buy_args())

        assert _is_error(result)
        assert "not configured" in _parse_text(result).lower()

    async def test_500_generic_error(self) -> None:
        client = _make_client(_mock_response(500, text="Internal Server Error"))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_buy(_valid_buy_args())

        assert _is_error(result)
        text = _parse_text(result)
        assert "500" in text or "error" in text.lower()

    async def test_500_on_sell(self) -> None:
        client = _make_client(_mock_response(500, text="boom"))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_sell(_valid_sell_args())

        assert _is_error(result)


# ---------------------------------------------------------------------------
# Transport failures: "never reached the bridge" vs "order status unknown"
# ---------------------------------------------------------------------------
_REQ = httpx.Request("POST", "http://bridge/options/trade")


def _assert_unknown(result: dict[str, Any]) -> None:
    assert _is_error(result)
    text = _parse_text(result)
    assert "UNKNOWN" in text
    assert "Do NOT retry" in text
    assert "Webull" in text


class TestTransportFailures:
    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("refused", request=_REQ),
        httpx.ConnectTimeout("connect timed out", request=_REQ),
    ])
    async def test_never_sent_says_not_placed(self, exc: Exception) -> None:
        client = _make_client(side_effect=exc)

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_buy(_valid_buy_args())

        assert _is_error(result)
        text = _parse_text(result)
        assert "not placed" in text.lower()
        assert "UNKNOWN" not in text
        assert client.post.await_count == 1

    @pytest.mark.parametrize("exc", [
        httpx.ReadTimeout("read timed out", request=_REQ),
        httpx.WriteTimeout("write timed out", request=_REQ),
        httpx.RemoteProtocolError("server disconnected", request=_REQ),
        httpx.ReadError("connection reset", request=_REQ),
        RuntimeError("something odd"),
    ])
    async def test_maybe_sent_reports_unknown_buy(self, exc: Exception) -> None:
        client = _make_client(side_effect=exc)

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_buy(_valid_buy_args())

        _assert_unknown(result)
        # The tool itself must never resend the order.
        assert client.post.await_count == 1

    async def test_maybe_sent_reports_unknown_sell(self) -> None:
        client = _make_client(side_effect=httpx.ReadTimeout("read timed out", request=_REQ))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_sell(_valid_sell_args())

        _assert_unknown(result)

    async def test_unexpected_http_500_reports_unknown(self) -> None:
        # The bridge returns 500 if it fails after Webull accepted the order
        # (e.g. saving the order row), so a 500 does not mean "not placed".
        client = _make_client(_mock_response(500, text="Internal Server Error"))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_buy(_valid_buy_args())

        _assert_unknown(result)
        assert "500" in _parse_text(result)

    def test_tool_descriptions_forbid_resubmitting_unknown(self) -> None:
        for t in webull_server.WEBULL_TOOLS:
            desc = t.description
            assert "UNKNOWN" in desc
            assert "never resubmit" in desc.lower()


# ---------------------------------------------------------------------------
# Local validation (must reject before any HTTP call)
# ---------------------------------------------------------------------------
class TestLocalValidationBuy:
    async def test_qty_zero_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(qty=0))
        assert _is_error(result)
        assert "qty" in _parse_text(result).lower()
        mock_cls.assert_not_called()

    async def test_qty_negative_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(qty=-1))
        assert _is_error(result)
        mock_cls.assert_not_called()

    async def test_strike_zero_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(strike=0.0))
        assert _is_error(result)
        assert "strike" in _parse_text(result).lower()
        mock_cls.assert_not_called()

    async def test_strike_negative_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(strike=-50.0))
        assert _is_error(result)
        mock_cls.assert_not_called()

    async def test_limit_price_zero_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(limit_price=0.0))
        assert _is_error(result)
        assert "limit_price" in _parse_text(result).lower() or "price" in _parse_text(result).lower()
        mock_cls.assert_not_called()

    async def test_limit_price_negative_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(limit_price=-1.5))
        assert _is_error(result)
        mock_cls.assert_not_called()

    async def test_bad_option_type_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(option_type="STRADDLE"))
        assert _is_error(result)
        assert "option_type" in _parse_text(result).lower() or "CALL" in _parse_text(result)
        mock_cls.assert_not_called()

    async def test_bad_expiry_format_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(expiry="06/19/2026"))
        assert _is_error(result)
        assert "expiry" in _parse_text(result).lower()
        mock_cls.assert_not_called()

    async def test_garbage_expiry_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(expiry="not-a-date"))
        assert _is_error(result)
        mock_cls.assert_not_called()


class TestLocalValidationSell:
    async def test_qty_zero_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_sell(_valid_sell_args(qty=0))
        assert _is_error(result)
        mock_cls.assert_not_called()

    async def test_strike_zero_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_sell(_valid_sell_args(strike=0.0))
        assert _is_error(result)
        mock_cls.assert_not_called()

    async def test_bad_option_type_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_sell(_valid_sell_args(option_type="banana"))
        assert _is_error(result)
        mock_cls.assert_not_called()

    async def test_bad_expiry_rejected(self) -> None:
        client = _make_client(_mock_response(200))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_sell(_valid_sell_args(expiry="2026/06/19"))
        assert _is_error(result)
        mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Security: API key never appears in returned text
# ---------------------------------------------------------------------------
class TestApiKeyNotLeaked:
    async def test_key_not_in_success_text(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "ord-1", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_buy(_valid_buy_args())

        assert "test-secret-key-do-not-leak" not in _parse_text(result)

    async def test_key_not_in_error_text_401(self) -> None:
        client = _make_client(_mock_response(401, {"detail": "bad key"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_buy(_valid_buy_args())

        assert "test-secret-key-do-not-leak" not in _parse_text(result)

    async def test_key_not_in_error_text_500(self) -> None:
        client = _make_client(_mock_response(500, text="server explosion"))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client):
                result = await _options_sell(_valid_sell_args())

        assert "test-secret-key-do-not-leak" not in _parse_text(result)

    async def test_key_not_in_validation_error(self) -> None:
        with patch.dict("os.environ", _ENV, clear=True):
            result = await _options_buy(_valid_buy_args(qty=0))
        assert "test-secret-key-do-not-leak" not in _parse_text(result)


# ---------------------------------------------------------------------------
# Configuration guards (missing OPTIONS_API_KEY env)
# ---------------------------------------------------------------------------
class TestMissingConfig:
    async def test_missing_api_key_returns_not_configured_before_http(self) -> None:
        client = _make_client(_mock_response(200))

        # Only OPTIONS_API_URL set; no OPTIONS_API_KEY.
        with patch.dict("os.environ", {"OPTIONS_API_URL": "https://options.example.com"}, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args())

        assert _is_error(result)
        assert "not configured" in _parse_text(result).lower()
        mock_cls.assert_not_called()

    async def test_missing_api_key_on_sell(self) -> None:
        client = _make_client(_mock_response(200))

        with patch.dict("os.environ", {"OPTIONS_API_URL": "https://options.example.com"}, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_sell(_valid_sell_args())

        assert _is_error(result)
        assert "not configured" in _parse_text(result).lower()
        mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# MAX_OPTIONS_TRADE_USD safety guard
# ---------------------------------------------------------------------------
# Contract: when env var MAX_OPTIONS_TRADE_USD is set to a numeric value,
# the tools compute exposure = qty * limit_price * 100 (standard options
# contract multiplier) and reject orders whose exposure exceeds the limit
# BEFORE making any HTTP call. The error text includes both the configured
# limit and the computed exposure. When the env var is unset, no limit is
# enforced (an empty value counts as unset). When the env var is set to a
# value that is not a finite number, the trade is refused (fail-closed) so a
# typo can never silently remove the cap.
class TestMaxTradeSizeGuard:
    async def test_below_limit_allowed_buy(self) -> None:
        # qty=1 * limit=1.25 * 100 = $125, below $500 limit
        client = _make_client(_mock_response(200, {"order_id": "ok-1", "status": "submitted"}))
        env = {**_ENV, "MAX_OPTIONS_TRADE_USD": "500"}

        with patch.dict("os.environ", env, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(qty=1, limit_price=1.25))

        assert not _is_error(result)
        mock_cls.assert_called()

    async def test_above_limit_rejected_buy(self) -> None:
        # qty=4 * limit=2.00 * 100 = $800, above $500 limit
        client = _make_client(_mock_response(200, {"order_id": "should-not-fire", "status": "submitted"}))
        env = {**_ENV, "MAX_OPTIONS_TRADE_USD": "500"}

        with patch.dict("os.environ", env, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(qty=4, limit_price=2.00))

        assert _is_error(result)
        text = _parse_text(result)
        # Error mentions both the limit and the computed exposure
        assert "500" in text
        assert "800" in text
        mock_cls.assert_not_called()

    async def test_above_limit_rejected_sell(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "should-not-fire", "status": "submitted"}))
        env = {**_ENV, "MAX_OPTIONS_TRADE_USD": "500"}

        with patch.dict("os.environ", env, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_sell(_valid_sell_args(qty=4, limit_price=2.00))

        assert _is_error(result)
        text = _parse_text(result)
        assert "500" in text
        assert "800" in text
        mock_cls.assert_not_called()

    async def test_below_limit_allowed_sell(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "ok-2", "status": "submitted"}))
        env = {**_ENV, "MAX_OPTIONS_TRADE_USD": "500"}

        with patch.dict("os.environ", env, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_sell(_valid_sell_args(qty=1, limit_price=1.25))

        assert not _is_error(result)
        mock_cls.assert_called()

    async def test_unset_no_limit_enforced_buy(self) -> None:
        # Large exposure ($10,000) but no env var set — should pass through
        client = _make_client(_mock_response(200, {"order_id": "big", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):  # _ENV has no MAX_OPTIONS_TRADE_USD
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(qty=10, limit_price=10.00))

        assert not _is_error(result)
        mock_cls.assert_called()

    async def test_unset_no_limit_enforced_sell(self) -> None:
        client = _make_client(_mock_response(200, {"order_id": "big", "status": "submitted"}))

        with patch.dict("os.environ", _ENV, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_sell(_valid_sell_args(qty=10, limit_price=10.00))

        assert not _is_error(result)
        mock_cls.assert_called()

    async def test_invalid_limit_value_refuses_trade(self) -> None:
        # A set-but-unparsable cap must fail closed: no HTTP call, clear error.
        client = _make_client(_mock_response(200, {"order_id": "should-not-fire", "status": "submitted"}))
        env = {**_ENV, "MAX_OPTIONS_TRADE_USD": "foo"}

        with patch.dict("os.environ", env, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(qty=1, limit_price=1.25))

        assert _is_error(result)
        text = _parse_text(result)
        assert "MAX_OPTIONS_TRADE_USD" in text
        assert "foo" in text
        mock_cls.assert_not_called()

    async def test_formatted_limit_value_refuses_trade_sell(self) -> None:
        # "$500" / "1,000" are not plain floats -> refuse rather than trade uncapped.
        client = _make_client(_mock_response(200, {"order_id": "should-not-fire", "status": "submitted"}))
        env = {**_ENV, "MAX_OPTIONS_TRADE_USD": "$1,000"}

        with patch.dict("os.environ", env, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_sell(_valid_sell_args(qty=1, limit_price=1.25))

        assert _is_error(result)
        assert "MAX_OPTIONS_TRADE_USD" in _parse_text(result)
        mock_cls.assert_not_called()

    async def test_nan_limit_value_refuses_trade(self) -> None:
        # float("nan") parses, but every comparison with it is False -> would be no cap.
        client = _make_client(_mock_response(200, {"order_id": "should-not-fire", "status": "submitted"}))
        env = {**_ENV, "MAX_OPTIONS_TRADE_USD": "nan"}

        with patch.dict("os.environ", env, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(qty=10, limit_price=10.00))

        assert _is_error(result)
        mock_cls.assert_not_called()

    async def test_empty_limit_value_means_no_cap(self) -> None:
        # Empty string is treated the same as unset: no cap.
        client = _make_client(_mock_response(200, {"order_id": "big", "status": "submitted"}))
        env = {**_ENV, "MAX_OPTIONS_TRADE_USD": ""}

        with patch.dict("os.environ", env, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(qty=10, limit_price=10.00))

        assert not _is_error(result)
        mock_cls.assert_called()

    async def test_exactly_at_limit_allowed(self) -> None:
        # Boundary contract: exposure == limit is allowed (`<=` semantics).
        # qty=1 * limit=1.25 * 100 = $125, exactly at the $125 limit.
        client = _make_client(_mock_response(200, {"order_id": "boundary", "status": "submitted"}))
        env = {**_ENV, "MAX_OPTIONS_TRADE_USD": "125"}

        with patch.dict("os.environ", env, clear=True):
            with patch("src.mcp.webull_server.httpx.AsyncClient", return_value=client) as mock_cls:
                result = await _options_buy(_valid_buy_args(qty=1, limit_price=1.25))

        assert not _is_error(result)
        mock_cls.assert_called()


# ---------------------------------------------------------------------------
# WEBULL_TOOLS export
# ---------------------------------------------------------------------------
class TestWebullToolsExport:
    def test_exports_two_tools(self) -> None:
        tools = webull_server.WEBULL_TOOLS
        assert len(tools) == 2

    def test_tool_names(self) -> None:
        names = {t.name for t in webull_server.WEBULL_TOOLS}
        assert names == {"options_buy", "options_sell"}

    def test_tools_have_handlers(self) -> None:
        for t in webull_server.WEBULL_TOOLS:
            assert callable(t.handler)

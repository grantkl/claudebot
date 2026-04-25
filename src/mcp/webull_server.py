"""Webull MCP server tools for submitting options orders."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import httpx

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

DEFAULT_TRADE_URL = "http://host.docker.internal:8000"
OPTION_CONTRACT_MULTIPLIER = 100


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _parse_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and "detail" in data:
            return str(data["detail"])
        return str(data)
    except Exception:
        return resp.text or ""


async def _submit_trade(side: str, args: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ.get("OPTIONS_API_KEY", "")
    if not api_key:
        return _error("Options endpoint not configured (OPTIONS_API_KEY missing)")

    ticker = args.get("ticker", "")
    strike = args.get("strike")
    expiry = args.get("expiry", "")
    option_type_raw = args.get("option_type", "")
    qty = args.get("qty")
    limit_price = args.get("limit_price")

    if not isinstance(strike, (int, float)) or strike <= 0:
        return _error("Invalid strike: must be greater than 0")
    if not isinstance(qty, int) or qty <= 0:
        return _error("Invalid qty: must be a positive integer")
    if not isinstance(limit_price, (int, float)) or limit_price <= 0:
        return _error("Invalid limit_price: must be greater than 0")

    option_type = str(option_type_raw).upper()
    if option_type not in ("CALL", "PUT"):
        return _error("Invalid option_type: must be CALL or PUT")

    try:
        datetime.strptime(expiry, "%Y-%m-%d")
    except (ValueError, TypeError):
        return _error("Invalid expiry: must be YYYY-MM-DD format")

    max_trade_raw = os.environ.get("MAX_OPTIONS_TRADE_USD")
    if max_trade_raw is not None:
        try:
            max_trade = float(max_trade_raw)
            exposure = qty * limit_price * OPTION_CONTRACT_MULTIPLIER
            if exposure > max_trade:
                return _error(
                    f"Trade exposure ${exposure:g} exceeds MAX_OPTIONS_TRADE_USD limit of ${max_trade:g}"
                )
        except (ValueError, TypeError):
            pass

    base = os.environ.get("WEBULL_TRADE_URL", DEFAULT_TRADE_URL).rstrip("/")
    url = f"{base}/options/trade"
    headers = {"X-API-Key": api_key}
    body = {
        "ticker": ticker.upper(),
        "side": side,
        "contract": {
            "strike": strike,
            "expiry": expiry,
            "type": option_type,
        },
        "qty": qty,
        "limit_price": limit_price,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.RequestError as exc:
        return _error(f"Trade request failed: {type(exc).__name__}: {exc}")
    except Exception as exc:
        return _error(f"Trade request failed: {type(exc).__name__}: {exc}")

    status = resp.status_code
    if status == 200:
        try:
            data = resp.json()
        except Exception:
            data = {}
        order_id = data.get("order_id", "")
        order_status = data.get("status", "submitted")
        return _text(f"Order submitted. order_id={order_id} status={order_status}")
    if status == 401:
        return _error("Invalid API key")
    if status == 422:
        return _error(f"Validation error: {_parse_detail(resp)}")
    if status == 502:
        return _error(f"Webull rejected order: {_parse_detail(resp)}")
    if status == 503:
        return _error(f"Options endpoint not configured: {_parse_detail(resp)}")
    return _error(f"Trade failed (HTTP {status}): {resp.text}")


_TRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string", "description": "Underlying ticker symbol (e.g., 'SPY')."},
        "strike": {"type": "number", "description": "Strike price of the contract."},
        "expiry": {"type": "string", "description": "Contract expiration date in YYYY-MM-DD format."},
        "option_type": {
            "type": "string",
            "description": "Option type: CALL or PUT.",
            "enum": ["CALL", "PUT"],
        },
        "qty": {"type": "integer", "description": "Number of contracts."},
        "limit_price": {"type": "number", "description": "Limit price per contract."},
    },
    "required": ["ticker", "strike", "expiry", "option_type", "qty", "limit_price"],
}


@tool(
    "options_buy",
    "Submit a BUY order for a single options contract via the Webull trading service. Real money — only call when the user explicitly requests a trade with all parameters confirmed.",
    _TRADE_SCHEMA,
)
async def options_buy(args: dict[str, Any]) -> dict[str, Any]:
    return await _submit_trade("BUY", args)


@tool(
    "options_sell",
    "Submit a SELL order for a single options contract via the Webull trading service. Real money — only call when the user explicitly requests a trade with all parameters confirmed.",
    _TRADE_SCHEMA,
)
async def options_sell(args: dict[str, Any]) -> dict[str, Any]:
    return await _submit_trade("SELL", args)


WEBULL_TOOLS: list[SdkMcpTool] = [options_buy, options_sell]

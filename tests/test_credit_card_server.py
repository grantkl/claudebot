"""Light integration tests for the credit_card MCP server tool wrappers.

The pure rule engine has thorough coverage in ``test_credit_card_engine.py``.
This file just confirms the tool wrappers return the expected shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# Mock claude_agent_sdk so the server module can import without the real SDK.
def _make_sdk_mock() -> MagicMock:
    sdk = MagicMock()
    sdk.SdkMcpTool = MagicMock

    def _tool(name: str, description: str, schema: Any) -> Any:
        def decorator(fn: Any) -> Any:
            wrapper = MagicMock()
            wrapper.handler = fn
            wrapper.__name__ = fn.__name__
            return wrapper

        return decorator

    sdk.tool = _tool
    return sdk


sys.modules["claude_agent_sdk"] = _make_sdk_mock()
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())

import importlib  # noqa: E402

sys.modules.pop("src.mcp.credit_card_server", None)

from src.mcp import credit_card_server  # noqa: E402

importlib.reload(credit_card_server)


_whichcard = credit_card_server.whichcard.handler
_card_audit = credit_card_server.card_audit.handler
_credits_status = credit_card_server.credits_status.handler
_credits_mark_used = credit_card_server.credits_mark_used.handler
_cards_list = credit_card_server.cards_list.handler
_cards_get = credit_card_server.cards_get.handler
_merchant_categorize = credit_card_server.merchant_categorize.handler


def _payload(result: dict[str, Any]) -> Any:
    return json.loads(result["content"][0]["text"])


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


@pytest.fixture(autouse=True)
def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset singletons + point ledger to a temp file for each test."""

    monkeypatch.setenv("CREDIT_CARD_LEDGER", str(tmp_path / "ledger.json"))
    credit_card_server.reset_cache()
    credit_card_server.reset_singleton()
    yield
    credit_card_server.reset_cache()
    credit_card_server.reset_singleton()


@pytest.mark.asyncio
async def test_whichcard_basic() -> None:
    result = await _whichcard({"merchant": "Uber"})
    assert not _is_error(result)
    payload = _payload(result)
    assert payload["recommended_card"] == "amex_plat"
    assert payload["credit_triggered"] is True


@pytest.mark.asyncio
async def test_whichcard_chase_travel_hotel() -> None:
    result = await _whichcard(
        {"merchant": "Marriott", "booking_channel": "chase_travel"}
    )
    payload = _payload(result)
    assert payload["recommended_card"] == "csr"
    assert payload["effective_rate"] == pytest.approx(0.144, rel=1e-3)


@pytest.mark.asyncio
async def test_card_audit_summary_shape() -> None:
    result = await _card_audit(
        {
            "transactions": [
                {
                    "merchant": "Uber",
                    "amount": 25,
                    "card_last_4": "1027",
                    "tx_id": "x1",
                },
                {
                    "merchant": "Uber",
                    "amount": 30,
                    "card_last_4": "8034",
                    "tx_id": "x2",
                },
            ]
        }
    )
    assert not _is_error(result)
    payload = _payload(result)
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["ok"] == 1
    assert payload["summary"]["suboptimal"] == 1


@pytest.mark.asyncio
async def test_credits_status_lists_credits() -> None:
    result = await _credits_status({})
    assert not _is_error(result)
    payload = _payload(result)
    assert "credits" in payload
    assert isinstance(payload["credits"], list)
    # All credits start unused
    statuses = {c["status"] for c in payload["credits"]}
    assert statuses <= {"unused", "expired"}


@pytest.mark.asyncio
async def test_credits_mark_used_idempotent() -> None:
    first = await _credits_mark_used(
        {"credit_id": "amex_plat_uber_cash", "amount": 5, "tx_id": "t1"}
    )
    p1 = _payload(first)
    assert p1["used"] == 5
    assert p1["noop"] is False

    second = await _credits_mark_used(
        {"credit_id": "amex_plat_uber_cash", "amount": 5, "tx_id": "t1"}
    )
    p2 = _payload(second)
    assert p2["noop"] is True
    assert p2["used"] == 5


@pytest.mark.asyncio
async def test_credits_mark_used_unknown() -> None:
    result = await _credits_mark_used(
        {"credit_id": "ghost", "amount": 5}
    )
    assert _is_error(result)


@pytest.mark.asyncio
async def test_cards_list() -> None:
    result = await _cards_list({})
    payload = _payload(result)
    ids = {c["id"] for c in payload["cards"]}
    assert {"amex_plat", "csr", "apple_card"}.issubset(ids)


@pytest.mark.asyncio
async def test_cards_get_unknown() -> None:
    result = await _cards_get({"id": "ghost"})
    assert _is_error(result)


@pytest.mark.asyncio
async def test_merchant_categorize() -> None:
    result = await _merchant_categorize({"merchant": "Uber"})
    payload = _payload(result)
    assert payload["override"] is not None
    assert payload["override"]["card_id"] == "amex_plat"


@pytest.mark.asyncio
async def test_credit_card_tools_export() -> None:
    """The TOOLS list must include all 7 tools, in stable order."""

    names = [t.__name__ for t in credit_card_server.CREDIT_CARD_TOOLS]
    assert names == [
        "whichcard",
        "card_audit",
        "credits_status",
        "credits_mark_used",
        "cards_list",
        "cards_get",
        "merchant_categorize",
    ]

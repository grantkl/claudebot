"""Credit card recommendation MCP server.

Thin wrappers over the deterministic rule engine in
``src/mcp/credit_card/``. Each tool returns a JSON-encoded text content
block so downstream LLM prompts can ``json.loads`` the response.

Tools:
    whichcard            — recommend the best card for a charge.
    card_audit           — bulk-audit transactions against optimal-card rules.
    credits_status       — list credits with usage and remaining cap.
    credits_mark_used    — record credit consumption (idempotent on tx_id).
    cards_list           — read-only catalog dump.
    cards_get            — single-card lookup.
    merchant_categorize  — debug aid: show category + override match.
"""

from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from .credit_card import (
    AuditResult,
    Recommendation,
    audit,
    load_catalog,
    recommend,
)
from .credit_card.catalog import Catalog, Credit, reset_cache
from .credit_card.categorize import categorize
from .credit_card.ledger import (
    CreditLedger,
    get_ledger,
    is_active,
    period_key,
    reset_singleton,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _json(payload: Any) -> dict[str, Any]:
    return _text(json.dumps(payload, default=str))


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _today(arg_value: Any) -> date_cls:
    if isinstance(arg_value, str) and arg_value:
        return date_cls.fromisoformat(arg_value)
    return date_cls.today()


def _credit_status(catalog: Catalog, ledger: CreditLedger, credit: Credit, today: date_cls) -> dict[str, Any]:
    used = ledger.used(credit, today)
    remaining = ledger.cap_remaining(credit, today)
    if not is_active(credit, today):
        status = "expired"
    elif used >= credit.period_amount:
        status = "used"
    elif used > 0:
        status = "partial"
    else:
        status = "unused"
    return {
        "credit_id": credit.id,
        "name": credit.name,
        "card_id": credit.card_id,
        "card_name": (catalog.card_by_id(credit.card_id).name if catalog.card_by_id(credit.card_id) else credit.card_id),
        "period": credit.period,
        "period_amount": credit.period_amount,
        "period_key": period_key(credit, today),
        "used": round(used, 2),
        "remaining": round(remaining, 2),
        "status": status,
        "active_through": credit.active_through.isoformat() if credit.active_through else None,
        "enrollment_required": credit.enrollment_required,
    }


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@tool(
    "whichcard",
    "Recommend the best credit card for a purchase. Applies recurring-credit overrides first, then booking-channel rates, then earn-rate matrix. Returns JSON: {recommended_card, recommended_card_name, effective_rate, raw_rate, reason, alternatives, credit_triggered, credit_id, cap_remaining, category}.",
    {
        "type": "object",
        "properties": {
            "merchant": {"type": "string", "description": "Merchant name as it would appear on a charge (e.g. 'Uber', 'Trader Joe's', 'Marriott')."},
            "amount": {"type": "number", "description": "Optional purchase amount in dollars. Used by audit; not strictly required for whichcard."},
            "apple_pay": {"type": "boolean", "description": "True if this purchase would use Apple Pay (unlocks Apple Card 3% partner program)."},
            "booking_channel": {"type": "string", "description": "For travel: 'chase_travel', 'amex_travel', 'fhr', 'direct', 'third_party'. Routes through booking_channels table."},
            "mcc": {"type": "string", "description": "Optional 4-digit Merchant Category Code, used as a categorization hint."},
            "today": {"type": "string", "description": "Optional ISO date (YYYY-MM-DD). Defaults to today. Useful for testing or backfill."},
        },
        "required": ["merchant"],
    },
)
async def whichcard(args: dict[str, Any]) -> dict[str, Any]:
    try:
        catalog = load_catalog()
        ledger = get_ledger()
        rec: Recommendation = recommend(
            catalog,
            ledger,
            merchant=str(args["merchant"]),
            amount=args.get("amount"),
            apple_pay=bool(args.get("apple_pay", False)),
            booking_channel=args.get("booking_channel"),
            mcc=args.get("mcc"),
            today=_today(args.get("today")),
        )
        return _json(rec.as_dict())
    except Exception as e:
        logger.exception("whichcard failed")
        return _error(f"whichcard failed: {e}")


@tool(
    "card_audit",
    "Audit a batch of transactions against optimal-card rules. Returns one entry per transaction: {merchant, amount, used_card, optimal_card, used_rate, optimal_rate, delta_dollars, status, advice}. Status is 'ok' if right card was used (or delta < $0.50), else 'suboptimal' (with switch-card-on-file advice if applicable, suppressed for 30 days per merchant+card pair). Idempotent against the credit ledger via tx_id.",
    {
        "type": "object",
        "properties": {
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "merchant": {"type": "string"},
                        "amount": {"type": "number"},
                        "card_last_4": {"type": "string"},
                        "timestamp": {"type": "string", "description": "ISO 8601 timestamp."},
                        "tx_id": {"type": "string", "description": "Unique id (e.g. Gmail message id) for idempotency."},
                        "apple_pay": {"type": "boolean"},
                        "booking_channel": {"type": "string"},
                        "mcc": {"type": "string"},
                    },
                    "required": ["merchant", "amount", "card_last_4"],
                },
                "description": "Transactions to audit.",
            },
            "today": {"type": "string", "description": "Optional ISO date (YYYY-MM-DD)."},
            "delta_threshold": {"type": "number", "description": "Skip flagging when (optimal_rate - used_rate) * amount < this (default 0.50)."},
        },
        "required": ["transactions"],
    },
)
async def card_audit(args: dict[str, Any]) -> dict[str, Any]:
    try:
        catalog = load_catalog()
        ledger = get_ledger()
        results: list[AuditResult] = audit(
            catalog,
            ledger,
            transactions=list(args.get("transactions") or []),
            today=_today(args.get("today")),
            delta_threshold=float(args.get("delta_threshold", 0.50)),
        )
        ok = sum(1 for r in results if r.status == "ok")
        suboptimal = sum(1 for r in results if r.status == "suboptimal")
        unknown = sum(1 for r in results if r.status == "unknown_card")
        total_delta = round(sum(r.delta_dollars for r in results if r.status == "suboptimal"), 2)
        return _json(
            {
                "summary": {
                    "total": len(results),
                    "ok": ok,
                    "suboptimal": suboptimal,
                    "unknown_card": unknown,
                    "total_delta_dollars": total_delta,
                },
                "results": [r.as_dict() for r in results],
            }
        )
    except Exception as e:
        logger.exception("card_audit failed")
        return _error(f"card_audit failed: {e}")


@tool(
    "credits_status",
    "Return per-credit usage state for the current period. Each entry: {credit_id, name, card_id, card_name, period, period_amount, period_key, used, remaining, status, active_through, enrollment_required}. Status is 'used' (full cap consumed), 'partial', 'unused', or 'expired'. Optional filters by card_id or status.",
    {
        "type": "object",
        "properties": {
            "today": {"type": "string", "description": "Optional ISO date (YYYY-MM-DD)."},
            "card_id": {"type": "string", "description": "Optional filter: only credits for this card_id."},
            "status": {"type": "string", "description": "Optional filter: 'used' | 'partial' | 'unused' | 'expired'."},
        },
    },
)
async def credits_status(args: dict[str, Any]) -> dict[str, Any]:
    try:
        catalog = load_catalog()
        ledger = get_ledger()
        today = _today(args.get("today"))
        card_filter = args.get("card_id")
        status_filter = args.get("status")
        out: list[dict[str, Any]] = []
        total_remaining = 0.0
        for credit in catalog.credits:
            if card_filter and credit.card_id != card_filter:
                continue
            entry = _credit_status(catalog, ledger, credit, today)
            if status_filter and entry["status"] != status_filter:
                continue
            out.append(entry)
            if entry["status"] in ("unused", "partial"):
                total_remaining += entry["remaining"]
        return _json(
            {
                "today": today.isoformat(),
                "total_remaining_at_risk": round(total_remaining, 2),
                "credits": out,
            }
        )
    except Exception as e:
        logger.exception("credits_status failed")
        return _error(f"credits_status failed: {e}")


@tool(
    "credits_mark_used",
    "Record credit consumption. Idempotent on tx_id (re-running on the same Gmail message is a no-op). Returns post-update {used, remaining, noop}.",
    {
        "type": "object",
        "properties": {
            "credit_id": {"type": "string", "description": "Credit identifier (see credits_status)."},
            "amount": {"type": "number", "description": "Amount of credit consumed in dollars."},
            "date": {"type": "string", "description": "ISO date (YYYY-MM-DD) of the usage. Defaults to today."},
            "tx_id": {"type": "string", "description": "Idempotency key (e.g. Gmail message id)."},
        },
        "required": ["credit_id", "amount"],
    },
)
async def credits_mark_used(args: dict[str, Any]) -> dict[str, Any]:
    try:
        catalog = load_catalog()
        ledger = get_ledger()
        credit_id = str(args["credit_id"])
        credit = catalog.credit_by_id(credit_id)
        if credit is None:
            return _error(f"unknown credit_id: {credit_id}")
        usage_date = _today(args.get("date"))
        result = ledger.mark_used(
            credit,
            amount=float(args["amount"]),
            usage_date=usage_date,
            tx_id=args.get("tx_id"),
        )
        return _json({"credit_id": credit_id, **result})
    except Exception as e:
        logger.exception("credits_mark_used failed")
        return _error(f"credits_mark_used failed: {e}")


@tool(
    "cards_list",
    "Read-only catalog dump. Returns the full list of cards with their earn rules and effective rates per category.",
    {"type": "object", "properties": {}},
)
async def cards_list(args: dict[str, Any]) -> dict[str, Any]:
    try:
        catalog = load_catalog()
        out = []
        for card in catalog.cards:
            cents = catalog.point_valuations.get(card.base_rate.currency, 1.0)
            out.append(
                {
                    "id": card.id,
                    "name": card.name,
                    "issuer": card.issuer,
                    "network": card.network,
                    "annual_fee": card.annual_fee,
                    "currency": card.base_rate.currency,
                    "currency_value_cents": cents,
                    "base_effective_rate": round(
                        (card.base_rate.points_per_dollar * cents) / 100.0, 4
                    ),
                    "category_bonuses": [
                        {
                            "category": b.category,
                            "points_per_dollar": b.points_per_dollar,
                            "effective_rate": round((b.points_per_dollar * cents) / 100.0, 4),
                            "notes": b.notes,
                        }
                        for b in card.category_bonuses
                    ],
                    "booking_channels": card.booking_channels,
                }
            )
        return _json({"cards": out})
    except Exception as e:
        logger.exception("cards_list failed")
        return _error(f"cards_list failed: {e}")


@tool(
    "cards_get",
    "Get a single card by id, including all category bonuses and booking-channel rules.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Card id (e.g. 'amex_plat')."},
        },
        "required": ["id"],
    },
)
async def cards_get(args: dict[str, Any]) -> dict[str, Any]:
    try:
        catalog = load_catalog()
        card = catalog.card_by_id(str(args["id"]))
        if card is None:
            return _error(f"unknown card id: {args['id']}")
        cents = catalog.point_valuations.get(card.base_rate.currency, 1.0)
        return _json(
            {
                "id": card.id,
                "name": card.name,
                "issuer": card.issuer,
                "network": card.network,
                "annual_fee": card.annual_fee,
                "currency": card.base_rate.currency,
                "currency_value_cents": cents,
                "base_rate_points_per_dollar": card.base_rate.points_per_dollar,
                "category_bonuses": [
                    {
                        "category": b.category,
                        "points_per_dollar": b.points_per_dollar,
                        "effective_rate": round((b.points_per_dollar * cents) / 100.0, 4),
                        "notes": b.notes,
                    }
                    for b in card.category_bonuses
                ],
                "booking_channels": card.booking_channels,
                "credits": [
                    {"id": c.id, "name": c.name, "period": c.period, "period_amount": c.period_amount}
                    for c in catalog.credits_for_card(card.id)
                ],
            }
        )
    except Exception as e:
        logger.exception("cards_get failed")
        return _error(f"cards_get failed: {e}")


@tool(
    "merchant_categorize",
    "Debug aid. Resolve a merchant string to a category, plus any override or Apple Pay partner match.",
    {
        "type": "object",
        "properties": {
            "merchant": {"type": "string"},
            "mcc": {"type": "string"},
            "today": {"type": "string"},
        },
        "required": ["merchant"],
    },
)
async def merchant_categorize(args: dict[str, Any]) -> dict[str, Any]:
    try:
        catalog = load_catalog()
        result = categorize(
            catalog,
            str(args["merchant"]),
            mcc=args.get("mcc"),
            today=_today(args.get("today")),
        )
        return _json(
            {
                "category": result.category,
                "override": (
                    {
                        "card_id": result.override.card_id,
                        "credit_id": result.override.credit_id,
                    }
                    if result.override
                    else None
                ),
                "apple_pay_partner": (
                    {
                        "rate": result.apple_pay_partner.rate,
                        "active_through": (
                            result.apple_pay_partner.active_through.isoformat()
                            if result.apple_pay_partner.active_through
                            else None
                        ),
                        "spend_cap": result.apple_pay_partner.spend_cap,
                    }
                    if result.apple_pay_partner
                    else None
                ),
            }
        )
    except Exception as e:
        logger.exception("merchant_categorize failed")
        return _error(f"merchant_categorize failed: {e}")


CREDIT_CARD_TOOLS: list[SdkMcpTool] = [
    whichcard,
    card_audit,
    credits_status,
    credits_mark_used,
    cards_list,
    cards_get,
    merchant_categorize,
]


# Re-export reset functions for tests.
__all__ = [
    "CREDIT_CARD_TOOLS",
    "card_audit",
    "cards_get",
    "cards_list",
    "credits_mark_used",
    "credits_status",
    "merchant_categorize",
    "reset_cache",
    "reset_singleton",
    "whichcard",
]

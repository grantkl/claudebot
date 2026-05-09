"""Tests for the deterministic transaction-alert email parser."""

from __future__ import annotations

import pytest

from src.mcp.credit_card.email_parser import (
    ParseError,
    ParsedTransaction,
    detect_issuer,
    parse_amex,
    parse_bofa,
    parse_chase,
    parse_fnbo,
    parse_transaction_email,
)
from tests.fixtures.credit_card_emails import (
    AMEX_INLINE,
    AMEX_LARGE_PURCHASE,
    AMEX_PLAN_IT_NON_TRANSACTION,
    AMEX_REAL_CNP_2019,
    AMEX_TRAVEL_INSURANCE_MARKETING,
    BOFA_PURCHASE,
    BOFA_PURCHASE_PROSE,
    BOFA_STATEMENT_AVAILABLE,
    CHASE_BODY_FALLBACK,
    CHASE_DECLINED,
    CHASE_SUBJECT_DRIVEN,
    FNBO_PAYMENT_CONFIRMATION,
    FNBO_PURCHASE,
    FNBO_PURCHASE_PROSE,
    UNKNOWN_ISSUER,
)


# --------------------------------------------------------------------------- #
# Issuer detection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fixture, expected_issuer",
    [
        (AMEX_LARGE_PURCHASE, "amex"),
        (AMEX_INLINE, "amex"),
        (AMEX_REAL_CNP_2019, "amex"),
        (AMEX_PLAN_IT_NON_TRANSACTION, "amex"),
        (CHASE_SUBJECT_DRIVEN, "chase"),
        (CHASE_BODY_FALLBACK, "chase"),
        (BOFA_PURCHASE, "bofa"),
        (BOFA_PURCHASE_PROSE, "bofa"),
        (FNBO_PURCHASE, "fnbo"),
        (FNBO_PURCHASE_PROSE, "fnbo"),
        (UNKNOWN_ISSUER, None),
    ],
)
def test_detect_issuer(fixture, expected_issuer):
    assert detect_issuer(fixture) == expected_issuer


# --------------------------------------------------------------------------- #
# Amex — happy path
# --------------------------------------------------------------------------- #


def test_amex_large_purchase_html():
    result = parse_amex(AMEX_LARGE_PURCHASE)
    assert isinstance(result, ParsedTransaction)
    assert result.issuer == "amex"
    assert result.amount == 245.18
    assert result.merchant == "WHOLE FOODS MARKET"
    assert result.card_last_4 == "1027"  # last-5 71027 -> last-4
    assert result.tx_id == "amex_large_001"
    assert "T" in result.timestamp  # ISO-8601


def test_amex_inline_format():
    result = parse_amex(AMEX_INLINE)
    assert isinstance(result, ParsedTransaction)
    assert result.amount == 89.42
    assert result.merchant == "UBER TRIP"
    assert result.card_last_4 == "1027"


# --------------------------------------------------------------------------- #
# Amex — negative cases
# --------------------------------------------------------------------------- #


def test_amex_real_2019_missing_amount_returns_error():
    """Historic plaintext omits amount/merchant — should fail cleanly."""
    result = parse_amex(AMEX_REAL_CNP_2019)
    assert isinstance(result, ParseError)
    assert result.issuer == "amex"
    assert "amount" in result.reason
    assert result.fields_found.get("card_last_4") == "1005"  # last-5 -> last-4


def test_amex_plan_it_marketing_rejected():
    result = parse_amex(AMEX_PLAN_IT_NON_TRANSACTION)
    assert isinstance(result, ParseError)
    assert result.reason == "non-transaction subject"


def test_amex_travel_insurance_marketing_rejected():
    result = parse_amex(AMEX_TRAVEL_INSURANCE_MARKETING)
    assert isinstance(result, ParseError)
    assert result.reason == "non-transaction subject"


# --------------------------------------------------------------------------- #
# Chase — happy path
# --------------------------------------------------------------------------- #


def test_chase_subject_driven():
    result = parse_chase(CHASE_SUBJECT_DRIVEN)
    assert isinstance(result, ParsedTransaction)
    assert result.amount == 124.07
    assert result.merchant == "TRADER JOE'S #123"
    assert result.card_last_4 == "3253"


def test_chase_body_fallback():
    result = parse_chase(CHASE_BODY_FALLBACK)
    assert isinstance(result, ParsedTransaction)
    assert result.amount == 52.30
    assert result.merchant == "SHELL OIL 1234567"
    assert result.card_last_4 == "5502"


def test_chase_declined_returns_error():
    result = parse_chase(CHASE_DECLINED)
    assert isinstance(result, ParseError)
    assert "missing" in result.reason


# --------------------------------------------------------------------------- #
# BofA — happy path
# --------------------------------------------------------------------------- #


def test_bofa_purchase_html():
    result = parse_bofa(BOFA_PURCHASE)
    assert isinstance(result, ParsedTransaction)
    assert result.amount == 312.55
    assert result.merchant == "ALASKA AIR 0271234567"
    assert result.card_last_4 == "4365"


def test_bofa_purchase_prose():
    result = parse_bofa(BOFA_PURCHASE_PROSE)
    assert isinstance(result, ParsedTransaction)
    assert result.amount == 48.20
    assert result.merchant == "CHEVRON 0091234"
    assert result.card_last_4 == "4365"


def test_bofa_statement_available_returns_error():
    result = parse_bofa(BOFA_STATEMENT_AVAILABLE)
    assert isinstance(result, ParseError)


# --------------------------------------------------------------------------- #
# FNBO — happy path
# --------------------------------------------------------------------------- #


def test_fnbo_purchase_html():
    result = parse_fnbo(FNBO_PURCHASE)
    assert isinstance(result, ParsedTransaction)
    assert result.amount == 87.65
    assert result.merchant == "MGM GRAND HOTEL"
    assert result.card_last_4 == "7337"


def test_fnbo_purchase_prose():
    result = parse_fnbo(FNBO_PURCHASE_PROSE)
    assert isinstance(result, ParsedTransaction)
    assert result.amount == 19.99
    assert result.merchant == "NETFLIX.COM"
    assert result.card_last_4 == "7337"


def test_fnbo_payment_confirmation_returns_error():
    """Payment received emails contain $ amount + ending — must not be mistaken
    for a purchase. The body has no 'transaction/purchase/charge of $X' phrase."""
    result = parse_fnbo(FNBO_PAYMENT_CONFIRMATION)
    assert isinstance(result, ParseError)


# --------------------------------------------------------------------------- #
# Top-level dispatcher
# --------------------------------------------------------------------------- #


def test_dispatcher_routes_to_amex():
    result = parse_transaction_email(AMEX_LARGE_PURCHASE)
    assert isinstance(result, ParsedTransaction)
    assert result.issuer == "amex"


def test_dispatcher_routes_to_chase():
    result = parse_transaction_email(CHASE_SUBJECT_DRIVEN)
    assert isinstance(result, ParsedTransaction)
    assert result.issuer == "chase"


def test_dispatcher_routes_to_bofa():
    result = parse_transaction_email(BOFA_PURCHASE)
    assert isinstance(result, ParsedTransaction)
    assert result.issuer == "bofa"


def test_dispatcher_routes_to_fnbo():
    result = parse_transaction_email(FNBO_PURCHASE)
    assert isinstance(result, ParsedTransaction)
    assert result.issuer == "fnbo"


def test_dispatcher_unknown_issuer_returns_error():
    result = parse_transaction_email(UNKNOWN_ISSUER)
    assert isinstance(result, ParseError)
    assert result.reason == "unrecognized issuer"


def test_dispatcher_handles_non_dict_input():
    result = parse_transaction_email("not a dict")  # type: ignore[arg-type]
    assert isinstance(result, ParseError)
    assert "not a dict" in result.reason


# --------------------------------------------------------------------------- #
# Output shape — drop-in compatibility with card_audit
# --------------------------------------------------------------------------- #


def test_parsed_transaction_as_dict_is_audit_compatible():
    """``card_audit`` requires merchant, amount, card_last_4, and accepts
    optional tx_id + timestamp. ParsedTransaction.as_dict() must include all."""
    result = parse_transaction_email(AMEX_LARGE_PURCHASE)
    assert isinstance(result, ParsedTransaction)
    d = result.as_dict()
    for required in ("merchant", "amount", "card_last_4", "tx_id", "timestamp"):
        assert required in d


def test_parse_error_as_dict_includes_diagnostic_fields():
    result = parse_transaction_email(AMEX_PLAN_IT_NON_TRANSACTION)
    assert isinstance(result, ParseError)
    d = result.as_dict()
    assert "reason" in d
    assert "raw_excerpt" in d
    assert "raw_subject" in d
    assert d["issuer"] == "amex"


# --------------------------------------------------------------------------- #
# Idempotency — tx_id is the Gmail message id by default
# --------------------------------------------------------------------------- #


def test_tx_id_defaults_to_gmail_message_id():
    result = parse_transaction_email(AMEX_LARGE_PURCHASE)
    assert isinstance(result, ParsedTransaction)
    assert result.tx_id == AMEX_LARGE_PURCHASE["id"]


def test_tx_id_uses_explicit_field_when_provided():
    msg = dict(AMEX_LARGE_PURCHASE)
    msg["id"] = ""
    msg["tx_id"] = "explicit-tx-123"
    result = parse_transaction_email(msg)
    assert isinstance(result, ParsedTransaction)
    assert result.tx_id == "explicit-tx-123"

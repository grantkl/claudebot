"""Credit card recommendation rule engine.

This package contains the deterministic rule engine for credit card
recommendations. The MCP tool definitions live in
``src/mcp/credit_card_server.py`` and are thin wrappers around the pure
functions exposed here.

Modules:
    catalog: Load + validate the YAML catalog of cards, credits, and rules.
    ledger:  JSON-backed mutable state tracking credit usage per period.
    categorize: Merchant string -> category resolver (regex + dict + Apple Pay).
    engine:  Pure functions ``recommend`` and ``audit``.
"""

from .catalog import Catalog, load_catalog
from .email_parser import (
    ParseError,
    ParseResult,
    ParsedTransaction,
    parse_transaction_email,
)
from .engine import AuditResult, Recommendation, audit, recommend
from .ledger import CreditLedger, period_key

__all__ = [
    "AuditResult",
    "Catalog",
    "CreditLedger",
    "ParseError",
    "ParseResult",
    "ParsedTransaction",
    "Recommendation",
    "audit",
    "load_catalog",
    "parse_transaction_email",
    "period_key",
    "recommend",
]

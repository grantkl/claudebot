"""Deterministic transaction-alert email parser.

Extracts ``{merchant, amount, card_last_4, timestamp}`` tuples from issuer
transaction-alert emails so the daily audit task no longer relies on the
LLM to do free-form text extraction.

Design:
    * One function per issuer (``parse_amex``, ``parse_chase``,
      ``parse_bofa``, ``parse_fnbo``).
    * Top-level dispatcher (:func:`parse_transaction_email`) selects an
      issuer parser by ``from`` / ``subject`` heuristics.
    * Every parser is a pure function: input is the dict shape returned
      by ``gmail_get_email`` (keys: ``id``, ``from``, ``subject``,
      ``date``, ``body``); output is :class:`ParsedTransaction` on
      success or :class:`ParseError` on failure (with a ``reason`` and
      a short ``raw_excerpt`` for debugging).

Stdlib only — no LLM, no network, no third-party deps. Strips HTML with
``html.parser`` since most issuer emails are HTML-only and the bot
sometimes hands us the multipart text part, sometimes the HTML.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParsedTransaction:
    """Successful parse — drop-in compatible with ``card_audit`` input."""

    tx_id: str
    merchant: str
    amount: float
    card_last_4: str
    timestamp: str  # ISO 8601, UTC
    issuer: str
    confidence: float = 1.0  # 1.0 = all fields from explicit format match
    raw_subject: str = ""
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ParseError:
    """Parse failed — caller should fall back or skip."""

    tx_id: str
    reason: str
    issuer: str | None = None
    raw_excerpt: str = ""
    raw_subject: str = ""
    fields_found: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


ParseResult = ParsedTransaction | ParseError


# --------------------------------------------------------------------------- #
# HTML stripping
# --------------------------------------------------------------------------- #


class _HTMLStripper(HTMLParser):
    """Minimal HTML stripper. Preserves whitespace boundaries between tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "head"}:
            self._skip += 1
        # Block-level tags add a newline so we don't glue text together.
        if tag.lower() in {
            "br", "p", "div", "tr", "td", "li", "h1", "h2", "h3", "h4", "table"
        }:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "head"} and self._skip > 0:
            self._skip -= 1
        if tag.lower() in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _strip_html(body: str) -> str:
    """Return body with HTML tags removed and whitespace normalized."""
    if "<" not in body:
        return body
    parser = _HTMLStripper()
    try:
        parser.feed(body)
    except Exception:  # pragma: no cover — broken HTML
        return body
    return parser.text()


_WS_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n[ \t]*\n+")


def _normalize(text: str) -> str:
    """Collapse runs of spaces/tabs and strip blank-line clusters."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# Shared utilities
# --------------------------------------------------------------------------- #


_AMOUNT_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+(?:\.\d{2}))")
_LAST4_RE = re.compile(r"\b(\d{4})\b")


def _to_iso(date_header: str) -> str:
    """Parse RFC 2822 date header, fall back to today UTC midnight."""
    try:
        dt = parsedate_to_datetime(date_header)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(tz=timezone.utc).isoformat()


def _parse_amount(s: str) -> float | None:
    m = _AMOUNT_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _excerpt(text: str, limit: int = 240) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _msg_id(message: dict[str, Any]) -> str:
    return str(message.get("id") or message.get("tx_id") or "")


def _extract_body_text(message: dict[str, Any]) -> str:
    body = message.get("body") or ""
    if not body:
        return ""
    return _normalize(_strip_html(body))


# --------------------------------------------------------------------------- #
# Per-issuer parsers
# --------------------------------------------------------------------------- #


# ---- Amex ---------------------------------------------------------------- #

# Amex large-purchase / CNP alerts share a small set of layouts. The most
# stable signals across formats:
#   - "Amount: $X.XX"  OR  "$X.XX at MERCHANT"
#   - "Merchant: NAME" OR  "at NAME on DATE"
#   - "Account Ending: XXXXX" OR "Ending in X-XXXXX" (last-5; the user's
#     mapping uses last-4, so we slice the last 4 of any 4-or-5 digit run).

_AMEX_AMOUNT_LABELED = re.compile(
    r"(?:Amount|Charge|Purchase\s*amount)\s*[:\-]\s*\$\s?"
    r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+(?:\.\d{2}))",
    re.IGNORECASE,
)
_AMEX_AMOUNT_INLINE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\s+(?:at|charge\s+at|purchase\s+at)\s+",
    re.IGNORECASE,
)
_AMEX_MERCHANT_LABELED = re.compile(
    r"Merchant\s*[:\-]\s*([^\n]+?)(?:\n|$)", re.IGNORECASE
)
_AMEX_MERCHANT_AT = re.compile(
    r"\$\s?\d[\d,]*\.\d{2}\s+at\s+([^\n]+?)(?:\s+on\s+|\n|$)",
    re.IGNORECASE,
)
_AMEX_LAST4 = re.compile(
    r"(?:Account\s+ending|Ending(?:\s+in)?)\s*[:\-]?\s*[-X*]?(\d{4,5})",
    re.IGNORECASE,
)


def parse_amex(message: dict[str, Any]) -> ParseResult:
    body = _extract_body_text(message)
    subject = message.get("subject", "")
    tx_id = _msg_id(message)

    # Marketing / non-transaction emails to reject early.
    bad_subjects = (
        "plan it",
        "trip cancel guard",
        "travel insurance",
        "splitting a purchase",
    )
    if any(s in subject.lower() for s in bad_subjects):
        return ParseError(
            tx_id=tx_id,
            issuer="amex",
            reason="non-transaction subject",
            raw_subject=subject,
            raw_excerpt=_excerpt(body),
        )

    fields: dict[str, Any] = {}

    m = _AMEX_AMOUNT_LABELED.search(body)
    if m:
        fields["amount"] = float(m.group(1).replace(",", ""))
    else:
        m = _AMEX_AMOUNT_INLINE.search(body)
        if m:
            fields["amount"] = float(m.group(1).replace(",", ""))

    m = _AMEX_MERCHANT_LABELED.search(body)
    if m:
        fields["merchant"] = m.group(1).strip()
    else:
        m = _AMEX_MERCHANT_AT.search(body)
        if m:
            fields["merchant"] = m.group(1).strip()

    m = _AMEX_LAST4.search(body)
    if m:
        digits = m.group(1)
        # Amex shows last-5 sometimes; user maps by last-4, so take the trailing 4.
        fields["card_last_4"] = digits[-4:]

    missing = [k for k in ("amount", "merchant", "card_last_4") if k not in fields]
    if missing:
        return ParseError(
            tx_id=tx_id,
            issuer="amex",
            reason=f"missing fields: {', '.join(missing)}",
            raw_subject=subject,
            raw_excerpt=_excerpt(body),
            fields_found=fields,
        )

    return ParsedTransaction(
        tx_id=tx_id,
        merchant=fields["merchant"],
        amount=float(fields["amount"]),
        card_last_4=str(fields["card_last_4"]),
        timestamp=_to_iso(message.get("date", "")),
        issuer="amex",
        raw_subject=subject,
    )


# ---- Chase --------------------------------------------------------------- #

# Chase alert subjects are distinctive: "Your $X.XX transaction with MERCHANT".
# Body usually repeats merchant + last-4 ("Account ending in 1234").

_CHASE_SUBJECT = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\s+"
    r"(?:transaction|purchase|charge)\s+(?:with|at)\s+(.+?)$",
    re.IGNORECASE,
)
_CHASE_BODY_AMOUNT = re.compile(
    r"(?:transaction|purchase|charge)\s+of\s+\$\s?"
    r"(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})",
    re.IGNORECASE,
)
_CHASE_BODY_MERCHANT = re.compile(
    r"(?:Merchant|at)\s*[:\-]?\s*([^\n]+?)(?:\s+(?:was|on|exceeded)\s+|\n|$)",
    re.IGNORECASE,
)
_CHASE_LAST4 = re.compile(
    r"(?:Account|card)\s+ending\s+(?:in\s+)?[-*•]*\s*(\d{4})",
    re.IGNORECASE,
)


def parse_chase(message: dict[str, Any]) -> ParseResult:
    body = _extract_body_text(message)
    subject = message.get("subject", "")
    tx_id = _msg_id(message)

    fields: dict[str, Any] = {}

    sm = _CHASE_SUBJECT.search(subject)
    if sm:
        fields["amount"] = float(sm.group(1).replace(",", ""))
        fields["merchant"] = sm.group(2).strip().rstrip(".")

    if "amount" not in fields:
        bm = _CHASE_BODY_AMOUNT.search(body)
        if bm:
            fields["amount"] = float(bm.group(1).replace(",", ""))

    if "merchant" not in fields:
        mm = _CHASE_BODY_MERCHANT.search(body)
        if mm:
            cand = mm.group(1).strip().rstrip(".")
            # Reject obvious false-positive captures like "Account ... ending"
            if not re.search(r"ending|account|card", cand, re.IGNORECASE):
                fields["merchant"] = cand

    lm = _CHASE_LAST4.search(body)
    if lm:
        fields["card_last_4"] = lm.group(1)

    missing = [k for k in ("amount", "merchant", "card_last_4") if k not in fields]
    if missing:
        return ParseError(
            tx_id=tx_id,
            issuer="chase",
            reason=f"missing fields: {', '.join(missing)}",
            raw_subject=subject,
            raw_excerpt=_excerpt(body),
            fields_found=fields,
        )

    return ParsedTransaction(
        tx_id=tx_id,
        merchant=fields["merchant"],
        amount=float(fields["amount"]),
        card_last_4=str(fields["card_last_4"]),
        timestamp=_to_iso(message.get("date", "")),
        issuer="chase",
        raw_subject=subject,
    )


# ---- Bank of America (Alaska Visa) --------------------------------------- #

# BofA "Credit card transaction exceeds $X" alerts:
#   "A purchase of $X.XX was made on [DATE] at MERCHANT NAME."
#   "Account ending in - 1234"
# Merchant is often ALL CAPS.

_BOFA_AMOUNT = re.compile(
    r"(?:purchase|transaction|charge)\s+of\s+\$\s?"
    r"(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})",
    re.IGNORECASE,
)
_BOFA_MERCHANT = re.compile(
    r"\bat\s+([A-Z0-9][A-Z0-9 &'\-./*#]{2,}?)(?:\s+on\s+|\.\s|\n)",
)
_BOFA_LAST4 = re.compile(
    r"(?:Account|Card)\s+(?:number\s+)?ending\s+(?:in\s+)?[-*\s]*(\d{4})",
    re.IGNORECASE,
)


def parse_bofa(message: dict[str, Any]) -> ParseResult:
    body = _extract_body_text(message)
    subject = message.get("subject", "")
    tx_id = _msg_id(message)

    fields: dict[str, Any] = {}

    m = _BOFA_AMOUNT.search(body)
    if m:
        fields["amount"] = float(m.group(1).replace(",", ""))

    m = _BOFA_MERCHANT.search(body)
    if m:
        fields["merchant"] = m.group(1).strip().rstrip(".")

    m = _BOFA_LAST4.search(body)
    if m:
        fields["card_last_4"] = m.group(1)

    missing = [k for k in ("amount", "merchant", "card_last_4") if k not in fields]
    if missing:
        return ParseError(
            tx_id=tx_id,
            issuer="bofa",
            reason=f"missing fields: {', '.join(missing)}",
            raw_subject=subject,
            raw_excerpt=_excerpt(body),
            fields_found=fields,
        )

    return ParsedTransaction(
        tx_id=tx_id,
        merchant=fields["merchant"],
        amount=float(fields["amount"]),
        card_last_4=str(fields["card_last_4"]),
        timestamp=_to_iso(message.get("date", "")),
        issuer="bofa",
        raw_subject=subject,
    )


# ---- FNBO (MGM Rewards) -------------------------------------------------- #

# FNBO uses a friendlier prose style:
#   "A transaction of $X.XX was processed on [DATE] at MERCHANT."
#   "Card ending in 1234" or "...account ending 1234".

_FNBO_AMOUNT = re.compile(
    r"(?:transaction|purchase|charge)\s+of\s+\$\s?"
    r"(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})",
    re.IGNORECASE,
)
_FNBO_MERCHANT = re.compile(
    r"\bat\s+([^\n]+?)(?:\s+on\s+|\.\s|\n)",
    re.IGNORECASE,
)
_FNBO_LAST4 = re.compile(
    r"(?:card|account)\s+ending\s+(?:in\s+)?[-*\s]*(\d{4})",
    re.IGNORECASE,
)


def parse_fnbo(message: dict[str, Any]) -> ParseResult:
    body = _extract_body_text(message)
    subject = message.get("subject", "")
    tx_id = _msg_id(message)

    fields: dict[str, Any] = {}

    m = _FNBO_AMOUNT.search(body)
    if m:
        fields["amount"] = float(m.group(1).replace(",", ""))

    m = _FNBO_MERCHANT.search(body)
    if m:
        cand = m.group(1).strip().rstrip(".")
        if not re.search(r"ending|account|card", cand, re.IGNORECASE):
            fields["merchant"] = cand

    m = _FNBO_LAST4.search(body)
    if m:
        fields["card_last_4"] = m.group(1)

    missing = [k for k in ("amount", "merchant", "card_last_4") if k not in fields]
    if missing:
        return ParseError(
            tx_id=tx_id,
            issuer="fnbo",
            reason=f"missing fields: {', '.join(missing)}",
            raw_subject=subject,
            raw_excerpt=_excerpt(body),
            fields_found=fields,
        )

    return ParsedTransaction(
        tx_id=tx_id,
        merchant=fields["merchant"],
        amount=float(fields["amount"]),
        card_last_4=str(fields["card_last_4"]),
        timestamp=_to_iso(message.get("date", "")),
        issuer="fnbo",
        raw_subject=subject,
    )


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #


_AMEX_FROM = re.compile(
    r"@(?:welcome\.aexp\.com|member\.americanexpress\.com|americanexpress\.com)",
    re.IGNORECASE,
)
_CHASE_FROM = re.compile(r"@chase\.com", re.IGNORECASE)
_BOFA_FROM = re.compile(
    r"@(?:[a-z0-9.-]+\.)?(?:bankofamerica\.com|baml\.com)", re.IGNORECASE
)
_FNBO_FROM = re.compile(r"@(?:firstnational(?:bank)?\.com|fnbo\.com|card\.fnbo\.com)", re.IGNORECASE)


def detect_issuer(message: dict[str, Any]) -> str | None:
    """Return ``'amex'|'chase'|'bofa'|'fnbo'`` or ``None``.

    Uses the ``from`` header. Subject is consulted as a tie-breaker for
    Chase (since their alert pattern is so distinctive).
    """
    sender = message.get("from", "") or ""
    subject = message.get("subject", "") or ""
    if _AMEX_FROM.search(sender):
        return "amex"
    if _CHASE_FROM.search(sender):
        return "chase"
    if _BOFA_FROM.search(sender):
        return "bofa"
    if _FNBO_FROM.search(sender):
        return "fnbo"
    if _CHASE_SUBJECT.search(subject):
        return "chase"
    return None


_PARSERS = {
    "amex": parse_amex,
    "chase": parse_chase,
    "bofa": parse_bofa,
    "fnbo": parse_fnbo,
}


def parse_transaction_email(message: dict[str, Any]) -> ParseResult:
    """Parse a Gmail message dict into a ``ParsedTransaction`` or ``ParseError``.

    ``message`` is the dict returned by the ``gmail_get_email`` MCP tool:
    keys ``id``, ``from``, ``subject``, ``date``, ``body`` (plus optional
    ``links`` etc., which are ignored).

    Always returns a value — never raises. Errors are surfaced as
    :class:`ParseError` with a human-readable ``reason`` so the calling
    audit task can decide whether to skip or fall back.
    """
    if not isinstance(message, dict):
        return ParseError(
            tx_id="",
            reason="message is not a dict",
            raw_excerpt=_excerpt(str(message)),
        )

    tx_id = _msg_id(message)
    issuer = detect_issuer(message)
    if issuer is None:
        return ParseError(
            tx_id=tx_id,
            reason="unrecognized issuer",
            raw_subject=message.get("subject", ""),
            raw_excerpt=_excerpt(message.get("from", "")),
        )
    return _PARSERS[issuer](message)


__all__ = [
    "ParseError",
    "ParseResult",
    "ParsedTransaction",
    "detect_issuer",
    "parse_amex",
    "parse_bofa",
    "parse_chase",
    "parse_fnbo",
    "parse_transaction_email",
]

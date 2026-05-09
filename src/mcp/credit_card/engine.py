"""Pure rule engine for credit card recommendations.

All functions take ``Catalog`` and ``CreditLedger`` explicitly so they can be
unit-tested without touching disk. The MCP server module is responsible for
loading the catalog/ledger singletons and passing them in.

Ranking algorithm (in priority order):

1. **Override stage** — if the merchant matches a recurring-credit override
   and the credit's cap_remaining > 0, that card wins outright. The
   recommendation flags ``credit_triggered=True`` and reports the credit
   identifier and remaining cap.
2. **Apple Pay partner stage** — when ``apple_pay=True``, an Apple Card
   partner-rate candidate is added to the candidate pool.
3. **Booking-channel stage** — when ``booking_channel`` is set (e.g.
   ``chase_travel`` for hotels), each card's
   ``booking_channels[<category_type>][<channel>]`` multiplier is consulted.
4. **Default earn-rate stage** — for each card, find the best matching
   ``category_bonus`` for the resolved category, falling back to base_rate.

Effective rate = ``points_per_dollar * cents_per_point / 100``. Picking the
winner = highest effective rate; ties broken by (a) prefer no-AF, then (b)
deterministic id sort.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .catalog import Card, Catalog
from .categorize import categorize, normalize_merchant
from .ledger import CreditLedger, is_active

# Channels that map to the "hotel" booking_channels group on a card.
_HOTEL_CATEGORIES = {"hotel_amex_travel", "hotel_direct", "hotel"}
# Channels that map to the "flight" group.
_FLIGHT_CATEGORIES = {"airline_direct", "flight"}

# Suboptimal threshold: skip flagging audit transactions where the optimal
# delta is below this dollar amount (rounding noise).
DEFAULT_AUDIT_DELTA_THRESHOLD = 0.50


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CardCandidate:
    card_id: str
    card_name: str
    effective_rate: float
    raw_rate: float
    reason: str


@dataclass(frozen=True)
class Recommendation:
    recommended_card: str            # card id
    recommended_card_name: str
    effective_rate: float            # cash-back-equivalent fraction (0.072 = 7.2%)
    raw_rate: float                  # nominal points/dollar or partner rate
    reason: str
    alternatives: tuple[CardCandidate, ...] = ()
    credit_triggered: bool = False
    credit_id: str | None = None
    cap_remaining: float | None = None
    category: str = "other"

    def as_dict(self) -> dict[str, Any]:
        return {
            "recommended_card": self.recommended_card,
            "recommended_card_name": self.recommended_card_name,
            "effective_rate": round(self.effective_rate, 4),
            "raw_rate": round(self.raw_rate, 4),
            "reason": self.reason,
            "alternatives": [
                {
                    "card_id": a.card_id,
                    "card_name": a.card_name,
                    "effective_rate": round(a.effective_rate, 4),
                    "raw_rate": round(a.raw_rate, 4),
                    "reason": a.reason,
                }
                for a in self.alternatives
            ],
            "credit_triggered": self.credit_triggered,
            "credit_id": self.credit_id,
            "cap_remaining": (
                round(self.cap_remaining, 2) if self.cap_remaining is not None else None
            ),
            "category": self.category,
        }


@dataclass(frozen=True)
class AuditResult:
    merchant: str
    amount: float
    timestamp: str | None
    used_card: str | None
    used_card_name: str | None
    optimal_card: str
    optimal_card_name: str
    used_rate: float
    optimal_rate: float
    delta_dollars: float
    status: str                      # "ok" | "suboptimal" | "unknown_card"
    advice: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "merchant": self.merchant,
            "amount": self.amount,
            "timestamp": self.timestamp,
            "used_card": self.used_card,
            "used_card_name": self.used_card_name,
            "optimal_card": self.optimal_card,
            "optimal_card_name": self.optimal_card_name,
            "used_rate": round(self.used_rate, 4),
            "optimal_rate": round(self.optimal_rate, 4),
            "delta_dollars": round(self.delta_dollars, 2),
            "status": self.status,
            "advice": self.advice,
        }


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _channel_group(category: str) -> str | None:
    if category in _HOTEL_CATEGORIES:
        return "hotel"
    if category in _FLIGHT_CATEGORIES:
        return "flight"
    return None


def _earn_rate_for_card(card: Card, category: str, point_valuations: dict[str, float]) -> tuple[float, float, str]:
    """Compute the best (effective_rate, raw_rate, reason) for ``card`` on
    ``category`` from category_bonuses + base_rate.
    """

    cents_per_point = point_valuations.get(card.base_rate.currency, 1.0)
    best_ppd = card.base_rate.points_per_dollar
    best_reason = f"base rate ({card.base_rate.points_per_dollar}x)"
    for bonus in card.category_bonuses:
        if bonus.category == category and bonus.points_per_dollar > best_ppd:
            best_ppd = bonus.points_per_dollar
            best_reason = f"{bonus.points_per_dollar}x on {bonus.category}"
    effective = (best_ppd * cents_per_point) / 100.0
    return effective, best_ppd, best_reason


def _channel_rate_for_card(
    card: Card,
    category: str,
    booking_channel: str,
    point_valuations: dict[str, float],
) -> tuple[float, float, str] | None:
    """Compute the booking-channel rate for ``card`` if ``booking_channel`` is
    defined on the card, else None.
    """

    group = _channel_group(category)
    if group is None:
        return None
    channel_map = card.booking_channels.get(group)
    if not channel_map:
        return None
    if booking_channel not in channel_map:
        return None
    multiplier = channel_map[booking_channel]
    cents_per_point = point_valuations.get(card.base_rate.currency, 1.0)
    effective = (multiplier * cents_per_point) / 100.0
    reason = f"{multiplier}x on {group}s via {booking_channel}"
    return effective, multiplier, reason


def _tie_breaker_key(card: Card) -> tuple[float, str]:
    return (card.annual_fee, card.id)


def _rank(candidates: list[CardCandidate], cards_by_id: dict[str, Card]) -> list[CardCandidate]:
    """Sort candidates by effective_rate desc, breaking ties by prefer-no-AF
    then deterministic id."""

    def key(c: CardCandidate) -> tuple[float, float, str]:
        card = cards_by_id[c.card_id]
        return (-c.effective_rate, card.annual_fee, card.id)

    return sorted(candidates, key=key)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def recommend(
    catalog: Catalog,
    ledger: CreditLedger,
    merchant: str,
    *,
    amount: float | None = None,
    apple_pay: bool = False,
    booking_channel: str | None = None,
    mcc: str | None = None,
    today: date | None = None,
) -> Recommendation:
    """Recommend the best card for a given charge."""

    today = today or date.today()
    cat = categorize(catalog, merchant, mcc=mcc, today=today)
    cards_by_id = {c.id: c for c in catalog.cards}

    # Stage 1: override with active credit cap remaining wins outright.
    if cat.override is not None:
        card = cards_by_id.get(cat.override.card_id)
        if card is not None:
            credit = (
                catalog.credit_by_id(cat.override.credit_id)
                if cat.override.credit_id
                else None
            )
            cap_left: float | None = None
            credit_id: str | None = None
            triggered = False
            if credit is not None and is_active(credit, today):
                cap_left = ledger.cap_remaining(credit, today)
                credit_id = credit.id
                if cap_left > 0.0:
                    triggered = True
            elif credit is None:
                # Override has no credit reference: always wins.
                triggered = True

            if triggered:
                # The base earn-rate component (e.g. Amex Plat 1x MR @ 1.8c
                # = 1.8% on the Uber spend itself).
                eff, raw, reason = _earn_rate_for_card(card, cat.category, catalog.point_valuations)
                # When a recurring credit is in play, the override's value is
                # dominated by the credit rebate, not the earn rate. We model
                # that by adding (cap_used / amount) to the effective rate.
                # Without an amount we assume the credit covers the charge
                # fully (cap_remaining or amount, whichever is smaller).
                if cap_left is not None and cap_left > 0 and amount is not None and amount > 0:
                    cap_used = min(float(amount), cap_left)
                    eff = eff + (cap_used / float(amount))
                elif cap_left is not None and cap_left > 0 and amount is None:
                    # No amount provided; the rate field is informational only.
                    # Bump it to convey "uses credit" but leave numeric value
                    # at the earn rate so amount-free queries don't misreport.
                    pass
                full_reason = (
                    f"recurring credit {credit.name!r}; "
                    f"cap remaining ${cap_left:.2f}"
                ) if credit is not None else f"merchant override -> {card.name}"
                return Recommendation(
                    recommended_card=card.id,
                    recommended_card_name=card.name,
                    effective_rate=eff,
                    raw_rate=raw,
                    reason=full_reason,
                    alternatives=(),
                    credit_triggered=True,
                    credit_id=credit_id,
                    cap_remaining=cap_left,
                    category=cat.category,
                )

    # Stage 2-4: build the candidate pool.
    candidates: list[CardCandidate] = []

    for card in catalog.cards:
        # Booking-channel rate if applicable.
        if booking_channel is not None:
            channel_rate = _channel_rate_for_card(
                card, cat.category, booking_channel, catalog.point_valuations
            )
            if channel_rate is not None:
                eff, raw, reason = channel_rate
                candidates.append(
                    CardCandidate(
                        card_id=card.id,
                        card_name=card.name,
                        effective_rate=eff,
                        raw_rate=raw,
                        reason=reason,
                    )
                )
                continue
        # Default earn-rate.
        eff, raw, reason = _earn_rate_for_card(card, cat.category, catalog.point_valuations)
        candidates.append(
            CardCandidate(
                card_id=card.id,
                card_name=card.name,
                effective_rate=eff,
                raw_rate=raw,
                reason=reason,
            )
        )

    # Apple Pay partner candidate.
    if apple_pay and cat.apple_pay_partner is not None:
        apple_card = catalog.card_by_id("apple_card")
        if apple_card is not None:
            partner_rate = cat.apple_pay_partner.rate
            candidates.append(
                CardCandidate(
                    card_id=apple_card.id,
                    card_name=apple_card.name,
                    effective_rate=partner_rate,
                    raw_rate=partner_rate * 100,
                    reason=f"Apple Pay 3% partner ({partner_rate * 100:.0f}% rate)",
                )
            )

    ranked = _rank(candidates, cards_by_id)
    if not ranked:
        # Should never happen with a valid catalog; defend anyway.
        return Recommendation(
            recommended_card="",
            recommended_card_name="(none)",
            effective_rate=0.0,
            raw_rate=0.0,
            reason="no candidate cards in catalog",
            category=cat.category,
        )
    winner = ranked[0]
    alts = tuple(ranked[1:4])
    return Recommendation(
        recommended_card=winner.card_id,
        recommended_card_name=winner.card_name,
        effective_rate=winner.effective_rate,
        raw_rate=winner.raw_rate,
        reason=winner.reason,
        alternatives=alts,
        credit_triggered=False,
        category=cat.category,
    )


def audit(
    catalog: Catalog,
    ledger: CreditLedger,
    transactions: list[dict[str, Any]],
    *,
    today: date | None = None,
    delta_threshold: float = DEFAULT_AUDIT_DELTA_THRESHOLD,
    record_switch_suggestions: bool = True,
) -> list[AuditResult]:
    """Audit a list of transactions against optimal-card rules.

    Each ``transaction`` dict accepts:
        merchant: str         (required)
        amount: float          (required)
        card_last_4: str       (required)
        timestamp: str         (optional, ISO 8601)
        tx_id: str             (optional, used as idempotency key downstream)
        apple_pay: bool        (optional, default False)
        booking_channel: str   (optional)
        mcc: str               (optional)

    Returns one AuditResult per transaction.
    """

    today = today or date.today()
    out: list[AuditResult] = []
    for tx in transactions:
        merchant = str(tx.get("merchant", ""))
        amount = float(tx.get("amount", 0))
        card_last_4 = str(tx.get("card_last_4", ""))
        timestamp = tx.get("timestamp")
        apple_pay = bool(tx.get("apple_pay", False))
        booking_channel = tx.get("booking_channel")
        mcc = tx.get("mcc")

        used_card = catalog.card_for_last_4(card_last_4)
        used_card_id = used_card.id if used_card else None
        used_card_name = used_card.name if used_card else None

        rec = recommend(
            catalog,
            ledger,
            merchant,
            amount=amount,
            apple_pay=apple_pay,
            booking_channel=booking_channel,
            mcc=mcc,
            today=today,
        )

        # Used-card effective rate: the rate the user ACTUALLY got. We
        # synthesize this by running the earn-rate computation against the
        # used card with the same category/channel.
        if used_card is None:
            used_rate = 0.0
            status = "unknown_card"
        else:
            cat_result = categorize(catalog, merchant, mcc=mcc, today=today)
            used_rate_tuple: tuple[float, float, str] | None = None
            if booking_channel is not None:
                used_rate_tuple = _channel_rate_for_card(
                    used_card, cat_result.category, booking_channel, catalog.point_valuations
                )
            if used_rate_tuple is None:
                used_rate_tuple = _earn_rate_for_card(
                    used_card, cat_result.category, catalog.point_valuations
                )
            # Apple Pay partner adjustment for the USED card if it's the apple card.
            if (
                apple_pay
                and used_card.id == "apple_card"
                and cat_result.apple_pay_partner is not None
            ):
                partner_rate = cat_result.apple_pay_partner.rate
                if partner_rate > used_rate_tuple[0]:
                    used_rate_tuple = (partner_rate, partner_rate * 100, "Apple Pay partner")
            used_rate = used_rate_tuple[0]
            if used_card.id == rec.recommended_card:
                status = "ok"
            else:
                delta = (rec.effective_rate - used_rate) * amount
                status = "suboptimal" if delta >= delta_threshold else "ok"

        delta_dollars = (rec.effective_rate - used_rate) * amount
        advice: str | None = None

        if status == "suboptimal":
            merchant_norm = normalize_merchant(merchant)
            already_nagged = ledger.recently_suggested_switch(
                merchant_norm, used_card_id or "", today
            )
            if not already_nagged:
                advice = (
                    f"Switch the card on file at {merchant} to {rec.recommended_card_name}."
                )
                if record_switch_suggestions and used_card_id is not None:
                    ledger.record_switch_suggestion(merchant_norm, used_card_id, today)

        out.append(
            AuditResult(
                merchant=merchant,
                amount=amount,
                timestamp=timestamp if isinstance(timestamp, str) else None,
                used_card=used_card_id,
                used_card_name=used_card_name,
                optimal_card=rec.recommended_card,
                optimal_card_name=rec.recommended_card_name,
                used_rate=used_rate,
                optimal_rate=rec.effective_rate,
                delta_dollars=delta_dollars,
                status=status,
                advice=advice,
            )
        )
    return out

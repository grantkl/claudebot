"""Catalog loader and schema validation for the credit_card MCP server.

The catalog is read-only at runtime: cards, earn rates, point valuations,
merchant overrides, Apple Pay partners, category resolvers, and credit
definitions. It loads from a bundled YAML file shipped with the module
(``credit_cards.yaml``), or from a path provided via the
``CREDIT_CARDS_CATALOG`` env var. Re-reads the file when its mtime changes
so YAML edits take effect without a process restart.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

CATALOG_VERSION = 1

VALID_PERIODS = {"monthly", "quarterly", "semi_annual", "annual", "cardmember_year"}


class CatalogError(Exception):
    """Raised when the catalog YAML is malformed."""


@dataclass(frozen=True)
class BaseRate:
    currency: str
    points_per_dollar: float


@dataclass(frozen=True)
class CategoryBonus:
    category: str
    points_per_dollar: float
    notes: str = ""


@dataclass(frozen=True)
class Card:
    id: str
    name: str
    issuer: str
    network: str
    annual_fee: float
    base_rate: BaseRate
    category_bonuses: tuple[CategoryBonus, ...] = ()
    booking_channels: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class MerchantOverride:
    pattern: re.Pattern[str]
    card_id: str
    credit_id: str | None = None


@dataclass(frozen=True)
class ApplePayPartner:
    pattern: re.Pattern[str]
    rate: float
    active_through: date | None = None
    spend_cap: float | None = None


@dataclass(frozen=True)
class CategoryPattern:
    regex: re.Pattern[str]
    category: str


@dataclass(frozen=True)
class Credit:
    id: str
    card_id: str
    name: str
    period: str
    period_amount: float
    enrollment_required: bool = False
    cardmember_anchor_month: int | None = None
    active_from: date | None = None
    active_through: date | None = None


@dataclass(frozen=True)
class Catalog:
    version: int
    point_valuations: dict[str, float]
    cards: tuple[Card, ...]
    last_4_map: dict[str, str]
    merchant_overrides: tuple[MerchantOverride, ...]
    apple_pay_partners: tuple[ApplePayPartner, ...]
    category_patterns: tuple[CategoryPattern, ...]
    mcc_overrides: dict[str, str]
    credits: tuple[Credit, ...]

    def card_by_id(self, card_id: str) -> Card | None:
        for c in self.cards:
            if c.id == card_id:
                return c
        return None

    def credit_by_id(self, credit_id: str) -> Credit | None:
        for c in self.credits:
            if c.id == credit_id:
                return c
        return None

    def card_for_last_4(self, last_4: str) -> Card | None:
        card_id = self.last_4_map.get(last_4)
        if card_id is None:
            return None
        return self.card_by_id(card_id)

    def credits_for_card(self, card_id: str) -> tuple[Credit, ...]:
        return tuple(c for c in self.credits if c.card_id == card_id)


# --------------------------------------------------------------------------- #
# Loading + caching
# --------------------------------------------------------------------------- #


_DEFAULT_CATALOG_PATH = Path(__file__).parent / "credit_cards.yaml"

_cached_catalog: Catalog | None = None
_cached_path: Path | None = None
_cached_mtime: float | None = None


def _resolve_path() -> Path:
    override = os.environ.get("CREDIT_CARDS_CATALOG")
    if override:
        return Path(override)
    return _DEFAULT_CATALOG_PATH


def load_catalog(path: str | os.PathLike[str] | None = None, *, force: bool = False) -> Catalog:
    """Load the catalog, returning a cached copy if mtime is unchanged.

    Pass ``force=True`` to bypass the mtime cache (used in tests).
    """

    global _cached_catalog, _cached_path, _cached_mtime
    target = Path(path) if path is not None else _resolve_path()
    try:
        mtime = target.stat().st_mtime
    except OSError as e:
        raise CatalogError(f"Catalog not found at {target}: {e}") from e

    if (
        not force
        and _cached_catalog is not None
        and _cached_path == target
        and _cached_mtime == mtime
    ):
        return _cached_catalog

    with open(target) as f:
        raw = yaml.safe_load(f)
    catalog = _parse(raw)
    _cached_catalog = catalog
    _cached_path = target
    _cached_mtime = mtime
    return catalog


def _parse(raw: Any) -> Catalog:
    if not isinstance(raw, dict):
        raise CatalogError("Catalog root must be a mapping")
    version = raw.get("version")
    if version != CATALOG_VERSION:
        raise CatalogError(f"Unsupported catalog version: {version}")

    point_valuations = raw.get("point_valuations") or {}
    if not isinstance(point_valuations, dict):
        raise CatalogError("point_valuations must be a mapping")
    point_valuations = {str(k): float(v) for k, v in point_valuations.items()}

    cards = _parse_cards(raw.get("cards") or [], point_valuations)
    last_4_map = {str(k): str(v) for k, v in (raw.get("last_4_map") or {}).items()}
    overrides = _parse_overrides(raw.get("merchant_overrides") or [])
    partners = _parse_partners(raw.get("apple_pay_partners") or [])
    cats = _parse_categories(raw.get("merchant_categories") or {})
    credits_ = _parse_credits(raw.get("credits") or [], {c.id for c in cards})

    # Cross-reference checks: every override card_id and credit_id must exist.
    card_ids = {c.id for c in cards}
    credit_ids = {c.id for c in credits_}
    for ov in overrides:
        if ov.card_id not in card_ids:
            raise CatalogError(f"override references unknown card_id: {ov.card_id}")
        if ov.credit_id is not None and ov.credit_id not in credit_ids:
            raise CatalogError(f"override references unknown credit_id: {ov.credit_id}")
    for last_4_card in last_4_map.values():
        if last_4_card not in card_ids:
            raise CatalogError(f"last_4_map references unknown card_id: {last_4_card}")

    return Catalog(
        version=version,
        point_valuations=point_valuations,
        cards=tuple(cards),
        last_4_map=last_4_map,
        merchant_overrides=tuple(overrides),
        apple_pay_partners=tuple(partners),
        category_patterns=cats[0],
        mcc_overrides=cats[1],
        credits=tuple(credits_),
    )


def _parse_cards(raw_cards: list[Any], point_valuations: dict[str, float]) -> list[Card]:
    cards: list[Card] = []
    seen: set[str] = set()
    for raw in raw_cards:
        if not isinstance(raw, dict):
            raise CatalogError("each card entry must be a mapping")
        cid = raw.get("id")
        if not isinstance(cid, str) or not cid:
            raise CatalogError("card.id is required and must be a string")
        if cid in seen:
            raise CatalogError(f"duplicate card id: {cid}")
        seen.add(cid)

        base_rate_raw = raw.get("base_rate") or {}
        currency = base_rate_raw.get("currency")
        if currency not in point_valuations:
            raise CatalogError(
                f"card {cid}: base_rate.currency '{currency}' missing from point_valuations"
            )
        base_rate = BaseRate(
            currency=str(currency),
            points_per_dollar=float(base_rate_raw.get("points_per_dollar", 1)),
        )

        bonuses: list[CategoryBonus] = []
        for b in raw.get("category_bonuses") or []:
            if not isinstance(b, dict):
                raise CatalogError(f"card {cid}: each category_bonus must be a mapping")
            bonuses.append(
                CategoryBonus(
                    category=str(b["category"]),
                    points_per_dollar=float(b["points_per_dollar"]),
                    notes=str(b.get("notes", "")),
                )
            )

        channels_raw = raw.get("booking_channels") or {}
        channels: dict[str, dict[str, float]] = {}
        for ch_type, ch_map in channels_raw.items():
            if not isinstance(ch_map, dict):
                raise CatalogError(f"card {cid}: booking_channels.{ch_type} must be a mapping")
            channels[str(ch_type)] = {str(k): float(v) for k, v in ch_map.items()}

        cards.append(
            Card(
                id=cid,
                name=str(raw.get("name", cid)),
                issuer=str(raw.get("issuer", "")),
                network=str(raw.get("network", "")),
                annual_fee=float(raw.get("annual_fee", 0)),
                base_rate=base_rate,
                category_bonuses=tuple(bonuses),
                booking_channels=channels,
            )
        )
    return cards


def _parse_overrides(raw: list[Any]) -> list[MerchantOverride]:
    out: list[MerchantOverride] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise CatalogError("each merchant_override must be a mapping")
        pattern_str = entry.get("merchant_pattern")
        if not isinstance(pattern_str, str):
            raise CatalogError("merchant_override.merchant_pattern is required")
        try:
            pattern = re.compile(pattern_str)
        except re.error as e:
            raise CatalogError(f"invalid merchant_override regex {pattern_str!r}: {e}") from e
        out.append(
            MerchantOverride(
                pattern=pattern,
                card_id=str(entry["card_id"]),
                credit_id=(str(entry["credit_id"]) if entry.get("credit_id") else None),
            )
        )
    return out


def _parse_partners(raw: list[Any]) -> list[ApplePayPartner]:
    out: list[ApplePayPartner] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise CatalogError("each apple_pay_partner must be a mapping")
        pattern_str = entry.get("merchant_pattern")
        if not isinstance(pattern_str, str):
            raise CatalogError("apple_pay_partner.merchant_pattern is required")
        try:
            pattern = re.compile(pattern_str)
        except re.error as e:
            raise CatalogError(f"invalid apple_pay_partner regex {pattern_str!r}: {e}") from e
        out.append(
            ApplePayPartner(
                pattern=pattern,
                rate=float(entry.get("rate", 0.03)),
                active_through=_parse_date(entry.get("active_through")),
                spend_cap=(float(entry["spend_cap"]) if entry.get("spend_cap") is not None else None),
            )
        )
    return out


def _parse_categories(raw: Any) -> tuple[tuple[CategoryPattern, ...], dict[str, str]]:
    if not isinstance(raw, dict):
        return ((), {})
    patterns: list[CategoryPattern] = []
    for entry in raw.get("patterns") or []:
        if not isinstance(entry, dict):
            raise CatalogError("each merchant_categories.pattern must be a mapping")
        regex_str = entry.get("regex")
        if not isinstance(regex_str, str):
            raise CatalogError("merchant_categories.pattern.regex is required")
        try:
            regex = re.compile(regex_str)
        except re.error as e:
            raise CatalogError(f"invalid category regex {regex_str!r}: {e}") from e
        patterns.append(CategoryPattern(regex=regex, category=str(entry["category"])))
    mcc = {str(k): str(v) for k, v in (raw.get("mcc_overrides") or {}).items()}
    return tuple(patterns), mcc


def _parse_credits(raw: list[Any], known_card_ids: set[str]) -> list[Credit]:
    out: list[Credit] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise CatalogError("each credit must be a mapping")
        cid = entry.get("id")
        if not isinstance(cid, str) or not cid:
            raise CatalogError("credit.id is required and must be a string")
        if cid in seen:
            raise CatalogError(f"duplicate credit id: {cid}")
        seen.add(cid)
        period = entry.get("period")
        if period not in VALID_PERIODS:
            raise CatalogError(f"credit {cid}: invalid period {period!r}")
        card_id = entry.get("card_id")
        if card_id not in known_card_ids:
            raise CatalogError(f"credit {cid}: unknown card_id {card_id!r}")
        anchor = entry.get("cardmember_anchor_month")
        if period == "cardmember_year" and (
            not isinstance(anchor, int) or not 1 <= anchor <= 12
        ):
            raise CatalogError(
                f"credit {cid}: cardmember_year period requires cardmember_anchor_month (1-12)"
            )
        out.append(
            Credit(
                id=cid,
                card_id=str(card_id),
                name=str(entry.get("name", cid)),
                period=str(period),
                period_amount=float(entry["period_amount"]),
                enrollment_required=bool(entry.get("enrollment_required", False)),
                cardmember_anchor_month=anchor if isinstance(anchor, int) else None,
                active_from=_parse_date(entry.get("active_from")),
                active_through=_parse_date(entry.get("active_through")),
            )
        )
    return out


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as e:
            raise CatalogError(f"invalid date string {value!r}: {e}") from e
    raise CatalogError(f"unparseable date: {value!r}")


def reset_cache() -> None:
    """Reset the load cache. Used by tests."""

    global _cached_catalog, _cached_path, _cached_mtime
    _cached_catalog = None
    _cached_path = None
    _cached_mtime = None

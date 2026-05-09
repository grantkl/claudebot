"""Merchant string -> category resolver.

Card-issuer transaction emails almost never include MCC, so the resolver is
primarily regex-based (``merchant_categories.patterns`` in the catalog) with
optional MCC overrides for the rare path where an MCC was extracted.

Returns:
    CategorizeResult — bundles the resolved category, override match (if any),
    and Apple Pay partner match (if any). The engine consumes this directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .catalog import (
    ApplePayPartner,
    Catalog,
    MerchantOverride,
)


@dataclass(frozen=True)
class CategorizeResult:
    category: str
    override: MerchantOverride | None
    apple_pay_partner: ApplePayPartner | None


def normalize_merchant(merchant: str) -> str:
    """Lower-case + strip + collapse internal whitespace.

    Used as the canonical key for switch-suggestion cooldown.
    """

    return " ".join(merchant.lower().split())


def categorize(
    catalog: Catalog,
    merchant: str,
    *,
    mcc: str | None = None,
    today: date | None = None,
) -> CategorizeResult:
    """Resolve ``merchant`` into a category + override/partner matches.

    ``today`` is used to exclude expired Apple Pay partner promos
    (e.g. Walgreens 5% through 2026-05-20). Defaults to today's date.
    """

    today = today or date.today()
    merchant_norm = normalize_merchant(merchant)

    # 1. Override match (highest priority signal).
    override: MerchantOverride | None = None
    for ov in catalog.merchant_overrides:
        if ov.pattern.search(merchant_norm) or ov.pattern.search(merchant):
            override = ov
            break

    # 2. Apple Pay partner match (independent signal — used only when
    #    apple_pay=True at recommend() time).
    partner: ApplePayPartner | None = None
    for p in catalog.apple_pay_partners:
        if p.active_through is not None and today > p.active_through:
            continue
        if p.pattern.search(merchant_norm) or p.pattern.search(merchant):
            partner = p
            break

    # 3. Category resolution. MCC override wins if present, else first
    #    matching regex.
    category: str = "other"
    if mcc and mcc in catalog.mcc_overrides:
        category = catalog.mcc_overrides[mcc]
    else:
        for cp in catalog.category_patterns:
            if cp.regex.search(merchant_norm) or cp.regex.search(merchant):
                category = cp.category
                break

    return CategorizeResult(
        category=category,
        override=override,
        apple_pay_partner=partner,
    )

"""Tests for the credit_card rule engine and merchant categorization."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from src.mcp.credit_card.catalog import load_catalog, reset_cache
from src.mcp.credit_card.categorize import categorize, normalize_merchant
from src.mcp.credit_card.engine import audit, recommend
from src.mcp.credit_card.ledger import CreditLedger


# --------------------------------------------------------------------------- #
# Test catalog: small but representative.
# --------------------------------------------------------------------------- #

TEST_CATALOG = {
    "version": 1,
    "point_valuations": {
        "amex_mr": 1.8,
        "chase_ur": 1.8,
        "delta_skymiles": 1.2,
        "alaska_miles": 1.4,
        "cash": 1.0,
    },
    "cards": [
        {
            "id": "amex_plat",
            "name": "Amex Platinum",
            "issuer": "amex",
            "network": "amex",
            "annual_fee": 895,
            "base_rate": {"currency": "amex_mr", "points_per_dollar": 1},
            "category_bonuses": [
                {"category": "airline_direct", "points_per_dollar": 5},
                {"category": "hotel_amex_travel", "points_per_dollar": 5},
            ],
            "booking_channels": {
                "hotel": {"amex_travel": 5, "fhr": 5, "direct": 1},
                "flight": {"direct": 5, "amex_travel": 5},
            },
        },
        {
            "id": "csr",
            "name": "Chase Sapphire Reserve",
            "issuer": "chase",
            "network": "visa",
            "annual_fee": 795,
            "base_rate": {"currency": "chase_ur", "points_per_dollar": 1},
            "category_bonuses": [
                {"category": "dining", "points_per_dollar": 3},
            ],
            "booking_channels": {
                "hotel": {"chase_travel": 8, "direct": 4},
                "flight": {"chase_travel": 8, "direct": 4},
            },
        },
        {
            "id": "amazon_prime_visa",
            "name": "Amazon Prime Visa",
            "issuer": "chase",
            "network": "visa",
            "annual_fee": 0,
            "base_rate": {"currency": "cash", "points_per_dollar": 1},
            "category_bonuses": [
                {"category": "amazon", "points_per_dollar": 5},
                {"category": "dining", "points_per_dollar": 2},
                {"category": "gas", "points_per_dollar": 2},
            ],
        },
        {
            "id": "alaska_visa",
            "name": "Alaska Visa",
            "issuer": "bofa",
            "network": "visa",
            "annual_fee": 95,
            "base_rate": {"currency": "alaska_miles", "points_per_dollar": 1},
            "category_bonuses": [
                {"category": "alaska_air", "points_per_dollar": 3},
                {"category": "gas", "points_per_dollar": 2},
                {"category": "streaming", "points_per_dollar": 2},
            ],
        },
        {
            "id": "apple_card",
            "name": "Apple Card",
            "issuer": "goldman",
            "network": "mastercard",
            "annual_fee": 0,
            "base_rate": {"currency": "cash", "points_per_dollar": 1},
        },
        {
            "id": "mgm_mastercard",
            "name": "MGM",
            "issuer": "fnbo",
            "network": "mastercard",
            "annual_fee": 0,
            "base_rate": {"currency": "cash", "points_per_dollar": 1},
            "category_bonuses": [
                {"category": "grocery", "points_per_dollar": 2},
                {"category": "gas", "points_per_dollar": 2},
            ],
        },
    ],
    "last_4_map": {
        "1027": "amex_plat",
        "3253": "csr",
        "5502": "amazon_prime_visa",
        "4365": "alaska_visa",
        "8034": "apple_card",
        "7337": "mgm_mastercard",
    },
    "merchant_overrides": [
        {"merchant_pattern": "(?i)^uber( eats)?$", "card_id": "amex_plat", "credit_id": "amex_plat_uber_cash"},
        {"merchant_pattern": "(?i)^lyft$", "card_id": "csr", "credit_id": "csr_lyft"},
    ],
    "apple_pay_partners": [
        {"merchant_pattern": "(?i)^uber", "rate": 0.03},
        {"merchant_pattern": "(?i)walgreens", "rate": 0.05, "active_through": "2026-05-20"},
    ],
    "merchant_categories": {
        "patterns": [
            {"regex": "(?i)trader joe|safeway|kroger", "category": "grocery"},
            {"regex": "(?i)amazon|amzn|whole foods", "category": "amazon"},
            {"regex": "(?i)shell|chevron", "category": "gas"},
            {"regex": "(?i)hilton|marriott|hyatt", "category": "hotel_direct"},
            {"regex": "(?i)delta air", "category": "airline_direct"},
            {"regex": "(?i)alaska air", "category": "alaska_air"},
            {"regex": "(?i)netflix|hulu|spotify", "category": "streaming"},
            {"regex": "(?i)restaurant|pizza|cafe", "category": "dining"},
        ],
        "mcc_overrides": {"5411": "grocery", "5812": "dining"},
    },
    "credits": [
        {
            "id": "amex_plat_uber_cash",
            "card_id": "amex_plat",
            "name": "Uber Cash",
            "period": "monthly",
            "period_amount": 15,
        },
        {
            "id": "csr_lyft",
            "card_id": "csr",
            "name": "Lyft credit",
            "period": "monthly",
            "period_amount": 10,
        },
    ],
}


@pytest.fixture()
def catalog(tmp_path: Path):
    reset_cache()
    path = tmp_path / "test_catalog.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(TEST_CATALOG, f)
    yield load_catalog(path, force=True)
    reset_cache()


@pytest.fixture()
def ledger(tmp_path: Path) -> CreditLedger:
    return CreditLedger(tmp_path / "ledger.json")


TODAY = date(2026, 5, 9)


# --------------------------------------------------------------------------- #
# normalize_merchant
# --------------------------------------------------------------------------- #


class TestNormalize:
    def test_lower_strip_collapse(self) -> None:
        assert normalize_merchant("  Uber  Eats  ") == "uber eats"
        assert normalize_merchant("Trader Joe's") == "trader joe's"


# --------------------------------------------------------------------------- #
# categorize
# --------------------------------------------------------------------------- #


class TestCategorize:
    def test_grocery(self, catalog) -> None:
        assert categorize(catalog, "Trader Joe's", today=TODAY).category == "grocery"
        assert categorize(catalog, "SAFEWAY #1234", today=TODAY).category == "grocery"

    def test_dining(self, catalog) -> None:
        assert categorize(catalog, "Joe's Pizza", today=TODAY).category == "dining"

    def test_other_default(self, catalog) -> None:
        assert categorize(catalog, "Mystery Place LLC", today=TODAY).category == "other"

    def test_mcc_override_wins(self, catalog) -> None:
        # Generic merchant string falls back to "other", but with MCC 5812 the
        # mcc_overrides table forces "dining".
        result = categorize(catalog, "MERCHANT XYZ", mcc="5812", today=TODAY)
        assert result.category == "dining"

    def test_override_match(self, catalog) -> None:
        result = categorize(catalog, "Uber", today=TODAY)
        assert result.override is not None
        assert result.override.card_id == "amex_plat"

    def test_apple_pay_partner_match(self, catalog) -> None:
        result = categorize(catalog, "Walgreens", today=date(2026, 5, 9))
        assert result.apple_pay_partner is not None
        assert result.apple_pay_partner.rate == 0.05

    def test_apple_pay_partner_expired(self, catalog) -> None:
        # Walgreens promo ends 2026-05-20.
        result = categorize(catalog, "Walgreens", today=date(2026, 5, 21))
        assert result.apple_pay_partner is None


# --------------------------------------------------------------------------- #
# recommend — earn-rate stages
# --------------------------------------------------------------------------- #


class TestRecommendEarnRate:
    def test_dining_picks_csr(self, catalog, ledger) -> None:
        rec = recommend(catalog, ledger, "Some Pizza Cafe", today=TODAY)
        # CSR 3x UR @ 1.8c = 5.4%. Amazon 2x cash = 2%. Alaska base 1x @ 1.4c = 1.4%.
        assert rec.recommended_card == "csr"
        assert rec.effective_rate == pytest.approx(0.054)

    def test_grocery_picks_mgm(self, catalog, ledger) -> None:
        rec = recommend(catalog, ledger, "Trader Joe's", today=TODAY)
        # MGM 2x cash = 2% beats CSR base 1x UR @ 1.8c = 1.8%.
        assert rec.recommended_card == "mgm_mastercard"

    def test_amazon_picks_amazon_visa(self, catalog, ledger) -> None:
        rec = recommend(catalog, ledger, "AMZN Mktp US", today=TODAY)
        assert rec.recommended_card == "amazon_prime_visa"
        assert rec.effective_rate == pytest.approx(0.05)

    def test_streaming_picks_alaska(self, catalog, ledger) -> None:
        rec = recommend(catalog, ledger, "Spotify Premium", today=TODAY)
        # Alaska 2x miles @ 1.4c = 2.8% beats CSR base 1.8%.
        assert rec.recommended_card == "alaska_visa"
        assert rec.effective_rate == pytest.approx(0.028)

    def test_other_falls_back_to_csr_or_amex(self, catalog, ledger) -> None:
        rec = recommend(catalog, ledger, "Mystery Place LLC", today=TODAY)
        # CSR base 1x UR @ 1.8c and Amex Plat base 1x MR @ 1.8c are tied.
        # Tie-breaker: CSR has $795 AF, Amex has $895 AF -> CSR wins.
        assert rec.recommended_card in ("csr", "amex_plat")

    def test_4x_ur_beats_3pct_raw(self, catalog, ledger) -> None:
        """A core test: point math (4x UR @ 1.8c = 7.2%) must beat 3% raw."""

        # Use direct hotel booking. CSR 4x UR direct = 7.2%; Amex Plat direct
        # hotel multiplier is 1x = 1.8%; Apple Card no partner here.
        rec = recommend(
            catalog, ledger, "Marriott Resort", booking_channel="direct", today=TODAY
        )
        assert rec.recommended_card == "csr"
        assert rec.effective_rate == pytest.approx(0.072)


# --------------------------------------------------------------------------- #
# recommend — overrides + credit caps
# --------------------------------------------------------------------------- #


class TestRecommendOverrides:
    def test_uber_override_amex_plat(self, catalog, ledger) -> None:
        rec = recommend(catalog, ledger, "Uber", today=TODAY)
        assert rec.recommended_card == "amex_plat"
        assert rec.credit_triggered is True
        assert rec.credit_id == "amex_plat_uber_cash"
        assert rec.cap_remaining == 15.0

    def test_uber_override_falls_through_when_cap_exhausted(
        self, catalog, ledger
    ) -> None:
        # Exhaust the Uber Cash credit
        credit = catalog.credit_by_id("amex_plat_uber_cash")
        ledger.mark_used(credit, 15.0, TODAY, tx_id="exhaust")

        rec = recommend(catalog, ledger, "Uber", today=TODAY)
        # Override still references amex_plat but credit_triggered is False
        # because cap remaining is 0. Engine falls through to earn-rate stage.
        assert rec.credit_triggered is False
        # No bonus category for "rideshare" exists, so candidates compete on
        # base rates. Apple Card without apple_pay = 1%. CSR/Amex Plat = 1.8%.
        assert rec.recommended_card in ("csr", "amex_plat")

    def test_uber_override_falls_through_to_apple_pay_partner(
        self, catalog, ledger
    ) -> None:
        credit = catalog.credit_by_id("amex_plat_uber_cash")
        ledger.mark_used(credit, 15.0, TODAY, tx_id="exhaust")

        rec = recommend(catalog, ledger, "Uber", apple_pay=True, today=TODAY)
        # Apple Card 3% via Apple Pay partner now beats CSR/Plat 1.8%.
        assert rec.recommended_card == "apple_card"
        assert rec.effective_rate == pytest.approx(0.03)


# --------------------------------------------------------------------------- #
# recommend — booking channels
# --------------------------------------------------------------------------- #


class TestRecommendBookingChannel:
    def test_hotel_via_chase_travel(self, catalog, ledger) -> None:
        rec = recommend(
            catalog, ledger, "Marriott", booking_channel="chase_travel", today=TODAY
        )
        # CSR 8x UR via chase_travel = 14.4%. Beats everything else.
        assert rec.recommended_card == "csr"
        assert rec.effective_rate == pytest.approx(0.144)

    def test_hotel_via_amex_travel(self, catalog, ledger) -> None:
        rec = recommend(
            catalog, ledger, "Marriott", booking_channel="amex_travel", today=TODAY
        )
        # Amex Plat 5x MR via amex_travel = 9%.
        assert rec.recommended_card == "amex_plat"
        assert rec.effective_rate == pytest.approx(0.09)

    def test_flight_direct_amex_wins(self, catalog, ledger) -> None:
        rec = recommend(
            catalog, ledger, "Delta Airlines", booking_channel="direct", today=TODAY
        )
        # Amex Plat 5x MR direct = 9%. CSR 4x UR direct = 7.2%.
        assert rec.recommended_card == "amex_plat"


# --------------------------------------------------------------------------- #
# recommend — Apple Pay partners
# --------------------------------------------------------------------------- #


class TestRecommendApplePayPartner:
    def test_walgreens_with_apple_pay_promo(self, catalog, ledger) -> None:
        rec = recommend(catalog, ledger, "Walgreens", apple_pay=True, today=TODAY)
        # 5% promo through 2026-05-20.
        assert rec.recommended_card == "apple_card"
        assert rec.effective_rate == pytest.approx(0.05)

    def test_walgreens_after_promo_falls_back(self, catalog, ledger) -> None:
        rec = recommend(
            catalog, ledger, "Walgreens", apple_pay=True, today=date(2026, 6, 1)
        )
        # Promo expired -> Apple Card no longer 5%. Apple Pay tools still
        # active but Walgreens isn't in the standard 3% partner list, so it
        # falls back to earn-rate winners.
        assert rec.recommended_card != "apple_card" or rec.effective_rate < 0.05

    def test_no_apple_pay_means_no_partner_rate(self, catalog, ledger) -> None:
        # Same merchant, but apple_pay=False — partner rate not unlocked.
        rec = recommend(catalog, ledger, "Walgreens", apple_pay=False, today=TODAY)
        # No bonus category for Walgreens; falls to base-rate winners.
        assert rec.effective_rate < 0.05


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #


class TestAudit:
    def test_used_optimal_card_marks_ok(self, catalog, ledger) -> None:
        results = audit(
            catalog,
            ledger,
            [
                {
                    "merchant": "Uber",
                    "amount": 25.00,
                    "card_last_4": "1027",  # Amex Plat — correct
                    "tx_id": "tx1",
                }
            ],
            today=TODAY,
        )
        assert results[0].status == "ok"
        assert results[0].used_card == "amex_plat"
        assert results[0].optimal_card == "amex_plat"

    def test_used_wrong_card_flags_suboptimal(self, catalog, ledger) -> None:
        results = audit(
            catalog,
            ledger,
            [
                {
                    "merchant": "Uber",
                    "amount": 25.00,
                    "card_last_4": "8034",  # Apple Card — wrong (no apple pay)
                    "tx_id": "tx2",
                }
            ],
            today=TODAY,
        )
        assert results[0].status == "suboptimal"
        assert results[0].used_card == "apple_card"
        assert results[0].optimal_card == "amex_plat"
        assert results[0].advice is not None
        assert "amex" in results[0].advice.lower() or "platinum" in results[0].advice.lower()

    def test_unknown_card_marks_unknown(self, catalog, ledger) -> None:
        results = audit(
            catalog,
            ledger,
            [
                {
                    "merchant": "Uber",
                    "amount": 25.00,
                    "card_last_4": "0000",  # Unmapped
                    "tx_id": "tx3",
                }
            ],
            today=TODAY,
        )
        assert results[0].status == "unknown_card"
        assert results[0].used_card is None

    def test_small_delta_treated_as_ok(self, catalog, ledger) -> None:
        # On a $1 charge, a 0.6% rate diff = $0.006 — under threshold.
        results = audit(
            catalog,
            ledger,
            [
                {
                    "merchant": "Mystery Place LLC",
                    "amount": 1.00,
                    "card_last_4": "5502",  # Amazon Visa 1% (cash)
                    "tx_id": "tx_small",
                }
            ],
            today=TODAY,
        )
        # Optimal might be CSR (1.8%) but $0.008 delta < $0.50 threshold.
        assert results[0].status == "ok"

    def test_switch_advice_suppressed_within_cooldown(self, catalog, ledger) -> None:
        first = audit(
            catalog,
            ledger,
            [
                {
                    "merchant": "Uber",
                    "amount": 25.00,
                    "card_last_4": "8034",
                    "tx_id": "tx_first",
                }
            ],
            today=TODAY,
        )
        assert first[0].advice is not None

        second = audit(
            catalog,
            ledger,
            [
                {
                    "merchant": "Uber",
                    "amount": 30.00,
                    "card_last_4": "8034",
                    "tx_id": "tx_second",
                }
            ],
            today=date(2026, 5, 15),  # Within 30-day cooldown
        )
        # Still flagged suboptimal but no nag advice.
        assert second[0].status == "suboptimal"
        assert second[0].advice is None

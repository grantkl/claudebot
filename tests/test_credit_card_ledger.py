"""Tests for the credit_card ledger and period-key math."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from src.mcp.credit_card.catalog import Credit
from src.mcp.credit_card.ledger import (
    SUGGESTION_COOLDOWN_DAYS,
    CreditLedger,
    is_active,
    period_key,
)


def _credit(
    period: str,
    *,
    cid: str = "c",
    period_amount: float = 50.0,
    cardmember_anchor_month: int | None = None,
    active_from: date | None = None,
    active_through: date | None = None,
) -> Credit:
    return Credit(
        id=cid,
        card_id="card",
        name=cid,
        period=period,
        period_amount=period_amount,
        cardmember_anchor_month=cardmember_anchor_month,
        active_from=active_from,
        active_through=active_through,
    )


class TestPeriodKey:
    def test_monthly(self) -> None:
        c = _credit("monthly")
        assert period_key(c, date(2026, 1, 15)) == "2026-01"
        assert period_key(c, date(2026, 12, 1)) == "2026-12"

    @pytest.mark.parametrize(
        "month,expected",
        [(1, "Q1"), (3, "Q1"), (4, "Q2"), (6, "Q2"), (7, "Q3"), (9, "Q3"), (10, "Q4"), (12, "Q4")],
    )
    def test_quarterly(self, month: int, expected: str) -> None:
        c = _credit("quarterly")
        assert period_key(c, date(2026, month, 15)) == f"2026-{expected}"

    def test_semi_annual(self) -> None:
        c = _credit("semi_annual")
        assert period_key(c, date(2026, 6, 30)) == "2026-H1"
        assert period_key(c, date(2026, 7, 1)) == "2026-H2"

    def test_annual(self) -> None:
        c = _credit("annual")
        assert period_key(c, date(2026, 5, 9)) == "2026"

    def test_cardmember_year_within_anchor_year(self) -> None:
        # Anchor month 5 (May). On 2026-05-15 the cy bucket is 2026-05_2027-04.
        c = _credit("cardmember_year", cardmember_anchor_month=5)
        assert period_key(c, date(2026, 5, 15)) == "cy:2026-05_2027-04"
        assert period_key(c, date(2026, 12, 1)) == "cy:2026-05_2027-04"
        assert period_key(c, date(2027, 4, 30)) == "cy:2026-05_2027-04"

    def test_cardmember_year_before_anchor_uses_prior_year(self) -> None:
        c = _credit("cardmember_year", cardmember_anchor_month=5)
        # Jan 2026 is in the bucket that started May 2025.
        assert period_key(c, date(2026, 1, 1)) == "cy:2025-05_2026-04"

    def test_cardmember_year_january_anchor(self) -> None:
        c = _credit("cardmember_year", cardmember_anchor_month=1)
        assert period_key(c, date(2026, 6, 1)) == "cy:2026-01_2026-12"

    def test_cardmember_year_requires_anchor(self) -> None:
        c = _credit("cardmember_year", cardmember_anchor_month=None)
        with pytest.raises(ValueError, match="cardmember_anchor_month"):
            period_key(c, date(2026, 5, 1))

    def test_unknown_period_raises(self) -> None:
        c = _credit("weekly")
        with pytest.raises(ValueError, match="unknown period"):
            period_key(c, date(2026, 5, 1))


class TestIsActive:
    def test_within_window(self) -> None:
        c = _credit("annual", active_from=date(2026, 1, 1), active_through=date(2026, 12, 31))
        assert is_active(c, date(2026, 6, 15)) is True

    def test_before_active_from(self) -> None:
        c = _credit("annual", active_from=date(2026, 6, 1))
        assert is_active(c, date(2026, 1, 1)) is False

    def test_after_active_through(self) -> None:
        c = _credit("annual", active_through=date(2026, 5, 20))
        assert is_active(c, date(2026, 5, 21)) is False
        assert is_active(c, date(2026, 5, 20)) is True

    def test_no_window_always_active(self) -> None:
        c = _credit("monthly")
        assert is_active(c, date(2030, 1, 1)) is True


class TestCreditLedger:
    def test_initial_state_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        ledger = CreditLedger(path)
        c = _credit("monthly", period_amount=15)
        assert ledger.used(c, date(2026, 5, 1)) == 0.0
        assert ledger.cap_remaining(c, date(2026, 5, 1)) == 15.0

    def test_mark_used_basic(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        ledger = CreditLedger(path)
        c = _credit("monthly", period_amount=15)
        result = ledger.mark_used(c, 5.0, date(2026, 5, 1), tx_id="t1")
        assert result["used"] == 5.0
        assert result["remaining"] == 10.0
        assert result["noop"] is False
        assert ledger.used(c, date(2026, 5, 1)) == 5.0

    def test_mark_used_idempotent_on_tx_id(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        ledger = CreditLedger(path)
        c = _credit("monthly", period_amount=15)
        ledger.mark_used(c, 5.0, date(2026, 5, 1), tx_id="t1")
        result = ledger.mark_used(c, 5.0, date(2026, 5, 1), tx_id="t1")
        assert result["noop"] is True
        # Used did not double-count
        assert ledger.used(c, date(2026, 5, 1)) == 5.0

    def test_mark_used_no_tx_id_always_increments(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        ledger = CreditLedger(path)
        c = _credit("monthly", period_amount=15)
        ledger.mark_used(c, 3.0, date(2026, 5, 1))
        ledger.mark_used(c, 3.0, date(2026, 5, 1))
        assert ledger.used(c, date(2026, 5, 1)) == 6.0

    def test_separate_periods(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        ledger = CreditLedger(path)
        c = _credit("monthly", period_amount=15)
        ledger.mark_used(c, 15.0, date(2026, 5, 1), tx_id="may")
        ledger.mark_used(c, 5.0, date(2026, 6, 1), tx_id="jun")
        assert ledger.used(c, date(2026, 5, 15)) == 15.0
        assert ledger.used(c, date(2026, 6, 15)) == 5.0
        assert ledger.cap_remaining(c, date(2026, 5, 15)) == 0.0
        assert ledger.cap_remaining(c, date(2026, 6, 15)) == 10.0

    def test_inactive_credit_zero_remaining(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        ledger = CreditLedger(path)
        c = _credit("annual", period_amount=100, active_through=date(2026, 5, 20))
        # On 2026-05-21 the credit is past its active window.
        assert ledger.cap_remaining(c, date(2026, 5, 21)) == 0.0

    def test_persistence(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        ledger = CreditLedger(path)
        c = _credit("monthly", period_amount=15)
        ledger.mark_used(c, 12.50, date(2026, 5, 1), tx_id="t")

        ledger2 = CreditLedger(path)
        assert ledger2.used(c, date(2026, 5, 1)) == 12.50

    def test_switch_suggestion_cooldown(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        ledger = CreditLedger(path)
        today = date(2026, 5, 9)
        assert ledger.recently_suggested_switch("uber", "apple_card", today) is False
        ledger.record_switch_suggestion("uber", "apple_card", today)
        assert ledger.recently_suggested_switch("uber", "apple_card", today) is True
        # Within cooldown
        within = today + timedelta(days=SUGGESTION_COOLDOWN_DAYS - 1)
        assert ledger.recently_suggested_switch("uber", "apple_card", within) is True
        # Past cooldown
        past = today + timedelta(days=SUGGESTION_COOLDOWN_DAYS + 1)
        assert ledger.recently_suggested_switch("uber", "apple_card", past) is False

"""Tests for the credit_card catalog loader and schema validation."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest
import yaml

from src.mcp.credit_card import catalog as catalog_mod
from src.mcp.credit_card.catalog import (
    Catalog,
    CatalogError,
    load_catalog,
    reset_cache,
)


MINIMAL_YAML = {
    "version": 1,
    "point_valuations": {"cash": 1.0, "amex_mr": 1.8},
    "cards": [
        {
            "id": "test_card",
            "name": "Test Card",
            "issuer": "test",
            "network": "visa",
            "annual_fee": 0,
            "base_rate": {"currency": "cash", "points_per_dollar": 1},
            "category_bonuses": [
                {"category": "dining", "points_per_dollar": 3},
            ],
        },
    ],
    "last_4_map": {"1234": "test_card"},
    "merchant_overrides": [],
    "apple_pay_partners": [],
    "merchant_categories": {
        "patterns": [
            {"regex": "(?i)pizza|coffee", "category": "dining"},
        ],
        "mcc_overrides": {"5812": "dining"},
    },
    "credits": [
        {
            "id": "test_monthly",
            "card_id": "test_card",
            "name": "Test Monthly Credit",
            "period": "monthly",
            "period_amount": 10.0,
        },
    ],
}


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_cache()
    yield
    reset_cache()


def _write_yaml(tmp_path: Path, data: dict, name: str = "credit_cards.yaml") -> Path:
    path = tmp_path / name
    with open(path, "w") as f:
        yaml.safe_dump(data, f)
    return path


class TestCatalogLoading:
    def test_load_valid_catalog(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, MINIMAL_YAML)
        cat = load_catalog(path)
        assert isinstance(cat, Catalog)
        assert cat.version == 1
        assert len(cat.cards) == 1
        assert cat.cards[0].id == "test_card"
        assert cat.cards[0].category_bonuses[0].category == "dining"
        assert cat.last_4_map == {"1234": "test_card"}
        assert cat.point_valuations["amex_mr"] == 1.8

    def test_load_default_catalog_succeeds(self) -> None:
        """The shipped credit_cards.yaml must always pass validation."""

        cat = load_catalog()
        assert cat.version == 1
        assert len(cat.cards) >= 6
        # Sanity: known card ids present
        ids = {c.id for c in cat.cards}
        assert {"amex_plat", "csr", "amex_delta_reserve"}.issubset(ids)
        # last_4_map keys map to existing card ids
        for last_4, cid in cat.last_4_map.items():
            assert any(c.id == cid for c in cat.cards), f"{last_4}->{cid} unknown"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CatalogError):
            load_catalog(tmp_path / "nonexistent.yaml")

    def test_unsupported_version(self, tmp_path: Path) -> None:
        bad = dict(MINIMAL_YAML, version=999)
        path = _write_yaml(tmp_path, bad)
        with pytest.raises(CatalogError, match="version"):
            load_catalog(path)

    def test_unknown_currency_in_base_rate(self, tmp_path: Path) -> None:
        bad = dict(MINIMAL_YAML)
        bad["cards"] = [
            {
                "id": "x",
                "name": "x",
                "issuer": "x",
                "network": "x",
                "annual_fee": 0,
                "base_rate": {"currency": "moonbucks", "points_per_dollar": 1},
            }
        ]
        path = _write_yaml(tmp_path, bad)
        with pytest.raises(CatalogError, match="moonbucks"):
            load_catalog(path)

    def test_duplicate_card_id(self, tmp_path: Path) -> None:
        bad = dict(MINIMAL_YAML)
        bad["cards"] = MINIMAL_YAML["cards"] + MINIMAL_YAML["cards"]
        path = _write_yaml(tmp_path, bad)
        with pytest.raises(CatalogError, match="duplicate card id"):
            load_catalog(path)

    def test_override_references_unknown_card(self, tmp_path: Path) -> None:
        bad = dict(MINIMAL_YAML)
        bad["merchant_overrides"] = [
            {"merchant_pattern": "uber", "card_id": "ghost"},
        ]
        path = _write_yaml(tmp_path, bad)
        with pytest.raises(CatalogError, match="unknown card_id"):
            load_catalog(path)

    def test_override_references_unknown_credit(self, tmp_path: Path) -> None:
        bad = dict(MINIMAL_YAML)
        bad["merchant_overrides"] = [
            {
                "merchant_pattern": "uber",
                "card_id": "test_card",
                "credit_id": "ghost",
            }
        ]
        path = _write_yaml(tmp_path, bad)
        with pytest.raises(CatalogError, match="unknown credit_id"):
            load_catalog(path)

    def test_invalid_period(self, tmp_path: Path) -> None:
        bad = dict(MINIMAL_YAML)
        bad["credits"] = [
            {
                "id": "weekly",
                "card_id": "test_card",
                "name": "weekly",
                "period": "weekly",
                "period_amount": 5,
            }
        ]
        path = _write_yaml(tmp_path, bad)
        with pytest.raises(CatalogError, match="invalid period"):
            load_catalog(path)

    def test_cardmember_year_requires_anchor(self, tmp_path: Path) -> None:
        bad = dict(MINIMAL_YAML)
        bad["credits"] = [
            {
                "id": "cm",
                "card_id": "test_card",
                "name": "cm",
                "period": "cardmember_year",
                "period_amount": 99,
            }
        ]
        path = _write_yaml(tmp_path, bad)
        with pytest.raises(CatalogError, match="cardmember_anchor_month"):
            load_catalog(path)

    def test_invalid_regex(self, tmp_path: Path) -> None:
        bad = dict(MINIMAL_YAML)
        bad["merchant_overrides"] = [
            {"merchant_pattern": "[unclosed", "card_id": "test_card"},
        ]
        path = _write_yaml(tmp_path, bad)
        with pytest.raises(CatalogError, match="invalid"):
            load_catalog(path)

    def test_invalid_active_through_date(self, tmp_path: Path) -> None:
        bad = dict(MINIMAL_YAML)
        bad["credits"] = [
            {
                "id": "x",
                "card_id": "test_card",
                "name": "x",
                "period": "annual",
                "period_amount": 100,
                "active_through": "not-a-date",
            }
        ]
        path = _write_yaml(tmp_path, bad)
        with pytest.raises(CatalogError, match="invalid date"):
            load_catalog(path)


class TestCatalogHotReload:
    def test_mtime_cache_hit(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, MINIMAL_YAML)
        cat1 = load_catalog(path)
        cat2 = load_catalog(path)
        # Same object identity due to mtime cache
        assert cat1 is cat2

    def test_mtime_cache_invalidates_on_change(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, MINIMAL_YAML)
        cat1 = load_catalog(path)
        # Wait a moment + bump mtime
        time.sleep(0.02)
        modified = dict(MINIMAL_YAML)
        modified["cards"] = MINIMAL_YAML["cards"] + [
            {
                "id": "second",
                "name": "Second",
                "issuer": "x",
                "network": "x",
                "annual_fee": 0,
                "base_rate": {"currency": "cash", "points_per_dollar": 1},
            }
        ]
        with open(path, "w") as f:
            yaml.safe_dump(modified, f)
        cat2 = load_catalog(path)
        assert cat1 is not cat2
        assert len(cat2.cards) == 2

    def test_force_bypasses_cache(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, MINIMAL_YAML)
        cat1 = load_catalog(path)
        cat2 = load_catalog(path, force=True)
        assert cat1 is not cat2


class TestCatalogLookups:
    def test_card_by_id(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, MINIMAL_YAML)
        cat = load_catalog(path)
        assert cat.card_by_id("test_card") is not None
        assert cat.card_by_id("nope") is None

    def test_card_for_last_4(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, MINIMAL_YAML)
        cat = load_catalog(path)
        assert cat.card_for_last_4("1234").id == "test_card"
        assert cat.card_for_last_4("0000") is None

    def test_credits_for_card(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, MINIMAL_YAML)
        cat = load_catalog(path)
        credits_ = cat.credits_for_card("test_card")
        assert len(credits_) == 1
        assert credits_[0].id == "test_monthly"

    def test_active_through_parsed(self, tmp_path: Path) -> None:
        spec = dict(MINIMAL_YAML)
        spec["credits"] = [
            {
                "id": "x",
                "card_id": "test_card",
                "name": "x",
                "period": "annual",
                "period_amount": 100,
                "active_through": "2026-12-31",
            }
        ]
        path = _write_yaml(tmp_path, spec)
        cat = load_catalog(path)
        assert cat.credits[0].active_through == date(2026, 12, 31)

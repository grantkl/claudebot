"""Tests for the Facebook Marketplace MCP server.

Covers the regex-based listing parser (most likely piece to silently break on
FB DOM changes), the seen-IDs dedup store (including the 60-day TTL prune and
legacy-schema migration), the URL builder, and the shared browser pool.
"""

from __future__ import annotations

import asyncio
import datetime
import importlib
import json
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Build a mock claude_agent_sdk so the @tool decorator preserves handlers.
# ---------------------------------------------------------------------------


class _FakeSdkMcpTool:
    def __init__(self, handler: Any, name: str, description: str, schema: Any) -> None:
        self.handler = handler
        self.name = name
        self.description = description
        self.schema = schema


def _fake_tool(name: str, description: str, schema: Any):  # noqa: ANN201
    def decorator(fn: Any) -> _FakeSdkMcpTool:
        return _FakeSdkMcpTool(fn, name, description, schema)
    return decorator


_mock_sdk = MagicMock()
_mock_sdk.tool = _fake_tool
_mock_sdk.SdkMcpTool = _FakeSdkMcpTool
sys.modules["claude_agent_sdk"] = _mock_sdk

sys.modules.pop("src.mcp.facebook_marketplace_server", None)

from src.mcp import facebook_marketplace_server as fb  # noqa: E402

importlib.reload(fb)


# ---------------------------------------------------------------------------
# _parse_listing_text
# ---------------------------------------------------------------------------


class TestParseListingText:
    def test_full_car_card(self) -> None:
        text = (
            "$45,000\n"
            "2004 Porsche 911 Carrera 4S\n"
            "85,000 miles\n"
            "Seattle, WA"
        )
        parsed = fb._parse_listing_text(text)
        assert parsed["price"] == "$45000"
        assert parsed["title"] == "2004 Porsche 911 Carrera 4S"
        assert parsed["year"] == 2004
        assert parsed["mileage"] == "85,000 miles"
        assert parsed["location"] == "Seattle, WA"

    def test_price_with_decimals(self) -> None:
        parsed = fb._parse_listing_text("$12,500.00\n2015 Honda Civic\n45,000 mi\nPortland, OR")
        assert parsed["price"] == "$12500.00"
        assert parsed["year"] == 2015

    def test_missing_price_still_returns_title(self) -> None:
        parsed = fb._parse_listing_text("2010 Toyota Camry\n90,000 miles\nSan Francisco, CA")
        assert parsed["price"] is None
        assert parsed["title"] == "2010 Toyota Camry"
        assert parsed["year"] == 2010

    def test_missing_year(self) -> None:
        parsed = fb._parse_listing_text("$3,000\nVintage bicycle\nSeattle, WA")
        assert parsed["price"] == "$3000"
        assert parsed["title"] == "Vintage bicycle"
        assert parsed["year"] is None

    def test_missing_mileage_and_location(self) -> None:
        parsed = fb._parse_listing_text("$500\nBroken surfboard")
        assert parsed["price"] == "$500"
        assert parsed["title"] == "Broken surfboard"
        assert parsed["mileage"] is None
        # Only 2 lines → no location candidate
        assert parsed["location"] is None

    def test_k_miles_variant(self) -> None:
        parsed = fb._parse_listing_text("$22,000\n2018 Subaru Outback\n45K miles\nBellevue, WA")
        assert parsed["mileage"] is not None
        assert "45" in parsed["mileage"]

    def test_empty_text(self) -> None:
        parsed = fb._parse_listing_text("")
        assert parsed == {
            "title": None,
            "price": None,
            "year": None,
            "mileage": None,
            "location": None,
        }

    def test_whitespace_only_lines_ignored(self) -> None:
        parsed = fb._parse_listing_text("\n   \n$1,200\n\n  \n2012 Honda Fit\n")
        assert parsed["price"] == "$1200"
        assert parsed["title"] == "2012 Honda Fit"

    def test_location_with_city_state(self) -> None:
        parsed = fb._parse_listing_text("$100\nCoffee table\nWood, solid\nTacoma, WA")
        assert parsed["location"] == "Tacoma, WA"

    def test_year_picked_from_title_not_mileage(self) -> None:
        # 1999 in title should win over any 4-digit sequences later.
        parsed = fb._parse_listing_text("$8,500\n1999 BMW M3\n125,000 miles\nSeattle, WA")
        assert parsed["year"] == 1999


# ---------------------------------------------------------------------------
# _SeenStore
# ---------------------------------------------------------------------------


class TestSeenStore:
    def test_basic_mark_and_filter(self, tmp_path) -> None:
        path = str(tmp_path / "seen.json")
        store = fb._SeenStore(path, ttl_days=0)
        assert store.filter_new("cars", ["1", "2", "3"]) == ["1", "2", "3"]
        store.mark_seen("cars", ["1", "2"])
        assert store.filter_new("cars", ["1", "2", "3"]) == ["3"]

    def test_label_isolation(self, tmp_path) -> None:
        path = str(tmp_path / "seen.json")
        store = fb._SeenStore(path, ttl_days=0)
        store.mark_seen("cars", ["a", "b"])
        store.mark_seen("bikes", ["a"])
        # 'a' seen in cars shouldn't hide it from bikes
        assert store.filter_new("cars", ["a", "b", "c"]) == ["c"]
        assert store.filter_new("bikes", ["a", "b"]) == ["b"]

    def test_persistence_across_instances(self, tmp_path) -> None:
        path = str(tmp_path / "seen.json")
        s1 = fb._SeenStore(path, ttl_days=0)
        s1.mark_seen("cars", ["1", "2"])
        s2 = fb._SeenStore(path, ttl_days=0)
        assert s2.filter_new("cars", ["1", "2", "3"]) == ["3"]

    def test_forget_clears_label(self, tmp_path) -> None:
        path = str(tmp_path / "seen.json")
        store = fb._SeenStore(path, ttl_days=0)
        store.mark_seen("cars", ["1", "2"])
        count = store.forget("cars")
        assert count == 2
        assert store.filter_new("cars", ["1", "2"]) == ["1", "2"]
        # Forgetting a missing label returns 0, not an error.
        assert store.forget("cars") == 0

    def test_legacy_schema_migration(self, tmp_path) -> None:
        """Old format was {id: ts}; new format is {label: {id: ts}}."""
        path = str(tmp_path / "seen.json")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(path, "w") as f:
            json.dump({"abc123": now, "xyz789": now}, f)
        store = fb._SeenStore(path, ttl_days=0)
        # Legacy IDs should land under the 'default' label.
        assert store.filter_new("default", ["abc123", "xyz789", "new"]) == ["new"]

    def test_ttl_prunes_old_entries(self, tmp_path) -> None:
        path = str(tmp_path / "seen.json")
        # Pre-seed file with timestamps 100 days old and 1 day old.
        old_ts = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=100)
        ).isoformat()
        new_ts = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        ).isoformat()
        with open(path, "w") as f:
            json.dump(
                {"cars": {"old_id": old_ts, "new_id": new_ts}},
                f,
            )
        # TTL of 60 days should drop old_id but keep new_id.
        store = fb._SeenStore(path, ttl_days=60)
        assert store.filter_new("cars", ["old_id", "new_id"]) == ["old_id"]

    def test_ttl_zero_disables_pruning(self, tmp_path) -> None:
        path = str(tmp_path / "seen.json")
        old_ts = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1000)
        ).isoformat()
        with open(path, "w") as f:
            json.dump({"cars": {"ancient": old_ts}}, f)
        store = fb._SeenStore(path, ttl_days=0)
        # With TTL disabled, ancient entry remains and hides the ID.
        assert store.filter_new("cars", ["ancient"]) == []

    def test_prune_drops_empty_label_bucket(self, tmp_path) -> None:
        path = str(tmp_path / "seen.json")
        old_ts = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365)
        ).isoformat()
        with open(path, "w") as f:
            json.dump({"cars": {"a": old_ts, "b": old_ts}}, f)
        store = fb._SeenStore(path, ttl_days=30)
        # After prune, the label bucket should be gone entirely.
        assert "cars" not in store._data

    def test_prune_returns_removed_count(self, tmp_path) -> None:
        path = str(tmp_path / "seen.json")
        old = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=100)
        ).isoformat()
        recent = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        ).isoformat()
        with open(path, "w") as f:
            json.dump({"cars": {"old": old, "new": recent}}, f)
        store = fb._SeenStore(path, ttl_days=0)  # no auto-prune
        removed = store.prune_older_than(60)
        assert removed == 1

    def test_corrupt_file_recovers(self, tmp_path) -> None:
        path = str(tmp_path / "seen.json")
        with open(path, "w") as f:
            f.write("not json {{{{")
        # Should not raise — logs warning and starts empty.
        store = fb._SeenStore(path, ttl_days=0)
        assert store.filter_new("cars", ["x"]) == ["x"]


# ---------------------------------------------------------------------------
# _build_search_url
# ---------------------------------------------------------------------------


class TestBuildSearchUrl:
    def test_basic_url(self) -> None:
        url = fb._build_search_url(
            query="porsche 911",
            location="seattle",
            min_price=None,
            max_price=None,
            radius_miles=50,
            days_listed=None,
            sort_by="newest",
        )
        assert url.startswith("https://www.facebook.com/marketplace/seattle/search/?")
        assert "query=porsche%20911" in url
        assert "radius=50" in url
        assert "sortBy=creation_time_descend" in url

    def test_price_bounds(self) -> None:
        url = fb._build_search_url(
            query="car",
            location="portland",
            min_price=5000,
            max_price=25000,
            radius_miles=100,
            days_listed=7,
            sort_by="price_asc",
        )
        assert "minPrice=5000" in url
        assert "maxPrice=25000" in url
        assert "daysSinceListed=7" in url
        assert "sortBy=price_ascend" in url

    def test_unknown_sort_falls_back_to_newest(self) -> None:
        url = fb._build_search_url(
            query="x",
            location="seattle",
            min_price=None,
            max_price=None,
            radius_miles=50,
            days_listed=None,
            sort_by="bogus_mode",
        )
        assert "sortBy=creation_time_descend" in url

    def test_query_special_chars_are_escaped(self) -> None:
        url = fb._build_search_url(
            query="porsche 996 & 997",
            location="seattle",
            min_price=None,
            max_price=None,
            radius_miles=50,
            days_listed=None,
            sort_by="newest",
        )
        # Space and '&' must be encoded so they don't break the query string.
        # Isolate only the query=... value (stops at the first unescaped '&').
        query_part = url.split("query=", 1)[1].split("&", 1)[0]
        assert "porsche%20996" in query_part
        assert "%26" in query_part  # '&' encoded inside the value
        # The raw '&' must not leak into the query value (would create a
        # spurious param).
        assert "&" not in query_part


# ---------------------------------------------------------------------------
# Browser pool
# ---------------------------------------------------------------------------


class TestBrowserPool:
    def setup_method(self) -> None:
        # Reset module-level singletons between tests.
        fb._playwright_instance = None
        fb._browser = None
        fb._browser_lock = None

    def teardown_method(self) -> None:
        fb._playwright_instance = None
        fb._browser = None
        fb._browser_lock = None

    def test_browser_is_cached_across_calls(self) -> None:
        """Second call to _get_browser should reuse the first browser."""
        connected_browser = MagicMock()
        connected_browser.is_connected = MagicMock(return_value=True)

        mock_chromium = MagicMock()
        mock_chromium.launch = AsyncMock(return_value=connected_browser)

        mock_instance = MagicMock()
        mock_instance.chromium = mock_chromium
        mock_instance.stop = AsyncMock()

        mock_async_playwright = MagicMock()
        mock_async_playwright.return_value.start = AsyncMock(return_value=mock_instance)

        fake_pw_module = MagicMock()
        fake_pw_module.async_playwright = mock_async_playwright

        with patch.dict(sys.modules, {"playwright.async_api": fake_pw_module}):
            async def run() -> None:
                b1 = await fb._get_browser()
                b2 = await fb._get_browser()
                assert b1 is b2
                # Chromium should only be launched once.
                assert mock_chromium.launch.await_count == 1

            asyncio.run(run())

    def test_disconnected_browser_is_relaunched(self) -> None:
        first_browser = MagicMock()
        first_browser.is_connected = MagicMock(return_value=False)
        first_browser.close = AsyncMock()

        second_browser = MagicMock()
        second_browser.is_connected = MagicMock(return_value=True)

        mock_chromium = MagicMock()
        mock_chromium.launch = AsyncMock(side_effect=[first_browser, second_browser])

        mock_instance = MagicMock()
        mock_instance.chromium = mock_chromium

        mock_async_playwright = MagicMock()
        mock_async_playwright.return_value.start = AsyncMock(return_value=mock_instance)

        fake_pw_module = MagicMock()
        fake_pw_module.async_playwright = mock_async_playwright

        with patch.dict(sys.modules, {"playwright.async_api": fake_pw_module}):
            async def run() -> None:
                b1 = await fb._get_browser()
                assert b1 is first_browser
                b2 = await fb._get_browser()
                assert b2 is second_browser
                # Stale browser should have been closed.
                first_browser.close.assert_awaited_once()

            asyncio.run(run())

    def test_missing_playwright_raises_scrape_error(self) -> None:
        """If playwright is not importable, _get_browser must raise _ScrapeError."""
        # Sabotage the playwright import.
        with patch.dict(sys.modules, {"playwright.async_api": None}):
            async def run() -> None:
                try:
                    await fb._get_browser()
                except fb._ScrapeError as exc:
                    assert "playwright is not installed" in str(exc)
                else:
                    raise AssertionError("expected _ScrapeError")

            asyncio.run(run())


# ---------------------------------------------------------------------------
# Helper / config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_ttl_days_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("FB_SEEN_TTL_DAYS", "90")
        assert fb._seen_ttl_days() == 90

    def test_ttl_days_invalid_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("FB_SEEN_TTL_DAYS", "not-a-number")
        assert fb._seen_ttl_days() == fb._DEFAULT_SEEN_TTL_DAYS

    def test_ttl_days_negative_clamped_to_zero(self, monkeypatch) -> None:
        monkeypatch.setenv("FB_SEEN_TTL_DAYS", "-5")
        assert fb._seen_ttl_days() == 0

    def test_ttl_days_unset_uses_default(self, monkeypatch) -> None:
        monkeypatch.delenv("FB_SEEN_TTL_DAYS", raising=False)
        assert fb._seen_ttl_days() == fb._DEFAULT_SEEN_TTL_DAYS

    def test_cookies_path_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("FB_COOKIES_FILE", "/custom/path.json")
        assert fb._cookies_path() == "/custom/path.json"

    def test_location_default_lowercased(self, monkeypatch) -> None:
        monkeypatch.setenv("FB_DEFAULT_LOCATION", "PORTLAND")
        assert fb._location_default() == "portland"

"""Facebook Marketplace MCP server tools.

Scrapes Facebook Marketplace using Playwright with a persistent logged-in
session. Session state is loaded from a Playwright ``storage_state`` JSON
file (see ``scripts/fb-cookies-export.py`` for how to generate one from
your own Chrome profile).

This is a best-effort scraper. Facebook ships anti-bot changes that may
break extraction; when that happens, tools return a helpful error and the
caller (Claude) can report honestly rather than fabricate data.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)


_DEFAULT_COOKIES_PATH = "data/fb_cookies.json"
_DEFAULT_SEEN_PATH = "data/fb_marketplace_seen.json"
_DEFAULT_LOCATION = "seattle"
_DEFAULT_RADIUS_MILES = 100
_DEFAULT_LIMIT = 25
_MAX_LIMIT = 60

_SORT_MODES = {
    "newest": "creation_time_descend",
    "distance": "distance_ascend",
    "price_asc": "price_ascend",
    "price_desc": "price_descend",
    "best_match": "best_match",
}

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _cookies_path() -> str:
    return os.environ.get("FB_COOKIES_FILE", _DEFAULT_COOKIES_PATH)


def _seen_path() -> str:
    return os.environ.get("FB_SEEN_FILE", _DEFAULT_SEEN_PATH)


def _location_default() -> str:
    return os.environ.get("FB_DEFAULT_LOCATION", _DEFAULT_LOCATION).lower()


# ---------------------------------------------------------------------------
# Seen-IDs store (for deduping alerts across scheduled runs)
# ---------------------------------------------------------------------------


class _SeenStore:
    """JSON-backed set of listing IDs we've already surfaced to the user.

    Keyed by watch label (e.g. ``porsche_911``) so multiple scheduled tasks
    can each track their own dedup set.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                # Upgrade legacy {id: ts} schema to {label: {id: ts}}
                if raw and not isinstance(next(iter(raw.values())), dict):
                    raw = {"default": raw}
                self._data = raw
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load FB seen store at %s: %s", self._path, exc)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def filter_new(self, label: str, ids: list[str]) -> list[str]:
        known = self._data.get(label, {})
        return [i for i in ids if i not in known]

    def mark_seen(self, label: str, ids: list[str]) -> int:
        bucket = self._data.setdefault(label, {})
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        added = 0
        for listing_id in ids:
            if listing_id not in bucket:
                bucket[listing_id] = now
                added += 1
        if added:
            self._save()
        return added

    def forget(self, label: str) -> int:
        if label in self._data:
            count = len(self._data[label])
            del self._data[label]
            self._save()
            return count
        return 0


_seen: _SeenStore | None = None


def _get_seen_store() -> _SeenStore:
    global _seen
    if _seen is None:
        _seen = _SeenStore(_seen_path())
    return _seen


# ---------------------------------------------------------------------------
# URL and parsing helpers
# ---------------------------------------------------------------------------


def _build_search_url(
    *,
    query: str,
    location: str,
    min_price: float | None,
    max_price: float | None,
    radius_miles: int,
    days_listed: int | None,
    sort_by: str,
) -> str:
    from urllib.parse import quote

    params: list[str] = [f"query={quote(query)}"]
    # FB uses radius in km when the locale is metric, miles otherwise; the
    # "radius" query param accepts either — the UI converts. Passing miles is
    # fine for US locations.
    params.append(f"radius={radius_miles}")
    if min_price is not None:
        params.append(f"minPrice={int(min_price)}")
    if max_price is not None:
        params.append(f"maxPrice={int(max_price)}")
    if days_listed is not None:
        params.append(f"daysSinceListed={int(days_listed)}")
    sort_value = _SORT_MODES.get(sort_by, _SORT_MODES["newest"])
    params.append(f"sortBy={sort_value}")
    # Hide out-of-stock / pending-pickup style noise
    params.append("exact=false")
    qs = "&".join(params)
    return f"https://www.facebook.com/marketplace/{quote(location)}/search/?{qs}"


_PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")
_MILEAGE_RE = re.compile(r"([\d,]+)\s*(?:mi|miles|K miles|k miles)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20\d{2})\b")
_LISTING_ID_RE = re.compile(r"/marketplace/item/(\d+)")


def _parse_listing_text(text: str) -> dict[str, Any]:
    """Parse the visible text of a marketplace card into structured fields."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    price = None
    title = None
    location = None
    mileage = None
    year = None

    for line in lines:
        if price is None and _PRICE_RE.match(line):
            price = line.split()[0].replace(",", "")
        elif title is None and not _PRICE_RE.match(line):
            title = line

    if title:
        m = _YEAR_RE.search(title)
        if m:
            year = int(m.group(1))

    joined = "\n".join(lines)
    m = _MILEAGE_RE.search(joined)
    if m:
        mileage = m.group(0)

    # Best-effort: last line often looks like "Seattle, WA"
    if len(lines) >= 3:
        candidate = lines[-1]
        if "," in candidate and len(candidate) < 60:
            location = candidate

    return {
        "title": title,
        "price": price,
        "year": year,
        "mileage": mileage,
        "location": location,
    }


def _format_listings(listings: list[dict[str, Any]], query: str, location: str) -> str:
    if not listings:
        return f"No Facebook Marketplace results for '{query}' in {location}."

    lines = [f"Found {len(listings)} listing(s) for '{query}' in {location}:"]
    for listing in listings:
        parts: list[str] = []
        price = listing.get("price")
        if price:
            parts.append(str(price))
        title = listing.get("title") or "(no title)"
        parts.append(title)
        if listing.get("mileage"):
            parts.append(str(listing["mileage"]))
        loc = listing.get("location")
        if loc:
            parts.append(loc)
        url = listing.get("url") or ""
        line = " — ".join(parts)
        if url:
            line = f"  • {line}\n    {url}"
        else:
            line = f"  • {line}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Playwright driver
# ---------------------------------------------------------------------------


class _ScrapeError(Exception):
    pass


async def _scrape_search(
    *,
    query: str,
    location: str,
    min_price: float | None,
    max_price: float | None,
    radius_miles: int,
    days_listed: int | None,
    sort_by: str,
    limit: int,
) -> list[dict[str, Any]]:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError as exc:
        raise _ScrapeError(
            "playwright is not installed. Install with `uv pip install playwright` "
            "and `playwright install chromium`."
        ) from exc

    cookies_path = _cookies_path()
    storage_state: Any
    if os.path.exists(cookies_path):
        storage_state = cookies_path
    else:
        storage_state = None
        logger.warning(
            "No FB cookies file at %s — scraping anonymously. Results will be limited.",
            cookies_path,
        )

    url = _build_search_url(
        query=query,
        location=location,
        min_price=min_price,
        max_price=max_price,
        radius_miles=radius_miles,
        days_listed=days_listed,
        sort_by=sort_by,
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                storage_state=storage_state,
                user_agent=_USER_AGENT,
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                raise _ScrapeError(f"Could not load Marketplace search page: {exc}") from exc

            # Let JS render and trigger the first GraphQL batch
            await page.wait_for_timeout(3000)

            # Detect login gate
            current = page.url
            if "/login" in current or "checkpoint" in current:
                raise _ScrapeError(
                    "Facebook redirected to login/checkpoint. Cookies file at "
                    f"{cookies_path} is missing or expired. Re-export from Chrome."
                )

            # Scroll to trigger lazy-load of more cards, then snapshot the DOM.
            for _ in range(3):
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(1200)

            raw = await page.evaluate(
                """() => {
                    const anchors = document.querySelectorAll('a[href*="/marketplace/item/"]');
                    const seen = new Set();
                    const out = [];
                    anchors.forEach((a) => {
                        const href = a.getAttribute('href') || '';
                        const match = href.match(/\\/marketplace\\/item\\/(\\d+)/);
                        if (!match) return;
                        const id = match[1];
                        if (seen.has(id)) return;
                        seen.add(id);
                        // climb up to the card container for richer text
                        let node = a;
                        for (let i = 0; i < 6 && node.parentElement; i++) {
                            node = node.parentElement;
                        }
                        const text = (node.innerText || a.innerText || '').trim();
                        const img = a.querySelector('img');
                        const image = img ? img.getAttribute('src') : null;
                        out.push({ id, href, text, image });
                    });
                    return out;
                }"""
            )
        finally:
            await browser.close()

    results: list[dict[str, Any]] = []
    for item in raw[:limit]:
        listing_id = item["id"]
        href = item["href"]
        if href.startswith("/"):
            href = "https://www.facebook.com" + href
        # Strip tracking params
        href = href.split("?")[0]
        parsed = _parse_listing_text(item.get("text") or "")
        parsed.update({
            "id": listing_id,
            "url": href,
            "image": item.get("image"),
        })
        results.append(parsed)
    return results


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    "fb_marketplace_search",
    "Search Facebook Marketplace for listings. Requires a Facebook session "
    "cookie file (data/fb_cookies.json). Returns title, price, year, mileage, "
    "location, and listing URL.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (e.g., 'porsche 996 c4s')."},
            "location": {
                "type": "string",
                "description": "City slug for the search (e.g., 'seattle', 'portland', 'sanfrancisco'). Defaults to FB_DEFAULT_LOCATION or 'seattle'.",
            },
            "min_price": {"type": "number", "description": "Minimum price in USD."},
            "max_price": {"type": "number", "description": "Maximum price in USD."},
            "radius_miles": {
                "type": "integer",
                "description": f"Search radius in miles. Default {_DEFAULT_RADIUS_MILES}.",
            },
            "days_listed": {
                "type": "integer",
                "description": "Only show listings posted within this many days (e.g. 1, 7, 30).",
            },
            "sort_by": {
                "type": "string",
                "enum": list(_SORT_MODES.keys()),
                "description": "Sort mode. Default 'newest'.",
            },
            "limit": {
                "type": "integer",
                "description": f"Maximum results to return (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT}).",
            },
            "dedup_label": {
                "type": "string",
                "description": "If set, only return listings not previously seen under this label (persists in data/fb_marketplace_seen.json). Useful for scheduled hunter tasks.",
            },
            "mark_seen": {
                "type": "boolean",
                "description": "If true and dedup_label is set, mark the returned listings as seen. Default true.",
            },
        },
        "required": ["query"],
    },
)
async def fb_marketplace_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return _error("query is required")

    location = (args.get("location") or _location_default()).lower().replace(" ", "")
    radius_miles = int(args.get("radius_miles") or _DEFAULT_RADIUS_MILES)
    sort_by = args.get("sort_by") or "newest"
    limit = min(int(args.get("limit") or _DEFAULT_LIMIT), _MAX_LIMIT)
    dedup_label = args.get("dedup_label")
    mark_seen = bool(args.get("mark_seen", True))

    try:
        listings = await asyncio.wait_for(
            _scrape_search(
                query=query,
                location=location,
                min_price=args.get("min_price"),
                max_price=args.get("max_price"),
                radius_miles=radius_miles,
                days_listed=args.get("days_listed"),
                sort_by=sort_by,
                limit=limit,
            ),
            timeout=90,
        )
    except asyncio.TimeoutError:
        return _error("Facebook Marketplace search timed out after 90s.")
    except _ScrapeError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("FB Marketplace scrape failed")
        return _error(f"Facebook Marketplace search failed: {exc}")

    if dedup_label:
        store = _get_seen_store()
        ids = [listing["id"] for listing in listings]
        new_ids = set(store.filter_new(dedup_label, ids))
        listings = [listing for listing in listings if listing["id"] in new_ids]
        if mark_seen and listings:
            store.mark_seen(dedup_label, [listing["id"] for listing in listings])

    return _text(_format_listings(listings, query=query, location=location))


@tool(
    "fb_marketplace_listing_details",
    "Fetch full details for a single Facebook Marketplace listing (title, price, description, seller location, posting age).",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full Facebook Marketplace item URL."},
        },
        "required": ["url"],
    },
)
async def fb_marketplace_listing_details(args: dict[str, Any]) -> dict[str, Any]:
    url = (args.get("url") or "").strip()
    if not url or "/marketplace/item/" not in url:
        return _error("A Facebook Marketplace item URL is required.")

    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        return _error("playwright not installed on this server.")

    cookies_path = _cookies_path()
    storage_state: Any = cookies_path if os.path.exists(cookies_path) else None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    storage_state=storage_state,
                    user_agent=_USER_AGENT,
                    viewport={"width": 1366, "height": 900},
                    locale="en-US",
                )
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
                current = page.url
                if "/login" in current or "checkpoint" in current:
                    return _error(
                        "Facebook redirected to login. Cookies at "
                        f"{cookies_path} missing or expired."
                    )
                text = await page.evaluate(
                    "() => (document.querySelector('main') || document.body).innerText"
                )
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("FB listing details scrape failed")
        return _error(f"Failed to fetch listing details: {exc}")

    # Trim extreme length — the main column contains listing + below-the-fold
    # "more from this seller" cards we don't need.
    snippet = (text or "").strip()
    if len(snippet) > 4000:
        snippet = snippet[:4000] + "\n…(truncated)"
    if not snippet:
        return _error("No listing text found (page may have failed to render).")
    return _text(snippet)


@tool(
    "fb_marketplace_forget_seen",
    "Clear the seen-listings dedup cache for a label (so the next search returns all matches again).",
    {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Dedup label to clear (e.g. 'porsche_911')."},
        },
        "required": ["label"],
    },
)
async def fb_marketplace_forget_seen(args: dict[str, Any]) -> dict[str, Any]:
    label = (args.get("label") or "").strip()
    if not label:
        return _error("label is required")
    count = _get_seen_store().forget(label)
    return _text(f"Cleared {count} seen listing(s) for label '{label}'.")


FB_MARKETPLACE_TOOLS: list[SdkMcpTool] = [
    fb_marketplace_search,
    fb_marketplace_listing_details,
    fb_marketplace_forget_seen,
]

"""Stocks MCP server tools for market data via yfinance."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, timedelta
from typing import Any

import httpx
import yfinance as yf

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


async def _run_sync(fn: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(None, fn)


def _clean(v: Any) -> Any:
    """Return None for NaN values (NaN != NaN), pass through everything else."""
    return v if v == v else None


# ---------------------------------------------------------------------------
# 1. stock_quote
# ---------------------------------------------------------------------------
@tool(
    "stock_quote",
    "Get a current price snapshot for a stock symbol. Returns price, change, change_pct, volume, 52w_high, 52w_low, pe_ratio, market_cap, and name.",
    {"symbol": str},
)
async def stock_quote(args: dict[str, Any]) -> dict[str, Any]:
    try:
        symbol = args["symbol"].upper()

        def _fetch() -> dict[str, Any]:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            price = _clean(info.get("currentPrice")) or _clean(info.get("regularMarketPrice"))
            prev_close = _clean(info.get("previousClose")) or _clean(info.get("regularMarketPreviousClose"))
            change = round(price - prev_close, 4) if price and prev_close else None
            change_pct = round((change / prev_close) * 100, 2) if change and prev_close else None
            return {
                "symbol": symbol,
                "name": info.get("shortName") or info.get("longName"),
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "volume": _clean(info.get("volume")) or _clean(info.get("regularMarketVolume")),
                "52w_high": _clean(info.get("fiftyTwoWeekHigh")),
                "52w_low": _clean(info.get("fiftyTwoWeekLow")),
                "pe_ratio": _clean(info.get("trailingPE")),
                "market_cap": _clean(info.get("marketCap")),
            }

        result = await _run_sync(_fetch)
        return _text(json.dumps(result, indent=2))
    except Exception as e:
        return _error(f"Failed to get quote for {args.get('symbol', '?')}: {e}")


# ---------------------------------------------------------------------------
# 2. options_expirations
# ---------------------------------------------------------------------------
@tool(
    "options_expirations",
    "List available option expiration dates for a stock symbol.",
    {"symbol": str},
)
async def options_expirations(args: dict[str, Any]) -> dict[str, Any]:
    try:
        symbol = args["symbol"].upper()

        def _fetch() -> list[str]:
            ticker = yf.Ticker(symbol)
            return list(ticker.options)

        expirations = await _run_sync(_fetch)
        if not expirations:
            return _error(f"No options available for {symbol}")
        return _text(json.dumps({"symbol": symbol, "expirations": expirations}, indent=2))
    except Exception as e:
        return _error(f"Failed to get options expirations for {args.get('symbol', '?')}: {e}")


# ---------------------------------------------------------------------------
# 3. options_chain
# ---------------------------------------------------------------------------
@tool(
    "options_chain",
    "Get the options chain for a stock at a specific expiration. Returns calls and/or puts filtered to strikes within a percentage range of the current price.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Stock ticker symbol"},
            "expiration": {"type": "string", "description": "Expiration date (from options_expirations)"},
            "option_type": {
                "type": "string",
                "enum": ["calls", "puts", "both"],
                "description": "Type of options to return (default: both)",
            },
            "strike_range_pct": {
                "type": "number",
                "description": "Filter strikes to within this % of current price (default: 10)",
            },
        },
        "required": ["symbol", "expiration"],
    },
)
async def options_chain(args: dict[str, Any]) -> dict[str, Any]:
    try:
        symbol = args["symbol"].upper()
        expiration = args["expiration"]
        option_type = args.get("option_type", "both")
        strike_range_pct = args.get("strike_range_pct", 10)

        def _fetch() -> dict[str, Any]:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            current_price = _clean(info.get("currentPrice")) or _clean(info.get("regularMarketPrice"))
            chain = ticker.option_chain(expiration)

            low_strike = current_price * (1 - strike_range_pct / 100) if current_price else 0
            high_strike = current_price * (1 + strike_range_pct / 100) if current_price else float("inf")

            fields = ["strike", "bid", "ask", "lastPrice", "volume", "openInterest", "impliedVolatility", "inTheMoney"]

            def _process_df(df: Any) -> list[dict[str, Any]]:
                filtered = df[(df["strike"] >= low_strike) & (df["strike"] <= high_strike)]
                rows = []
                for _, row in filtered.iterrows():
                    rows.append({f: _clean(row.get(f)) for f in fields})
                return rows

            result: dict[str, Any] = {
                "symbol": symbol,
                "expiration": expiration,
                "current_price": current_price,
            }
            if option_type in ("calls", "both"):
                result["calls"] = _process_df(chain.calls)
            if option_type in ("puts", "both"):
                result["puts"] = _process_df(chain.puts)
            return result

        result = await _run_sync(_fetch)
        return _text(json.dumps(result, indent=2))
    except Exception as e:
        return _error(f"Failed to get options chain for {args.get('symbol', '?')}: {e}")


# ---------------------------------------------------------------------------
# 4. stock_technicals
# ---------------------------------------------------------------------------
@tool(
    "stock_technicals",
    "Get pre-computed technical indicators for a stock from 1 year of daily data. Includes SMA(20/50/200), RSI(14), MACD(12,26,9), Bollinger Bands(20,2), average volume, and price-vs-SMA flags.",
    {"symbol": str},
)
async def stock_technicals(args: dict[str, Any]) -> dict[str, Any]:
    try:
        symbol = args["symbol"].upper()

        def _compute() -> dict[str, Any]:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1y")
            if hist.empty:
                raise ValueError(f"No historical data for {symbol}")

            close = hist["Close"]
            volume = hist["Volume"]
            last_close = float(close.iloc[-1])

            # SMAs
            sma_20 = float(close.rolling(20).mean().iloc[-1])
            sma_50 = float(close.rolling(50).mean().iloc[-1])
            sma_200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

            # RSI(14)
            delta = close.diff()
            gain = delta.where(delta > 0, 0.0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
            rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] != 0 else 0
            rsi = 100 - (100 / (1 + rs))

            # MACD(12, 26, 9)
            ema_12 = close.ewm(span=12, adjust=False).mean()
            ema_26 = close.ewm(span=26, adjust=False).mean()
            macd_line = ema_12 - ema_26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            macd_hist = macd_line - signal_line

            # Bollinger Bands(20, 2)
            bb_mid = close.rolling(20).mean()
            bb_std = close.rolling(20).std()
            bb_upper = bb_mid + 2 * bb_std
            bb_lower = bb_mid - 2 * bb_std

            # Avg volume
            avg_volume = float(volume.mean())

            return {
                "symbol": symbol,
                "last_close": round(last_close, 4),
                "sma_20": round(sma_20, 4),
                "sma_50": round(sma_50, 4),
                "sma_200": round(sma_200, 4) if sma_200 is not None else None,
                "rsi_14": round(float(rsi), 2),
                "macd": {
                    "macd_line": round(float(macd_line.iloc[-1]), 4),
                    "signal_line": round(float(signal_line.iloc[-1]), 4),
                    "histogram": round(float(macd_hist.iloc[-1]), 4),
                },
                "bollinger_bands": {
                    "upper": round(float(bb_upper.iloc[-1]), 4),
                    "middle": round(float(bb_mid.iloc[-1]), 4),
                    "lower": round(float(bb_lower.iloc[-1]), 4),
                },
                "avg_volume": round(avg_volume),
                "price_vs_sma": {
                    "above_sma_20": last_close > sma_20,
                    "above_sma_50": last_close > sma_50,
                    "above_sma_200": last_close > sma_200 if sma_200 is not None else None,
                },
            }

        result = await _run_sync(_compute)
        return _text(json.dumps(result, indent=2))
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Failed to compute technicals for {args.get('symbol', '?')}: {e}")


# ---------------------------------------------------------------------------
# 5. short_interest — merged yfinance + Finnhub short interest data
# ---------------------------------------------------------------------------
async def _fetch_finnhub_metrics(symbol: str) -> dict[str, Any] | None:
    """Fetch Finnhub /stock/metric for cross-reference short data. Returns None if no key or error."""
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        return None
    url = "https://finnhub.io/api/v1/stock/metric"
    params = {"symbol": symbol, "metric": "all", "token": key}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            return data.get("metric") or {}
    except Exception as e:
        logger.warning("Finnhub metric fetch failed for %s: %s", symbol, e)
        return None


@tool(
    "short_interest",
    "Get short interest data for a stock, merging yfinance (primary) and Finnhub (cross-reference). Returns shares_short, short_pct_of_float, days_to_cover, float_shares, shares_short_prior_month, si_change_pct (MoM), and source attribution. Use for short squeeze research.",
    {"symbol": str},
)
async def short_interest(args: dict[str, Any]) -> dict[str, Any]:
    try:
        symbol = args["symbol"].upper()

        def _fetch_yf() -> dict[str, Any]:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            shares_short = _clean(info.get("sharesShort"))
            shares_short_prior = _clean(info.get("sharesShortPriorMonth"))
            si_change_pct = None
            if shares_short and shares_short_prior:
                si_change_pct = round(((shares_short - shares_short_prior) / shares_short_prior) * 100, 2)
            return {
                "shares_short": shares_short,
                "shares_short_prior_month": shares_short_prior,
                "si_change_pct": si_change_pct,
                "short_pct_of_float": _clean(info.get("shortPercentOfFloat")),
                "days_to_cover": _clean(info.get("shortRatio")),
                "float_shares": _clean(info.get("floatShares")),
                "shares_outstanding": _clean(info.get("sharesOutstanding")),
                "short_interest_date": info.get("dateShortInterest"),
            }

        yf_data = await _run_sync(_fetch_yf)
        finnhub = await _fetch_finnhub_metrics(symbol)

        result: dict[str, Any] = {"symbol": symbol, **yf_data, "sources": ["yfinance"]}
        if finnhub:
            result["finnhub"] = {
                "short_ratio": _clean(finnhub.get("shortRatioAnnual")) or _clean(finnhub.get("shortRatio")),
                "short_interest_shares": _clean(finnhub.get("shortInterestSharesOutstanding")),
            }
            result["sources"].append("finnhub")

        # Normalize short_pct_of_float to a percentage (yfinance returns fraction like 0.25 = 25%)
        if result.get("short_pct_of_float") is not None and result["short_pct_of_float"] < 1.5:
            result["short_pct_of_float"] = round(result["short_pct_of_float"] * 100, 2)

        return _text(json.dumps(result, indent=2))
    except Exception as e:
        return _error(f"Failed to get short interest for {args.get('symbol', '?')}: {e}")


# ---------------------------------------------------------------------------
# 6. short_volume — FINRA daily short sale volume (reg SHO)
# ---------------------------------------------------------------------------
async def _fetch_finra_short_volume(target_date: date) -> dict[str, dict[str, int]] | None:
    """Download and parse FINRA reg SHO daily short volume file. Returns {symbol: {short, total, ratio}}."""
    url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{target_date.strftime('%Y%m%d')}.txt"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            text = r.text
    except Exception as e:
        logger.warning("FINRA fetch failed for %s: %s", target_date, e)
        return None

    out: dict[str, dict[str, int]] = {}
    lines = text.splitlines()
    for line in lines[1:]:  # skip header
        parts = line.split("|")
        if len(parts) < 5:
            continue
        sym = parts[1].strip().upper()
        try:
            short_vol = int(parts[2])
            total_vol = int(parts[4])
        except ValueError:
            continue
        if not sym or total_vol == 0:
            continue
        out[sym] = {"short_volume": short_vol, "total_volume": total_vol}
    return out


async def _compute_short_volume(symbol: str, days: int) -> dict[str, Any] | None:
    """Pull FINRA reg SHO daily short volume for a symbol across the last N trading days.

    Returns None if nothing was found, else {symbol, days_returned, avg_short_ratio_pct, daily}.
    """
    results: list[dict[str, Any]] = []
    d = date.today() - timedelta(days=1)
    attempts = 0
    while len(results) < days and attempts < days * 3:
        attempts += 1
        if d.weekday() >= 5:  # skip weekends
            d -= timedelta(days=1)
            continue
        data = await _fetch_finra_short_volume(d)
        if data is None:
            # File not published yet (holiday or not-yet-released); step back
            d -= timedelta(days=1)
            continue
        row = data.get(symbol)
        if row:
            ratio = round(row["short_volume"] / row["total_volume"] * 100, 2) if row["total_volume"] else None
            results.append({
                "date": d.isoformat(),
                "short_volume": row["short_volume"],
                "total_volume": row["total_volume"],
                "short_ratio_pct": ratio,
            })
        d -= timedelta(days=1)

    if not results:
        return None

    ratios = [r["short_ratio_pct"] for r in results if r["short_ratio_pct"] is not None]
    avg_ratio = round(sum(ratios) / len(ratios), 2) if ratios else None
    return {
        "symbol": symbol,
        "days_returned": len(results),
        "avg_short_ratio_pct": avg_ratio,
        "daily": results,
    }


@tool(
    "short_volume",
    "Get daily short sale volume from FINRA reg SHO for the last N trading days. Returns daily short_volume / total_volume / ratio. High short-volume ratio (>50%) combined with rising price is a squeeze signal. Free, no API key required.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Stock ticker symbol"},
            "days": {"type": "integer", "description": "Number of trading days to fetch (default: 5, max: 10)"},
        },
        "required": ["symbol"],
    },
)
async def short_volume(args: dict[str, Any]) -> dict[str, Any]:
    try:
        symbol = args["symbol"].upper()
        days = min(int(args.get("days", 5)), 10)
        result = await _compute_short_volume(symbol, days)
        if not result:
            return _error(f"No FINRA short volume data found for {symbol}")
        return _text(json.dumps(result, indent=2))
    except Exception as e:
        return _error(f"Failed to get short volume for {args.get('symbol', '?')}: {e}")


# ---------------------------------------------------------------------------
# 7. squeeze_score — composite short squeeze signal
# ---------------------------------------------------------------------------
def _score_short_interest(si_pct: float | None) -> tuple[float, str]:
    if si_pct is None:
        return 0.0, "no SI data"
    # 40 pts at 40%+, linear scale below
    pts = min(si_pct, 40.0)
    return pts, f"SI={si_pct:.1f}%"


def _score_days_to_cover(dtc: float | None) -> tuple[float, str]:
    if dtc is None:
        return 0.0, "no DTC"
    # 20 pts at 10+ days, linear below
    pts = min(dtc * 2.0, 20.0)
    return pts, f"DTC={dtc:.1f}d"


def _score_si_trend(si_change_pct: float | None) -> tuple[float, str]:
    if si_change_pct is None:
        return 0.0, "no SI trend"
    # 10 pts at +50% MoM growth, 0 at flat, negative means shorts covering
    if si_change_pct <= 0:
        return 0.0, f"SI trend {si_change_pct:+.1f}%"
    pts = min(si_change_pct / 5.0, 10.0)
    return pts, f"SI trend {si_change_pct:+.1f}%"


def _score_short_vol_ratio(avg_ratio: float | None) -> tuple[float, str]:
    if avg_ratio is None:
        return 0.0, "no short-vol"
    # 10 pts at 60%+, 5 at 40%
    if avg_ratio < 30:
        return 0.0, f"short-vol {avg_ratio:.0f}%"
    pts = min((avg_ratio - 30) / 3.0, 10.0)
    return pts, f"short-vol {avg_ratio:.0f}%"


def _score_volume_spike(today_vol: float | None, avg_vol: float | None) -> tuple[float, str]:
    if not today_vol or not avg_vol:
        return 0.0, "no vol data"
    ratio = today_vol / avg_vol
    # 10 pts at 3x, 5 at 2x
    if ratio < 1.2:
        return 0.0, f"vol {ratio:.1f}x"
    pts = min((ratio - 1.2) * 5.0, 10.0)
    return pts, f"vol {ratio:.1f}x"


def _score_technical(last_close: float, sma_20: float | None, rsi: float | None) -> tuple[float, str]:
    pts = 0.0
    parts = []
    if sma_20 and last_close > sma_20:
        pts += 5
        parts.append(">SMA20")
    if rsi is not None and 55 <= rsi <= 80:
        pts += 5
        parts.append(f"RSI={rsi:.0f}")
    return pts, ",".join(parts) if parts else "no breakout"


async def _compute_squeeze_score(symbol: str) -> dict[str, Any]:
    """Compute the short squeeze score for a single symbol. Returns the JSON-serializable payload."""

    def _fetch_yf_bundle() -> dict[str, Any]:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        hist = ticker.history(period="3mo")

        si_pct = _clean(info.get("shortPercentOfFloat"))
        if si_pct is not None and si_pct < 1.5:
            si_pct = si_pct * 100  # fraction -> percent

        shares_short = _clean(info.get("sharesShort"))
        shares_short_prior = _clean(info.get("sharesShortPriorMonth"))
        si_change_pct = None
        if shares_short and shares_short_prior:
            si_change_pct = ((shares_short - shares_short_prior) / shares_short_prior) * 100

        last_close = None
        today_vol = None
        avg_vol = None
        sma_20 = None
        rsi = None
        if not hist.empty:
            close = hist["Close"]
            volume = hist["Volume"]
            last_close = float(close.iloc[-1])
            today_vol = float(volume.iloc[-1])
            avg_vol = float(volume.mean())
            if len(close) >= 20:
                sma_20 = float(close.rolling(20).mean().iloc[-1])
            if len(close) >= 15:
                delta = close.diff()
                gain = delta.where(delta > 0, 0.0).rolling(14).mean()
                loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
                loss_last = loss.iloc[-1]
                if loss_last and loss_last != 0:
                    rs = gain.iloc[-1] / loss_last
                    rsi = float(100 - (100 / (1 + rs)))

        return {
            "si_pct": si_pct,
            "days_to_cover": _clean(info.get("shortRatio")),
            "si_change_pct": si_change_pct,
            "last_close": last_close,
            "today_vol": today_vol,
            "avg_vol": avg_vol,
            "sma_20": sma_20,
            "rsi": rsi,
            "float_shares": _clean(info.get("floatShares")),
        }

    yf_data = await _run_sync(_fetch_yf_bundle)

    # FINRA short volume (5 trading days) via internal helper, not the tool wrapper.
    sv_payload = await _compute_short_volume(symbol, 5)
    avg_short_vol_ratio = sv_payload.get("avg_short_ratio_pct") if sv_payload else None

    si_pts, si_desc = _score_short_interest(yf_data["si_pct"])
    dtc_pts, dtc_desc = _score_days_to_cover(yf_data["days_to_cover"])
    trend_pts, trend_desc = _score_si_trend(yf_data["si_change_pct"])
    sv_pts, sv_desc = _score_short_vol_ratio(avg_short_vol_ratio)
    vol_pts, vol_desc = _score_volume_spike(yf_data["today_vol"], yf_data["avg_vol"])
    tech_pts, tech_desc = _score_technical(
        yf_data["last_close"] or 0.0, yf_data["sma_20"], yf_data["rsi"],
    )

    total = round(si_pts + dtc_pts + trend_pts + sv_pts + vol_pts + tech_pts, 1)

    flags = []
    if yf_data["float_shares"] and yf_data["float_shares"] < 20_000_000:
        flags.append("low_float")
    if yf_data["si_pct"] and yf_data["si_pct"] > 30:
        flags.append("extreme_short_interest")
    if yf_data["days_to_cover"] and yf_data["days_to_cover"] > 7:
        flags.append("high_days_to_cover")

    verdict = (
        "strong squeeze setup" if total >= 70
        else "moderate squeeze setup" if total >= 50
        else "weak squeeze setup" if total >= 30
        else "no squeeze signal"
    )

    return {
        "symbol": symbol,
        "squeeze_score": total,
        "verdict": verdict,
        "flags": flags,
        "components": {
            "short_interest": {"points": round(si_pts, 1), "max": 40, "detail": si_desc},
            "days_to_cover": {"points": round(dtc_pts, 1), "max": 20, "detail": dtc_desc},
            "si_trend": {"points": round(trend_pts, 1), "max": 10, "detail": trend_desc},
            "short_volume_ratio": {"points": round(sv_pts, 1), "max": 10, "detail": sv_desc},
            "volume_spike": {"points": round(vol_pts, 1), "max": 10, "detail": vol_desc},
            "technical_breakout": {"points": round(tech_pts, 1), "max": 10, "detail": tech_desc},
        },
        "inputs": {
            "si_pct": yf_data["si_pct"],
            "days_to_cover": yf_data["days_to_cover"],
            "si_change_pct_mom": round(yf_data["si_change_pct"], 2) if yf_data["si_change_pct"] else None,
            "avg_short_volume_ratio_pct": avg_short_vol_ratio,
            "last_close": yf_data["last_close"],
            "volume_vs_avg": (
                round(yf_data["today_vol"] / yf_data["avg_vol"], 2)
                if yf_data["today_vol"] and yf_data["avg_vol"] else None
            ),
            "rsi_14": round(yf_data["rsi"], 1) if yf_data["rsi"] is not None else None,
            "float_shares": yf_data["float_shares"],
        },
    }


@tool(
    "squeeze_score",
    "Compute a composite short-squeeze score (0-100) for a stock. Combines short interest %, days to cover, SI month-over-month trend, FINRA short-volume ratio, volume spike, and technical breakout (price vs SMA20, RSI). Returns score, breakdown of each component, and raw inputs. Requires no API key but uses FINNHUB_API_KEY if set.",
    {"symbol": str},
)
async def squeeze_score(args: dict[str, Any]) -> dict[str, Any]:
    try:
        symbol = args["symbol"].upper()
        payload = await _compute_squeeze_score(symbol)
        return _text(json.dumps(payload, indent=2))
    except Exception as e:
        return _error(f"Failed to compute squeeze score for {args.get('symbol', '?')}: {e}")


# ---------------------------------------------------------------------------
# 8. squeeze_screener — rank a list of symbols by squeeze score
# ---------------------------------------------------------------------------
@tool(
    "squeeze_screener",
    "Screen a list of symbols for short squeeze setups. Returns symbols ranked by squeeze_score (highest first) with score, verdict, and flags. Useful for scanning a watchlist. Max 25 symbols per call.",
    {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of ticker symbols to screen (max 25)",
            },
            "min_score": {
                "type": "number",
                "description": "Only return symbols with score >= this value (default: 30)",
            },
        },
        "required": ["symbols"],
    },
)
async def squeeze_screener(args: dict[str, Any]) -> dict[str, Any]:
    try:
        symbols = [s.upper() for s in args["symbols"]][:25]
        min_score = args.get("min_score", 30)

        # Run scoring concurrently for each symbol via the plain helper (not the tool wrapper).
        tasks = [_compute_squeeze_score(s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        ranked: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for sym, res in zip(symbols, results):
            if isinstance(res, Exception):
                errors.append({"symbol": sym, "error": str(res)})
                continue
            if res.get("squeeze_score", 0) >= min_score:
                ranked.append({
                    "symbol": res["symbol"],
                    "squeeze_score": res["squeeze_score"],
                    "verdict": res["verdict"],
                    "flags": res["flags"],
                    "si_pct": res["inputs"]["si_pct"],
                    "days_to_cover": res["inputs"]["days_to_cover"],
                })

        ranked.sort(key=lambda r: r["squeeze_score"], reverse=True)

        return _text(json.dumps({
            "scanned": len(symbols),
            "matched": len(ranked),
            "min_score": min_score,
            "results": ranked,
            "errors": errors,
        }, indent=2))
    except Exception as e:
        return _error(f"Failed to run squeeze screener: {e}")


# ---------------------------------------------------------------------------
# Export all tools
# ---------------------------------------------------------------------------
STOCKS_TOOLS: list[SdkMcpTool] = [
    stock_quote,
    options_expirations,
    options_chain,
    stock_technicals,
    short_interest,
    short_volume,
    squeeze_score,
    squeeze_screener,
]

"""Stocks MCP server tools for market data via yfinance."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

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
# Export all tools
# ---------------------------------------------------------------------------
STOCKS_TOOLS: list[SdkMcpTool] = [
    stock_quote,
    options_expirations,
    options_chain,
    stock_technicals,
]

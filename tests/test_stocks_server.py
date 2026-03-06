"""Tests for the Stocks MCP server tools."""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# Install a proper fake claude_agent_sdk that preserves decorated functions.
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

# Mock yfinance before importing stocks_server
sys.modules.setdefault("yfinance", MagicMock())

# Force reimport so stocks_server picks up the proper fake decorator
sys.modules.pop("src.mcp.stocks_server", None)

from src.mcp import stocks_server  # noqa: E402

# Access underlying async handlers
_stock_quote = stocks_server.stock_quote.handler
_options_expirations = stocks_server.options_expirations.handler
_options_chain = stocks_server.options_chain.handler
_stock_technicals = stocks_server.stock_technicals.handler


def _parse_text(result: dict[str, Any]) -> str:
    """Extract text content from a tool result."""
    return result["content"][0]["text"]


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("is_error", False)


def _make_ticker_info(
    *,
    current_price: float = 150.0,
    previous_close: float = 148.0,
    volume: int = 1000000,
    high_52w: float = 180.0,
    low_52w: float = 120.0,
    pe: float = 25.5,
    market_cap: int = 2_500_000_000_000,
    name: str = "Apple Inc.",
) -> dict[str, Any]:
    return {
        "currentPrice": current_price,
        "previousClose": previous_close,
        "volume": volume,
        "fiftyTwoWeekHigh": high_52w,
        "fiftyTwoWeekLow": low_52w,
        "trailingPE": pe,
        "marketCap": market_cap,
        "shortName": name,
    }


class _MockSeries:
    """Lightweight mock that supports pandas Series operations needed by stock_technicals."""

    def __init__(self, values: list[float]) -> None:
        self._values = values

    class _Iloc:
        def __init__(self, values: list[float]) -> None:
            self._values = values

        def __getitem__(self, idx: int) -> float:
            return self._values[idx]

    @property
    def iloc(self) -> _Iloc:
        return self._Iloc(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __sub__(self, other: Any) -> _MockSeries:
        if isinstance(other, _MockSeries):
            return _MockSeries([a - b for a, b in zip(self._values, other._values)])
        return _MockSeries([v - other for v in self._values])

    def __add__(self, other: Any) -> _MockSeries:
        if isinstance(other, _MockSeries):
            return _MockSeries([a + b for a, b in zip(self._values, other._values)])
        return _MockSeries([v + other for v in self._values])

    def __mul__(self, other: Any) -> _MockSeries:
        if isinstance(other, (int, float)):
            return _MockSeries([v * other for v in self._values])
        return NotImplemented

    def __rmul__(self, other: Any) -> _MockSeries:
        return self.__mul__(other)

    def __neg__(self) -> _MockSeries:
        return _MockSeries([-v for v in self._values])

    def __gt__(self, other: Any) -> _MockBoolSeries:
        if isinstance(other, (int, float)):
            return _MockBoolSeries([v > other for v in self._values])
        return NotImplemented

    def __lt__(self, other: Any) -> _MockBoolSeries:
        if isinstance(other, (int, float)):
            return _MockBoolSeries([v < other for v in self._values])
        return NotImplemented

    def rolling(self, window: int) -> _MockRolling:
        return _MockRolling(self._values, window)

    def ewm(self, span: int, adjust: bool = False) -> _MockEwm:
        return _MockEwm(self._values, span)

    def diff(self) -> _MockSeries:
        values = [0.0] + [self._values[i] - self._values[i - 1] for i in range(1, len(self._values))]
        return _MockSeries(values)

    def where(self, cond: Any, fill: float) -> _MockSeries:
        # pandas where: keep value where cond is True, replace with fill where False
        if isinstance(cond, _MockBoolSeries):
            return _MockSeries([v if c else fill for v, c in zip(self._values, cond._values)])
        # Fallback for non-bool-series conditions
        return _MockSeries(self._values)

    def mean(self) -> float:
        return sum(self._values) / len(self._values) if self._values else 0.0


class _MockBoolSeries:
    """Boolean mask series for filtering operations."""

    def __init__(self, values: list[bool]) -> None:
        self._values = values


class _MockRolling:
    def __init__(self, values: list[float], window: int) -> None:
        self._values = values
        self._window = window

    def mean(self) -> _MockSeries:
        n = len(self._values)
        w = self._window
        result = []
        for i in range(n):
            if i < w - 1:
                result.append(float("nan"))
            else:
                result.append(sum(self._values[i - w + 1:i + 1]) / w)
        return _MockSeries(result)

    def std(self) -> _MockSeries:
        import math as _math
        n = len(self._values)
        w = self._window
        result = []
        for i in range(n):
            if i < w - 1:
                result.append(float("nan"))
            else:
                chunk = self._values[i - w + 1:i + 1]
                m = sum(chunk) / len(chunk)
                var = sum((x - m) ** 2 for x in chunk) / (len(chunk) - 1)
                result.append(_math.sqrt(var))
        return _MockSeries(result)


class _MockEwm:
    def __init__(self, values: list[float], span: int) -> None:
        self._values = values
        self._span = span

    def mean(self) -> _MockSeries:
        alpha = 2 / (self._span + 1)
        ema = [self._values[0]]
        for i in range(1, len(self._values)):
            ema.append(alpha * self._values[i] + (1 - alpha) * ema[-1])
        return _MockSeries(ema)


class _MockHistoryDF:
    """Mock pandas DataFrame for ticker.history() output."""

    def __init__(self, close_values: list[float], volume_values: list[float]) -> None:
        self._close = _MockSeries(close_values)
        self._volume = _MockSeries(volume_values)
        self.empty = False

    def __getitem__(self, key: str) -> _MockSeries:
        if key == "Close":
            return self._close
        if key == "Volume":
            return self._volume
        return _MockSeries([])


def _make_history_df() -> _MockHistoryDF:
    """Create a mock pandas DataFrame with 250 rows of price history."""
    import math

    n = 250
    close_values = [100 + 10 * math.sin(i * 0.1) + i * 0.05 for i in range(n)]
    volume_values = [float(1000000 + i * 100) for i in range(n)]
    return _MockHistoryDF(close_values, volume_values)


def _make_options_df(strikes: list[float], current_price: float = 150.0) -> MagicMock:
    """Create a mock options DataFrame."""
    df = MagicMock()
    rows = []
    for s in strikes:
        rows.append({
            "strike": s,
            "bid": s - 1.0,
            "ask": s + 1.0,
            "lastPrice": s,
            "volume": 100,
            "openInterest": 500,
            "impliedVolatility": 0.25,
            "inTheMoney": s < current_price,
        })

    # Simulate pandas filtering: df[(df["strike"] >= low) & (df["strike"] <= high)]
    class _FilterableDF:
        def __init__(self, data: list[dict[str, Any]]) -> None:
            self._data = data

        def __getitem__(self, key: Any) -> Any:
            if isinstance(key, str):
                return _Column([r[key] for r in self._data])
            # Boolean mask filtering
            if isinstance(key, _BoolMask):
                filtered = [r for r, keep in zip(self._data, key._values) if keep]
                return _FilterableDF(filtered)
            return self

        def iterrows(self) -> Any:
            for i, row in enumerate(self._data):
                yield i, row

    class _Column:
        def __init__(self, values: list[Any]) -> None:
            self._values = values

        def __ge__(self, other: float) -> _BoolMask:
            return _BoolMask([v >= other for v in self._values])

        def __le__(self, other: float) -> _BoolMask:
            return _BoolMask([v <= other for v in self._values])

    class _BoolMask:
        def __init__(self, values: list[bool]) -> None:
            self._values = values

        def __and__(self, other: _BoolMask) -> _BoolMask:
            return _BoolMask([a and b for a, b in zip(self._values, other._values)])

    return _FilterableDF(rows)


# ---------------------------------------------------------------------------
# stock_quote
# ---------------------------------------------------------------------------
class TestStockQuote:
    @patch("src.mcp.stocks_server.yf")
    async def test_valid_quote(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.info = _make_ticker_info()
        mock_yf.Ticker.return_value = ticker

        result = await _stock_quote({"symbol": "AAPL"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["symbol"] == "AAPL"
        assert data["name"] == "Apple Inc."
        assert data["price"] == 150.0
        assert data["change"] == 2.0
        assert data["change_pct"] == 1.35
        assert data["volume"] == 1000000
        assert data["52w_high"] == 180.0
        assert data["52w_low"] == 120.0
        assert data["pe_ratio"] == 25.5
        assert data["market_cap"] == 2_500_000_000_000

    @patch("src.mcp.stocks_server.yf")
    async def test_lowercased_symbol_uppercased(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.info = _make_ticker_info()
        mock_yf.Ticker.return_value = ticker

        result = await _stock_quote({"symbol": "aapl"})
        data = json.loads(_parse_text(result))

        assert data["symbol"] == "AAPL"
        mock_yf.Ticker.assert_called_once_with("AAPL")

    @patch("src.mcp.stocks_server.yf")
    async def test_nan_fields_become_none(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        info = _make_ticker_info()
        info["trailingPE"] = float("nan")
        info["marketCap"] = float("nan")
        ticker.info = info
        mock_yf.Ticker.return_value = ticker

        result = await _stock_quote({"symbol": "AAPL"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["pe_ratio"] is None
        assert data["market_cap"] is None

    @patch("src.mcp.stocks_server.yf")
    async def test_error_on_exception(self, mock_yf: MagicMock) -> None:
        mock_yf.Ticker.side_effect = Exception("API error")

        result = await _stock_quote({"symbol": "INVALID"})

        assert _is_error(result)
        assert "Failed to get quote" in _parse_text(result)

    @patch("src.mcp.stocks_server.yf")
    async def test_fallback_to_regularmarket_fields(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.info = {
            "regularMarketPrice": 200.0,
            "regularMarketPreviousClose": 195.0,
            "regularMarketVolume": 500000,
            "fiftyTwoWeekHigh": 250.0,
            "fiftyTwoWeekLow": 150.0,
            "shortName": "Test Corp",
        }
        mock_yf.Ticker.return_value = ticker

        result = await _stock_quote({"symbol": "TEST"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["price"] == 200.0
        assert data["volume"] == 500000


# ---------------------------------------------------------------------------
# options_expirations
# ---------------------------------------------------------------------------
class TestOptionsExpirations:
    @patch("src.mcp.stocks_server.yf")
    async def test_returns_expirations(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.options = ("2024-01-19", "2024-02-16", "2024-03-15")
        mock_yf.Ticker.return_value = ticker

        result = await _options_expirations({"symbol": "AAPL"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["symbol"] == "AAPL"
        assert len(data["expirations"]) == 3
        assert data["expirations"][0] == "2024-01-19"

    @patch("src.mcp.stocks_server.yf")
    async def test_no_options_available(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.options = ()
        mock_yf.Ticker.return_value = ticker

        result = await _options_expirations({"symbol": "BOND"})

        assert _is_error(result)
        assert "No options available" in _parse_text(result)

    @patch("src.mcp.stocks_server.yf")
    async def test_error_on_exception(self, mock_yf: MagicMock) -> None:
        mock_yf.Ticker.side_effect = Exception("API error")

        result = await _options_expirations({"symbol": "BAD"})

        assert _is_error(result)
        assert "Failed to get options expirations" in _parse_text(result)


# ---------------------------------------------------------------------------
# options_chain
# ---------------------------------------------------------------------------
class TestOptionsChain:
    @patch("src.mcp.stocks_server.yf")
    async def test_returns_both_calls_and_puts(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.info = {"currentPrice": 150.0}
        strikes = [140.0, 145.0, 150.0, 155.0, 160.0]
        chain = MagicMock()
        chain.calls = _make_options_df(strikes, 150.0)
        chain.puts = _make_options_df(strikes, 150.0)
        ticker.option_chain.return_value = chain
        mock_yf.Ticker.return_value = ticker

        result = await _options_chain({"symbol": "AAPL", "expiration": "2024-01-19"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["symbol"] == "AAPL"
        assert data["current_price"] == 150.0
        assert "calls" in data
        assert "puts" in data
        assert len(data["calls"]) == 5  # all within 10% of 150

    @patch("src.mcp.stocks_server.yf")
    async def test_calls_only(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.info = {"currentPrice": 150.0}
        chain = MagicMock()
        chain.calls = _make_options_df([145.0, 150.0, 155.0], 150.0)
        ticker.option_chain.return_value = chain
        mock_yf.Ticker.return_value = ticker

        result = await _options_chain({
            "symbol": "AAPL",
            "expiration": "2024-01-19",
            "option_type": "calls",
        })
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert "calls" in data
        assert "puts" not in data

    @patch("src.mcp.stocks_server.yf")
    async def test_puts_only(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.info = {"currentPrice": 150.0}
        chain = MagicMock()
        chain.puts = _make_options_df([145.0, 150.0, 155.0], 150.0)
        ticker.option_chain.return_value = chain
        mock_yf.Ticker.return_value = ticker

        result = await _options_chain({
            "symbol": "AAPL",
            "expiration": "2024-01-19",
            "option_type": "puts",
        })
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert "puts" in data
        assert "calls" not in data

    @patch("src.mcp.stocks_server.yf")
    async def test_strike_range_filtering(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.info = {"currentPrice": 100.0}
        # 5% range of 100 = 95-105, so 90 and 110 should be excluded
        strikes = [90.0, 95.0, 100.0, 105.0, 110.0]
        chain = MagicMock()
        chain.calls = _make_options_df(strikes, 100.0)
        chain.puts = _make_options_df(strikes, 100.0)
        ticker.option_chain.return_value = chain
        mock_yf.Ticker.return_value = ticker

        result = await _options_chain({
            "symbol": "TEST",
            "expiration": "2024-01-19",
            "strike_range_pct": 5,
        })
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert len(data["calls"]) == 3  # 95, 100, 105

    @patch("src.mcp.stocks_server.yf")
    async def test_nan_in_implied_volatility(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.info = {"currentPrice": 150.0}

        class _NanDF:
            def __getitem__(self, key: Any) -> Any:
                if isinstance(key, str):
                    return _Col([150.0])
                return self

            def iterrows(self) -> Any:
                yield 0, {
                    "strike": 150.0,
                    "bid": 2.0,
                    "ask": 3.0,
                    "lastPrice": 2.5,
                    "volume": 100,
                    "openInterest": 500,
                    "impliedVolatility": float("nan"),
                    "inTheMoney": True,
                }

        class _Col:
            def __init__(self, vals: list[float]) -> None:
                self._vals = vals

            def __ge__(self, other: float) -> _BM:
                return _BM([v >= other for v in self._vals])

            def __le__(self, other: float) -> _BM:
                return _BM([v <= other for v in self._vals])

        class _BM:
            def __init__(self, vals: list[bool]) -> None:
                self._values = vals

            def __and__(self, other: _BM) -> _BM:
                return _BM([a and b for a, b in zip(self._values, other._values)])

        chain = MagicMock()
        chain.calls = _NanDF()
        chain.puts = _NanDF()
        ticker.option_chain.return_value = chain
        mock_yf.Ticker.return_value = ticker

        result = await _options_chain({"symbol": "AAPL", "expiration": "2024-01-19"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["calls"][0]["impliedVolatility"] is None

    @patch("src.mcp.stocks_server.yf")
    async def test_error_on_exception(self, mock_yf: MagicMock) -> None:
        mock_yf.Ticker.side_effect = Exception("Bad symbol")

        result = await _options_chain({"symbol": "BAD", "expiration": "2024-01-19"})

        assert _is_error(result)
        assert "Failed to get options chain" in _parse_text(result)


# ---------------------------------------------------------------------------
# stock_technicals
# ---------------------------------------------------------------------------
class TestStockTechnicals:
    @patch("src.mcp.stocks_server.yf")
    async def test_valid_technicals(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        ticker.history.return_value = _make_history_df()
        mock_yf.Ticker.return_value = ticker

        result = await _stock_technicals({"symbol": "AAPL"})
        data = json.loads(_parse_text(result))

        assert not _is_error(result)
        assert data["symbol"] == "AAPL"
        assert "last_close" in data
        assert "sma_20" in data
        assert "sma_50" in data
        assert "sma_200" in data
        assert "rsi_14" in data
        assert "macd" in data
        assert "macd_line" in data["macd"]
        assert "signal_line" in data["macd"]
        assert "histogram" in data["macd"]
        assert "bollinger_bands" in data
        assert "upper" in data["bollinger_bands"]
        assert "middle" in data["bollinger_bands"]
        assert "lower" in data["bollinger_bands"]
        assert "avg_volume" in data
        assert "price_vs_sma" in data
        assert "above_sma_20" in data["price_vs_sma"]
        assert "above_sma_50" in data["price_vs_sma"]
        assert "above_sma_200" in data["price_vs_sma"]

    @patch("src.mcp.stocks_server.yf")
    async def test_empty_history_returns_error(self, mock_yf: MagicMock) -> None:
        ticker = MagicMock()
        df = MagicMock()
        df.empty = True
        ticker.history.return_value = df
        mock_yf.Ticker.return_value = ticker

        result = await _stock_technicals({"symbol": "EMPTY"})

        assert _is_error(result)
        assert "No historical data" in _parse_text(result)

    @patch("src.mcp.stocks_server.yf")
    async def test_error_on_exception(self, mock_yf: MagicMock) -> None:
        mock_yf.Ticker.side_effect = Exception("Network error")

        result = await _stock_technicals({"symbol": "BAD"})

        assert _is_error(result)
        assert "Failed to compute technicals" in _parse_text(result)


# ---------------------------------------------------------------------------
# STOCKS_TOOLS export
# ---------------------------------------------------------------------------
class TestStocksToolsExport:
    def test_exports_all_four_tools(self) -> None:
        tools = stocks_server.STOCKS_TOOLS
        assert len(tools) == 4
        names = {t.name for t in tools}
        assert names == {"stock_quote", "options_expirations", "options_chain", "stock_technicals"}

    def test_tools_are_sdk_mcp_tool_instances(self) -> None:
        for t in stocks_server.STOCKS_TOOLS:
            assert isinstance(t, _FakeSdkMcpTool)
            assert callable(t.handler)


# ---------------------------------------------------------------------------
# _clean helper
# ---------------------------------------------------------------------------
class TestCleanHelper:
    def test_passes_through_normal_values(self) -> None:
        assert stocks_server._clean(42) == 42
        assert stocks_server._clean(3.14) == 3.14
        assert stocks_server._clean("hello") == "hello"
        assert stocks_server._clean(None) is None

    def test_nan_becomes_none(self) -> None:
        assert stocks_server._clean(float("nan")) is None

"""Offline tests for the Robinhood market-data provider — NO network, NO MCP.

Feeds a fake direct MCP client the real JSON shapes captured from the live
tools and asserts the field contract the rest of the system relies on, the deterministic
financials computation, the sector mapping, and graceful fallback to the wrapped
provider on any missing symbol.

Run:  python -m pytest test_robinhood_provider.py -q
"""
from __future__ import annotations

import asyncio

from data.robinhood_mcp import RobinhoodMCPError
from data.robinhood_provider import (
    RobinhoodDataProvider, _map_rh_sector, profile_from_result, quote_from_result,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeInner:
    """Stands in for the wrapped DataProvider (yfinance/AV/FRED)."""

    def __init__(self):
        self.sector_overrides = {}
        self.cache = object()
        self.quote_calls = []
        self.series_calls = []
        self.fund_calls = []

    def get_quote(self, ticker):
        self.quote_calls.append(ticker)
        return {"ticker": ticker, "price": 1.11, "prev_close": 1.10, "source": "yfinance"}

    def get_daily_series(self, ticker, lookback=100):
        self.series_calls.append((ticker, lookback))
        return [{"date": "2026-01-01", "open": 1, "high": 1, "low": 1,
                 "close": 1, "volume": 1}]

    def get_fundamentals(self, ticker):
        self.fund_calls.append(ticker)
        # The gap fields Robinhood cannot supply come from here.
        return {"ticker": ticker, "sector": "Unknown", "source": "yfinance",
                "operating_margin": 30.0, "debt_to_equity": 1.2,
                "free_cash_flow": 2.0e10, "beta": 1.1, "name": "NVIDIA Corp",
                "next_earnings_date": "2026-08-27", "pe_ratio": None,
                "revenue_ttm": None, "market_cap": None}

    def get_news_sentiment(self, *a, **k):
        return {"source": "alphavantage", "article_count": 1, "aggregate_score": 0.2,
                "window_days": 14, "articles": []}

    def get_macro(self):
        return {"fed_funds": 4.5, "ten_year": 4.2, "cpi": 3.1, "unemployment": 4.0}


# Real captured shapes (trimmed) from the live Robinhood tools, as the direct
# client returns them (the `data` half of the {"data", "guide"} envelope).
_QUOTES = {"NVDA": {"quote": {"symbol": "NVDA", "last_trade_price": "206.960000",
                              "last_non_reg_trade_price": None,
                              "previous_close": "208.760000"},
                    "close": {"symbol": "NVDA", "price": "208.76"}}}
_SERIES = {"NVDA": [
    {"date": "2026-06-15", "open": 208.92, "high": 212.71, "low": 208.34,
     "close": 212.45, "volume": 149936688},
    {"date": "2026-06-16", "open": 211.18, "high": 211.49, "low": 207.29,
     "close": 207.41, "volume": 125694100},
]}
_PROFILE = {"NVDA": {"symbol": "NVDA", "sector": "Electronic Technology",
                     "industry": "Semiconductors",
                     "description": "NVIDIA Corp. designs GPUs.", "market_cap": "5.09e12",
                     "pe_ratio": "31.76", "shares_outstanding": "24.6e9",
                     "high_52_weeks": "236.54", "low_52_weeks": "164.07"}}
# 5 quarters so YoY growth is computable (index 0 vs index 4).
_FIN = {"NVDA": [
    {"revenue": 81.6e9, "gross_profit": 61.1e9, "net_income": 58.3e9, "net_margin": 71.46},
    {"revenue": 68.1e9, "gross_profit": 51.0e9, "net_income": 42.9e9, "net_margin": 63.06},
    {"revenue": 57.0e9, "gross_profit": 41.8e9, "net_income": 31.9e9, "net_margin": 55.98},
    {"revenue": 46.7e9, "gross_profit": 33.8e9, "net_income": 26.4e9, "net_margin": 56.53},
    {"revenue": 44.0e9, "gross_profit": 32.0e9, "net_income": 22.0e9, "net_margin": 50.0},
]}


class FakeRH:
    """Stands in for data.robinhood_mcp.RobinhoodMCP: answers each tool with the
    raw payload shape, scoped to the requested symbols."""

    def __init__(self, quotes=None, profiles=None, fins=None):
        self.calls: list[tuple[str, dict]] = []     # every (tool, args) issued
        self.quotes = _QUOTES if quotes is None else quotes
        self.profiles = _PROFILE if profiles is None else profiles
        self.fins = _FIN if fins is None else fins

    async def call(self, tool, args):
        self.calls.append((tool, args))
        syms = args.get("symbols") or []
        if tool == "get_equity_quotes":
            return {"results": [self.quotes[s] for s in syms if s in self.quotes]}
        if tool == "get_equity_fundamentals":
            return {"results": [self.profiles[s] for s in syms if s in self.profiles]}
        if tool == "get_financials":
            return {"results": [{"symbol": s, "period": "quarterly", "financials": self.fins[s]}
                                if s in self.fins else None for s in syms]}
        raise RobinhoodMCPError(f"unexpected tool {tool}")


def _provider(rh: FakeRH | None = None):
    inner = FakeInner()
    rh = rh or FakeRH()
    p = RobinhoodDataProvider(inner, rh)
    p.rh = rh
    return p, inner


def _tools(p) -> list[str]:
    return [t for t, _ in p.rh.calls]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_prefetch_populates_quotes_from_robinhood():
    p, inner = _provider()
    asyncio.run(p.prefetch(["NVDA"]))
    q = p.get_quote("NVDA")
    assert q["price"] == 206.96 and q["source"] == "robinhood"
    assert inner.quote_calls == []        # served from the store, not the inner provider


def test_prefetch_never_requests_historicals():
    # Series is deliberately NOT sourced from Robinhood (yfinance serves the
    # full lookback free). Prefetch must issue quotes + fundamentals +
    # financials calls, but NEVER get_equity_historicals.
    p, _ = _provider()
    asyncio.run(p.prefetch(["NVDA"]))
    assert p.rh.calls, "prefetch should issue some MCP calls"
    assert "get_equity_historicals" not in _tools(p)
    assert "get_equity_quotes" in _tools(p)


def test_series_always_delegates_to_yfinance():
    # Even a prefetched ticker's series comes from the wrapped provider (yfinance),
    # never from Robinhood.
    p, inner = _provider()
    asyncio.run(p.prefetch(["NVDA"]))
    s = p.get_daily_series("NVDA", 260)
    assert inner.series_calls == [("NVDA", 260)]   # delegated to the inner provider
    assert s is not None and s[0]["date"] == "2026-01-01"   # the FakeInner (yfinance) stub


def test_fundamentals_merge_compute_and_sector_map():
    p, _ = _provider()
    asyncio.run(p.prefetch(["NVDA"]))
    f = p.get_fundamentals("NVDA")
    # Robinhood profile fields win.
    assert f["market_cap"] == 5.09e12 and f["pe_ratio"] == 31.76
    assert f["week52_high"] == 236.54 and f["industry"] == "Semiconductors"
    # Computed from financials.
    assert round(f["revenue_ttm"]) == round(81.6e9 + 68.1e9 + 57.0e9 + 46.7e9)
    assert f["ps_ratio"] == round(5.09e12 / f["revenue_ttm"], 3)
    assert f["profit_margin"] == 71.46                    # latest net_margin, already %
    assert f["gross_margin"] is not None and 60 < f["gross_margin"] < 100
    assert f["eps_ttm"] is not None                       # net_income TTM / shares
    assert f["revenue_growth_yoy"] == round((81.6e9 / 44.0e9 - 1) * 100, 2)
    # Gap fields still come from the wrapped provider.
    assert f["debt_to_equity"] == 1.2 and f["free_cash_flow"] == 2.0e10
    assert f["beta"] == 1.1 and f["name"] == "NVIDIA Corp"
    assert f["next_earnings_date"] == "2026-08-27"
    # RH taxonomy mapped to the guardrail bucket.
    assert f["sector"] == "Technology"
    assert f["source"].startswith("robinhood")


def test_missing_symbol_falls_back_to_inner():
    p, inner = _provider()
    asyncio.run(p.prefetch(["NVDA"]))       # ZZZ is never prefetched
    q = p.get_quote("ZZZ")
    assert q["source"] == "yfinance" and inner.quote_calls == ["ZZZ"]
    s = p.get_daily_series("ZZZ", 100)
    assert inner.series_calls == [("ZZZ", 100)]
    f = p.get_fundamentals("ZZZ")
    assert f["source"] == "yfinance" and "ZZZ" in inner.fund_calls


def test_sector_override_wins_over_rh():
    p, inner = _provider()
    inner.sector_overrides = {"NVDA": "Semiconductors-Custom"}
    asyncio.run(p.prefetch(["NVDA"]))
    # The wrapped provider already applies the override into base['sector'];
    # RobinhoodDataProvider must NOT overwrite it with the mapped RH sector.
    inner.get_fundamentals = lambda t: {"ticker": t, "sector": "Semiconductors-Custom",
                                        "source": "yfinance"}
    assert p.get_fundamentals("NVDA")["sector"] == "Semiconductors-Custom"


def test_delegates_news_and_macro():
    p, _ = _provider()
    assert p.get_news_sentiment("NVDA")["source"] == "alphavantage"
    assert p.get_macro()["fed_funds"] == 4.5


def test_sector_map_unit():
    assert _map_rh_sector("Electronic Technology") == "Technology"
    assert _map_rh_sector("Finance") == "Financials"
    assert _map_rh_sector("Health Technology") == "Health Care"
    assert _map_rh_sector("Totally Unknown Sector", "Energy") == "Energy"   # fallback


# --------------------------------------------------------------------------- #
# DataProvider: series must not spend Alpha Vantage quota
# --------------------------------------------------------------------------- #
def test_dataprovider_series_skips_alphavantage():
    import tempfile
    from data.providers import DataProvider, ProviderConfig

    with tempfile.TemporaryDirectory() as tmp:
        # use_alphavantage_series False (default) + no yfinance -> series returns
        # None WITHOUT ever calling Alpha Vantage (no premium-blocked TIME_SERIES
        # calls, no wasted budget).
        cfg = ProviderConfig(cache_dir=tmp, av_api_key="dummy",
                             use_alphavantage_series=False, use_yfinance_fallback=False)
        dp = DataProvider(cfg)
        calls = []
        dp._alpha_vantage = lambda params: calls.append(params) or None
        assert dp.get_daily_series("AAPL", 260) is None
        assert calls == [], "series must not call Alpha Vantage when disabled"

        # Opt back in -> Alpha Vantage IS consulted (premium key scenario).
        cfg2 = ProviderConfig(cache_dir=tmp, av_api_key="dummy",
                              use_alphavantage_series=True, use_yfinance_fallback=False)
        dp2 = DataProvider(cfg2)
        calls2 = []
        dp2._alpha_vantage = lambda params: calls2.append(params) or None
        dp2.get_daily_series("AAPL", 260)
        assert calls2 and calls2[0]["function"] == "TIME_SERIES_DAILY"


# --------------------------------------------------------------------------- #
# Unquotable symbols and payload mapping
# --------------------------------------------------------------------------- #
def test_symbol_absent_from_a_real_payload_is_blacklisted_once():
    """A well-formed quotes payload that omits a symbol is proof the broker has
    no instrument for it (delisted/acquired). Record it and stop asking."""
    p, _ = _provider()
    asyncio.run(p.prefetch(["NVDA", "WBS"]))
    assert "WBS" in p._unavailable
    assert "NVDA" not in p._unavailable
    assert _tools(p).count("get_equity_quotes") == 1          # no pointless retry

    # A later prefetch must not ask about the known-unavailable symbol again.
    p.rh.calls.clear()
    p._prefetched.clear()
    asyncio.run(p.prefetch(["WBS"]))
    assert "get_equity_quotes" not in _tools(p)


def test_failed_quotes_call_falls_back_without_blacklisting():
    """A transport/tool error resolves nothing: nothing is blacklisted and the
    wrapped provider serves the quote."""
    class Broken(FakeRH):
        async def call(self, tool, args):
            self.calls.append((tool, args))
            raise RobinhoodMCPError("boom")

    p, inner = _provider(Broken())
    asyncio.run(p.prefetch(["NVDA"]))
    assert p._unavailable == set()
    assert p.get_quote("NVDA")["source"] == "yfinance"
    assert inner.quote_calls == ["NVDA"]


def test_quote_from_result_prefers_regular_session_then_extended():
    row = {"quote": {"symbol": "aapl", "last_trade_price": None,
                     "last_non_reg_trade_price": "101.5", "previous_close": "100"},
           "close": {"symbol": "AAPL", "price": "99.5"}}
    q = quote_from_result(row)
    assert q == {"ticker": "AAPL", "price": 101.5, "prev_close": 99.5}
    # No price at all -> no quote (never a zero).
    assert quote_from_result({"quote": {"symbol": "X", "last_trade_price": None}}) is None
    assert quote_from_result("garbage") is None


def test_profile_from_result_maps_52_week_fields():
    prof = profile_from_result(_PROFILE["NVDA"])
    assert prof["week52_high"] == "236.54" and prof["week52_low"] == "164.07"
    assert "description" not in prof


def test_quotes_are_batched_at_twenty():
    """Above 20 symbols get_equity_quotes omits the official closes, so the
    provider must split the request."""
    syms = [f"S{i:02d}" for i in range(45)]
    quotes = {s: {"quote": {"symbol": s, "last_trade_price": "1.0"}} for s in syms}
    p, _ = _provider(FakeRH(quotes=quotes, profiles={}, fins={}))
    asyncio.run(p._prefetch_quotes(syms))
    sizes = [len(a["symbols"]) for t, a in p.rh.calls if t == "get_equity_quotes"]
    assert sizes == [20, 20, 5]
    assert len(p._quotes) == 45

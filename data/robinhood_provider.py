"""Robinhood-backed market-data provider.

Wraps the Alpha-Vantage/yfinance/FRED :class:`~data.providers.DataProvider` and
takes over the three rate-limited paths — **quotes, daily OHLCV series, and the
fundamentals Robinhood can supply** — from the connected **Robinhood Trading
MCP**, which is real-time and quota-free. Alpha Vantage is left to do only what
Robinhood has no tool for: **news sentiment** (and FRED still supplies macro).

Robinhood data is read DIRECTLY from the MCP server through
:class:`data.robinhood_mcp.RobinhoodMCP` — no model in the loop (the previous
LLM-mediated bridge cost ~$0.03 and ~30s per call and failed 18% of the time).
We do **one batched prefetch per run** — a few multi-symbol tool calls up
front — into an in-memory store, so the existing *synchronous* per-ticker
provider methods just read the store (and transparently fall back to the
wrapped provider on any miss, so the system degrades to its previous behaviour
if a Robinhood read is thin).
"""
from __future__ import annotations

from typing import Any

from data.robinhood_mcp import RobinhoodMCP, RobinhoodMCPError

# Robinhood's sector taxonomy (FactSet-style) -> the guardrail sector buckets
# used across config.yaml `sectors:` and risk/max_sector_pct. Anything not mapped
# falls back to the wrapped provider's sector (yfinance/GICS) so grouping is never
# silently wrong.
_RH_SECTOR_MAP = {
    "Electronic Technology": "Technology",
    "Technology Services": "Technology",
    "Finance": "Financials",
    "Health Technology": "Health Care",
    "Health Services": "Health Care",
    "Retail Trade": "Consumer Discretionary",
    "Consumer Durables": "Consumer Discretionary",
    "Consumer Services": "Consumer Discretionary",
    "Consumer Non-Durables": "Consumer Staples",
    "Energy Minerals": "Energy",
    "Producer Manufacturing": "Industrials",
    "Commercial Services": "Industrials",
    "Industrial Services": "Industrials",
    "Distribution Services": "Industrials",
    "Transportation": "Industrials",
    "Process Industries": "Materials",
    "Non-Energy Minerals": "Materials",
    "Utilities": "Utilities",
    "Communications": "Communication Services",
}


def _map_rh_sector(rh_sector: Any, fallback: str = "Unknown") -> str:
    if not rh_sector:
        return fallback
    return _RH_SECTOR_MAP.get(str(rh_sector).strip(), fallback)


def _f(v: Any) -> float | None:
    try:
        if v in (None, "None", "-", ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _batches(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def quote_from_result(row: Any) -> dict | None:
    """One ``get_equity_quotes`` result -> ``{ticker, price, prev_close}``.

    Price is the regular-session last trade, falling back to the extended-hours
    print when the regular one is missing (pre-market runs); prev_close is the
    official prior close, falling back to the quote's own previous_close."""
    if not isinstance(row, dict):
        return None
    q = row.get("quote") if isinstance(row.get("quote"), dict) else row
    sym = str(q.get("symbol") or "").upper()
    price = _f(q.get("last_trade_price")) or _f(q.get("last_non_reg_trade_price"))
    if not sym or not price:
        return None
    close = row.get("close") if isinstance(row.get("close"), dict) else {}
    prev = _f(close.get("price")) or _f(q.get("previous_close")) \
        or _f(q.get("adjusted_previous_close")) or price
    return {"ticker": sym, "price": price, "prev_close": prev}


def profile_from_result(row: dict) -> dict:
    """Trim one ``get_equity_fundamentals`` result to the fields
    :meth:`RobinhoodDataProvider._compute_fundamentals` consumes."""
    return {
        "sector": row.get("sector"),
        "industry": row.get("industry"),
        "market_cap": row.get("market_cap"),
        "pe_ratio": row.get("pe_ratio"),
        "shares_outstanding": row.get("shares_outstanding"),
        "week52_high": row.get("high_52_weeks"),
        "week52_low": row.get("low_52_weeks"),
    }


class RobinhoodDataProvider:
    """Robinhood market data over a wrapped :class:`DataProvider` fallback."""

    def __init__(self, inner, client: RobinhoodMCP, *, audit=None):
        self._inner = inner
        self._rh = client
        self._audit = audit
        self._quotes: dict[str, dict] = {}
        self._fundamentals: dict[str, dict] = {}
        self._prefetched: set[str] = set()
        # Symbols a SUCCESSFUL quotes call declined to return — Robinhood has no
        # tradable instrument for them (delisted, acquired, never carried).
        self._unavailable: set[str] = set()

    # -- delegate the unchanged surface (news, macro, cache, overrides) -----
    @property
    def cache(self):
        return self._inner.cache

    def get_news_sentiment(self, *args, **kwargs):
        return self._inner.get_news_sentiment(*args, **kwargs)

    def get_macro(self, *args, **kwargs):
        return self._inner.get_macro(*args, **kwargs)

    def sector_for(self, ticker: str) -> str:
        t = ticker.upper()
        if t in self._inner.sector_overrides:
            return self._inner.sector_overrides[t]
        return self.get_fundamentals(t).get("sector", "Unknown")

    def _log(self, level: str, event: str, detail: Any) -> None:
        if self._audit:
            try:
                self._audit(level, event, detail)
            except Exception:
                pass

    async def _call(self, tool: str, args: dict) -> Any:
        """One direct tool call; ``None`` on failure (logged, never raised —
        a data read must not break the run; the wrapped provider fills in)."""
        try:
            return await self._rh.call(tool, args)
        except RobinhoodMCPError as e:
            self._log("WARN", "robinhood_call_failed", {"tool": tool, "error": str(e)[:300]})
            return None

    # -- prefetch (batched, one pass per run) ------------------------------
    async def prefetch(self, tickers: list[str]) -> None:
        syms = sorted({str(t).upper() for t in tickers if t})
        syms = [s for s in syms if s not in self._prefetched]
        if not syms:
            return
        # NOTE: daily SERIES is deliberately NOT prefetched from Robinhood —
        # yfinance serves it free with the full lookback. Only the compact
        # quotes + fundamentals come from Robinhood.
        for step in (self._prefetch_quotes, self._prefetch_fundamentals):
            try:
                await step(syms)
            except Exception as e:  # never let a data read break the run
                self._log("WARN", "robinhood_prefetch_step_error",
                          {"step": step.__name__, "error": str(e)})
        self._prefetched.update(syms)
        self._log("INFO", "robinhood_prefetch",
                  {"requested": len(syms), "quotes": len(self._quotes),
                   "fundamentals": len(self._fundamentals)})

    async def _prefetch_quotes(self, syms: list[str]) -> None:
        missing = [s for s in syms if s not in self._unavailable]
        # get_equity_quotes takes any number of symbols but omits the official
        # closes above 20 — keep batches at 20 so prev_close is always present.
        for batch in _batches(missing, 20):
            data = await self._call("get_equity_quotes", {"symbols": batch})
            rows = (data or {}).get("results") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                self._log("WARN", "robinhood_quotes_prefetch_failed", {"symbols": batch})
                continue
            for row in rows:
                q = quote_from_result(row)
                if q:
                    self._quotes[q["ticker"]] = {"price": q["price"], "prev_close": q["prev_close"]}
            # A symbol absent from a well-formed result has no tradable
            # instrument at Robinhood (delisted, acquired, never carried).
            unresolved = [s for s in batch if s not in self._quotes]
            if unresolved:
                self._unavailable.update(unresolved)
                self._log("WARN", "robinhood_quotes_unavailable", {"symbols": unresolved})

    async def _prefetch_fundamentals(self, syms: list[str]) -> None:
        profiles = await self._fetch_profiles(syms)
        financials = await self._fetch_financials(syms)
        for sym in syms:
            merged = self._compute_fundamentals(profiles.get(sym), financials.get(sym))
            if merged:
                self._fundamentals[sym] = merged

    async def _fetch_profiles(self, syms: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for batch in _batches(syms, 10):     # get_equity_fundamentals: <=10 symbols/call
            data = await self._call("get_equity_fundamentals", {"symbols": batch})
            rows = (data or {}).get("results") if isinstance(data, dict) else None
            for row in rows or []:
                if isinstance(row, dict) and row.get("symbol"):
                    out[str(row["symbol"]).upper()] = profile_from_result(row)
        return out

    async def _fetch_financials(self, syms: list[str]) -> dict[str, list]:
        out: dict[str, list] = {}
        for batch in _batches(syms, 20):     # get_financials: <=20 symbols/call
            data = await self._call("get_financials",
                                    {"symbols": batch, "period": "quarterly", "limit": 6})
            rows = (data or {}).get("results") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                continue
            # `results` is positional (one entry per requested symbol, null when
            # unresolved) but each entry also carries `symbol`; prefer that.
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol") or (batch[i] if i < len(batch) else "")).upper()
                fins = row.get("financials")
                if sym and isinstance(fins, list):
                    out[sym] = fins   # already most-recent-first
        return out

    # -- mapping helpers ---------------------------------------------------
    @staticmethod
    def _compute_fundamentals(prof: Any, fin: Any) -> dict | None:
        """Assemble the provider fundamentals contract from Robinhood's profile +
        quarterly financials. Margins/growth are percent (25.0 == 25%), matching
        DataProvider's units. Returns only the fields Robinhood can supply; the
        wrapped provider fills the rest (operating_margin, debt_to_equity,
        free_cash_flow, beta, analyst_target, name, next_earnings_date)."""
        out: dict[str, Any] = {}
        shares = None
        if isinstance(prof, dict):
            if prof.get("sector"):
                out["sector"] = prof["sector"]           # raw RH sector, mapped later
            if prof.get("industry"):
                out["industry"] = str(prof["industry"])
            # `description` is intentionally NOT fetched from Robinhood (its long
            # text bloats batched responses and risks truncation); the wrapped
            # provider (yfinance) supplies it.
            for k in ("market_cap", "pe_ratio", "week52_high", "week52_low"):
                v = _f(prof.get(k))
                if v is not None:
                    out[k] = v
            shares = _f(prof.get("shares_outstanding"))

        quarters = [q for q in (fin or []) if isinstance(q, dict)]

        def qf(q, key):
            return _f(q.get(key))

        if len(quarters) >= 4:
            revs = [qf(q, "revenue") for q in quarters[:4]]
            if all(r is not None for r in revs):
                rev_ttm = sum(revs)
                out["revenue_ttm"] = rev_ttm
                if out.get("market_cap") and rev_ttm:
                    out["ps_ratio"] = round(out["market_cap"] / rev_ttm, 3)
                gps = [qf(q, "gross_profit") for q in quarters[:4]]
                if all(g is not None for g in gps) and rev_ttm:
                    out["gross_margin"] = round(sum(gps) / rev_ttm * 100, 2)
                nis = [qf(q, "net_income") for q in quarters[:4]]
                if shares and all(n is not None for n in nis):
                    out["eps_ttm"] = round(sum(nis) / shares, 4)

        if len(quarters) >= 5:
            r0, r4 = qf(quarters[0], "revenue"), qf(quarters[4], "revenue")
            if r0 is not None and r4:
                out["revenue_growth_yoy"] = round((r0 / r4 - 1) * 100, 2)
            n0, n4 = qf(quarters[0], "net_income"), qf(quarters[4], "net_income")
            if n0 is not None and n4 and n4 > 0:
                out["eps_growth_yoy"] = round((n0 / n4 - 1) * 100, 2)

        if quarters:
            nm = qf(quarters[0], "net_margin")   # already a percentage
            if nm is not None:
                out["profit_margin"] = round(nm, 2)

        return out or None

    # -- public API (store first, wrapped provider on miss) ----------------
    def get_quote(self, ticker: str) -> dict | None:
        t = ticker.upper()
        q = self._quotes.get(t)
        if q:
            return {"ticker": t, "price": q["price"],
                    "prev_close": q.get("prev_close", q["price"]), "source": "robinhood"}
        return self._inner.get_quote(t)

    def get_daily_series(self, ticker: str, lookback: int = 100) -> list[dict] | None:
        # Series is intentionally NOT sourced from Robinhood: yfinance serves
        # the full lookback free and is already cached for 24h; nothing to gain.
        return self._inner.get_daily_series(ticker.upper(), lookback)

    def get_fundamentals(self, ticker: str) -> dict:
        t = ticker.upper()
        base = self._inner.get_fundamentals(t)   # full contract (yfinance/AV), gap fields
        rh = self._fundamentals.get(t)
        if not rh:
            return base
        merged = dict(base)
        for k, v in rh.items():
            if k == "sector":
                continue
            if v is not None:
                merged[k] = v                    # Robinhood wins for the fields it supplies
        if t not in self._inner.sector_overrides and rh.get("sector"):
            merged["sector"] = _map_rh_sector(rh["sector"], base.get("sector", "Unknown"))
        src = base.get("source")
        merged["source"] = "robinhood" + (f"+{src}" if src else "")
        return merged

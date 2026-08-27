"""Robinhood-backed market-data provider.

Wraps the Alpha-Vantage/yfinance/FRED :class:`~data.providers.DataProvider` and
takes over the three rate-limited paths — **quotes, daily OHLCV series, and the
fundamentals Robinhood can supply** — from the connected **Robinhood Trading
MCP**, which is real-time and quota-free. Alpha Vantage is left to do only what
Robinhood has no tool for: **news sentiment** (and FRED still supplies macro).

Robinhood data is read through the same LLM-mediated MCP bridge as the account
read (``generate_json`` inheriting Claude Code's OAuth via
``setting_sources=["local"]``). To keep that cheap and reliable we do **one
batched prefetch per run** — a few multi-symbol tool calls up front — into an
in-memory store, so the existing *synchronous* per-ticker provider methods just
read the store (and transparently fall back to the wrapped provider on any miss,
so the system degrades to its previous behaviour if a Robinhood read is thin).
"""
from __future__ import annotations

import json
from typing import Any

from agents.llm import generate_json

_SERVER = "robinhood-trading"
_TOOLS = [f"mcp__{_SERVER}"]
_SYSTEM = (
    "You are a read-only market-data bridge to the Robinhood Trading MCP. Use "
    "ONLY the Robinhood MCP tools to fulfil the request. Call NO "
    "buy/sell/place/submit/cancel/order tools. Do not invent data. After calling "
    "the necessary tools, your FINAL message MUST be the requested STRICT JSON "
    "object and nothing else — no prose, no markdown, and not a tool call."
)

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


class RobinhoodDataProvider:
    """Robinhood market data over a wrapped :class:`DataProvider` fallback."""

    def __init__(self, inner, model: str, *, audit=None):
        self._inner = inner
        self.model = model
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

    async def _ask(self, instruction: str, max_turns: int = 8) -> Any:
        self._log("INFO", "mcp_request",
                  {"server": _SERVER, "kind": "market_data", "instruction": instruction[:300]})
        parsed, raw = await generate_json(
            _SYSTEM, instruction, self.model,
            allowed_tools=_TOOLS, max_turns=max_turns, setting_sources=["local"])
        self._log("INFO", "mcp_response",
                  {"parsed_ok": isinstance(parsed, dict), "len": len(raw or "")})
        return parsed

    # -- prefetch (batched, one pass per run) ------------------------------
    async def prefetch(self, tickers: list[str]) -> None:
        syms = sorted({str(t).upper() for t in tickers if t})
        syms = [s for s in syms if s not in self._prefetched]
        if not syms:
            return
        # Sequential (not concurrent) — the MCP bridge is LLM-mediated; a burst of
        # parallel Agent-SDK queries is neither faster here nor more reliable.
        # NOTE: daily SERIES is deliberately NOT prefetched from Robinhood. A
        # get_equity_historicals result is hundreds of bars per symbol, and the
        # model cannot reliably re-emit that volume as JSON (it returns empty), so
        # series is served by yfinance via the wrapped provider instead. Only the
        # compact quotes + fundamentals go through the MCP bridge.
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
        for _ in range(2):
            if not missing:
                return
            instr = (
                f"Call get_equity_quotes ONCE with symbols={json.dumps(missing)}. For "
                "each symbol report its current price and prior close: use "
                "quote.last_trade_price for price (fall back to "
                "quote.last_non_reg_trade_price), and quote.previous_close (or "
                "results[].close.price) for prev_close. A symbol absent from "
                "results has no tradable instrument — OMIT it from your answer "
                "and do NOT call the tool again to look for it. Return STRICT "
                'JSON only, for the symbols results covers: {"SYM": '
                '{"price": <float>, "prev_close": <float>}, ...}')
            parsed = await self._ask(instr, max_turns=6)
            if not isinstance(parsed, dict):
                # Backend/transport failure — nothing was learned, so one retry
                # of the same request is worthwhile.
                continue
            for sym in list(missing):
                row = parsed.get(sym) or parsed.get(sym.upper())
                price = _f((row or {}).get("price"))
                if price:
                    self._quotes[sym] = {
                        "price": price,
                        "prev_close": _f(row.get("prev_close")) or price}
            unresolved = [s for s in missing if s not in self._quotes]
            if len(unresolved) == len(missing):
                # NOTHING came back keyed by a requested symbol. Being a dict is
                # not proof of a quotes payload — an error body, a wrapped
                # envelope or a truncated response all parse as one, and none of
                # them are evidence that the broker lacks these instruments.
                # Retry instead of blacklisting the batch for the whole process.
                self._log("WARN", "robinhood_quotes_unrecognised_response",
                          {"symbols": missing, "response_keys": sorted(parsed)[:10]})
                continue
            if unresolved:
                # At least one symbol resolved, so this IS a well-formed quotes
                # payload — the symbols it omitted are ones Robinhood has no
                # tradable instrument for. Re-asking cannot change that and only
                # burns another LLM round-trip.
                self._unavailable.update(unresolved)
                self._log("WARN", "robinhood_quotes_unavailable",
                          {"symbols": unresolved})
            return
        self._log("WARN", "robinhood_quotes_prefetch_failed", {"symbols": missing})

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
            instr = (
                f"Call get_equity_fundamentals with symbols={json.dumps(batch)}. For "
                "EVERY symbol return ONLY these numeric/short fields (do NOT include "
                "the description text): sector, industry, market_cap, pe_ratio, "
                "shares_outstanding, week52_high (high_52_weeks), week52_low "
                "(low_52_weeks). Return STRICT JSON only, covering every symbol: "
                '{"SYM": {"sector":<str>,"industry":<str>,"market_cap":<f>,'
                '"pe_ratio":<f>,"shares_outstanding":<f>,"week52_high":<f>,'
                '"week52_low":<f>}, ...}')
            parsed = await self._ask(instr, max_turns=6)
            if isinstance(parsed, dict):
                for sym in batch:
                    row = parsed.get(sym) or parsed.get(sym.upper())
                    if isinstance(row, dict):
                        out[sym] = row
        return out

    async def _fetch_financials(self, syms: list[str]) -> dict[str, list]:
        out: dict[str, list] = {}
        for batch in _batches(syms, 20):     # get_financials: <=20 symbols/call
            instr = (
                f"Call get_financials with symbols={json.dumps(batch)}, "
                'period="quarterly", limit=6. For EVERY symbol return its quarterly '
                "financials MOST-RECENT-FIRST as a list of {revenue, gross_profit, "
                "net_income, net_margin, fiscal_year, fiscal_quarter}. Return STRICT "
                'JSON only, covering every symbol: {"SYM": [{"revenue":<f>,'
                '"gross_profit":<f>,"net_income":<f>,"net_margin":<f>}, ...], ...}')
            parsed = await self._ask(instr, max_turns=6)
            if isinstance(parsed, dict):
                for sym in batch:
                    rows = parsed.get(sym) or parsed.get(sym.upper())
                    if isinstance(rows, list):
                        out[sym] = rows
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
        # Series is intentionally NOT sourced from Robinhood: the MCP bridge is
        # LLM-mediated and cannot reliably re-emit hundreds of OHLCV bars. The
        # wrapped provider serves it from yfinance (free, full lookback).
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

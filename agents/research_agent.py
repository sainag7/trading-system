"""Research Agent — gathers and structures facts for one ticker.

Pipeline position: **Research** -> Analysis -> Decision -> Risk -> Execution.

The agent collects facts; it does NOT give opinions, scores or sizing. Every
number is assembled **deterministically** from :mod:`data.providers` (quote, full
daily history, fundamentals, Alpha Vantage news+sentiment) and the pure
indicators in :mod:`data.indicators`. A field is therefore always either derived
from real data or ``null`` — the agent never invents a figure.

When an LLM backend is available it is used **only** to add short qualitative
text (a news-theme summary and data-quality notes); a guard prevents it from
overwriting any computed number. With no backend the agent still produces the
full schema, so the whole pipeline runs offline without API keys.

Output schema (per ticker):
  { ticker, as_of, fundamentals{...}, technicals{...}, news_sentiment{...},
    macro_context{...}, data_quality{...} }
plus internal ``_sector`` / ``_price`` keys the downstream agents use.
"""
from __future__ import annotations

import asyncio
from typing import Any

from agents.llm import generate_json, load_prompt
from data import indicators

SYSTEM_PROMPT = load_prompt("research_agent")

# How much daily history to request — enough for a 200-day SMA plus a buffer.
DAILY_LOOKBACK = 260

# Sector -> representative ETF (for the "sector ETF trend" macro context).
SECTOR_ETF = {
    "Technology": "XLK",
    "Information Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Healthcare": "XLV",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
}

# Macro indicators most relevant per sector (subset of the FRED snapshot keys).
# Rate-sensitive sectors lean on the curve; all sectors care about growth/rates.
SECTOR_MACRO = {
    "Financials": ["fed_funds", "ten_year"],
    "Real Estate": ["ten_year", "fed_funds"],
    "Utilities": ["ten_year", "fed_funds"],
    "Consumer Discretionary": ["fed_funds", "unemployment", "cpi"],
    "Consumer Staples": ["cpi", "unemployment"],
    "Energy": ["cpi", "ten_year"],
    "Technology": ["ten_year", "fed_funds"],
    "Communication Services": ["ten_year", "fed_funds"],
    "Health Care": ["cpi", "unemployment"],
    "Industrials": ["fed_funds", "unemployment"],
    "Materials": ["cpi", "ten_year"],
}


def _r(v: Any, nd: int = 2) -> float | None:
    return round(v, nd) if isinstance(v, (int, float)) else None


def _label_sentiment(score: float | None) -> str | None:
    """Alpha Vantage's sentiment buckets, applied to an aggregate score."""
    if score is None:
        return None
    if score >= 0.35:
        return "Bullish"
    if score >= 0.15:
        return "Somewhat-Bullish"
    if score > -0.15:
        return "Neutral"
    if score > -0.35:
        return "Somewhat-Bearish"
    return "Bearish"


def _trend(price, sma50, sma200, rsi14) -> tuple[str, int]:
    """Deterministic trend label + 0-100 strength from MAs and RSI."""
    if price and sma50:
        above_50 = price >= sma50
        stack_ok = sma200 is None or sma50 >= sma200
        if above_50 and stack_ok:
            base = 70
        elif not above_50 and (sma200 is None or sma50 <= sma200):
            base = 30
        else:
            base = 50
    else:
        return "sideways", 50
    # Nudge by RSI distance from the midline.
    if isinstance(rsi14, (int, float)):
        base += int((rsi14 - 50) * 0.4)
    strength = max(0, min(100, base))
    trend = "up" if strength >= 60 else "down" if strength <= 40 else "sideways"
    return trend, strength


def _build_technicals(series: list[dict] | None, quote: dict | None, fund: dict) -> dict:
    t: dict[str, Any] = {
        "price": None, "sma50": None, "sma200": None,
        "distance_from_sma50_pct": None, "distance_from_sma200_pct": None,
        "rsi14": None, "macd": {"macd": None, "signal": None, "histogram": None},
        "atr20": None, "atr20_pct": None,
        "week52_high": None, "week52_low": None,
        "distance_from_52w_high_pct": None, "distance_from_52w_low_pct": None,
        "volume": None, "avg_volume_50d": None, "volume_vs_avg": None,
        "trend": "sideways", "trend_strength": 50,
        # Time-series momentum features (percent returns) for the factor model.
        "ret_1m": None, "ret_3m": None, "ret_6m": None, "ret_12m": None,
        "ret_12_1": None, "above_sma200": None,
    }
    if not series:
        price = (quote or {}).get("price")
        t["price"] = _r(price, 4) if price else None
        return t

    closes = [r["close"] for r in series]
    highs = [r["high"] for r in series]
    lows = [r["low"] for r in series]
    vols = [r["volume"] for r in series]
    price = (quote or {}).get("price") or closes[-1]
    t["price"] = _r(price, 4)

    sma50 = indicators.sma(closes, 50)
    sma200 = indicators.sma(closes, 200)
    t["sma50"] = _r(sma50)
    t["sma200"] = _r(sma200)
    if price and sma50:
        t["distance_from_sma50_pct"] = _r((price / sma50 - 1) * 100)
    if price and sma200:
        t["distance_from_sma200_pct"] = _r((price / sma200 - 1) * 100)

    t["rsi14"] = _r(indicators.rsi(closes, 14))
    t["macd"] = indicators.macd(closes)

    atr = indicators.atr(highs, lows, closes, 20)
    t["atr20"] = _r(atr)
    if atr and price:
        t["atr20_pct"] = _r(atr / price * 100)

    # 52-week range: prefer the fundamentals figure, else derive from ~252 bars.
    w52h = fund.get("week52_high") or max(highs[-252:])
    w52l = fund.get("week52_low") or min(lows[-252:])
    t["week52_high"] = _r(w52h)
    t["week52_low"] = _r(w52l)
    if price and w52h:
        t["distance_from_52w_high_pct"] = _r((price / w52h - 1) * 100)
    if price and w52l:
        t["distance_from_52w_low_pct"] = _r((price / w52l - 1) * 100)

    t["volume"] = _r(vols[-1], 0)
    avg_vol = indicators.sma(vols, 50)
    t["avg_volume_50d"] = _r(avg_vol, 0)
    if avg_vol:
        t["volume_vs_avg"] = _r(vols[-1] / avg_vol)

    trend, strength = _trend(price, sma50, sma200, t["rsi14"])
    t["trend"] = trend
    t["trend_strength"] = strength

    # Momentum: percent returns over standard swing/position horizons (~21 trading
    # days per month) plus the classic 12-1 momentum factor.
    t["ret_1m"] = _r(indicators.pct_return(closes, 21))
    t["ret_3m"] = _r(indicators.pct_return(closes, 63))
    t["ret_6m"] = _r(indicators.pct_return(closes, 126))
    t["ret_12m"] = _r(indicators.pct_return(closes, 252))
    t["ret_12_1"] = _r(indicators.momentum_12_1(closes))
    if price and sma200:
        t["above_sma200"] = bool(price >= sma200)
    return t


def _build_fundamentals(fund: dict, price: float | None = None) -> dict:
    """Pass through the provider's fundamentals to the public schema shape."""
    keys = (
        "revenue_ttm", "revenue_growth_yoy", "eps_ttm", "eps_growth_yoy",
        "gross_margin", "operating_margin", "profit_margin", "pe_ratio",
        "ps_ratio", "debt_to_equity", "free_cash_flow", "next_earnings_date",
        "market_cap", "beta", "industry", "name", "description",
        # Forward-looking fields (previously fetched but dropped here): analyst
        # consensus and forward valuation, used by the factor model + briefing.
        "analyst_target", "week52_high", "week52_low",
        "forward_pe", "forward_eps", "recommendation_mean", "num_analysts",
    )
    out = {k: fund.get(k) for k in keys}
    out["sector"] = fund.get("sector", "Unknown")
    # Implied upside to the analyst consensus target (a genuine forward signal).
    tgt = out.get("analyst_target")
    if isinstance(tgt, (int, float)) and isinstance(price, (int, float)) and price > 0:
        out["analyst_upside_pct"] = round((tgt / price - 1) * 100, 2)
    else:
        out["analyst_upside_pct"] = None
    return out


def _build_news(news_raw: dict) -> dict:
    articles = news_raw.get("articles", []) or []
    agg = news_raw.get("aggregate_score")
    headlines = [
        {k: a.get(k) for k in ("title", "source", "time_published", "url",
                               "sentiment_score", "sentiment_label")}
        for a in articles[:5]
    ]
    return {
        "as_of": None,
        "window_days": news_raw.get("window_days", 14),
        "article_count": news_raw.get("article_count", len(articles)),
        "aggregate_score": agg,
        "aggregate_label": _label_sentiment(agg),
        "headlines": headlines,
        "summary": None,
        "source": news_raw.get("source"),
    }


def _sector_trend(provider, sector: str, cache: dict) -> dict:
    """Trend of the sector's ETF (price vs 50-SMA). Memoised per run + on disk."""
    etf = SECTOR_ETF.get(sector)
    result = {"symbol": etf, "trend": None, "change_50d_pct": None,
              "price_vs_sma50_pct": None}
    if not etf:
        return result
    if etf in cache:
        return cache[etf]
    series = provider.get_daily_series(etf, lookback=120)
    if series and len(series) >= 50:
        closes = [r["close"] for r in series]
        price = closes[-1]
        sma50 = indicators.sma(closes, 50)
        if sma50:
            result["price_vs_sma50_pct"] = _r((price / sma50 - 1) * 100)
            result["trend"] = "up" if price >= sma50 else "down"
        if len(closes) > 50:
            result["change_50d_pct"] = _r((closes[-1] / closes[-51] - 1) * 100)
    cache[etf] = result
    return result


def _build_macro(macro: dict, sector: str, sector_trend: dict) -> dict:
    relevant_keys = SECTOR_MACRO.get(sector, ["fed_funds", "ten_year", "cpi"])
    relevant = {k: macro.get(k) for k in relevant_keys if k in macro}
    if not relevant:  # always surface something if the snapshot has anything
        relevant = {k: v for k, v in macro.items() if v is not None}
    return {
        "sector": sector,
        "relevant_indicators": relevant,
        "sector_etf": sector_trend,
        "notes": None,
    }


def _data_quality(technicals: dict, fundamentals: dict, news: dict,
                  fund_src: str | None) -> dict:
    """Flag missing/stale fields so downstream agents can discount the data."""
    missing: list[str] = []
    if technicals.get("sma200") is None:
        missing.append("technicals.sma200")
    if technicals.get("rsi14") is None:
        missing.append("technicals.rsi14")
    for f in ("revenue_ttm", "eps_ttm", "pe_ratio", "gross_margin",
              "debt_to_equity", "free_cash_flow", "next_earnings_date"):
        if fundamentals.get(f) is None:
            missing.append(f"fundamentals.{f}")
    if news.get("aggregate_score") is None:
        missing.append("news_sentiment.aggregate_score")
    return {
        "missing_fields": missing,
        "sources": {
            "technicals": technicals.get("_source", "computed"),
            "fundamentals": fund_src or "none",
            "news": news.get("source") or "none",
        },
        "notes": None,
    }


def _offline_object(ticker, as_of, fundamentals, technicals, news,
                    macro_context, data_quality) -> dict:
    return {
        "ticker": ticker,
        "as_of": as_of,
        "fundamentals": fundamentals,
        "technicals": technicals,
        "news_sentiment": news,
        "macro_context": macro_context,
        "data_quality": data_quality,
    }


async def _enrich_with_llm(obj: dict, model: str) -> str | None:
    """Optionally let the model add qualitative TEXT only (never numbers).

    The model is shown the assembled facts and asked for the full schema, but we
    adopt only its free-text ``news_sentiment.summary`` and ``data_quality.notes``
    so it can never alter a computed figure. Returns the raw model text (or None).
    """
    parsed, raw = await generate_json(SYSTEM_PROMPT, obj, model)
    if isinstance(parsed, dict):
        ns = parsed.get("news_sentiment")
        if isinstance(ns, dict) and ns.get("summary"):
            obj["news_sentiment"]["summary"] = str(ns["summary"])[:600]
        dq = parsed.get("data_quality")
        if isinstance(dq, dict) and dq.get("notes"):
            obj["data_quality"]["notes"] = str(dq["notes"])[:600]
        mc = parsed.get("macro_context")
        if isinstance(mc, dict) and mc.get("notes"):
            obj["macro_context"]["notes"] = str(mc["notes"])[:600]
    return raw


async def research_ticker(
    ticker: str,
    provider,
    model: str,
    macro: dict,
    *,
    sector_trends: dict | None = None,
    llm_enrichment: bool = False,
    db=None,
    run_id: str | None = None,
) -> dict:
    """Produce a structured research object for ``ticker``.

    The blocking provider (yfinance/AV) calls run in worker threads so many
    tickers can be researched concurrently. All numbers are computed
    deterministically; the per-ticker LLM enrichment is optional (off by default
    for speed) and only adds free-text notes — news still drives the scores
    either way via the deterministic aggregate sentiment."""
    ticker = ticker.upper()
    sector_trends = sector_trends if sector_trends is not None else {}

    # Run the network-bound provider calls off the event loop (concurrent across tickers).
    quote = await asyncio.to_thread(provider.get_quote, ticker)
    series = await asyncio.to_thread(provider.get_daily_series, ticker, DAILY_LOOKBACK)
    fund = await asyncio.to_thread(provider.get_fundamentals, ticker)
    news_raw = await asyncio.to_thread(provider.get_news_sentiment, ticker)

    sector = fund.get("sector", "Unknown")
    technicals = _build_technicals(series, quote, fund)
    fundamentals = _build_fundamentals(fund, price=technicals.get("price"))
    news = _build_news(news_raw)
    as_of = series[-1]["date"] if series else (quote or {}).get("as_of")
    news["as_of"] = as_of
    sector_trend = await asyncio.to_thread(_sector_trend, provider, sector, sector_trends)
    macro_context = _build_macro(macro, sector, sector_trend)
    data_quality = _data_quality(technicals, fundamentals, news, fund.get("source"))

    obj = _offline_object(ticker, as_of, fundamentals, technicals, news,
                          macro_context, data_quality)

    # Optional LLM enrichment (text only); deterministic numbers are authoritative.
    raw = await _enrich_with_llm(obj, model) if llm_enrichment else None

    # Internal metadata the downstream agents rely on (not part of the schema).
    obj["_sector"] = sector
    obj["_price"] = technicals.get("price")

    if db and run_id:
        db.log_agent_output(
            run_id, "research", ticker=ticker, model=model,
            input_obj={"quote": quote, "fundamentals": fund,
                       "news_source": news_raw.get("source")},
            output_obj=obj, raw_text=raw,
        )
    return obj


async def run_research(
    universe: list[str], provider, model: str, *, db=None, run_id: str | None = None,
    llm_enrichment: bool = False, concurrency: int = 5,
) -> list[dict]:
    """Research the whole universe CONCURRENTLY (bounded by ``concurrency``).

    Returns objects in the same order as ``universe``. The Alpha Vantage throttle
    becomes approximate under concurrency, but the daily-budget cap + yfinance
    fallback still protect the quota."""
    # A Robinhood-backed provider batch-fetches the whole universe (plus the
    # sector-trend ETFs) up front, so the per-ticker reads below hit its store
    # instead of spending Alpha Vantage quota. No-op for the plain provider.
    if hasattr(provider, "prefetch"):
        etfs = sorted(set(SECTOR_ETF.values()))
        try:
            await provider.prefetch(list(universe) + etfs)
        except Exception:
            pass
    macro = await asyncio.to_thread(provider.get_macro)
    sector_trends: dict = {}  # memoised sector-ETF trends (DiskCache dedupes fetches)
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _one(ticker: str) -> dict:
        async with sem:
            return await research_ticker(
                ticker, provider, model, macro, sector_trends=sector_trends,
                llm_enrichment=llm_enrichment, db=db, run_id=run_id,
            )

    return list(await asyncio.gather(*(_one(t) for t in universe)))

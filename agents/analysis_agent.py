"""Analysis Agent — scores and ranks the universe for swing trades.

Pipeline position: Research -> **Analysis** -> Decision -> Risk -> Execution.

For each ticker it turns the Research Agent's structured facts into three
deterministic 0-100 sub-scores and a config-weighted composite:

  * ``technical_score``   — trend (price vs 50/200 SMA), momentum (RSI/MACD),
                            volatility (ATR), and location (proximity to highs).
  * ``fundamental_score`` — growth, profitability, valuation, balance-sheet health.
  * ``sentiment_score``   — from the aggregated news sentiment.
  * ``composite_score``   — weighted blend (default 40% technical / 35% fundamental
                            / 25% sentiment; weights read from ``config.analysis``).

It also classifies a ``swing_setup`` and lists deterministic ``key_risks``. The
scores are computed in plain Python (not by an LLM) so they are consistent,
auditable, and explained field-by-field in ``score_breakdown``. When an LLM
backend is present it is used **only** to refine the ``one_line_thesis`` and
``key_risks`` text — it can never change a score. The agent makes NO trade
decisions; that is the Decision Agent's job.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from agents.llm import generate_json, load_prompt

SYSTEM_PROMPT = load_prompt("analysis_agent")

# Swing trading leans technical; long-term investing would weight fundamentals.
DEFAULT_WEIGHTS = {"technical": 0.40, "fundamental": 0.35, "sentiment": 0.25}


def _clamp(x: float) -> int:
    return int(max(0, min(100, round(x))))


def _norm_weights(weights: dict | None) -> dict[str, float]:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    vals = {k: max(0.0, float(w.get(k, 0.0))) for k in DEFAULT_WEIGHTS}
    total = sum(vals.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in vals.items()}


# ---------------------------------------------------------------------------
# Technical score (trend / momentum / volatility / location)
# ---------------------------------------------------------------------------
def _trend_component(tech: dict) -> float:
    price, sma50, sma200 = tech.get("price"), tech.get("sma50"), tech.get("sma200")
    if not price or not sma50:
        return 50.0
    if price >= sma50 and (sma200 is None or sma50 >= sma200):
        return 100.0           # full uptrend / golden alignment
    if price >= sma50:
        return 70.0            # above short MA but long MA not yet aligned
    if sma200 is None or price >= sma200:
        return 45.0            # below short MA but longer trend intact (pullback)
    return 15.0                # below both -> downtrend


def _rsi_component(rsi: float | None) -> float:
    if rsi is None:
        return 50.0
    if rsi >= 70:
        return 60.0            # strong but overbought / extended
    if rsi >= 55:
        return 90.0            # healthy momentum sweet spot
    if rsi >= 45:
        return 70.0            # neutral
    if rsi >= 30:
        return 45.0            # weak
    return 50.0                # oversold: weak momentum, reversal potential


def _macd_component(macd: dict | None) -> float:
    hist = (macd or {}).get("histogram")
    if hist is None:
        return 50.0
    return 75.0 if hist > 0 else 35.0


def _momentum_component(tech: dict) -> float:
    return 0.6 * _rsi_component(tech.get("rsi14")) + 0.4 * _macd_component(tech.get("macd"))


def _location_component(tech: dict) -> float:
    d = tech.get("distance_from_52w_high_pct")
    if d is None:
        return 50.0
    if d > 2:
        return 75.0            # new high / extended
    if d >= -3:
        return 90.0            # breakout zone, just under the high
    if d >= -10:
        return 80.0
    if d >= -25:
        return 60.0
    return 35.0                # far below highs -> broken


def _volatility_component(tech: dict) -> float:
    a = tech.get("atr20_pct")
    if a is None:
        return 50.0
    if a < 1:
        return 60.0            # too quiet for a swing move
    if a <= 4:
        return 90.0            # tradable range
    if a <= 7:
        return 70.0
    return 45.0                # very volatile -> hard to size/stop


def _technical_score(tech: dict) -> tuple[int, dict]:
    trend = _trend_component(tech)
    momentum = _momentum_component(tech)
    location = _location_component(tech)
    volatility = _volatility_component(tech)
    score = 0.40 * trend + 0.30 * momentum + 0.15 * location + 0.15 * volatility
    return _clamp(score), {
        "trend": round(trend), "momentum": round(momentum),
        "location": round(location), "volatility": round(volatility),
    }


# ---------------------------------------------------------------------------
# Fundamental score (growth / profitability / valuation / balance sheet)
# ---------------------------------------------------------------------------
def _growth_sub(x: float | None) -> float | None:
    if x is None:
        return None
    if x < 0:
        return 30.0
    if x < 10:
        return 55.0
    if x < 20:
        return 75.0
    if x < 40:
        return 90.0
    return 95.0


def _growth_score(fund: dict) -> float:
    vals = [v for v in (_growth_sub(fund.get("revenue_growth_yoy")),
                        _growth_sub(fund.get("eps_growth_yoy"))) if v is not None]
    return sum(vals) / len(vals) if vals else 50.0


def _margin_sub(x: float | None) -> float | None:
    if x is None:
        return None
    if x < 0:
        return 20.0
    if x < 10:
        return 55.0
    if x < 20:
        return 75.0
    return 90.0


def _profitability_score(fund: dict) -> float:
    vals = [v for v in (_margin_sub(fund.get("profit_margin")),
                        _margin_sub(fund.get("operating_margin"))) if v is not None]
    base = sum(vals) / len(vals) if vals else 50.0
    fcf = fund.get("free_cash_flow")
    if isinstance(fcf, (int, float)):
        base += 5 if fcf > 0 else -10
    return max(0.0, min(100.0, base))


def _valuation_score(fund: dict) -> float:
    def pe_sub(x):
        if x is None:
            return None
        if x <= 0:
            return 40.0        # no earnings -> can't value on P/E
        if x < 15:
            return 90.0
        if x < 25:
            return 80.0
        if x < 40:
            return 65.0
        if x < 60:
            return 50.0
        return 35.0

    def ps_sub(x):
        if x is None:
            return None
        if x < 2:
            return 90.0
        if x < 5:
            return 80.0
        if x < 10:
            return 65.0
        if x < 20:
            return 50.0
        return 35.0

    vals = [v for v in (pe_sub(fund.get("pe_ratio")), ps_sub(fund.get("ps_ratio")))
            if v is not None]
    return sum(vals) / len(vals) if vals else 50.0


def _balance_sheet_score(fund: dict) -> float:
    de = fund.get("debt_to_equity")
    if de is None:
        return 50.0
    if de <= 0.5:
        return 90.0
    if de <= 1:
        return 80.0
    if de <= 2:
        return 65.0
    if de <= 3:
        return 45.0
    return 25.0


def _fundamental_score(fund: dict) -> tuple[int, dict]:
    growth = _growth_score(fund)
    profitability = _profitability_score(fund)
    valuation = _valuation_score(fund)
    balance = _balance_sheet_score(fund)
    score = 0.30 * growth + 0.30 * profitability + 0.25 * valuation + 0.15 * balance
    return _clamp(score), {
        "growth": round(growth), "profitability": round(profitability),
        "valuation": round(valuation), "balance_sheet": round(balance),
    }


# ---------------------------------------------------------------------------
# Sentiment score
# ---------------------------------------------------------------------------
def _sentiment_score(news: dict) -> tuple[int, dict]:
    agg = news.get("aggregate_score")
    count = news.get("article_count", 0) or 0
    if agg is None:
        return 50, {"aggregate_score": None, "article_count": count}
    base = (max(-1.0, min(1.0, agg)) + 1) / 2 * 100
    if count < 3:  # thin coverage -> discount toward neutral
        base = base + (50 - base) * 0.5
    return _clamp(base), {"aggregate_score": agg, "article_count": count}


# ---------------------------------------------------------------------------
# Swing setup + risks + thesis
# ---------------------------------------------------------------------------
def _swing_setup(tech: dict) -> str:
    price, sma50, sma200 = tech.get("price"), tech.get("sma50"), tech.get("sma200")
    rsi = tech.get("rsi14")
    dist_high = tech.get("distance_from_52w_high_pct")
    trend = tech.get("trend")
    if rsi is not None and rsi < 35 and (sma200 is None or (price and price >= sma200)):
        return "oversold-reversal"
    if dist_high is not None and dist_high >= -2 and trend == "up":
        return "breakout"
    if (price and sma50 and price < sma50
            and (sma200 is None or price >= sma200)
            and (rsi is None or rsi >= 40)):
        return "pullback-in-uptrend"
    return "none"


def _days_to_earnings(date_str) -> int | None:
    if not date_str:
        return None
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (d - date.today()).days


def _key_risks(r: dict, fund: dict, tech: dict, news: dict) -> list[str]:
    risks: list[str] = []
    dte = _days_to_earnings(fund.get("next_earnings_date"))
    if dte is not None and 0 <= dte <= 30:
        risks.append(f"earnings in ~{dte}d ({fund.get('next_earnings_date')})")
    pe, ps = fund.get("pe_ratio"), fund.get("ps_ratio")
    if (isinstance(pe, (int, float)) and pe > 40) or (isinstance(ps, (int, float)) and ps > 15):
        risks.append("stretched valuation")
    etf = (r.get("macro_context") or {}).get("sector_etf") or {}
    if etf.get("trend") == "down":
        risks.append(f"weak sector ({etf.get('symbol') or 'ETF'} below 50-SMA)")
    if tech.get("price") and tech.get("sma200") and tech["price"] < tech["sma200"]:
        risks.append("below 200-day SMA")
    if isinstance(tech.get("rsi14"), (int, float)) and tech["rsi14"] > 75:
        risks.append("overbought (RSI>75)")
    if isinstance(tech.get("atr20_pct"), (int, float)) and tech["atr20_pct"] > 8:
        risks.append("high volatility (ATR>8%)")
    de = fund.get("debt_to_equity")
    if isinstance(de, (int, float)) and de > 2:
        risks.append("high leverage (D/E>2)")
    fcf = fund.get("free_cash_flow")
    if isinstance(fcf, (int, float)) and fcf < 0:
        risks.append("negative free cash flow")
    if isinstance(news.get("aggregate_score"), (int, float)) and news["aggregate_score"] < -0.15:
        risks.append("negative news sentiment")
    if len((r.get("data_quality") or {}).get("missing_fields") or []) >= 5:
        risks.append("incomplete data")
    return risks[:5]


def _thesis(ticker: str, setup: str, tech: dict, scores: dict) -> str:
    return (
        f"{ticker}: {setup} in a {tech.get('trend', '?')} trend — "
        f"tech {scores['technical']}/fund {scores['fundamental']}/"
        f"sent {scores['sentiment']} → composite {scores['composite']}"
    )


def _analyze_one(r: dict, weights: dict[str, float]) -> dict:
    tech = r.get("technicals") or {}
    fund = r.get("fundamentals") or {}
    news = r.get("news_sentiment") or {}

    t_score, t_bd = _technical_score(tech)
    f_score, f_bd = _fundamental_score(fund)
    s_score, s_bd = _sentiment_score(news)
    composite = _clamp(
        weights["technical"] * t_score
        + weights["fundamental"] * f_score
        + weights["sentiment"] * s_score
    )
    scores = {"technical": t_score, "fundamental": f_score,
              "sentiment": s_score, "composite": composite}
    setup = _swing_setup(tech)
    # Compact latest-news brief so the LLM thesis/risks can cite current news
    # even when the per-ticker research LLM enrichment is turned off for speed.
    news_brief = {
        "label": news.get("aggregate_label"),
        "score": news.get("aggregate_score"),
        "headlines": [h.get("title") for h in (news.get("headlines") or [])[:3] if h.get("title")],
    }
    return {
        "ticker": r["ticker"],
        "fundamental_score": f_score,
        "technical_score": t_score,
        "sentiment_score": s_score,
        "composite_score": composite,
        "score": composite,  # alias: downstream decision/orchestrator read `score`
        "swing_setup": setup,
        "key_risks": _key_risks(r, fund, tech, news),
        "one_line_thesis": _thesis(r["ticker"], setup, tech, scores),
        "score_breakdown": {
            "technical": t_bd, "fundamental": f_bd, "sentiment": s_bd,
            "weights": {k: round(v, 3) for k, v in weights.items()},
        },
        "_news_brief": news_brief,
        "_sector": r.get("_sector", "Unknown"),
        "_price": r.get("_price"),
    }


async def _enrich_with_llm(items: list[dict], weights: dict, model: str) -> str | None:
    """Let the model refine thesis/risks TEXT only; scores stay deterministic."""
    payload = {
        "instruction": ("Refine one_line_thesis and key_risks for each ticker, drawing on "
                        "`_news_brief` (latest news label + recent headlines) so the thesis "
                        "reflects current news. Do NOT change any *_score field — they are "
                        "authoritative."),
        "weights": weights,
        "analysis": items,
    }
    parsed, raw = await generate_json(SYSTEM_PROMPT, payload, model)
    if isinstance(parsed, list):
        by_ticker = {i.get("ticker"): i for i in parsed if isinstance(i, dict)}
        for item in items:
            llm = by_ticker.get(item["ticker"])
            if not llm:
                continue
            thesis = llm.get("one_line_thesis")
            if isinstance(thesis, str) and thesis.strip():
                item["one_line_thesis"] = thesis.strip()[:240]
            risks = llm.get("key_risks")
            if isinstance(risks, list) and risks and all(isinstance(x, str) for x in risks):
                item["key_risks"] = [x[:120] for x in risks[:5]]
    return raw


async def run_analysis(
    research: list[dict], model: str, strategy: dict,
    weights: dict | None = None, macro: dict | None = None,
    *, db=None, run_id: str | None = None,
) -> list[dict]:
    """Score and rank the universe. Returns analysis objects, best composite first."""
    w = _norm_weights(weights)
    items = [_analyze_one(r, w) for r in research if r.get("ticker")]
    items.sort(key=lambda x: x["composite_score"], reverse=True)
    for i, item in enumerate(items, start=1):
        item["rank"] = i

    raw = await _enrich_with_llm(items, w, model)

    if db and run_id:
        db.log_agent_output(
            run_id, "analysis", model=model,
            input_obj={"weights": w, "research_count": len(research)},
            output_obj=items, raw_text=raw,
        )
    return items

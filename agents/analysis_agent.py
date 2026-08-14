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

import statistics
from datetime import date, datetime
from typing import Any

from agents.llm import generate_json, load_prompt

SYSTEM_PROMPT = load_prompt("analysis_agent")

# Legacy blend (methodology="legacy"): technical / fundamental / sentiment.
DEFAULT_WEIGHTS = {"technical": 0.40, "fundamental": 0.35, "sentiment": 0.25}

# Factor model (methodology="momentum_quality", the default). Evidence-based
# factors, momentum + quality tilted; z-scored across the universe for a
# cross-sectional tilt on top of the absolute score.
FACTOR_NAMES = ("momentum", "quality", "value", "growth", "sentiment")
DEFAULT_FACTOR_WEIGHTS = {
    "momentum": 0.30, "quality": 0.25, "value": 0.15,
    "growth": 0.15, "sentiment": 0.15,
}
# How strongly the cross-sectional rank tilts the absolute composite. The
# absolute score keeps discipline (never buy a broken name just because it is the
# "least bad" in a weak universe); the relative tilt prefers the strongest names.
CROSS_SECTIONAL_TILT = 0.30


def _clamp(x: float) -> int:
    return int(max(0, min(100, round(x))))


def _smooth(x: float | None, lo: float, hi: float,
            out_lo: float = 0.0, out_hi: float = 100.0) -> float | None:
    """Linear, clamped map of ``x`` in [lo, hi] to [out_lo, out_hi]. Continuous —
    replaces the coarse step ladders so a P/E of 14.99 vs 15.01 no longer jumps
    the score by 10. ``None`` in -> ``None`` out (the field is simply absent)."""
    if x is None:
        return None
    if hi == lo:
        return (out_lo + out_hi) / 2
    t = max(0.0, min(1.0, (x - lo) / (hi - lo)))
    return out_lo + t * (out_hi - out_lo)


def _blend(pairs: list[tuple[float, float | None]]) -> float:
    """Weighted mean over the non-None components; 50 (neutral) if all absent."""
    num = sum(w * v for w, v in pairs if v is not None)
    den = sum(w for w, v in pairs if v is not None)
    return num / den if den > 0 else 50.0


def _norm_factor_weights(weights: dict | None) -> dict[str, float]:
    w = {**DEFAULT_FACTOR_WEIGHTS, **(weights or {})}
    vals = {k: max(0.0, float(w.get(k, 0.0))) for k in FACTOR_NAMES}
    total = sum(vals.values())
    if total <= 0:
        return dict(DEFAULT_FACTOR_WEIGHTS)
    return {k: v / total for k, v in vals.items()}


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


# ---------------------------------------------------------------------------
# Factor model (momentum / quality / value / growth / sentiment)
# ---------------------------------------------------------------------------
def _trend_smooth(tech: dict) -> float | None:
    """Continuous trend from distance above/below the 50 and 200 SMAs."""
    parts = []
    d50 = tech.get("distance_from_sma50_pct")
    d200 = tech.get("distance_from_sma200_pct")
    if d50 is not None:
        parts.append((0.5, _smooth(d50, -15, 15, 20, 95)))
    if d200 is not None:
        parts.append((0.5, _smooth(d200, -20, 20, 15, 95)))
    return _blend(parts) if parts else None


def _location_smooth(tech: dict) -> float | None:
    """Proximity to the 52-week high — near highs is momentum-positive."""
    d = tech.get("distance_from_52w_high_pct")
    return _smooth(d, -40, 0, 30, 92) if d is not None else None


def _momentum_factor(tech: dict) -> float:
    """Time-series momentum: 12-1 return (primary), 3/6m returns, trend, location."""
    return _blend([
        (0.35, _smooth(tech.get("ret_12_1"), -30, 50, 10, 95)),
        (0.15, _smooth(tech.get("ret_3m"), -20, 30, 20, 90)),
        (0.15, _smooth(tech.get("ret_6m"), -25, 40, 20, 92)),
        (0.20, _trend_smooth(tech)),
        (0.15, _location_smooth(tech)),
    ])


def _quality_factor(fund: dict) -> float:
    """Profitability, cash generation, balance-sheet strength."""
    de = fund.get("debt_to_equity")
    fcf = fund.get("free_cash_flow")
    return _blend([
        (0.30, _smooth(fund.get("profit_margin"), 0, 30, 30, 95)),
        (0.20, _smooth(fund.get("operating_margin"), 0, 35, 30, 95)),
        (0.20, _smooth(fund.get("gross_margin"), 20, 70, 40, 95)),
        (0.15, _smooth(de, 0, 3, 95, 25) if de is not None else None),   # lower better
        (0.15, (85.0 if fcf > 0 else 25.0) if isinstance(fcf, (int, float)) else None),
    ])


def _value_factor(fund: dict) -> float:
    """Cheapness (forward P/E preferred) + implied upside to the analyst target."""
    pe = fund.get("forward_pe") or fund.get("pe_ratio")
    if pe is None:
        pe_s = None
    elif pe <= 0:
        pe_s = 45.0                       # no earnings — can't value on P/E
    else:
        pe_s = _smooth(pe, 10, 60, 90, 30)  # lower P/E scores higher
    return _blend([
        (0.40, pe_s),
        (0.25, _smooth(fund.get("ps_ratio"), 1, 20, 88, 30)),
        (0.35, _smooth(fund.get("analyst_upside_pct"), -20, 40, 20, 92)),
    ])


def _growth_factor(fund: dict) -> float:
    return _blend([
        (0.5, _smooth(fund.get("revenue_growth_yoy"), -10, 40, 25, 92)),
        (0.5, _smooth(fund.get("eps_growth_yoy"), -10, 50, 25, 92)),
    ])


def _sentiment_factor(news: dict, fund: dict) -> float:
    """News sentiment + analyst recommendation consensus."""
    agg = news.get("aggregate_score")
    count = news.get("article_count", 0) or 0
    news_s = None
    if agg is not None:
        news_s = (max(-1.0, min(1.0, agg)) + 1) / 2 * 100
        if count < 3:                     # thin coverage -> shrink toward neutral
            news_s = news_s + (50 - news_s) * 0.5
    rec = fund.get("recommendation_mean")   # 1=strong buy .. 5=sell
    rec_s = _smooth(rec, 1, 5, 92, 20) if rec is not None else None
    return _blend([(0.6, news_s), (0.4, rec_s)])


def _lowvol_mult(tech: dict) -> float:
    """Down-weight names with extreme ATR% (hard to size/hold). 1.0 up to ~5%,
    tapering to 0.85 by ~14% — a mild haircut, not a hard filter."""
    a = tech.get("atr20_pct")
    if a is None or a <= 5:
        return 1.0
    t = min(1.0, (a - 5) / 9)
    return 1.0 - 0.15 * t


def _compute_factors(tech: dict, fund: dict, news: dict) -> dict[str, float]:
    return {
        "momentum": _momentum_factor(tech),
        "quality": _quality_factor(fund),
        "value": _value_factor(fund),
        "growth": _growth_factor(fund),
        "sentiment": _sentiment_factor(news, fund),
    }


def _analyze_one(r: dict, weights: dict[str, float],
                 factor_weights: dict[str, float], methodology: str) -> dict:
    tech = r.get("technicals") or {}
    fund = r.get("fundamentals") or {}
    news = r.get("news_sentiment") or {}

    factors = _compute_factors(tech, fund, news)
    if methodology == "legacy":
        t_score, t_bd = _technical_score(tech)
        f_score, f_bd = _fundamental_score(fund)
        s_score, s_bd = _sentiment_score(news)
        composite_abs = _clamp(
            weights["technical"] * t_score
            + weights["fundamental"] * f_score
            + weights["sentiment"] * s_score)
    else:
        # Factor model. Legacy sub-scores are kept as display views so the
        # dashboard / briefing still show a technical / fundamental / sentiment
        # breakdown, but the composite is the factor blend, vol-adjusted.
        t_score = _clamp(factors["momentum"])
        f_score = _clamp((factors["quality"] + factors["value"] + factors["growth"]) / 3)
        s_score = _clamp(factors["sentiment"])
        t_bd = {"momentum": round(factors["momentum"]), "trend": round(_trend_smooth(tech) or 50)}
        f_bd = {"quality": round(factors["quality"]), "value": round(factors["value"]),
                "growth": round(factors["growth"])}
        s_bd = {"sentiment": round(factors["sentiment"])}
        raw = sum(factor_weights[k] * factors[k] for k in FACTOR_NAMES)
        composite_abs = _clamp(raw * _lowvol_mult(tech))

    composite = composite_abs
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
        "factor_scores": {k: round(v) for k, v in factors.items()},
        "score_breakdown": {
            "technical": t_bd, "fundamental": f_bd, "sentiment": s_bd,
            "factors": {k: round(v) for k, v in factors.items()},
            "methodology": methodology,
            "weights": {k: round(v, 3) for k, v in weights.items()},
        },
        # Private carry-through for the cross-sectional pass in run_analysis.
        "_factors": factors,
        "_composite_abs": composite_abs,
        "_news_brief": news_brief,
        "_sector": r.get("_sector", "Unknown"),
        "_price": r.get("_price"),
        "_atr": tech.get("atr20"),
        "_atr_pct": tech.get("atr20_pct"),
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


def _apply_cross_sectional(items: list[dict], fw: dict[str, float]) -> None:
    """Z-score each factor across the universe and tilt the absolute composite
    toward the strongest names. Percentile ~ 50 + 20·z (±2.5σ spans 0..100). The
    absolute score already enforced discipline; this only reorders/nudges."""
    pct: dict[str, dict[str, float]] = {it["ticker"]: {} for it in items}
    for f in FACTOR_NAMES:
        vals = [it["_factors"][f] for it in items]
        mean = statistics.fmean(vals)
        sd = statistics.pstdev(vals) or 1.0
        for it in items:
            z = (it["_factors"][f] - mean) / sd
            pct[it["ticker"]][f] = max(0.0, min(100.0, 50 + 20 * z))
    for it in items:
        rel = sum(fw[f] * (pct[it["ticker"]][f] - 50) for f in FACTOR_NAMES)
        it["composite_score"] = _clamp(it["_composite_abs"] + CROSS_SECTIONAL_TILT * rel)
        it["score"] = it["composite_score"]
        it["score_breakdown"]["factor_percentiles"] = {
            f: round(pct[it["ticker"]][f]) for f in FACTOR_NAMES}


async def run_analysis(
    research: list[dict], model: str, strategy: dict,
    weights: dict | None = None, macro: dict | None = None,
    factor_weights: dict | None = None,
    *, db=None, run_id: str | None = None,
) -> list[dict]:
    """Score and rank the universe. Returns analysis objects, best composite first."""
    w = _norm_weights(weights)
    fw = _norm_factor_weights(factor_weights)
    methodology = (strategy or {}).get("methodology", "momentum_quality")
    items = [_analyze_one(r, w, fw, methodology) for r in research if r.get("ticker")]

    # Cross-sectional tilt needs a few names to be meaningful; single-ticker
    # explain runs (and legacy mode) keep the pure absolute score.
    if methodology != "legacy" and len(items) >= 3:
        _apply_cross_sectional(items, fw)

    items.sort(key=lambda x: x["composite_score"], reverse=True)
    for i, item in enumerate(items, start=1):
        item["rank"] = i
    for item in items:  # drop private carry-throughs before persist/return
        item.pop("_factors", None)
        item.pop("_composite_abs", None)

    raw = await _enrich_with_llm(items, w, model)

    if db and run_id:
        db.log_agent_output(
            run_id, "analysis", model=model,
            input_obj={"weights": w, "factor_weights": fw, "methodology": methodology,
                       "research_count": len(research)},
            output_obj=items, raw_text=raw,
        )
    return items

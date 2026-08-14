"""Explain Agent — single-ticker deep-research briefing.

Used only by ``--mode explain``. Everything numeric — returns, moving averages,
support/resistance, ATR-derived scenario levels — is computed HERE,
deterministically, from provider data (via :mod:`data.indicators`; no new
indicator math). The one LLM call only writes narrative over that payload.

Anti-hallucination is enforced in code, not just in the prompt:
  * ``why_it_moved.drivers`` are post-filtered — any driver citing a headline
    that is not in the fetched news payload is dropped.
  * If the news payload has NO headlines, ``why_it_moved`` is forced to
    "no clear catalyst found in available news" regardless of the model output.
  * Fields the providers didn't return are reported as unavailable, and the
    company snapshot (name/description) comes from provider data only.

The offline fallback builds the same strict JSON schema from the payload with
templated text, so explain mode runs end-to-end without any LLM/network.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from agents.llm import generate_json, load_prompt
from agents.decision_agent import _plan_levels
from data import indicators

SYSTEM_PROMPT = load_prompt("explain_agent")

NO_CATALYST = "no clear catalyst found in available news"
DISCLAIMER = ("Forward lean, expected value and scenario probabilities are rough "
              "estimates from momentum, valuation and analyst consensus — wide "
              "error bars, not guarantees, not financial advice.")


def _r(v, nd: int = 2):
    return round(v, nd) if isinstance(v, (int, float)) else None


def _pct_return(closes: list[float], n_back: int) -> float | None:
    """% return over the last ``n_back`` trading days (needs n_back+1 closes)."""
    if len(closes) <= n_back or not closes[-(n_back + 1)]:
        return None
    return _r((closes[-1] / closes[-(n_back + 1)] - 1) * 100)


def _ytd_return(series: list[dict]) -> float | None:
    if not series:
        return None
    year = str(series[-1].get("date", ""))[:4]
    first = next((row for row in series if str(row.get("date", "")).startswith(year)), None)
    if not first or not first.get("close"):
        return None
    return _r((series[-1]["close"] / first["close"] - 1) * 100)


def _returns(series: list[dict]) -> dict:
    closes = [row["close"] for row in series] if series else []
    return {
        "d1": _pct_return(closes, 1),
        "d5": _pct_return(closes, 5),
        "m1": _pct_return(closes, 21),
        "m3": _pct_return(closes, 63),
        "ytd": _ytd_return(series),
    }


def _levels(series: list[dict], price: float | None, atr: float | None) -> dict:
    """Deterministic support/resistance + ATR context for the scenarios."""
    if not series or not price:
        return {"support": None, "resistance": None, "high_3m": None, "low_3m": None,
                "high_20d": None, "low_20d": None, "atr": _r(atr)}
    highs = [row["high"] for row in series]
    lows = [row["low"] for row in series]
    h20, l20 = max(highs[-20:]), min(lows[-20:])
    h3m, l3m = max(highs[-63:]), min(lows[-63:])
    return {
        "high_20d": _r(h20), "low_20d": _r(l20),
        "high_3m": _r(h3m), "low_3m": _r(l3m),
        "resistance": _r(h20 if h20 < h3m else h3m),   # nearest meaningful ceiling
        "support": _r(l20 if l20 > l3m else l3m),      # nearest meaningful floor
        "atr": _r(atr),
    }


def _scenario_probs(tech: dict) -> dict:
    """Rough probabilities for bull/base/bear, tilted by trend + 3-month momentum.
    Deliberately coarse — a lean, not a precise forecast. Base case anchors the
    middle; a confirmed up/down-trend shifts weight toward bull/bear respectively."""
    bull, base, bear = 0.33, 0.34, 0.33
    above200 = tech.get("above_sma200")
    if above200 is True:
        bull, bear = bull + 0.10, bear - 0.10
    elif above200 is False:
        bear, bull = bear + 0.10, bull - 0.10
    ret3 = tech.get("ret_3m")
    if isinstance(ret3, (int, float)):
        if ret3 > 5:
            bull, bear = bull + 0.05, bear - 0.05
        elif ret3 < -5:
            bear, bull = bear + 0.05, bull - 0.05
    vals = {"bull": max(0.05, bull), "base": max(0.05, base), "bear": max(0.05, bear)}
    total = sum(vals.values())
    return {k: round(v / total, 2) for k, v in vals.items()}


def _scenarios(price: float | None, levels: dict, atr: float | None,
               tech: dict | None = None) -> list[dict]:
    """Bull/base/bear conditional levels, each with a rough probability so the
    reader gets a directional lean and an expected value — not just 'if X then Y'."""
    if not price:
        return []
    atr = atr or max(price * 0.02, 0.01)  # conservative fallback band
    res = levels.get("resistance") or _r(price + 1.5 * atr)
    sup = levels.get("support") or _r(price - 1.5 * atr)
    probs = _scenario_probs(tech or {})
    return [
        {
            "name": "bull", "probability": probs["bull"],
            "condition": f"breaks and holds above resistance ${res:,.2f}",
            "target_level": _r(res + 2 * atr),
            "confirm": f"daily close above ${res:,.2f} on above-average volume",
            "invalidate": f"rejection at ${res:,.2f} and close back below ${_r(price - atr):,.2f}",
        },
        {
            "name": "base", "probability": probs["base"],
            "condition": f"holds the ${sup:,.2f}–${res:,.2f} range",
            "target_level": _r((sup + res) / 2),
            "confirm": f"closes remaining between ${sup:,.2f} and ${res:,.2f}",
            "invalidate": f"daily close outside the range in either direction",
        },
        {
            "name": "bear", "probability": probs["bear"],
            "condition": f"loses support at ${sup:,.2f}",
            "target_level": _r(sup - 2 * atr),
            "confirm": f"daily close below ${sup:,.2f}, especially on volume",
            "invalidate": f"reclaim of ${sup:,.2f} within 1–2 sessions",
        },
    ]


def _expected_move_pct(atr_pct: float | None, days: int | None) -> float | None:
    """Vol-scaled expected move over ``days`` sessions (ATR% · sqrt(days)). A
    volatility band, not a directional forecast."""
    if not isinstance(atr_pct, (int, float)) or not days or days <= 0:
        return None
    return round(atr_pct * (days ** 0.5), 1)


def _expected_value_pct(price: float | None, scenarios: list[dict]) -> float | None:
    """Probability-weighted expected return across the scenarios (rough estimate)."""
    if not price or not scenarios:
        return None
    ev = 0.0
    for s in scenarios:
        tgt, p = s.get("target_level"), s.get("probability")
        if not isinstance(tgt, (int, float)) or not isinstance(p, (int, float)):
            return None
        ev += p * (tgt / price - 1) * 100
    return round(ev, 1)


def _forward_view(price, tech: dict, fund: dict, plan: dict | None) -> dict:
    """A clearly-labeled forward lean from data the system actually has: analyst
    consensus upside, recent momentum, trend regime, and the plan's reward:risk.
    An estimate with wide error bars — never a promise."""
    up = fund.get("analyst_upside_pct")
    ret3 = tech.get("ret_3m")
    above200 = tech.get("above_sma200")

    votes, n = 0, 0
    for v in (above200, (ret3 or 0) > 0 if ret3 is not None else None):
        if v is not None:
            votes += 1 if v else -1
            n += 1
    if isinstance(up, (int, float)):
        votes += 1 if up > 5 else (-1 if up < -5 else 0)
        n += 1
    lean = "neutral"
    if n:
        if votes >= 2:
            lean = "bullish"
        elif votes <= -2:
            lean = "bearish"

    parts = [(0.6, up), (0.4, ret3)]
    er_num = sum(w * v for w, v in parts if isinstance(v, (int, float)))
    er_den = sum(w for w, v in parts if isinstance(v, (int, float)))
    expected_return = round(er_num / er_den, 1) if er_den else None
    return {
        "lean": lean,
        "expected_return_pct": expected_return,   # rough blend of analyst upside + momentum
        "analyst_upside_pct": up,
        "risk_reward_r": (plan or {}).get("risk_reward_r"),
    }


def _days_until(date_str) -> int | None:
    try:
        return (date.fromisoformat(str(date_str)[:10])
                - datetime.now(timezone.utc).date()).days
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Verdict (deterministic buy/sell recommendation; the LLM only narrates it)
# ---------------------------------------------------------------------------
def _verdict(price, composite, held: bool | None, strategy: dict, *,
             event_risk: bool = False, atr=None,
             atr_pct=None, news_score=None, article_count: int = 0,
             missing_fields: int = 0) -> dict:
    """Map the composite score + position context onto an explicit action using
    the configured strategy thresholds — the same numbers recommend mode trades
    on. Pure & unit-testable; advice only (never writes a plan/order)."""
    buy_th = float(strategy.get("min_score_to_buy", 65))
    add_th = float(strategy.get("min_score_to_add", 70))
    trim_th = float(strategy.get("trim_below_score", 45))
    exit_th = float(strategy.get("exit_below_score", 35))
    score = float(composite) if isinstance(composite, (int, float)) else 50.0

    reasons: list[str] = []
    if held is True:
        if score <= exit_th:
            action, threshold = "sell", exit_th
            reasons.append(f"held and composite {score:.0f} ≤ exit threshold {exit_th:.0f}")
        elif score <= trim_th:
            action, threshold = "trim", trim_th
            reasons.append(f"held and composite {score:.0f} ≤ trim threshold {trim_th:.0f}")
        elif score >= add_th:
            action, threshold = "add", add_th
            reasons.append(f"held and composite {score:.0f} ≥ add threshold {add_th:.0f}")
        else:
            action, threshold = "hold", None
            reasons.append(f"held; composite {score:.0f} sits between trim {trim_th:.0f} "
                           f"and add {add_th:.0f}")
    else:
        if held is None:
            reasons.append("no account connected — assuming not held")
        if score >= buy_th:
            action, threshold = "buy", buy_th
            reasons.append(f"composite {score:.0f} ≥ buy threshold {buy_th:.0f}")
        elif score <= exit_th:
            action, threshold = "avoid", exit_th
            reasons.append(f"composite {score:.0f} ≤ weak-name threshold {exit_th:.0f}")
        else:
            action, threshold = "watch", buy_th
            reasons.append(f"composite {score:.0f} is {buy_th - score:.0f} points below "
                           f"the buy threshold {buy_th:.0f} — not actionable yet")

    # Confidence: distance from the deciding threshold, dampened by known risks.
    if action in ("hold", "watch"):
        conf = 50.0
    else:
        conf = min(90.0, 50.0 + 2.0 * abs(score - (threshold or score)))
    if event_risk:
        conf -= 10
        reasons.append("earnings inside the swing horizon (event risk) lowers conviction")
    if isinstance(atr_pct, (int, float)) and atr_pct > 6:
        conf -= 5
        reasons.append(f"high volatility (ATR {atr_pct:.1f}%)")
    if isinstance(news_score, (int, float)) and news_score <= -0.15:
        conf -= 5
        reasons.append("negative news sentiment")
    if article_count < 3:
        conf -= 3
        reasons.append("thin news coverage")
    if missing_fields >= 5:
        conf -= 7
        reasons.append("incomplete data")
    conf = int(max(5, min(95, round(conf))))

    plan = None
    if action in ("buy", "add") and isinstance(price, (int, float)) and price > 0:
        stop, take, until = _plan_levels(price, atr, strategy)
        rr = round((take - price) / (price - stop), 2) if (take and stop and price > stop) else None
        plan = {"stop": stop, "target": take, "max_hold_until": until,
                "risk_reward_r": rr,
                "note": "informational levels only — nothing is planned or ordered"}

    return {
        "action": action,
        "confidence": conf,
        "based_on": {"composite": _r(score, 0), "buy_threshold": buy_th,
                     "add_threshold": add_th, "trim_threshold": trim_th,
                     "exit_threshold": exit_th, "held": held},
        "reasons": reasons,
        "suggested_plan": plan,
    }


# ---------------------------------------------------------------------------
# Payload assembly (deterministic)
# ---------------------------------------------------------------------------
def build_payload(ticker: str, research: dict, analysis: dict, series: list[dict],
                  strategy: dict, position: dict | None) -> dict:
    tech = research.get("technicals") or {}
    fund = research.get("fundamentals") or {}
    news = research.get("news_sentiment") or {}
    macro = research.get("macro_context") or {}
    price = tech.get("price")

    closes = [row["close"] for row in series] if series else []
    sma20 = _r(indicators.sma(closes, 20))
    atr = tech.get("atr20")
    levels = _levels(series, price, atr)

    horizon = int(strategy.get("holding_period_days_max", 120))
    edate = fund.get("next_earnings_date")
    edays = _days_until(edate)
    event_risk = bool(edays is not None and 0 <= edays <= horizon)

    held = position.get("held") if isinstance(position, dict) else None
    verdict_seed = _verdict(
        price, analysis.get("composite_score"), held, strategy,
        event_risk=event_risk, atr=atr, atr_pct=tech.get("atr20_pct"),
        news_score=news.get("aggregate_score"),
        article_count=int(news.get("article_count", 0) or 0),
        missing_fields=len((research.get("data_quality") or {}).get("missing_fields") or []),
    )
    # Forward lean (analyst upside + momentum + trend) and probability-weighted
    # expected value across the scenarios. Estimates with wide error bars, merged
    # onto the verdict so the briefing states a direction, not just levels.
    scenario_levels = _scenarios(price, levels, atr, tech)
    verdict_seed.update(_forward_view(price, tech, fund, verdict_seed.get("suggested_plan")))
    verdict_seed["expected_value_pct"] = _expected_value_pct(price, scenario_levels)
    expected_move = _expected_move_pct(tech.get("atr20_pct"), edays)

    return {
        "ticker": ticker,
        "as_of": research.get("as_of"),
        "snapshot": {
            "name": fund.get("name"),
            "description": fund.get("description"),
            "sector": fund.get("sector", "Unknown"),
            "industry": fund.get("industry"),
            "price": price,
            "market_cap": fund.get("market_cap"),
        },
        "returns": _returns(series),
        "technicals": {
            "sma20": sma20,
            "sma50": tech.get("sma50"), "sma200": tech.get("sma200"),
            "vs_sma20_pct": _r((price / sma20 - 1) * 100) if price and sma20 else None,
            "vs_sma50_pct": tech.get("distance_from_sma50_pct"),
            "vs_sma200_pct": tech.get("distance_from_sma200_pct"),
            "rsi14": tech.get("rsi14"), "macd": tech.get("macd"),
            "atr20": tech.get("atr20"), "atr20_pct": tech.get("atr20_pct"),
            "volume_vs_avg": tech.get("volume_vs_avg"), "trend": tech.get("trend"),
            "distance_from_52w_high_pct": tech.get("distance_from_52w_high_pct"),
            # Momentum (time-series) — the primary factor in the new methodology.
            "ret_1m": tech.get("ret_1m"), "ret_3m": tech.get("ret_3m"),
            "ret_6m": tech.get("ret_6m"), "ret_12m": tech.get("ret_12m"),
            "ret_12_1": tech.get("ret_12_1"), "above_sma200": tech.get("above_sma200"),
        },
        "levels": levels,
        "scenario_levels": scenario_levels,
        "news": {
            "aggregate_score": news.get("aggregate_score"),
            "aggregate_label": news.get("aggregate_label"),
            "article_count": news.get("article_count", 0),
            "headlines": [
                {"title": h.get("title"), "date": h.get("time_published"),
                 "sentiment": h.get("sentiment_label"), "score": h.get("sentiment_score")}
                for h in (news.get("headlines") or []) if h.get("title")
            ],
        },
        "macro": macro,
        "earnings": {
            "next_date": edate,
            "days_until": edays,
            "event_risk": event_risk,
            "horizon_days": horizon,
            "expected_move_pct": expected_move,   # vol-scaled band into the report
        },
        "verdict_seed": verdict_seed,
        "fundamentals": {k: fund.get(k) for k in (
            "pe_ratio", "ps_ratio", "eps_ttm", "eps_growth_yoy", "revenue_ttm",
            "revenue_growth_yoy", "gross_margin", "operating_margin",
            "debt_to_equity", "free_cash_flow", "beta",
            # Forward-looking: analyst consensus + forward valuation.
            "analyst_target", "analyst_upside_pct", "forward_pe", "forward_eps",
            "recommendation_mean", "num_analysts")},
        "analysis": {
            "composite_score": analysis.get("composite_score"),
            "swing_setup": analysis.get("swing_setup"),
            "key_risks": analysis.get("key_risks"),
            "one_line_thesis": analysis.get("one_line_thesis"),
        },
        "position": position,   # None when no account connected
        "data_quality": research.get("data_quality"),
    }


# ---------------------------------------------------------------------------
# Anti-hallucination guards (applied to LLM output)
# ---------------------------------------------------------------------------
def _enforce_grounding(report: dict, payload: dict) -> dict:
    """Force citation integrity: drivers must cite fetched headlines; empty news
    payload means NO catalyst narrative, full stop."""
    titles = [str(h.get("title", "")).lower()
              for h in payload.get("news", {}).get("headlines", [])]
    wim = report.get("why_it_moved")
    if not isinstance(wim, dict):
        wim = {}
    if not titles:
        report["why_it_moved"] = {"summary": NO_CATALYST, "drivers": [],
                                  "catalyst_found": False}
        return report
    drivers = []
    for d in (wim.get("drivers") or []):
        cited = str(d.get("headline", "")).lower()
        if cited and any(cited[:60] in t or t[:60] in cited for t in titles):
            drivers.append(d)
    wim["drivers"] = drivers
    if not drivers:
        wim["summary"] = NO_CATALYST
        wim["catalyst_found"] = False
    else:
        wim.setdefault("catalyst_found", True)
    report["why_it_moved"] = wim
    return report


# ---------------------------------------------------------------------------
# Offline fallback (same schema, templated text; keeps checks hermetic)
# ---------------------------------------------------------------------------
def _offline_report(payload: dict) -> dict:
    snap = payload["snapshot"]
    news = payload["news"]
    heads = news.get("headlines") or []
    if heads:
        wim = {
            "summary": (f"Recent coverage is {news.get('aggregate_label') or 'mixed'} "
                        f"({news.get('article_count', 0)} articles); see cited headlines."),
            "drivers": [{"claim": f"news flow: {h['title']}", "headline": h["title"],
                         "date": h.get("date")} for h in heads[:3]],
            "catalyst_found": True,
        }
    else:
        wim = {"summary": NO_CATALYST, "drivers": [], "catalyst_found": False}
    e = payload["earnings"]
    risks = list(payload["analysis"].get("key_risks") or [])
    if e.get("event_risk") and not any("earnings" in str(r).lower() for r in risks):
        risks.insert(0, f"earnings in ~{e['days_until']}d ({e['next_date']}) — event risk")
    seed = payload.get("verdict_seed") or {}
    fwd = []
    if seed.get("lean"):
        fwd.append(f"lean {seed['lean']}")
    if isinstance(seed.get("expected_return_pct"), (int, float)):
        fwd.append(f"~{seed['expected_return_pct']:+.0f}% est. return")
    if seed.get("risk_reward_r"):
        fwd.append(f"{seed['risk_reward_r']:.1f}R reward:risk")
    verdict = {**seed,
               "rationale": (f"{str(seed.get('action', 'watch')).upper()} — "
                             + "; ".join(list(seed.get("reasons", [])[:2]) + fwd))}
    return {
        "ticker": payload["ticker"],
        "as_of": payload["as_of"],
        "verdict": verdict,
        "snapshot": {**snap,
                     "summary": snap.get("description") or "company description unavailable"},
        "price_action": {
            "returns": payload["returns"],
            "summary": (f"trend {payload['technicals'].get('trend', 'unknown')}; "
                        f"RSI {payload['technicals'].get('rsi14', 'unavailable')}"),
        },
        "why_it_moved": wim,
        "earnings": {**e, "note": ("inside the swing horizon — event risk"
                                   if e.get("event_risk") else
                                   ("no date available" if not e.get("next_date")
                                    else "outside the swing horizon"))},
        "fundamentals": {**payload["fundamentals"],
                         "note": "fields not returned by providers are null/unavailable"},
        "scenarios": [
            {**s, "narrative": (f"{s['name']} (~{int(round(s.get('probability', 0) * 100))}%): "
                                f"if {s['condition']}, toward ${s['target_level']:,.2f}.")}
            for s in payload["scenario_levels"]
        ],
        "risks": risks or ["insufficient data to enumerate risks"],
        "watch_next": [w for w in [
            f"earnings on {e['next_date']}" if e.get("next_date") else None,
            "reaction at the scenario levels above",
            "news flow / sentiment shift",
        ] if w],
        "position": payload.get("position"),
        "analysis": payload["analysis"],
        "data_quality": payload.get("data_quality"),
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def run_explain(payload: dict, model: str, *, db=None,
                      run_id: str | None = None) -> dict:
    """One LLM call over the deterministic payload; guarded; persisted."""
    parsed, raw = await generate_json(SYSTEM_PROMPT, payload, model)

    if not isinstance(parsed, dict) or "why_it_moved" not in parsed:
        report = _offline_report(payload)
        raw = raw or "(offline fallback)"
    else:
        report = parsed
        # Authoritative numbers come from the payload, not the model: overlay
        # them onto the model's narrative sections so the printed figures can
        # never drift from what the providers returned.
        report["ticker"] = payload["ticker"]
        report["as_of"] = payload["as_of"]
        report.setdefault("snapshot", {}).update(
            {k: payload["snapshot"].get(k) for k in
             ("name", "sector", "price", "market_cap")})
        pa = report.get("price_action") or {}
        pa["returns"] = payload["returns"]
        report["price_action"] = pa
        report["fundamentals"] = {**payload["fundamentals"],
                                  "note": str((report.get("fundamentals") or {}).get("note", ""))[:800]}
        report["earnings"] = {**payload["earnings"],
                              "note": str((report.get("earnings") or {}).get("note", ""))[:400]}
        # Verdict ACTION/CONFIDENCE/plan are the deterministic seed's; the model
        # contributes only the rationale text.
        seed = payload.get("verdict_seed") or {}
        model_v = report.get("verdict") if isinstance(report.get("verdict"), dict) else {}
        rationale = str(model_v.get("rationale", "")).strip()[:400]
        if not rationale:
            rationale = (f"{str(seed.get('action', 'watch')).upper()} — "
                         + "; ".join(seed.get("reasons", [])[:3]))
        report["verdict"] = {**seed, "rationale": rationale}
        # Scenario LEVELS are deterministic; the model contributes narrative only.
        by_name = {str(s.get("name", "")).lower(): s
                   for s in (report.get("scenarios") or []) if isinstance(s, dict)}
        report["scenarios"] = [
            {**s, "narrative": str(by_name.get(s["name"], {}).get(
                "narrative", by_name.get(s["name"], {}).get("summary", "")))[:400]}
            for s in payload["scenario_levels"]
        ]
        report["position"] = payload.get("position")
        report["analysis"] = payload["analysis"]
        report["data_quality"] = payload.get("data_quality")
        report["disclaimer"] = DISCLAIMER
        report = _enforce_grounding(report, payload)

    if db and run_id:
        db.log_agent_output(
            run_id, "explain", ticker=payload["ticker"], model=model,
            input_obj=payload, output_obj=report, raw_text=raw,
        )
    return report

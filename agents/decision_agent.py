"""Decision Agent — turns ranked candidates + portfolio into order intents.

Pipeline position: Research -> Analysis -> **Decision** -> Risk -> Execution.

For every candidate AND every existing position it produces ONE action —
``buy`` / ``add`` / ``hold`` / ``trim`` / ``pass`` — with a confidence (0-100), a
target dollar amount, a 2-3 sentence rationale, suggested stop-loss / take-profit
levels and a swing time-stop (``max_hold_until``) for the Monitor agent.

Two important boundaries:
  * The agent only *proposes*. The deterministic risk guardrails run next and may
    resize or reject anything; this agent does NOT enforce hard limits. It is
    told the limits only so it proposes sensibly and stays conservative.
  * Full exits are the Monitor's job (stop / target / time / thesis). The
    Decision Agent reduces risk via ``trim`` but does not place outright sells.

When an LLM backend is available it makes the judgement call (the guardrails are
the safety net); a deterministic, conservative policy is used as the fallback so
the pipeline always runs offline. Either way every order is normalised to a
single schema before it leaves this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from agents.llm import generate_json, load_prompt

SYSTEM_PROMPT = load_prompt("decision_agent")

ACTIONABLE = {"buy", "add", "trim"}  # hold/pass produce no order


def _clamp(x: float, lo: float = 0, hi: float = 100) -> int:
    return int(max(lo, min(hi, round(x))))


def _score_of(item: dict, default: float = 50.0) -> float:
    return float(item.get("composite_score", item.get("score", default)) or default)


def _plan_levels(price: float | None, stop_pct: float, tp_pct: float,
                 max_days: int) -> tuple[float | None, float | None, str]:
    stop = round(price * (1 - stop_pct), 2) if price else None
    take = round(price * (1 + tp_pct), 2) if price else None
    until = (datetime.now(timezone.utc).date() + timedelta(days=max_days)).isoformat()
    return stop, take, until


def _order(ticker, action, side, *, confidence, target_usd, price, sector,
           rationale, stop=None, take=None, until=None, score=None) -> dict:
    return {
        "ticker": ticker,
        "action": action,
        "side": side,                       # BUY | SELL | None
        "confidence": _clamp(confidence),
        "target_dollar_amount": round(float(target_usd), 2) if target_usd else 0.0,
        "price": price,
        "sector": sector,
        "suggested_stop_loss": stop,
        "take_profit": take,
        "max_hold_until": until,
        "rationale": rationale,
        "_score": round(score) if score is not None else None,
    }


# ---------------------------------------------------------------------------
# Deterministic fallback policy
# ---------------------------------------------------------------------------
def _offline_decision(
    analysis: list[dict], account_summary: dict, limits: dict, strategy: dict,
    trades_remaining: int,
) -> dict:
    positions: dict = account_summary.get("positions", {})
    equity = float(account_summary.get("equity", 0.0) or 0.0)
    cash = float(account_summary.get("cash", 0.0) or 0.0)
    held = {t for t, p in positions.items() if float(p.get("shares", 0) or 0) > 0}
    open_count = len(held)

    target = strategy.get("target_portfolio_size", 10)
    buy_th = strategy.get("min_score_to_buy", 65)
    add_th = strategy.get("min_score_to_add", 70)
    trim_th = strategy.get("trim_below_score", 45)
    stop_pct = strategy.get("default_stop_loss_pct", 0.08)
    tp_pct = strategy.get("default_take_profit_pct", 0.20)
    max_days = strategy.get("max_holding_days", 120)

    per_trade = float(limits.get("per_trade_max_usd", 500))
    max_pos_pct = float(limits.get("max_position_pct", 0.15))
    max_sector_pct = float(limits.get("max_sector_pct", 0.40))
    min_cash_pct = float(limits.get("min_cash_reserve_pct", 0.10))

    # Running state we keep within configured caps as we propose.
    deployable = max(0.0, cash - min_cash_pct * equity)
    sector_val: dict[str, float] = {}
    for p in positions.values():
        s = p.get("sector", "Unknown")
        sector_val[s] = sector_val.get(s, 0.0) + float(p.get("market_value", 0) or 0)

    by_ticker = {a["ticker"]: a for a in analysis}
    budget = max(0, int(trades_remaining))
    orders: list[dict] = []

    def fit_note() -> str:
        return (f"Book has {open_count}/{target} names; "
                f"~${deployable:,.0f} deployable within the cash floor.")

    # 1) Existing positions, worst score first (trim risk before adding).
    for t in sorted(held, key=lambda x: _score_of(by_ticker.get(x, {}))):
        a = by_ticker.get(t, {})
        score = _score_of(a)
        pos = positions[t]
        sector = pos.get("sector", "Unknown")
        pos_val = float(pos.get("market_value", 0) or 0)
        shares = float(pos.get("shares", 0) or 0)
        price = a.get("_price") or (pos_val / shares if shares else None)
        stop, take, until = _plan_levels(price, stop_pct, tp_pct, max_days)

        if score <= trim_th and budget > 0:
            trim_usd = round(pos_val * 0.5, 2)
            orders.append(_order(
                t, "trim", "SELL",
                confidence=_clamp(55 + (trim_th - score)),
                target_usd=trim_usd, price=price, sector=sector,
                stop=stop, take=take, until=until, score=score,
                rationale=(f"Composite has decayed to {score:.0f} (≤ trim {trim_th}); "
                           f"reduce {t} by ~${trim_usd:,.0f} to cut risk while letting the "
                           f"remainder run. A full exit is left to the Monitor on a hard "
                           f"stop or thesis break."),
            ))
            budget -= 1
            sector_val[sector] = max(0.0, sector_val.get(sector, 0.0) - trim_usd)
        elif score >= add_th:
            can_add = (
                budget > 0
                and pos_val < max_pos_pct * equity * 0.9
                and deployable >= max(50.0, per_trade * 0.5)
                and sector_val.get(sector, 0.0) + per_trade * 0.5 <= max_sector_pct * equity
            )
            add_usd = round(min(per_trade * 0.5, max_pos_pct * equity - pos_val, deployable), 2)
            if can_add and add_usd >= 50:
                orders.append(_order(
                    t, "add", "BUY",
                    confidence=_clamp(score - 10),
                    target_usd=add_usd, price=price, sector=sector,
                    stop=stop, take=take, until=until, score=score,
                    rationale=(f"{t} is a winner (composite {score:.0f} ≥ add {add_th}); "
                               f"add ~${add_usd:,.0f} while it stays under the position cap. "
                               f"{fit_note()}"),
                ))
                deployable -= add_usd
                sector_val[sector] = sector_val.get(sector, 0.0) + add_usd
                budget -= 1
            else:
                orders.append(_hold(
                    t, score,
                    f"a winner (≥ add {add_th}) but already near the position/sector cap or "
                    f"cash floor — no room to add"))
        else:
            orders.append(_hold(t, score, "constructive but below the add threshold — avoid churn"))

    # 2) New candidates, best composite first (analysis is already sorted).
    for a in analysis:
        t = a["ticker"]
        if t in held:
            continue
        score = _score_of(a, default=0.0)
        sector = a.get("_sector", "Unknown")
        price = a.get("_price")
        setup = a.get("swing_setup", "none")

        room = (score >= buy_th and open_count < target and budget > 0
                and deployable >= 50
                and sector_val.get(sector, 0.0) < max_sector_pct * equity)
        if not room:
            orders.append(_pass(t, score, buy_th, open_count, target))
            continue
        if not price or price <= 0:
            orders.append(_pass(t, score, buy_th, open_count, target, reason="no reference price"))
            continue

        slice_usd = min(per_trade, max_pos_pct * equity, deployable)
        conf = _clamp(score + {"breakout": 5, "pullback-in-uptrend": 5,
                               "oversold-reversal": 0, "none": -10}.get(setup, 0))
        # Conservative sizing: scale 50–100% of the slice by confidence.
        size = round(max(50.0, min(slice_usd, slice_usd * (0.5 + 0.5 * conf / 100))), 2)
        stop, take, until = _plan_levels(price, stop_pct, tp_pct, max_days)
        sub = a.get("score_breakdown", {})
        orders.append(_order(
            t, "buy", "BUY",
            confidence=conf, target_usd=size, price=price, sector=sector,
            stop=stop, take=take, until=until, score=score,
            rationale=(f"Open {t}: composite {score:.0f} "
                       f"(T{a.get('technical_score','?')}/F{a.get('fundamental_score','?')}/"
                       f"S{a.get('sentiment_score','?')}) with a {setup} setup ranks it among "
                       f"today's best. Size ~${size:,.0f} keeps it within the per-trade and "
                       f"position caps. {fit_note()}"),
        ))
        open_count += 1
        deployable -= size
        sector_val[sector] = sector_val.get(sector, 0.0) + size
        budget -= 1

    n_buy = sum(1 for o in orders if o["action"] in ("buy", "add"))
    n_trim = sum(1 for o in orders if o["action"] == "trim")
    return {
        "market_view": "Deterministic fallback policy (no LLM backend).",
        "orders": orders,
        "notes": f"{n_buy} buy/add, {n_trim} trim, {len(orders)-n_buy-n_trim} hold/pass; "
                 f"guardrails still apply.",
    }


def _hold(ticker: str, score: float, why: str) -> dict:
    return _order(ticker, "hold", None, confidence=_clamp(score), target_usd=0.0,
                  price=None, sector=None, score=score,
                  rationale=f"Hold {ticker}: composite {score:.0f} — {why}.")


def _pass(ticker: str, score: float, buy_th, open_count, target, reason: str = "") -> dict:
    why = reason or (f"composite {score:.0f} below buy threshold {buy_th}"
                     if score < buy_th else f"portfolio full ({open_count}/{target}) or no cash room")
    return _order(ticker, "pass", None, confidence=_clamp(100 - score), target_usd=0.0,
                  price=None, sector=None, score=score,
                  rationale=f"Pass {ticker}: {why}; not actionable today.")


# ---------------------------------------------------------------------------
# Normalisation (applied to BOTH the LLM and the offline output)
# ---------------------------------------------------------------------------
def _side_for(action: str, explicit: Any) -> str | None:
    if explicit:
        return str(explicit).upper()
    if action in ("buy", "add"):
        return "BUY"
    if action == "trim":
        return "SELL"
    return None


def _normalize(order: dict, meta: dict, strategy: dict) -> dict:
    a = meta.get(str(order.get("ticker", "")).upper(), {})
    ticker = str(order.get("ticker", "")).upper()
    action = str(order.get("action", "")).lower() or "pass"
    side = _side_for(action, order.get("side"))
    price = order.get("price") or order.get("_price") or a.get("_price")

    conf = float(order.get("confidence", 0) or 0)  # accept 0-100 or 0-1
    conf = conf * 100 if conf <= 1 else conf

    stop = order.get("suggested_stop_loss")
    take = order.get("take_profit")
    until = order.get("max_hold_until")
    if action in ACTIONABLE and price and (stop is None or take is None or not until):
        d_stop, d_take, d_until = _plan_levels(
            price, strategy.get("default_stop_loss_pct", 0.08),
            strategy.get("default_take_profit_pct", 0.20),
            strategy.get("max_holding_days", 120),
        )
        stop = stop if stop is not None else d_stop
        take = take if take is not None else d_take
        until = until or d_until

    return {
        "ticker": ticker,
        "action": action,
        "side": side,
        "confidence": _clamp(conf),
        "target_dollar_amount": round(float(order.get("target_dollar_amount",
                                       order.get("usd_amount", 0)) or 0), 2),
        "price": price,
        "sector": order.get("sector") or a.get("_sector", "Unknown"),
        "suggested_stop_loss": stop,
        "take_profit": take,
        "max_hold_until": until,
        "rationale": str(order.get("rationale", ""))[:600],
        "_score": order.get("_score", a.get("composite_score", a.get("score"))),
    }


async def run_decision(
    analysis: list[dict], account_summary: dict, limits: dict, strategy: dict,
    trades_remaining: int, model: str, *, db=None, run_id: str | None = None,
) -> dict:
    """Produce the decision object (proposed order intents) for this cycle."""
    payload = {
        "analysis": analysis,
        "portfolio": account_summary,
        "risk_limits": limits,
        "strategy": strategy,
        "trades_remaining_today": trades_remaining,
    }
    parsed, raw = await generate_json(SYSTEM_PROMPT, payload, model)

    # The prompt asks for a JSON list of intents; also accept a wrapped dict.
    if isinstance(parsed, list):
        parsed = {"market_view": "", "orders": parsed, "notes": "llm intents"}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("orders"), list):
        parsed = _offline_decision(analysis, account_summary, limits, strategy, trades_remaining)
        raw = raw or "(offline fallback)"

    meta = {a["ticker"]: a for a in analysis}
    parsed["orders"] = [
        _normalize(o, meta, strategy) for o in parsed.get("orders", [])
        if o.get("ticker")
    ]

    if db and run_id:
        db.log_agent_output(
            run_id, "decision", model=model,
            input_obj=payload, output_obj=parsed, raw_text=raw,
        )
    return parsed

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

from datetime import date, datetime, timedelta, timezone
from typing import Any

from agents.llm import generate_json, last_error, load_prompt

SYSTEM_PROMPT = load_prompt("decision_agent")

ACTIONABLE = {"buy", "add", "trim"}  # hold/pass produce no order


def _clamp(x: float, lo: float = 0, hi: float = 100) -> int:
    return int(max(lo, min(hi, round(x))))


def _score_of(item: dict, default: float = 50.0) -> float:
    return float(item.get("composite_score", item.get("score", default)) or default)


def _until(strategy: dict) -> str:
    days = int(strategy.get("max_holding_days", 60))
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def _valid_until(value: Any, strategy: dict) -> tuple[str, str | None]:
    """Sanitise an LLM-supplied ``max_hold_until``.

    Returns ``(date, rejected_value)`` — ``rejected_value`` is non-None when the
    supplied date was unusable and the computed horizon was substituted.

    This is a time-stop the Monitor acts on: a date in the PAST makes it exit the
    position on the very next run. That is not theoretical — the model emitted
    "2025-10-24" for days, so every position opened was sold the following
    morning and rebought, paying a round trip of spread each time. A date beyond
    the configured horizon is rejected for the mirror reason: it silently
    disables the time-stop the strategy is supposed to enforce.
    """
    fallback = _until(strategy)
    if not value:
        return fallback, None
    try:
        # `date.fromisoformat` also accepts a full timestamp on 3.11+, so trim to
        # the date part rather than rejecting an otherwise-valid ISO datetime.
        supplied = date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return fallback, str(value)
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=int(strategy.get("max_holding_days", 60)))
    if not (today < supplied <= horizon):
        return fallback, str(value)
    return supplied.isoformat(), None


def _plan_levels(price: float | None, atr: float | None,
                 strategy: dict) -> tuple[float | None, float | None, str]:
    """Entry stop/target as a function of VOLATILITY, not a flat percentage.

    stop  = price − stop_atr_mult·ATR, but never risking more than max_loss_pct
            (so a high-ATR name can't set a 40%-wide stop and ride to −42%).
    target = price + target_r_multiple·(risk), a fixed reward:risk ratio.
    Falls back to a plain max_loss_pct stop when ATR is unavailable.
    """
    until = _until(strategy)
    if not price:
        return None, None, until
    max_loss = float(strategy.get("max_loss_pct",
                                  strategy.get("default_stop_loss_pct", 0.10)))
    if atr and atr > 0:
        stop = price - float(strategy.get("stop_atr_mult", 2.5)) * atr
        stop = max(stop, price * (1 - max_loss))   # cap the loss (stop floor)
    else:
        stop = price * (1 - max_loss)
    risk = max(price - stop, 0.01)
    take = price + float(strategy.get("target_r_multiple", 2.5)) * risk
    return round(stop, 2), round(take, 2), until


def _vol_size_factor(atr_pct: float | None, ref: float = 4.0) -> float:
    """Volatility-target multiplier: keep roughly constant dollar risk by shrinking
    the size of high-ATR names. 1.0 at/below the reference ATR%, tapering to a 0.3
    floor for very volatile names (a 10%-ATR name gets ~40% of a calm name's size)."""
    if not atr_pct or atr_pct <= ref:
        return 1.0
    return max(0.3, ref / atr_pct)


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

    # Never aim past the hard position cap — proposing an Nth name the
    # guardrail will reject on max_positions just manufactures a doomed order.
    # A `null` cap means there is no such ceiling, so the strategy's own target
    # is the only thing shaping the book.
    _max_positions = limits.get("max_positions", 15)
    target = int(strategy.get("target_portfolio_size", 10))
    if _max_positions is not None:
        target = min(target, int(_max_positions))
    buy_th = strategy.get("min_score_to_buy", 65)
    add_th = strategy.get("min_score_to_add", 70)
    trim_th = strategy.get("trim_below_score", 45)
    vol_sizing = bool(strategy.get("vol_target_sizing", True))

    # Mirror the guardrail's effective per-trade cap (the tighter of the dollar
    # ceiling and the equity-scaled pct). Proposing above it just gets resized.
    per_trade = min(
        float(limits.get("per_trade_max_usd", 500)),
        float(limits.get("per_trade_max_pct", 1.0)) * equity,
    )
    max_pos_pct = float(limits.get("max_position_pct", 0.15))
    max_sector_pct = float(limits.get("max_sector_pct", 0.40))
    min_cash_pct = float(limits.get("min_cash_reserve_pct", 0.10))
    # The smallest order worth placing. Reading this from the limits (rather
    # than hardcoding 50) is what keeps sizing consistent on the agentic book,
    # which overrides min_trade_usd down to 1.
    min_trade = float(limits.get("min_trade_usd", 50))

    # Running state we keep within configured caps as we propose.
    #
    # Prefer the budget the orchestrator already computed: it is measured against
    # buying_power (settled `cash` reads $0.00 on a limited-margin account even
    # when the book has money), it includes what this cycle's exits will free,
    # and it is floored to the cent so it never overstates. Falling back to
    # `cash` keeps this working for a summary built without those keys.
    deployable = account_summary.get("deployable_cash_after_exits")
    if deployable is None:
        deployable = max(0.0, cash - min_cash_pct * equity)
    deployable = max(0.0, float(deployable))
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
        stop, take, until = _plan_levels(price, a.get("_atr"), strategy)

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
                and deployable >= max(min_trade, per_trade * 0.5)
                and sector_val.get(sector, 0.0) + per_trade * 0.5 <= max_sector_pct * equity
            )
            add_usd = round(min(per_trade * 0.5, max_pos_pct * equity - pos_val, deployable), 2)
            if vol_sizing:
                add_usd = round(add_usd * _vol_size_factor(a.get("_atr_pct")), 2)
            if can_add and add_usd >= min_trade:
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
                and deployable >= min_trade
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
        # Conservative sizing: scale 50–100% of the slice by confidence. Never
        # clamp UP to a floor here — doing so proposes a size the per-trade cap
        # will only resize back down. If the slice is too small to be worth
        # trading, pass instead.
        size = round(slice_usd * (0.5 + 0.5 * conf / 100), 2)
        if vol_sizing:  # shrink high-ATR names to keep dollar risk roughly constant
            size = round(size * _vol_size_factor(a.get("_atr_pct")), 2)
        if size < min_trade:
            orders.append(_pass(
                t, score, buy_th, open_count, target,
                reason=f"size ${size:,.2f} is below the ${min_trade:,.2f} minimum trade"))
            continue
        stop, take, until = _plan_levels(price, a.get("_atr"), strategy)
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
    rejected_until = None
    if action in ACTIONABLE and price and (stop is None or take is None or not until):
        d_stop, d_take, d_until = _plan_levels(price, a.get("_atr"), strategy)
        stop = stop if stop is not None else d_stop
        take = take if take is not None else d_take
        until = until or d_until
    # Sanitise the time-stop on EVERY actionable order, not just ones that
    # omitted it. A supplied-but-wrong date is the dangerous case: the Monitor
    # exits on it, so a past date churns the position out the next morning.
    if action in ACTIONABLE:
        until, rejected_until = _valid_until(until, strategy)

    return {
        "_rejected_max_hold_until": rejected_until,
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
        # NEVER fall back silently: an unexplained switch to the deterministic
        # policy looks like normal operation while quietly ignoring the model.
        why = last_error() or "model returned an unexpected shape"
        print(f"\n⚠️  DECISION AGENT FELL BACK to the deterministic policy — {why}")
        if db and run_id:
            db.audit(run_id, "WARN", "decision_llm_fallback",
                     {"reason": why, "model": model,
                      "raw_preview": (raw or "")[:2000]})
        parsed = _offline_decision(analysis, account_summary, limits, strategy, trades_remaining)
        parsed["market_view"] = f"Deterministic fallback policy — {why}"
        raw = raw or "(offline fallback)"

    meta = {a["ticker"]: a for a in analysis}
    parsed["orders"] = [
        _normalize(o, meta, strategy) for o in parsed.get("orders", [])
        if o.get("ticker")
    ]

    # A model that starts emitting unusable time-stops must be visible, not
    # silently corrected — a past date here churns the whole book daily.
    bad_dates = {o["ticker"]: o["_rejected_max_hold_until"]
                 for o in parsed["orders"] if o.get("_rejected_max_hold_until")}
    if bad_dates:
        print(f"⚠️  Rejected out-of-range max_hold_until from the model: {bad_dates} "
              f"— substituted the configured horizon.")
        if db and run_id:
            db.audit(run_id, "WARN", "max_hold_until_rejected",
                     {"model": model, "rejected": bad_dates,
                      "substituted": _until(strategy)})

    if db and run_id:
        db.log_agent_output(
            run_id, "decision", model=model,
            input_obj=payload, output_obj=parsed, raw_text=raw,
        )
    return parsed

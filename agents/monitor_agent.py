"""Monitor Agent — exit discipline for OPEN positions only.

Pipeline position: ... -> Execution -> **Monitor** (its exits feed back into
Risk -> Execution like any entry). Runs daily; this is what makes the system a
*swing* system rather than buy-and-hold.

For each open position it reads the current price/technicals plus the **stored
trade plan** (absolute ``stop_loss`` / ``take_profit`` levels, ``max_hold_until``
date and the original thesis, persisted in the ``trade_plans`` table at entry)
and chooses one action: ``hold`` / ``exit_full`` / ``exit_partial``.

Exit when ANY of these trigger (capital protection is the priority, so the hard
rules are deterministic and can never be suppressed by the LLM):
  * price <= ``stop_loss``            -> exit_full   (cut losses)
  * ``max_hold_until`` date passed    -> exit_full   (swing time-stop)
  * thesis broken                     -> exit_full   (explain which signal)
      - composite score <= exit threshold
      - news sentiment turned sharply negative
      - broke below the 50-day SMA on elevated volume
  * price >= ``take_profit``          -> exit_partial (lock gains, trail the rest)
  * otherwise                         -> hold

Exits are emitted as SELL order intents and go through the SAME deterministic
risk guardrails and executor as entries. The LLM may add nuance (e.g. spotting
a bad-earnings thesis break the rules missed) but the deterministic exits are
always included.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from agents.llm import generate_json, load_prompt

SYSTEM_PROMPT = load_prompt("monitor_agent")

# Sentiment at/below this aggregate is treated as a sharp negative turn.
SENTIMENT_BREAK = -0.35
# Volume multiple of average that qualifies a 50-SMA break as "on volume".
VOLUME_BREAK_MULT = 1.5


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _exit(p: dict, action: str, shares, trigger: str, reason: str, conf: float) -> dict:
    return {
        "ticker": p["ticker"],
        "side": "SELL",
        "action": action,                 # exit_full | exit_partial
        "shares": shares,                 # None = sell all held (guardrail clamps)
        "trigger": trigger,               # stop_loss|time_stop|thesis_break|take_profit
        "reason": reason,
        "confidence": conf,
        "current_price": p.get("current_price"),
    }


def _deterministic_exits(positions: list[dict], strategy: dict) -> list[dict]:
    """Hard exit rules evaluated independently of any LLM. Decisive on stops."""
    exits: list[dict] = []
    today = _today()
    default_exit_below = strategy.get("exit_below_score", 35)

    for p in positions:
        price = p.get("current_price") or p.get("price") or 0.0
        shares = p.get("shares") or 0.0
        if price <= 0 or shares <= 0:
            continue
        stop = p.get("stop_loss")
        take = p.get("take_profit")
        mhu = p.get("max_hold_until")
        score = p.get("score")
        exit_below = p.get("exit_below_score", default_exit_below)
        sma50 = p.get("sma50")
        vol = p.get("volume_vs_avg")
        sentiment = p.get("sentiment_score")
        half = round(shares / 2, 6)

        # 1) STOP-LOSS — protect capital first.
        if stop and price <= stop + 1e-9:
            exits.append(_exit(p, "exit_full", None, "stop_loss",
                               f"price ${price:.2f} <= stop ${stop:.2f} — cut losses", 0.95))
            continue

        # 2) TIME-STOP — swing horizon elapsed.
        if mhu:
            try:
                passed = today >= date.fromisoformat(str(mhu)[:10])
            except (ValueError, TypeError):
                passed = False
            if passed:
                exits.append(_exit(p, "exit_full", None, "time_stop",
                                   f"max_hold_until {mhu} reached — swing time-stop", 0.8))
                continue

        # 3) THESIS BREAK — explain which signal fired.
        if score is not None and score <= exit_below:
            exits.append(_exit(p, "exit_full", None, "thesis_break",
                               f"composite {score} <= exit threshold {exit_below} — thesis decayed", 0.8))
            continue
        if sentiment is not None and sentiment <= SENTIMENT_BREAK:
            exits.append(_exit(p, "exit_full", None, "thesis_break",
                               f"news sentiment turned sharply negative ({sentiment:+.2f}) — thesis broken", 0.75))
            continue
        if sma50 and price < sma50 and vol is not None and vol >= VOLUME_BREAK_MULT:
            exits.append(_exit(p, "exit_full", None, "thesis_break",
                               f"broke below 50-day SMA (${sma50:.2f}) on elevated volume ({vol:.1f}x avg)", 0.75))
            continue

        # 4) TAKE-PROFIT — lock gains, trail the remainder.
        if take and price >= take - 1e-9:
            exits.append(_exit(p, "exit_partial", half, "take_profit",
                               f"price ${price:.2f} >= target ${take:.2f} — lock gains, trail the remainder", 0.6))
            continue
    return exits


async def run_monitor(
    positions: list[dict], strategy: dict, model: str,
    *, db=None, run_id: str | None = None,
) -> dict:
    """Return exit/hold decisions for open positions."""
    if not positions:
        return {"exits": [], "holds": [], "notes": "no open positions"}

    deterministic = _deterministic_exits(positions, strategy)
    payload = {"positions": positions, "strategy": strategy}
    parsed, raw = await generate_json(SYSTEM_PROMPT, payload, model)

    if not isinstance(parsed, dict) or "exits" not in parsed:
        exits = deterministic
        raw = raw or "(offline fallback)"
    else:
        # Safety net: union the deterministic hard exits with the LLM's, so the
        # model can NEVER suppress a triggered stop/time/thesis exit. The LLM may
        # still add an exit the rules missed (e.g. a bad-earnings thesis break).
        exits = list(parsed.get("exits") or [])
        seen = {e.get("ticker") for e in exits}
        for d in deterministic:
            if d["ticker"] not in seen:
                exits.append(d)
        # Normalise / backfill fields the executor needs.
        price_by = {p["ticker"]: (p.get("current_price") or p.get("price")) for p in positions}
        shares_by = {p["ticker"]: p.get("shares") for p in positions}
        det_by = {d["ticker"]: d for d in deterministic}
        for e in exits:
            t = e.get("ticker")
            e.setdefault("side", "SELL")
            e.setdefault("current_price", price_by.get(t))
            action = str(e.get("action", "")).lower()
            if action not in ("exit_full", "exit_partial"):
                # If a deterministic rule fired for this name, defer to it.
                action = det_by.get(t, {}).get("action", "exit_full")
                e["action"] = action
            # exit_full sells all held (shares=None); exit_partial defaults to half.
            if action == "exit_full":
                e["shares"] = None
            elif e.get("shares") is None and shares_by.get(t):
                e["shares"] = round(shares_by[t] / 2, 6)

    exit_tickers = {e.get("ticker") for e in exits}
    holds = [p["ticker"] for p in positions if p["ticker"] not in exit_tickers]
    result = {"exits": exits, "holds": holds,
              "notes": f"{len(exits)} exit(s), {len(holds)} hold(s)"}

    if db and run_id:
        db.log_agent_output(
            run_id, "monitor", model=model,
            input_obj=payload, output_obj=result, raw_text=raw,
        )
    return result

"""Review Agent — the post-close learning loop.

Pipeline position: it runs AFTER the trading day (``--mode review``), reads what
the other agents decided and what the ledger says happened, and turns that into
three things the next morning's run consumes:

  * **lessons** — short, evidence-backed rules stored in ``lessons`` and handed
    to the decision / monitor prompts while active;
  * a **playbook** — the durable rules in one paragraph, rewritten in full each
    review;
  * **parameter proposals** — changes to whitelisted ``strategy:`` keys, applied
    only inside hard numeric bounds and only once enough closed trades exist.

The split is the same as everywhere else in this system: **the scorecard is
deterministic Python** (FIFO round trips, R-multiples, forward returns on every
call the agents made, behaviour metrics, hit rates by bucket) and the model only
narrates it. A lesson the model writes cannot move a limit; a proposal it makes
cannot leave the bounds in ``config.yaml -> review.tunable``; ``risk:`` keys
are never writable from here at all.

Read-only with respect to the broker: this module never places an order.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable

from agents.llm import generate_json, last_error, load_prompt

SYSTEM_PROMPT = load_prompt("review_agent")

FILLED = ("filled", "submitted", "simulated")
STOP_TRIGGERS = ("stop_loss", "stop_trail")
DEFAULT_HORIZONS = (1, 5, 10, 20)

# Buckets used everywhere a hit rate is reported, so the dashboard, the brief
# and the model all slice the same way.
SCORE_BUCKETS = (("<65", -math.inf, 65), ("65-70", 65, 70), ("70-80", 70, 80), ("80+", 80, math.inf))
SIZE_BUCKETS = (("<10%", 0, 0.10), ("10-25%", 0.10, 0.25), ("25-50%", 0.25, 0.50), ("50%+", 0.50, math.inf))
HOLD_BUCKETS = (("<=3d", 0, 3), ("4-10d", 4, 10), ("11-30d", 11, 30), ("30d+", 31, math.inf))


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _d(ts: str | None) -> date | None:
    try:
        return date.fromisoformat(str(ts)[:10])
    except (TypeError, ValueError):
        return None


def _bucket(value: float | None, buckets) -> str | None:
    if value is None:
        return None
    for name, lo, hi in buckets:
        if lo <= value < hi or (hi is math.inf and value >= lo):
            return name
    return None


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def _pct(a: float, b: float) -> float | None:
    return None if not b else (a / b - 1.0) * 100.0


# ---------------------------------------------------------------------------
# Ledger reconstruction — FIFO round trips
# ---------------------------------------------------------------------------
def fifo_round_trips(trades: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Rebuild closed legs and open lots from the trade ledger.

    ``trades`` are dicts with ``ticker, side, qty, price, ts, run_id, detail``
    ordered by time. One closed leg is produced per SELL; the cost basis is the
    FIFO-weighted average of the lots it consumed, so a sell that spans two buys
    still yields one leg with one P&L. Returns ``(closed_legs, open_lots)``.
    """
    lots: dict[str, list[dict]] = defaultdict(list)
    legs: list[dict] = []
    for t in trades:
        ticker = str(t.get("ticker") or "").upper()
        qty = float(t.get("qty") or 0.0)
        price = float(t.get("price") or 0.0)
        if not ticker or qty <= 0 or price <= 0:
            continue
        detail = t.get("detail") or {}
        if t.get("side") == "BUY":
            lots[ticker].append({
                "qty": qty, "price": price, "ts": t.get("ts"), "run_id": t.get("run_id"),
                "action": detail.get("action"),
            })
            continue
        # SELL: consume FIFO
        remaining, cost, consumed = qty, 0.0, 0.0
        first_lot = None
        while remaining > 1e-9 and lots[ticker]:
            lot = lots[ticker][0]
            if first_lot is None:
                first_lot = dict(lot)
            take = min(remaining, lot["qty"])
            cost += take * lot["price"]
            consumed += take
            remaining -= take
            lot["qty"] -= take
            if lot["qty"] <= 1e-9:
                lots[ticker].pop(0)
        if consumed <= 1e-9:
            continue  # a sell with no recorded lot (pre-ledger position) — unattributable
        entry_price = cost / consumed
        proceeds = consumed * price
        entry_d, exit_d = _d(first_lot["ts"]), _d(t.get("ts"))
        legs.append({
            "ticker": ticker,
            "entry_date": entry_d.isoformat() if entry_d else None,
            "exit_date": exit_d.isoformat() if exit_d else None,
            "hold_days": (exit_d - entry_d).days if entry_d and exit_d else None,
            "shares": _r(consumed, 6),
            "entry_price": _r(entry_price, 4),
            "exit_price": _r(price, 4),
            "cost_basis": _r(cost),
            "proceeds": _r(proceeds),
            "pnl_usd": _r(proceeds - cost),
            "pnl_pct": _r(_pct(proceeds, cost)),
            "exit_trigger": detail.get("trigger"),
            "exit_action": detail.get("action"),
            "entry_run_id": first_lot.get("run_id"),
            "exit_run_id": t.get("run_id"),
            "closed_position": not lots[ticker],
        })
    open_lots = {k: v for k, v in lots.items() if v}
    return legs, open_lots


# ---------------------------------------------------------------------------
# Series helpers — forward returns and excursions
# ---------------------------------------------------------------------------
class _Series:
    """Daily bars for one ticker with date-indexed lookups; fetched once."""

    def __init__(self, bars: list[dict] | None):
        bars = [b for b in (bars or []) if b.get("date") and b.get("close")]
        bars.sort(key=lambda b: str(b["date"]))
        self.dates = [str(b["date"])[:10] for b in bars]
        self.close = [float(b["close"]) for b in bars]
        self.high = [float(b.get("high") or b["close"]) for b in bars]
        self.low = [float(b.get("low") or b["close"]) for b in bars]

    def __bool__(self) -> bool:
        return bool(self.dates)

    def index_on_or_after(self, d: str) -> int | None:
        i = bisect_left(self.dates, d[:10])
        return i if i < len(self.dates) else None

    def forward(self, d: str, horizons) -> dict[str, float | None]:
        """Close-to-close returns (%) at +h trading days from the first bar on
        or after ``d``; None where the future hasn't happened yet."""
        i = self.index_on_or_after(d)
        out: dict[str, float | None] = {}
        for h in horizons:
            j = None if i is None else i + int(h)
            out[f"r{h}"] = (_r(_pct(self.close[j], self.close[i]))
                            if j is not None and j < len(self.close) else None)
        return out

    def excursions(self, entry_d: str, entry_price: float,
                   exit_d: str | None = None) -> tuple[float | None, float | None]:
        """(MFE%, MAE%) — the best and worst the position saw between entry and
        exit (or now), measured on daily highs/lows."""
        i = self.index_on_or_after(entry_d)
        if i is None or not entry_price:
            return None, None
        j = self.index_on_or_after(exit_d) if exit_d else None
        j = len(self.dates) - 1 if j is None else min(j, len(self.dates) - 1)
        if j < i:
            return None, None
        mfe = (max(self.high[i:j + 1]) / entry_price - 1.0) * 100.0
        mae = (min(self.low[i:j + 1]) / entry_price - 1.0) * 100.0
        return _r(mfe), _r(mae)

    def close_at(self, d: str, offset: int = 0) -> float | None:
        i = self.index_on_or_after(d)
        if i is None:
            return None
        j = i + offset
        return self.close[j] if 0 <= j < len(self.close) else None

    def last_close(self) -> float | None:
        return self.close[-1] if self.close else None


class _SeriesCache:
    def __init__(self, provider, lookback: int):
        self.provider, self.lookback = provider, lookback
        self._cache: dict[str, _Series] = {}

    def get(self, ticker: str) -> _Series:
        t = ticker.upper()
        if t not in self._cache:
            bars = None
            if self.provider is not None:
                try:
                    bars = self.provider.get_daily_series(t, self.lookback)
                except Exception:
                    bars = None
            self._cache[t] = _Series(bars)
        return self._cache[t]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _stats(legs: list[dict]) -> dict:
    """Expectancy / win rate / profit factor for a list of closed legs."""
    pnls = [l["pnl_pct"] for l in legs if l.get("pnl_pct") is not None]
    usd = [l["pnl_usd"] for l in legs if l.get("pnl_usd") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(l["pnl_usd"] for l in legs if (l.get("pnl_usd") or 0) > 0)
    gross_loss = -sum(l["pnl_usd"] for l in legs if (l.get("pnl_usd") or 0) <= 0)
    rs = [l["r_multiple"] for l in legs if l.get("r_multiple") is not None]
    holds = [l["hold_days"] for l in legs if l.get("hold_days") is not None]
    return {
        "n": len(pnls),
        "win_rate": _r(100.0 * len(wins) / len(pnls)) if pnls else None,
        "avg_win_pct": _r(sum(wins) / len(wins)) if wins else None,
        "avg_loss_pct": _r(sum(losses) / len(losses)) if losses else None,
        "expectancy_pct": _r(sum(pnls) / len(pnls)) if pnls else None,
        "total_pnl_usd": _r(sum(usd)) if usd else 0.0,
        "profit_factor": (_r(gross_win / gross_loss) if gross_loss > 1e-9
                          else (None if gross_win <= 1e-9 else math.inf)),
        "avg_r": _r(sum(rs) / len(rs)) if rs else None,
        "median_hold_days": median(holds) if holds else None,
    }


def _grouped(legs: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for l in legs:
        k = l.get(key)
        groups[str(k) if k is not None else "unknown"].append(l)
    return {k: _stats(v) for k, v in sorted(groups.items())}


def _safe_inf(obj):
    """JSON has no infinity; the profit factor of an all-win sample is reported
    as the string 'inf' so the record still serialises."""
    if isinstance(obj, dict):
        return {k: _safe_inf(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_inf(v) for v in obj]
    if isinstance(obj, float) and math.isinf(obj):
        return "inf"
    return obj


# ---------------------------------------------------------------------------
# The scorecard
# ---------------------------------------------------------------------------
def build_scorecard(db, provider, account: str | None, *, window_days: int = 60,
                    horizons=DEFAULT_HORIZONS, min_position_usd: float = 0.0,
                    today: date | None = None, live_positions: dict | None = None) -> dict:
    """Everything the review needs, computed from the ledger with no LLM.

    ``db`` is a :class:`storage.db.Database`; ``provider`` anything with
    ``get_daily_series(ticker, lookback)`` (None disables forward returns);
    ``live_positions`` is an optional ``{ticker: Position}`` read from the broker
    for the open-lot section.
    """
    today = today or _today()
    since = (today - timedelta(days=int(window_days))).isoformat()
    horizons = tuple(int(h) for h in horizons)
    series = _SeriesCache(provider, lookback=max(120, int(window_days) + 40))

    # ---- raw rows ---------------------------------------------------------
    trades = [dict(r) for r in db.query(
        """SELECT id, ts, run_id, ticker, side, qty, price, notional_usd, status, detail, account
           FROM trades WHERE mode='live' AND (? IS NULL OR account = ?) ORDER BY id""",
        (account, account))]
    for t in trades:
        t["detail"] = _loads(t.get("detail"))
    filled = [t for t in trades if t["status"] in FILLED]

    runs = {r["run_id"]: dict(r) for r in db.query(
        """SELECT run_id, started_ts, mode, account FROM runs
           WHERE (? IS NULL OR account = ?) AND substr(started_ts,1,10) >= ?
           ORDER BY started_ts""", (account, account, since))}
    run_ids = list(runs)
    outputs = _agent_outputs(db, run_ids)          # run_id -> agent -> payload
    equity_by_run = _equity_by_run(db, run_ids)     # run_id -> equity at start
    monitor_trigger = _monitor_triggers(outputs)    # (run_id, ticker) -> trigger/action

    # Backfill exit triggers for legacy trades whose ledger detail predates the
    # tags, from the monitor output of the run that sold.
    for t in filled:
        if t["side"] == "SELL" and not t["detail"].get("trigger"):
            trig = monitor_trigger.get((t["run_id"], t["ticker"]))
            if trig:
                t["detail"] = {**t["detail"], **trig}

    # ---- round trips ------------------------------------------------------
    legs, open_lots = fifo_round_trips(filled)
    for leg in legs:
        _enrich_leg(leg, outputs, equity_by_run, series, today)
    window_legs = [l for l in legs if (l.get("exit_date") or "") >= since]

    # ---- open positions ---------------------------------------------------
    open_positions = _open_positions(open_lots, series, today, live_positions,
                                     equity_by_run, outputs)

    # ---- forward returns on the agents' calls -----------------------------
    calls = _decision_calls(outputs, runs, series, horizons, today)

    # ---- behaviour --------------------------------------------------------
    behaviour = _behaviour(db, account, trades, filled, run_ids, outputs, since,
                           min_position_usd)

    aggregates = {
        "all": _stats(window_legs),
        "by_score_bucket": _grouped(window_legs, "entry_score_bucket"),
        "by_setup": _grouped(window_legs, "entry_setup"),
        "by_exit_trigger": _grouped(window_legs, "exit_trigger"),
        "by_size_bucket": _grouped(window_legs, "size_bucket"),
        "by_hold_bucket": _grouped(window_legs, "hold_bucket"),
        "by_ticker": _grouped(window_legs, "ticker"),
        "whipsaw": _whipsaw(window_legs),
        "take_profit": _tp_left_on_table(window_legs),
    }

    return _safe_inf({
        "account": account,
        "as_of": today.isoformat(),
        "window_days": int(window_days),
        "window_start": since,
        "n_closed": len(window_legs),
        "n_closed_all_time": len(legs),
        "closed_legs": window_legs,
        "open_positions": open_positions,
        "aggregates": aggregates,
        "calls": calls,
        "behaviour": behaviour,
    })


def _loads(s):
    import json
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _agent_outputs(db, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    import json
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    if not run_ids:
        return out
    for i in range(0, len(run_ids), 400):
        chunk = run_ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        rows = db.query(
            f"""SELECT run_id, agent, output_json FROM agent_outputs
                WHERE run_id IN ({marks}) AND agent IN ('decision','analysis','monitor')
                ORDER BY id""", tuple(chunk))
        for r in rows:
            try:
                out[r["run_id"]][r["agent"]] = json.loads(r["output_json"] or "null")
            except Exception:
                continue
    return out


def _equity_by_run(db, run_ids: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    if not run_ids:
        return out
    for i in range(0, len(run_ids), 400):
        chunk = run_ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for r in db.query(
                f"SELECT run_id, equity FROM pnl WHERE run_id IN ({marks}) ORDER BY id",
                tuple(chunk)):
            out.setdefault(r["run_id"], float(r["equity"] or 0.0))
    return out


def _monitor_triggers(outputs) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for run_id, agents in outputs.items():
        mon = agents.get("monitor") or {}
        for e in (mon.get("exits") or []) if isinstance(mon, dict) else []:
            t = str(e.get("ticker") or "").upper()
            if t:
                out[(run_id, t)] = {"trigger": e.get("trigger"), "action": e.get("action")}
    return out


def _decision_meta(outputs, run_id: str | None, ticker: str) -> dict:
    """What the decision/analysis agents said about ``ticker`` in ``run_id``."""
    agents = outputs.get(run_id or "", {})
    meta: dict = {}
    ana = agents.get("analysis")
    if isinstance(ana, list):
        for a in ana:
            if str(a.get("ticker") or "").upper() == ticker:
                meta["score"] = a.get("composite_score", a.get("score"))
                meta["setup"] = a.get("swing_setup")
                meta["factor_scores"] = a.get("factor_scores")
                break
    dec = agents.get("decision")
    if isinstance(dec, dict):
        for o in dec.get("orders") or []:
            if str(o.get("ticker") or "").upper() == ticker:
                meta["action"] = o.get("action")
                meta["confidence"] = o.get("confidence")
                meta["stop"] = o.get("suggested_stop_loss")
                meta["take"] = o.get("take_profit")
                meta["rationale"] = o.get("rationale")
                if meta.get("score") is None:
                    meta["score"] = o.get("_score")
                break
    return meta


def _enrich_leg(leg: dict, outputs, equity_by_run, series: _SeriesCache, today: date) -> None:
    meta = _decision_meta(outputs, leg.get("entry_run_id"), leg["ticker"])
    entry = leg.get("entry_price") or 0.0
    stop = meta.get("stop")
    # Risk at entry as a % of entry: (entry - stop) / entry.
    risk_pct = ((entry - float(stop)) / entry * 100.0) if stop and entry and stop < entry else None
    leg["entry_score"] = meta.get("score")
    leg["entry_score_bucket"] = _bucket(meta.get("score"), SCORE_BUCKETS)
    leg["entry_setup"] = meta.get("setup") or "unknown"
    leg["entry_confidence"] = meta.get("confidence")
    leg["entry_action"] = meta.get("action")
    leg["risk_pct_at_entry"] = _r(risk_pct)
    leg["r_multiple"] = (_r(leg["pnl_pct"] / risk_pct)
                         if risk_pct and leg.get("pnl_pct") is not None else None)
    eq = equity_by_run.get(leg.get("entry_run_id") or "")
    size_frac = (leg["cost_basis"] / eq) if eq and leg.get("cost_basis") else None
    leg["size_pct_of_equity"] = _r(100.0 * size_frac) if size_frac is not None else None
    leg["size_bucket"] = _bucket(size_frac, SIZE_BUCKETS)
    leg["hold_bucket"] = _bucket(leg.get("hold_days"), HOLD_BUCKETS)
    leg["exit_trigger"] = leg.get("exit_trigger") or (
        "trim" if leg.get("exit_action") == "trim" else None) or "unknown"
    s = series.get(leg["ticker"])
    if s and leg.get("entry_date"):
        mfe, mae = s.excursions(leg["entry_date"], entry, leg.get("exit_date"))
        leg["mfe_pct"], leg["mae_pct"] = mfe, mae
        after = s.close_at(leg["exit_date"], 10) if leg.get("exit_date") else None
        leg["ret_10d_after_exit"] = _r(_pct(after, leg["exit_price"])) if after else None
    else:
        leg["mfe_pct"] = leg["mae_pct"] = leg["ret_10d_after_exit"] = None


def _open_positions(open_lots, series: _SeriesCache, today: date, live_positions,
                    equity_by_run, outputs) -> list[dict]:
    out = []
    for ticker, lots in open_lots.items():
        qty = sum(l["qty"] for l in lots)
        cost = sum(l["qty"] * l["price"] for l in lots)
        avg = cost / qty if qty else 0.0
        first = lots[0]
        entry_d = _d(first.get("ts"))
        s = series.get(ticker)
        live = (live_positions or {}).get(ticker)
        price = None
        if live is not None and getattr(live, "shares", 0):
            mv = float(getattr(live, "market_value", 0.0) or 0.0)
            price = mv / float(live.shares) if live.shares else None
        if not price and s:
            price = s.last_close()
        mfe, mae = (s.excursions(entry_d.isoformat(), avg) if s and entry_d else (None, None))
        meta = _decision_meta(outputs, first.get("run_id"), ticker)
        eq = equity_by_run.get(first.get("run_id") or "")
        out.append({
            "ticker": ticker, "shares": _r(qty, 6), "avg_cost": _r(avg, 4),
            "cost_basis": _r(cost), "entry_date": entry_d.isoformat() if entry_d else None,
            "hold_days": (today - entry_d).days if entry_d else None,
            "price": _r(price, 4), "market_value": _r(price * qty) if price else None,
            "unrealized_pct": _r(_pct(price, avg)) if price and avg else None,
            "mfe_pct": mfe, "mae_pct": mae,
            "entry_score": meta.get("score"), "entry_setup": meta.get("setup"),
            "size_pct_of_equity": _r(100.0 * cost / eq) if eq else None,
        })
    return sorted(out, key=lambda p: -(p.get("market_value") or 0))


def _decision_calls(outputs, runs, series: _SeriesCache, horizons, today: date) -> dict:
    """Forward returns for every call the decision agent made — and for every
    candidate it looked at and passed on — so 'did the buys beat the passes'
    is a number, not a feeling."""
    rows: list[dict] = []
    for run_id, agents in outputs.items():
        run = runs.get(run_id) or {}
        day = str(run.get("started_ts") or "")[:10]
        if not day or run.get("mode") == "explain":
            continue
        ana = agents.get("analysis") if isinstance(agents.get("analysis"), list) else []
        dec = agents.get("decision") if isinstance(agents.get("decision"), dict) else {}
        by_ticker = {str(a.get("ticker") or "").upper(): a for a in ana}
        acted: dict[str, dict] = {}
        for o in dec.get("orders") or []:
            t = str(o.get("ticker") or "").upper()
            if t:
                acted[t] = o
        for t, a in by_ticker.items():
            o = acted.get(t)
            action = str((o or {}).get("action") or "pass").lower()
            score = a.get("composite_score", a.get("score"))
            s = series.get(t)
            fwd = s.forward(day, horizons) if s else {f"r{h}": None for h in horizons}
            rows.append({"run_id": run_id, "date": day, "ticker": t, "action": action,
                         "score": score, "score_bucket": _bucket(score, SCORE_BUCKETS),
                         "setup": a.get("swing_setup"),
                         "confidence": (o or {}).get("confidence"), **fwd})
    # Aggregate: mean forward return + hit rate per action, per horizon.
    def agg(group: list[dict]) -> dict:
        out: dict[str, Any] = {"n": len(group)}
        for h in horizons:
            vals = [r[f"r{h}"] for r in group if r.get(f"r{h}") is not None]
            out[f"r{h}_mean"] = _r(sum(vals) / len(vals)) if vals else None
            out[f"r{h}_hit"] = _r(100.0 * sum(1 for v in vals if v > 0) / len(vals)) if vals else None
            out[f"r{h}_n"] = len(vals)
        return out

    by_action: dict[str, list[dict]] = defaultdict(list)
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_action[r["action"]].append(r)
        if r["action"] in ("buy", "add") and r.get("score_bucket"):
            by_bucket[r["score_bucket"]].append(r)
    recent = sorted(rows, key=lambda r: (r["date"], r["ticker"]))[-60:]
    return {
        "horizons": list(horizons),
        "by_action": {k: agg(v) for k, v in sorted(by_action.items())},
        "buys_by_score_bucket": {k: agg(v) for k, v in sorted(by_bucket.items())},
        "recent": recent,
        "n": len(rows),
    }


def _behaviour(db, account, trades, filled, run_ids, outputs, since: str,
               min_position_usd: float) -> dict:
    import json
    window = [t for t in trades if (t.get("ts") or "")[:10] >= since]
    win_filled = [t for t in filled if (t.get("ts") or "")[:10] >= since]
    dust = [{"ts": t["ts"][:10], "ticker": t["ticker"], "side": t["side"],
             "usd": _r(float(t.get("notional_usd") or 0.0))}
            for t in win_filled
            if t["side"] == "BUY" and min_position_usd > 0
            and 0 < float(t.get("notional_usd") or 0.0) < min_position_usd]
    rejections = [{"ts": t["ts"][:10], "ticker": t["ticker"], "side": t["side"],
                   "reason": str((t.get("detail") or {}).get("detail") or "")[:160]}
                  for t in window if t["status"] in ("rejected", "error")]
    # Same-name rebuy within 5 days of a sell.
    last_sell: dict[str, date] = {}
    rebuys = []
    for t in win_filled:
        d = _d(t["ts"])
        if not d:
            continue
        if t["side"] == "SELL":
            last_sell[t["ticker"]] = d
        elif t["ticker"] in last_sell and 0 <= (d - last_sell[t["ticker"]]).days <= 5:
            rebuys.append({"ticker": t["ticker"], "sold": last_sell[t["ticker"]].isoformat(),
                           "rebought": d.isoformat()})
    # Take-profit re-fires: more than one take_profit sell on the same name.
    tp_sells: dict[str, int] = defaultdict(int)
    for t in win_filled:
        if t["side"] == "SELL" and (t.get("detail") or {}).get("trigger") == "take_profit":
            tp_sells[t["ticker"]] += 1
    refires = {k: v for k, v in tp_sells.items() if v > 1}

    # Concentration / idle cash from the latest snapshot of this account.
    latest_pnl = db.query(
        "SELECT run_id, equity, cash FROM pnl WHERE (? IS NULL OR account=?) ORDER BY id DESC LIMIT 1",
        (account, account))
    concentration: dict[str, Any] = {}
    idle_cash_pct = None
    if latest_pnl:
        p = latest_pnl[0]
        eq = float(p["equity"] or 0.0)
        idle_cash_pct = _r(100.0 * float(p["cash"] or 0.0) / eq) if eq else None
        pos = db.query(
            """SELECT ticker, market_value FROM positions p
               JOIN (SELECT ticker AS t, MAX(id) AS mid FROM positions
                     WHERE run_id=? GROUP BY ticker) m ON m.mid = p.id""", (p["run_id"],))
        if pos and eq:
            weights = {r["ticker"]: _r(100.0 * float(r["market_value"] or 0.0) / eq) for r in pos}
            top = max(weights.items(), key=lambda kv: kv[1] or 0) if weights else (None, None)
            concentration = {"weights_pct": weights, "top": top[0], "top_pct": top[1]}

    # Guardrail outcomes and decision-agent fallbacks across the window's runs.
    guard = {"rejected": 0, "resized": 0, "by_check": {}}
    fallbacks = 0
    unscored: list[str] = []
    if run_ids:
        marks = ",".join("?" * len(run_ids))
        for r in db.query(
                f"SELECT approved, resized, checks_json FROM decisions WHERE run_id IN ({marks})",
                tuple(run_ids)):
            if not r["approved"]:
                guard["rejected"] += 1
                try:
                    failed = [c["name"] for c in json.loads(r["checks_json"] or "[]")
                              if not c.get("passed")]
                except Exception:
                    failed = []
                for name in failed[:1]:
                    guard["by_check"][name] = guard["by_check"].get(name, 0) + 1
            elif r["resized"]:
                guard["resized"] += 1
        fallbacks = db.query(
            f"SELECT COUNT(*) AS n FROM audit_log WHERE event='decision_llm_fallback' "
            f"AND run_id IN ({marks})", tuple(run_ids))[0]["n"]
        # Held names that received no score in the latest run.
        last_run = run_ids[-1]
        ana = outputs.get(last_run, {}).get("analysis")
        scored = {str(a.get("ticker") or "").upper() for a in ana} if isinstance(ana, list) else set()
        held = {r["ticker"] for r in db.query(
            "SELECT DISTINCT ticker FROM positions WHERE run_id=? AND shares > 0", (last_run,))}
        unscored = sorted(held - scored) if scored else []

    return {
        "trades_in_window": len(win_filled),
        "dust_orders": dust,
        "broker_rejections": rejections,
        "rebuys_within_5d": rebuys,
        "take_profit_refires": refires,
        "concentration": concentration,
        "idle_cash_pct": idle_cash_pct,
        "guardrail": guard,
        "decision_fallbacks": fallbacks,
        "unscored_holdings_latest_run": unscored,
    }


def _whipsaw(legs: list[dict]) -> dict:
    stops = [l for l in legs if l.get("exit_trigger") in STOP_TRIGGERS]
    measured = [l for l in stops if l.get("ret_10d_after_exit") is not None]
    whip = [l for l in measured if l["ret_10d_after_exit"] >= 5.0]
    return {"stop_outs": len(stops), "measured": len(measured), "whipsawed": len(whip),
            "rate_pct": _r(100.0 * len(whip) / len(measured)) if measured else None,
            "tickers": [l["ticker"] for l in whip]}


def _tp_left_on_table(legs: list[dict]) -> dict:
    tps = [l for l in legs if l.get("exit_trigger") == "take_profit"
           and l.get("ret_10d_after_exit") is not None]
    vals = [l["ret_10d_after_exit"] for l in tps]
    return {"n": len(tps),
            "avg_move_10d_after_exit_pct": _r(sum(vals) / len(vals)) if vals else None}


# ---------------------------------------------------------------------------
# Today's context for the model (what the agents did today, in their words)
# ---------------------------------------------------------------------------
def day_context(db, account: str | None, day: str) -> dict:
    import json
    runs = [dict(r) for r in db.query(
        """SELECT run_id, started_ts, mode FROM runs
           WHERE (? IS NULL OR account=?) AND substr(started_ts,1,10)=? AND mode IN ('live','preview','recommend')
           ORDER BY started_ts""", (account, account, day))]
    if not runs:
        return {"date": day, "runs": 0}
    ctx: dict[str, Any] = {"date": day, "runs": len(runs), "decisions": [], "exits": [],
                           "trades": [], "guardrail_rejections": []}
    for run in runs:
        rid = run["run_id"]
        for r in db.query("SELECT agent, output_json FROM agent_outputs WHERE run_id=? "
                          "AND agent IN ('decision','monitor') ORDER BY id", (rid,)):
            try:
                out = json.loads(r["output_json"] or "null")
            except Exception:
                continue
            if r["agent"] == "decision" and isinstance(out, dict):
                ctx["market_view"] = out.get("market_view")
                ctx["notes"] = out.get("notes")
                for o in out.get("orders") or []:
                    ctx["decisions"].append({k: o.get(k) for k in (
                        "ticker", "action", "confidence", "target_dollar_amount",
                        "suggested_stop_loss", "take_profit", "rationale", "_score")})
            elif r["agent"] == "monitor" and isinstance(out, dict):
                for e in out.get("exits") or []:
                    ctx["exits"].append({k: e.get(k) for k in (
                        "ticker", "action", "trigger", "reason", "shares")})
        for t in db.query("SELECT ticker, side, qty, price, notional_usd, status, detail "
                          "FROM trades WHERE run_id=? ORDER BY id", (rid,)):
            d = _loads(t["detail"])
            ctx["trades"].append({"ticker": t["ticker"], "side": t["side"],
                                  "qty": _r(t["qty"], 6), "price": _r(t["price"]),
                                  "usd": _r(t["notional_usd"]), "status": t["status"],
                                  "trigger": d.get("trigger"), "action": d.get("action"),
                                  "error": str(d.get("detail") or "")[:160]
                                  if t["status"] in ("rejected", "error") else None})
        for r in db.query("SELECT ticker, side, action, reasons FROM decisions "
                          "WHERE run_id=? AND approved=0", (rid,)):
            ctx["guardrail_rejections"].append({"ticker": r["ticker"], "side": r["side"],
                                                "action": r["action"],
                                                "reasons": r["reasons"]})
    return ctx


# ---------------------------------------------------------------------------
# LLM synthesis
# ---------------------------------------------------------------------------
def _compact_scorecard(sc: dict) -> dict:
    """The scorecard minus bulk the model doesn't need verbatim (it gets the
    aggregates plus the most recent legs/calls, not every row)."""
    out = dict(sc)
    out["closed_legs"] = sorted(sc.get("closed_legs") or [],
                                key=lambda l: l.get("exit_date") or "")[-30:]
    calls = dict(sc.get("calls") or {})
    calls["recent"] = (calls.get("recent") or [])[-25:]
    out["calls"] = calls
    return out


def _clip(text: Any, limit: int) -> str:
    """Bound a free-text field WITHOUT cutting mid-sentence: past ``limit``,
    drop back to the last paragraph or sentence boundary."""
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    head = s[:limit]
    cut = max(head.rfind("\n"), head.rfind(". "))
    return (head[:cut + 1] if cut > limit // 2 else head).rstrip()


def _normalize_output(parsed: Any) -> dict:
    """Coerce the model's review into the schema everything downstream expects."""
    p = parsed if isinstance(parsed, dict) else {}
    lessons = []
    for l in p.get("lessons") or []:
        if not isinstance(l, dict) or not l.get("text"):
            continue
        lid = str(l.get("id") or l["text"][:40]).strip().lower()
        lid = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in lid).strip("-")[:80]
        try:
            conf = int(max(0, min(100, float(l.get("confidence") or 0))))
        except (TypeError, ValueError):
            conf = 0
        applies = l.get("applies_to") or ["decision"]
        if isinstance(applies, str):
            applies = [applies]
        applies = [a for a in applies if a in ("decision", "monitor")] or ["decision"]
        status = str(l.get("status") or "new").lower()
        if status not in ("new", "reinforced", "retired"):
            status = "new"
        lessons.append({"id": lid, "text": str(l["text"]).strip()[:600],
                        "evidence": str(l.get("evidence") or "").strip()[:1000],
                        "confidence": conf, "applies_to": applies, "status": status})
    proposals = []
    for pr in p.get("parameter_proposals") or []:
        if not isinstance(pr, dict) or not pr.get("key"):
            continue
        try:
            proposed = float(pr.get("proposed"))
        except (TypeError, ValueError):
            continue
        try:
            n = int(pr.get("n_trades") or 0)
        except (TypeError, ValueError):
            n = 0
        proposals.append({"key": str(pr["key"]).strip(), "proposed": proposed,
                          "evidence": str(pr.get("evidence") or "")[:1000], "n_trades": n})
    return {
        "day_summary": _clip(p.get("day_summary"), 1200),
        "what_worked": [_clip(x, 300) for x in (p.get("what_worked") or [])][:8],
        "what_didnt": [_clip(x, 300) for x in (p.get("what_didnt") or [])][:8],
        "lessons": lessons[:12],
        "parameter_proposals": proposals[:6],
        # Asked for <= 1500; tolerate an overrun rather than truncate mid-word.
        "playbook": _clip(p.get("playbook"), 2200),
    }


def _offline_review(scorecard: dict, why: str) -> dict:
    """No model: a factual summary from the scorecard, no lessons, no proposals.
    The schedule must never fail because the model did."""
    a = (scorecard.get("aggregates") or {}).get("all") or {}
    n = scorecard.get("n_closed", 0)
    summary = (f"Deterministic review (model unavailable: {why}). {n} closed trade(s) in the "
               f"window; expectancy {a.get('expectancy_pct')}%, win rate {a.get('win_rate')}%, "
               f"total P&L ${a.get('total_pnl_usd')}.")
    return {"day_summary": summary, "what_worked": [], "what_didnt": [], "lessons": [],
            "parameter_proposals": [], "playbook": ""}


async def run_review_llm(scorecard: dict, context: dict, current_lessons: list[dict],
                         current_playbook: str | None, tunable: dict, model: str,
                         *, db=None, run_id: str | None = None) -> tuple[dict, str]:
    payload = {
        "scorecard": _compact_scorecard(scorecard),
        "today": context,
        "current_lessons": [{k: l.get(k) for k in ("id", "text", "evidence", "confidence",
                                                    "applies_to", "times_reinforced")}
                            for l in current_lessons],
        "current_playbook": current_playbook or "",
        "tunable_parameters": tunable,
    }
    parsed, raw = await generate_json(SYSTEM_PROMPT, payload, model)
    if not isinstance(parsed, dict):
        why = last_error() or "model returned an unexpected shape"
        print(f"\n⚠️  REVIEW AGENT FELL BACK to the deterministic summary — {why}")
        if db and run_id:
            db.audit(run_id, "WARN", "review_llm_fallback",
                     {"reason": why, "model": model, "raw_preview": (raw or "")[:2000]})
        out = _offline_review(scorecard, why)
        raw = raw or "(offline fallback)"
    else:
        out = _normalize_output(parsed)
    if db and run_id:
        db.log_agent_output(run_id, "review", model=model, input_obj=payload,
                            output_obj=out, raw_text=raw)
    return out, raw


# ---------------------------------------------------------------------------
# Parameter proposals — bounded, whitelisted, gated
# ---------------------------------------------------------------------------
def _lookup(raw: dict, dotted: str):
    cur: Any = raw
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def bound_proposal(key: str, proposed: float, current: float | None, tunable: dict) -> tuple[float | None, str | None]:
    """Clamp ``proposed`` to the key's bounds and to one ``step`` from
    ``current``. Returns ``(value, rejection_reason)``; a value equal to the
    current setting is a rejection ('no change')."""
    spec = tunable.get(key)
    if not isinstance(spec, dict):
        return None, f"{key} is not a tunable key"
    if not key.startswith("strategy."):
        return None, f"{key} is outside the strategy: block"
    try:
        lo, hi, step = float(spec["min"]), float(spec["max"]), float(spec.get("step") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None, f"{key} has an invalid bounds spec"
    value = max(lo, min(hi, float(proposed)))
    if current is not None and step > 0:
        try:
            cur = float(current)
        except (TypeError, ValueError):
            cur = None
        if cur is not None:
            value = max(cur - step, min(cur + step, value))
            # Keep values on the step grid relative to the current setting.
            value = cur + round((value - cur) / step) * step
            value = max(lo, min(hi, value))
    value = round(value, 6)
    if current is not None and abs(value - float(current)) < 1e-9:
        return value, "no change after applying bounds and step"
    return value, None


def apply_proposals(proposals: list[dict], *, cfg_raw: dict, tunable: dict,
                    n_closed: int, min_closed: int, auto_apply: bool,
                    write: Callable[[dict], dict] | None) -> list[dict]:
    """Decide each proposal's fate and (when allowed) write it to the overlay.

    Returns one record per proposal with ``status`` in
    ``applied | proposed | rejected`` and a ``reason``. ``write`` receives a
    nested patch like ``{"strategy": {"stop_atr_mult": 3.0}}`` and returns
    ``{"ok": bool, "error": str}`` (``server.config_io.apply_overrides``)."""
    records: list[dict] = []
    for pr in proposals or []:
        key = str(pr.get("key") or "")
        current = _lookup(cfg_raw, key)
        rec = {"key": key, "current": current, "proposed": pr.get("proposed"),
               "applied": None, "evidence": pr.get("evidence"),
               "n_trades": pr.get("n_trades"), "status": "rejected", "reason": None}
        value, why = bound_proposal(key, float(pr.get("proposed")), current, tunable)
        if why:
            rec["reason"] = why
            records.append(rec)
            continue
        if n_closed < int(min_closed):
            rec.update(status="proposed", applied=value,
                       reason=f"gated: {n_closed} closed trades < {min_closed} required "
                              "for auto-apply — recorded for manual review")
            records.append(rec)
            continue
        if not auto_apply or write is None:
            rec.update(status="proposed", applied=value,
                       reason="auto_apply is off — recorded for manual review")
            records.append(rec)
            continue
        section, _, leaf = key.partition(".")
        result = write({section: {leaf: value}}) or {}
        if result.get("ok"):
            rec.update(status="applied", applied=value,
                       reason=(f"applied within bounds ({current} -> {value})"
                               + (" [clamped]" if abs(value - float(pr.get('proposed'))) > 1e-9
                                  else "")))
        else:
            rec.update(status="rejected", reason=f"write failed: {result.get('error')}")
        records.append(rec)
    return records

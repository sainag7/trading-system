"""Read-only data layer for the web API.

Replicates every query the Streamlit dashboard ran against ``storage/trading.db``,
but returns plain JSON with timestamps and money/percent values already formatted
(see :mod:`server.format`) so the frontend never renders a raw ISO string or a bare
float. All functions are read-only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from config import load_config
from storage.db import Database
from server import format as F
from server import config_io

# When this server process started. The frontend compares its own build stamp
# against this to detect that it is talking to a server older than itself — the
# UI is served from disk on every request, so a long-lived process happily
# serves a newer bundle whose endpoints it does not have.
_STARTED = datetime.now(timezone.utc).isoformat()

ACTION_LABEL = {"buy": "buy", "add": "add", "trim": "trim", "hold": "hold",
                "pass": "pass", "sell": "sell"}
ACTION_RANK = {"buy": 0, "add": 1, "trim": 2, "sell": 2, "hold": 3, "pass": 4}

_db: Database | None = None


def db() -> Database:
    global _db
    if _db is None:
        _db = Database(str(load_config().db_path))
    return _db


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in db().query(sql, params)]
    except Exception:
        return []


def _one(sql: str, params: tuple = ()) -> dict | None:
    r = _rows(sql, params)
    return r[0] if r else None


def _parse(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


# --- shared lookups -------------------------------------------------------
def _latest_decision() -> tuple[dict, str, str]:
    row = _one("SELECT run_id, ts, output_json FROM agent_outputs WHERE agent='decision' "
               "ORDER BY ts DESC LIMIT 1")
    if not row:
        return {}, "", ""
    return (_parse(row["output_json"]) or {}), row["run_id"], row["ts"]


def _latest_analysis_map() -> dict[str, dict]:
    row = _one("SELECT output_json FROM agent_outputs WHERE agent='analysis' "
               "ORDER BY ts DESC LIMIT 1")
    arr = _parse(row["output_json"]) if row else None
    if not isinstance(arr, list):
        return {}
    return {str(i.get("ticker")): i for i in arr if isinstance(i, dict) and i.get("ticker")}


def _run_mode(run_id: str) -> str:
    """Run mode for ``run_id``.

    Historical rows still carry a ``profile=...`` token in ``runs.notes`` from
    before strategy profiles were removed. It is inert — nothing parses it now.
    """
    row = _one("SELECT mode FROM runs WHERE run_id=?", (run_id,))
    return row["mode"] if row else "?"


def _data_ok(run_id: str) -> bool:
    if not run_id:
        return False
    for r in _rows("SELECT output_json FROM agent_outputs WHERE agent='research' AND run_id=?",
                   (run_id,)):
        obj = _parse(r["output_json"]) or {}
        if (obj.get("technicals") or {}).get("price") is not None:
            return True
    return False


# --- status ---------------------------------------------------------------
def status() -> dict:
    import market
    from agents import llm

    cfg = load_config()
    _, run_id, ts = _latest_decision()
    mode = _run_mode(run_id) if run_id else cfg.mode
    try:
        backend = llm.backend_name()
    except Exception:
        backend = "unknown"
    data_ok = _data_ok(run_id)
    healthy = backend not in ("offline", "unknown") and data_ok
    warnings = []
    if backend in ("offline", "unknown"):
        warnings.append("No LLM backend — set ANTHROPIC_API_KEY in .env.")
    if not data_ok:
        warnings.append("No market data — install yfinance or set ALPHAVANTAGE_API_KEY.")

    accounts = [
        {"role": role, "number": (meta or {}).get("number"),
         "label": f"{role}" + (f" ({(meta or {}).get('number')})"
                               if (meta or {}).get("number") else "")}
        for role, meta in (cfg.accounts or {}).items()
    ]
    is_open = market.is_market_open()
    is_day = market.is_trading_day()
    return {
        "market": {
            "open": is_open,
            "trading_day": is_day,
            "label": "Open" if is_open else ("Closed" if is_day else "Closed (holiday/weekend)"),
            "now_et": F.fmt_ts(market.now_et().isoformat(), "time"),
        },
        "backend": backend,
        "data_ok": data_ok,
        "healthy": healthy,
        "warnings": warnings,
        "server_started_ts": _STARTED,
        "mode": cfg.mode,
        "kill_switch": {
            "engaged": cfg.kill_switch_enabled,
            "pinned": bool(cfg.raw.get("kill_switch", False)),
        },
        "accounts": accounts,
        "latest_run": (
            {"run_id": run_id, "mode": mode,
             "ts": ts, "ts_display": F.fmt_ts(ts)} if run_id else None
        ),
    }


# --- ideas ----------------------------------------------------------------
def ideas() -> dict:
    obj, run_id, ts = _latest_decision()
    if not obj:
        return {"has_data": False}
    mode = _run_mode(run_id)
    amap = _latest_analysis_map()
    orders = obj.get("orders", []) or []
    rows = []
    for o in orders:
        t = str(o.get("ticker", "")).upper()
        a = amap.get(t, {})
        act = str(o.get("action", "")).lower()
        setup = (a.get("swing_setup") or "—")
        setup = "—" if setup == "none" else setup
        rows.append({
            "ticker": t,
            "action": act,
            "action_label": ACTION_LABEL.get(act, act),
            "composite": a.get("composite_score", o.get("_score")),
            "tech": a.get("technical_score"),
            "fund": a.get("fundamental_score"),
            "sent": a.get("sentiment_score"),
            "setup": setup,
            "conf": o.get("confidence"),
            "target_usd": o.get("target_dollar_amount") or None,
            "target_display": F.fmt_money(o.get("target_dollar_amount"), 0)
            if o.get("target_dollar_amount") else "—",
            "stop": o.get("suggested_stop_loss"),
            "stop_display": F.fmt_money(o.get("suggested_stop_loss")),
            "take_profit": o.get("take_profit"),
            "tp_display": F.fmt_money(o.get("take_profit")),
            "_rank": ACTION_RANK.get(act, 9),
        })
    rows.sort(key=lambda r: (r["_rank"], -(r["composite"] or 0)))
    for r in rows:
        r.pop("_rank", None)

    mv = str(obj.get("market_view", ""))
    exits = []
    mon = _one("SELECT output_json FROM agent_outputs WHERE agent='monitor' "
               "ORDER BY ts DESC LIMIT 1")
    if mon:
        for e in (_parse(mon["output_json"]) or {}).get("exits", []) or []:
            exits.append({
                "ticker": str(e.get("ticker", "")).upper(),
                "action": e.get("action"),
                "trigger": e.get("trigger"),
                "reason": e.get("reason"),
                "confidence": e.get("confidence"),
            })
    return {
        "has_data": True,
        "run": {"run_id": run_id, "mode": mode,
                "ts_display": F.fmt_ts(ts)},
        "market_view": mv,
        "fallback": "fallback" in mv.lower(),
        "rows": rows,
        "exits": exits,
    }


def idea_detail(ticker: str) -> dict:
    ticker = ticker.upper()
    _, run_id, _ = _latest_decision()
    obj, _, _ = _latest_decision()
    order = next((o for o in (obj.get("orders", []) or [])
                  if str(o.get("ticker", "")).upper() == ticker), {})
    analysis = _latest_analysis_map().get(ticker, {})
    rrow = _one("SELECT output_json FROM agent_outputs WHERE agent='research' AND ticker=? "
                "ORDER BY ts DESC LIMIT 1", (ticker,))
    research = (_parse(rrow["output_json"]) if rrow else {}) or {}
    grow = _one("SELECT approved, approved_usd, resized, reasons, requested_usd FROM decisions "
                "WHERE ticker=? AND run_id=? ORDER BY ts DESC LIMIT 1", (ticker, run_id))
    guardrail = None
    if grow:
        guardrail = {
            "approved": bool(grow["approved"]),
            "resized": bool(grow["resized"]),
            "requested_display": F.fmt_money(grow["requested_usd"]),
            "approved_display": F.fmt_money(grow["approved_usd"]),
            "reasons": _parse(grow["reasons"]) or [],
        }
    return {
        "ticker": ticker,
        "action": str(order.get("action", "—")),
        "composite": analysis.get("composite_score", order.get("_score")),
        "setup": analysis.get("swing_setup", "—"),
        "conf": order.get("confidence"),
        "thesis": analysis.get("one_line_thesis") or order.get("rationale"),
        "rationale": order.get("rationale"),
        "key_risks": analysis.get("key_risks") or [],
        "plan": {
            "stop_display": F.fmt_money(order.get("suggested_stop_loss")),
            "target_display": F.fmt_money(order.get("take_profit")),
            "max_hold_until": F.fmt_day(order.get("max_hold_until")),
        },
        "technicals": research.get("technicals", {}) or {},
        "fundamentals": research.get("fundamentals", {}) or {},
        "news": research.get("news_sentiment", {}) or {},
        "scoring": {
            "composite": analysis.get("composite_score"),
            "technical": analysis.get("technical_score"),
            "fundamental": analysis.get("fundamental_score"),
            "sentiment": analysis.get("sentiment_score"),
            "breakdown": analysis.get("score_breakdown") or {},
        },
        "guardrail": guardrail,
        "data_quality": research.get("data_quality", {}) or {},
        "raw": {"research": research, "analysis": analysis, "decision": order},
    }


# --- deep dive ------------------------------------------------------------
def deepdive_tickers() -> dict:
    hidden = set(config_io.get_hidden_tickers())
    rows = _rows(
        "SELECT ticker, MAX(ts) AS latest, COUNT(*) AS n FROM agent_outputs "
        "WHERE agent='explain' AND ticker IS NOT NULL GROUP BY ticker ORDER BY latest DESC")
    visible = [
        {"ticker": r["ticker"], "latest_display": F.fmt_ts(r["latest"], "datetime"),
         "report_count": r["n"]}
        for r in rows if r["ticker"] not in hidden
    ]
    return {"tickers": visible, "hidden": sorted(hidden)}


def deepdive_reports(ticker: str) -> list[dict]:
    out = []
    for r in _rows("SELECT ts, run_id, output_json FROM agent_outputs "
                   "WHERE agent='explain' AND ticker=? ORDER BY ts DESC LIMIT 20",
                   (ticker.upper(),)):
        report = _parse(r["output_json"])
        if report is None:
            continue
        out.append({"ts": r["ts"], "ts_display": F.fmt_ts(r["ts"]),
                    "run_id": r["run_id"], "report": report})
    return out


# --- portfolio ------------------------------------------------------------
def _account_label(cfg, number) -> str:
    if number is None:
        return "legacy (unscoped)"
    for role, meta in (cfg.accounts or {}).items():
        if str((meta or {}).get("number")) == str(number):
            return f"{role} ({number})"
    return f"account {number}"


def portfolio(account: str | None = None) -> dict:
    cfg = load_config()
    pnl = _rows("SELECT * FROM pnl ORDER BY ts")
    if not pnl:
        return {"has_data": False}

    numbers = [r.get("account") for r in pnl]
    present = sorted({n for n in numbers if n})
    has_legacy = any(n is None for n in numbers)
    keys = list(present) + (["__legacy__"] if has_legacy else [])
    accounts = [{"key": k, "label": "legacy (unscoped)" if k == "__legacy__"
                 else _account_label(cfg, k)} for k in keys]

    selected = account
    if selected not in keys:
        latest_acct = pnl[-1].get("account")
        selected = latest_acct if latest_acct in present else (keys[0] if keys else None)

    if selected == "__legacy__":
        view = [r for r in pnl if r.get("account") is None]
    elif selected is not None:
        view = [r for r in pnl if r.get("account") == selected]
    else:
        view = pnl
    if not view:
        return {"has_data": True, "accounts": accounts, "selected": selected,
                "empty": True}

    last = view[-1]
    series = [{"ts_display": F.fmt_ts(r["ts"], "datetime"),
               "equity": r["equity"], "peak_equity": r["peak_equity"]} for r in view]

    if selected and selected != "__legacy__":
        positions = _rows(
            "SELECT ticker, shares, avg_cost, market_value, sector FROM positions "
            "WHERE account = ? AND run_id = (SELECT run_id FROM positions "
            "WHERE account = ? ORDER BY ts DESC LIMIT 1)", (selected, selected))
    else:
        positions = _rows(
            "SELECT ticker, shares, avg_cost, market_value, sector FROM positions "
            "WHERE run_id = (SELECT run_id FROM positions ORDER BY ts DESC LIMIT 1)")

    pos_out, sector_tot = [], {}
    for p in positions:
        pos_out.append({
            "ticker": p["ticker"],
            "shares_display": F.fmt_shares(p["shares"]),
            "avg_cost_display": F.fmt_money(p["avg_cost"]),
            "market_value": p["market_value"],
            "market_value_display": F.fmt_money(p["market_value"]),
            "sector": p["sector"] or "Unknown",
        })
        sector_tot[p["sector"] or "Unknown"] = sector_tot.get(p["sector"] or "Unknown", 0) \
            + (p["market_value"] or 0)

    return {
        "has_data": True,
        "accounts": accounts,
        "selected": selected,
        "summary": {
            "equity_display": F.fmt_money(last["equity"]),
            "cash_display": F.fmt_money(last["cash"]),
            # drawdown_pct is stored as a FRACTION (0.15 == 15%), while fmt_pct
            # expects percent units — without the scale a 15% drawdown, the level
            # that trips the halt, rendered as a harmless-looking "0.1%".
            "drawdown_display": F.fmt_pct((last["drawdown_pct"] or 0.0) * 100.0),
            "peak_display": F.fmt_money(last["peak_equity"]),
        },
        "series": series,
        "positions": pos_out,
        "sectors": [{"sector": s, "market_value": round(v, 2)}
                    for s, v in sorted(sector_tot.items(), key=lambda kv: -kv[1])],
    }


# --- activity -------------------------------------------------------------
def activity() -> dict:
    decisions = [{
        "ts_display": F.fmt_ts(r["ts"], "datetime"),
        "ticker": r["ticker"], "side": r["side"], "action": r["action"],
        "requested_display": F.fmt_money(r["requested_usd"]),
        "approved": bool(r["approved"]),
        "approved_display": F.fmt_money(r["approved_usd"]),
        "resized": bool(r["resized"]),
        "reasons": _parse(r["reasons"]) or [],
    } for r in _rows(
        "SELECT ts, ticker, side, action, requested_usd, approved, approved_usd, "
        "resized, reasons FROM decisions ORDER BY ts DESC LIMIT 200")]

    orders = [{
        "ts_display": F.fmt_ts(r["ts"], "datetime"),
        "mode": r["mode"], "ticker": r["ticker"], "side": r["side"],
        "qty_display": F.fmt_shares(r["qty"]),
        "notional_display": F.fmt_money(r["notional_usd"]),
        "status": r["status"],
    } for r in _rows(
        "SELECT ts, mode, ticker, side, qty, notional_usd, status "
        "FROM orders ORDER BY ts DESC LIMIT 100")]

    fills = [{
        "ts_display": F.fmt_ts(r["ts"], "datetime"),
        "ticker": r["ticker"], "side": r["side"],
        "qty_display": F.fmt_shares(r["qty"]),
        "price_display": F.fmt_money(r["price"]),
        "notional_display": F.fmt_money(r["notional_usd"]),
    } for r in _rows(
        "SELECT ts, ticker, side, qty, price, notional_usd "
        "FROM fills ORDER BY ts DESC LIMIT 100")]

    audit = [{
        "ts_display": F.fmt_ts(r["ts"], "datetime"),
        "level": r["level"], "event": r["event"], "detail": r["detail"],
    } for r in _rows(
        "SELECT ts, level, event, detail FROM audit_log ORDER BY ts DESC LIMIT 300")]

    return {"decisions": decisions, "orders": orders, "fills": fills, "audit": audit}


# --- LLM token / cost usage -----------------------------------------------
def _fmt_cost(v: float | None) -> str:
    """Money at sub-cent resolution — individual calls cost fractions of a cent,
    and rounding them to $0.00 would make the per-agent table useless."""
    if v is None:
        return "—"
    return f"${v:,.2f}" if v >= 1 else f"${v:.4f}"


def _fmt_tokens(n: int | None) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _usage_row(r: dict) -> dict:
    total_tokens = (int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
                    + int(r.get("cache_write_tokens") or 0)
                    + int(r.get("cache_read_tokens") or 0))
    return {
        **r,
        "total_tokens": total_tokens,
        "total_tokens_display": _fmt_tokens(total_tokens),
        "input_display": _fmt_tokens(r.get("input_tokens")),
        "output_display": _fmt_tokens(r.get("output_tokens")),
        "cache_read_display": _fmt_tokens(r.get("cache_read_tokens")),
        "cost_display": _fmt_cost(r.get("cost_usd")),
    }


def usage(days: int = 30) -> dict:
    """Token + cost totals for the dashboard.

    Note there is no history before this feature shipped — nothing recorded
    tokens previously, so an empty result means "not yet collected", not "$0".
    """
    d = db()
    daily = [_usage_row(dict(r)) for r in d.usage_daily(days)]
    for row in daily:
        row["day_display"] = F.fmt_ts(row["day"], "date") if row.get("day") else ""
    totals = _usage_row(d.usage_totals(days))
    all_time = _usage_row(d.usage_totals(None))
    today = daily[-1] if daily and daily[-1]["day"] == _today() else None
    return {
        "days": days,
        "daily": daily,
        "by_agent": [_usage_row(dict(r)) for r in d.usage_grouped("agent", days)],
        "by_model": [_usage_row(dict(r)) for r in d.usage_grouped("model", days)],
        # Split by backend: the Messages API bills the ANTHROPIC_API_KEY while the
        # Agent SDK path inherits Claude Code's OAuth and may bill a subscription.
        # Adding them into one number would be misleading, so the UI shows both.
        "by_backend": [_usage_row(dict(r)) for r in d.usage_grouped("backend", days)],
        "totals": totals,
        "all_time": all_time,
        "today": today or _usage_row({}),
        "has_data": bool(all_time.get("calls")),
    }


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --- review agent (Learning tab) ----------------------------------------------
def _review_account(account: str | None) -> tuple[str | None, str]:
    """Resolve a role or number to ``(number, label)``; defaults to agentic."""
    cfg = load_config()
    accts = cfg.accounts or {}
    role = account or ("agentic" if "agentic" in accts else None)
    if role in accts:
        number = str((accts[role] or {}).get("number") or "") or None
        return number, f"{role} ({number})" if number else role
    return account, _account_label(cfg, account)


def review(account: str | None = None) -> dict:
    """Everything the Learning tab shows: the latest review, the lessons in
    force, proposals and their fates, and the review history."""
    d = db()
    cfg = load_config()
    number, label = _review_account(account)
    latest = d.latest_review(number)
    lessons = d.all_lessons(number)
    for l in lessons:
        l["created_display"] = F.fmt_ts(l.get("created_ts")) if l.get("created_ts") else ""
        l["reinforced_display"] = (F.fmt_ts(l.get("last_reinforced_ts"))
                                   if l.get("last_reinforced_ts") else "")
    proposals = d.proposals(number)
    for p in proposals:
        p["ts_display"] = F.fmt_ts(p.get("ts")) if p.get("ts") else ""
    history = d.review_history(number)
    for h in history:
        h["ts_display"] = F.fmt_ts(h.get("ts")) if h.get("ts") else ""
    rv = cfg.review or {}
    scorecard = (latest or {}).get("scorecard") or {}
    return {
        "account": number,
        "account_label": label,
        "latest": ({k: latest.get(k) for k in ("id", "ts", "run_id", "review_date",
                                                "window_days", "model")}
                   | {"ts_display": F.fmt_ts(latest["ts"]) if latest.get("ts") else "",
                      "output": latest.get("output") or {}}) if latest else None,
        "scorecard": scorecard,
        "lessons": lessons,
        "active_count": sum(1 for l in lessons if l.get("status") == "active"),
        "proposals": proposals,
        "history": history,
        "settings": {
            "auto_apply": bool(rv.get("auto_apply", False)),
            "min_closed_trades": int(rv.get("min_closed_trades", 20) or 0),
            "window_days": int(rv.get("window_days", 60) or 60),
            "tunable": rv.get("tunable") or {},
            "model": (cfg.models or {}).get("review_agent"),
        },
        "n_closed": int(scorecard.get("n_closed") or 0),
        "has_data": latest is not None,
    }


def apply_proposal(proposal_id: int) -> dict:
    """Apply a recorded proposal by hand (the dashboard's Apply button). The
    bounds and whitelist still apply; only the trade-count gate is waived —
    that gate exists for the AUTOMATIC path, a human clicking is the review."""
    from agents.review_agent import bound_proposal
    d = db()
    p = d.get_proposal(proposal_id)
    if not p:
        return {"ok": False, "error": "no such proposal"}
    if p.get("status") == "applied":
        return {"ok": False, "error": "already applied"}
    cfg = load_config()
    tunable = (cfg.review or {}).get("tunable") or {}
    key = str(p["key"])
    section, _, leaf = key.partition(".")
    current = (cfg.raw.get(section) or {}).get(leaf) if section else None
    value, why = bound_proposal(key, float(p["proposed"]), current, tunable)
    if why:
        d.mark_proposal(proposal_id, "rejected", reason=why)
        return {"ok": False, "error": why}
    res = config_io.apply_overrides({section: {leaf: value}})
    if not res.get("ok"):
        d.mark_proposal(proposal_id, "rejected", reason=f"write failed: {res.get('error')}")
        return {"ok": False, "error": res.get("error")}
    d.mark_proposal(proposal_id, "applied", applied=value,
                    reason=f"applied by hand from the dashboard ({current} -> {value})")
    d.audit(f"dashboard-{_today()}", "INFO", "review_param_applied",
            {"key": key, "current": current, "applied": value, "proposal_id": proposal_id,
             "source": "dashboard"})
    return {"ok": True, "key": key, "applied": value}


def dismiss_proposal(proposal_id: int) -> dict:
    d = db()
    if not d.get_proposal(proposal_id):
        return {"ok": False, "error": "no such proposal"}
    d.mark_proposal(proposal_id, "dismissed", reason="dismissed from the dashboard")
    return {"ok": True}


def retire_lesson(lesson_id: str, account: str | None = None) -> dict:
    number, _ = _review_account(account)
    ok = db().retire_lesson(lesson_id, number)
    return {"ok": ok} if ok else {"ok": False, "error": "no such lesson"}

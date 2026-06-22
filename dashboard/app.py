"""Streamlit dashboard — read-only view of the trading system's audit DB.

Shows a system-health banner, the latest recommendations (click a stock to see
the full research/analysis/decision/guardrail reasoning behind it), equity/
drawdown, positions, orders/fills, and the audit log. It only READS the SQLite
database — it never trades and has no write path.

Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# Make the repo root importable so we can reuse the config + llm backend probe.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from config import load_config  # noqa: E402


@st.cache_data(ttl=15)
def load_table(db_path: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    con = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(sql, con, params=params)
    except Exception:
        return pd.DataFrame()
    finally:
        con.close()


def _parse(json_str) -> dict | list | None:
    try:
        return json.loads(json_str)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DB read helpers (latest agent outputs / guardrail verdicts)
# ---------------------------------------------------------------------------
def latest_decision(db_path: str) -> tuple[dict, str, str]:
    df = load_table(
        db_path,
        "SELECT run_id, ts, output_json FROM agent_outputs WHERE agent='decision' "
        "ORDER BY ts DESC LIMIT 1",
    )
    if df.empty:
        return {}, "", ""
    return (_parse(df.iloc[0]["output_json"]) or {}), df.iloc[0]["run_id"], df.iloc[0]["ts"]


def latest_analysis_map(db_path: str) -> dict[str, dict]:
    df = load_table(
        db_path,
        "SELECT output_json FROM agent_outputs WHERE agent='analysis' ORDER BY ts DESC LIMIT 1",
    )
    if df.empty:
        return {}
    arr = _parse(df.iloc[0]["output_json"]) or []
    return {str(i.get("ticker")): i for i in arr if isinstance(i, dict) and i.get("ticker")}


def research_for(db_path: str, ticker: str) -> dict:
    df = load_table(
        db_path,
        "SELECT output_json FROM agent_outputs WHERE agent='research' AND ticker=? "
        "ORDER BY ts DESC LIMIT 1",
        (ticker,),
    )
    return (_parse(df.iloc[0]["output_json"]) or {}) if not df.empty else {}


def guardrail_for(db_path: str, ticker: str, run_id: str) -> dict:
    df = load_table(
        db_path,
        "SELECT approved, approved_usd, resized, reasons, requested_usd FROM decisions "
        "WHERE ticker=? AND run_id=? ORDER BY ts DESC LIMIT 1",
        (ticker, run_id),
    )
    return df.iloc[0].to_dict() if not df.empty else {}


def system_health(db_path: str, run_id: str) -> tuple[str, bool]:
    """Return (llm_backend, have_market_data) for the latest run."""
    try:
        from agents import llm
        backend = llm.backend_name()
    except Exception:
        backend = "unknown"
    have_price = False
    if run_id:
        rdf = load_table(
            db_path,
            "SELECT output_json FROM agent_outputs WHERE agent='research' AND run_id=?",
            (run_id,),
        )
        for _, row in rdf.iterrows():
            obj = _parse(row["output_json"]) or {}
            if (obj.get("technicals") or {}).get("price") is not None:
                have_price = True
                break
    return backend, have_price


def _ticker_from_selection(event, df: pd.DataFrame) -> str | None:
    """Read the clicked ticker from a st.dataframe selection event (robustly)."""
    try:
        sel = getattr(event, "selection", None)
        rows = sel.get("rows") if isinstance(sel, dict) else getattr(sel, "rows", None)
        if rows:
            return str(df.iloc[rows[0]]["ticker"])
    except Exception:
        pass
    return None


def _fmt(v, money=False, pct=False):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if isinstance(v, (int, float)):
        if money:
            return f"${v:,.2f}"
        if pct:
            return f"{v:.2f}%"
        return f"{v:,.2f}"
    return str(v)


# ---------------------------------------------------------------------------
# Per-stock drill-down
# ---------------------------------------------------------------------------
def render_detail(db_path: str, ticker: str, order: dict, analysis: dict, run_id: str) -> None:
    research = research_for(db_path, ticker)
    tech = research.get("technicals", {}) or {}
    fund = research.get("fundamentals", {}) or {}
    news = research.get("news_sentiment", {}) or {}
    macro = research.get("macro_context", {}) or {}
    dq = research.get("data_quality", {}) or {}
    gr = guardrail_for(db_path, ticker, run_id)

    action = str(order.get("action", "—")).upper()
    conf = order.get("confidence", "—")
    composite = analysis.get("composite_score", order.get("_score", "—"))
    setup = analysis.get("swing_setup", "—")

    st.markdown(f"### {ticker} — **{action}**  ·  composite **{composite}**  ·  setup *{setup}*  ·  conf {conf}")
    thesis = analysis.get("one_line_thesis") or order.get("rationale")
    if thesis:
        st.markdown(f"> {thesis}")

    tabs = st.tabs(["Why", "Technicals", "Fundamentals", "News", "Scoring", "Guardrail", "Raw"])

    # --- Why -------------------------------------------------------------
    with tabs[0]:
        if order.get("rationale"):
            st.markdown(f"**Decision rationale:** {order['rationale']}")
        risks = analysis.get("key_risks") or []
        if risks:
            st.markdown("**Key risks:**")
            for r in risks:
                st.markdown(f"- {r}")
        if order.get("suggested_stop_loss") or order.get("take_profit"):
            st.markdown(
                f"**Plan:** stop {_fmt(order.get('suggested_stop_loss'), money=True)} · "
                f"target {_fmt(order.get('take_profit'), money=True)} · "
                f"by {order.get('max_hold_until', '—')}")
        if dq.get("missing_fields"):
            st.caption("Missing data: " + ", ".join(dq["missing_fields"]))

    # --- Technicals ------------------------------------------------------
    with tabs[1]:
        c = st.columns(4)
        c[0].metric("Price", _fmt(tech.get("price"), money=True))
        c[1].metric("Trend", f"{tech.get('trend', '—')} ({tech.get('trend_strength', '—')})")
        c[2].metric("RSI(14)", _fmt(tech.get("rsi14")))
        c[3].metric("ATR %", _fmt(tech.get("atr20_pct"), pct=True))
        c = st.columns(4)
        c[0].metric("SMA50", _fmt(tech.get("sma50"), money=True))
        c[1].metric("SMA200", _fmt(tech.get("sma200"), money=True))
        c[2].metric("vs 52w high", _fmt(tech.get("distance_from_52w_high_pct"), pct=True))
        c[3].metric("Vol vs avg", _fmt(tech.get("volume_vs_avg")))
        macd = tech.get("macd") or {}
        st.caption(f"MACD: {_fmt(macd.get('macd'))} · signal {_fmt(macd.get('signal'))} · "
                   f"hist {_fmt(macd.get('histogram'))}")

    # --- Fundamentals ----------------------------------------------------
    with tabs[2]:
        c = st.columns(4)
        c[0].metric("Revenue TTM", _fmt(fund.get("revenue_ttm"), money=True))
        c[1].metric("Rev growth YoY", _fmt(fund.get("revenue_growth_yoy"), pct=True))
        c[2].metric("EPS TTM", _fmt(fund.get("eps_ttm")))
        c[3].metric("EPS growth YoY", _fmt(fund.get("eps_growth_yoy"), pct=True))
        c = st.columns(4)
        c[0].metric("P/E", _fmt(fund.get("pe_ratio")))
        c[1].metric("P/S", _fmt(fund.get("ps_ratio")))
        c[2].metric("Gross margin", _fmt(fund.get("gross_margin"), pct=True))
        c[3].metric("Op margin", _fmt(fund.get("operating_margin"), pct=True))
        c = st.columns(4)
        c[0].metric("Debt/Equity", _fmt(fund.get("debt_to_equity")))
        c[1].metric("Free cash flow", _fmt(fund.get("free_cash_flow"), money=True))
        c[2].metric("Next earnings", fund.get("next_earnings_date") or "—")
        c[3].metric("Sector", fund.get("sector", "—"))

    # --- News ------------------------------------------------------------
    with tabs[3]:
        st.markdown(f"**Aggregate sentiment:** {_fmt(news.get('aggregate_score'))} "
                    f"({news.get('aggregate_label', '—')}) · {news.get('article_count', 0)} articles "
                    f"· source: {news.get('source') or 'none'}")
        if news.get("summary"):
            st.markdown(news["summary"])
        for h in (news.get("headlines") or []):
            title = h.get("title") or "(untitled)"
            url = h.get("url")
            label = h.get("sentiment_label") or ""
            score = h.get("sentiment_score")
            line = f"- [{title}]({url})" if url else f"- {title}"
            if label or score is not None:
                line += f"  *( {label} {('' if score is None else f'{score:+.2f}')} )*"
            st.markdown(line)
        if not (news.get("headlines")):
            st.caption("No headlines (news endpoint unavailable for this run).")

    # --- Scoring ---------------------------------------------------------
    with tabs[4]:
        c = st.columns(4)
        c[0].metric("Composite", _fmt(analysis.get("composite_score")))
        c[1].metric("Technical", _fmt(analysis.get("technical_score")))
        c[2].metric("Fundamental", _fmt(analysis.get("fundamental_score")))
        c[3].metric("Sentiment", _fmt(analysis.get("sentiment_score")))
        bd = analysis.get("score_breakdown") or {}
        for grp in ("technical", "fundamental"):
            if isinstance(bd.get(grp), dict):
                st.caption(f"{grp.title()} components: " +
                           ", ".join(f"{k}={v}" for k, v in bd[grp].items()))
        if isinstance(bd.get("weights"), dict):
            st.caption("Weights: " + ", ".join(f"{k}={v}" for k, v in bd["weights"].items()))

    # --- Guardrail -------------------------------------------------------
    with tabs[5]:
        if gr:
            verdict = "✅ approved" if gr.get("approved") else "❌ rejected"
            if gr.get("resized"):
                verdict += " (resized)"
            st.markdown(f"**Guardrail verdict:** {verdict}")
            st.markdown(f"Requested {_fmt(gr.get('requested_usd'), money=True)} → "
                        f"approved {_fmt(gr.get('approved_usd'), money=True)}")
            reasons = _parse(gr.get("reasons")) or []
            for r in reasons:
                st.markdown(f"- {r}")
        else:
            st.caption("No guardrail record (this name was hold/pass — no order proposed).")

    # --- Raw -------------------------------------------------------------
    with tabs[6]:
        st.json({"research": research, "analysis": analysis, "decision": order})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="Swing-Trading System", layout="wide")
    config = load_config()
    db_path = str(config.db_path)

    st.title("📈 Swing-Trading System — Dashboard")
    if not Path(db_path).exists():
        st.warning(f"No database yet at `{db_path}`. Run the orchestrator first "
                   "(`python orchestrator.py --mode recommend`).")
        return

    st.caption(f"Read-only view of `{db_path}` · mode default: **{config.mode}**")

    obj, run_id, ts = latest_decision(db_path)
    analysis_map = latest_analysis_map(db_path)

    # --- System health banner (explains an "empty"/all-50 dashboard) -----
    backend, have_price = system_health(db_path, run_id)
    if backend != "offline" and backend != "unknown" and have_price:
        st.success(f"System healthy — LLM backend: **{backend}**; market data: **available**.")
    else:
        problems = []
        if backend in ("offline", "unknown"):
            problems.append("**LLM backend = offline** → install deps and set a key: "
                            "`pip install -r requirements.txt` then `ANTHROPIC_API_KEY` in `.env`")
        if not have_price:
            problems.append("**market data = none** → install `yfinance` (no key needed) or set "
                            "`ALPHAVANTAGE_API_KEY` in `.env`")
        st.warning(
            "⚠️ **Running degraded — recommendations are placeholders.** With no data/LLM every "
            "score defaults to a neutral **50**, which is below the buy threshold, so every stock "
            "shows **pass**. Fix:\n\n- " + "\n- ".join(problems) +
            "\n\nThen re-run `python orchestrator.py --mode recommend` and refresh this page. "
            "Run everything from the **same** Python environment you install into."
        )

    # --- Latest recommendations (click a row to drill in) ----------------
    st.subheader("📋 Latest recommendations")
    if not obj:
        st.info("No recommendations yet. Run `python orchestrator.py --mode recommend`.")
    else:
        mode_row = load_table(db_path, "SELECT mode FROM runs WHERE run_id=?", (run_id,))
        run_mode = mode_row.iloc[0]["mode"] if not mode_row.empty else "?"
        st.caption(f"From the latest **{run_mode}** run ({ts}). "
                   "Click any row (or use the selector) to see the full reasoning.")
        if obj.get("market_view"):
            st.markdown(f"**Market view:** {obj['market_view']}")

        orders = obj.get("orders", []) or []
        if not orders:
            st.info("No recommendations in the latest run.")
        else:
            rows = []
            for o in orders:
                t = str(o.get("ticker", "")).upper()
                a = analysis_map.get(t, {})
                rows.append({
                    "ticker": t,
                    "action": o.get("action"),
                    "composite": a.get("composite_score", o.get("_score")),
                    "tech": a.get("technical_score"),
                    "fund": a.get("fundamental_score"),
                    "sent": a.get("sentiment_score"),
                    "setup": a.get("swing_setup"),
                    "confidence": o.get("confidence"),
                    "target $": o.get("target_dollar_amount"),
                    "stop": o.get("suggested_stop_loss"),
                    "take_profit": o.get("take_profit"),
                })
            df = pd.DataFrame(rows)
            rank = {"buy": 0, "add": 1, "trim": 2, "hold": 3, "pass": 4}
            df["_r"] = df["action"].astype(str).str.lower().map(rank).fillna(9)
            df = df.sort_values(["_r", "composite"], ascending=[True, False]).drop(columns="_r").reset_index(drop=True)

            event = st.dataframe(
                df, use_container_width=True, hide_index=True,
                on_select="rerun", selection_mode="single-row", key="rec_table",
            )
            clicked = _ticker_from_selection(event, df)

            # --- per-stock drill-down ------------------------------------
            st.subheader("🔍 Why this stock?")
            tickers = df["ticker"].tolist()
            default_idx = tickers.index(clicked) if clicked in tickers else 0
            chosen = st.selectbox("Inspect a stock (or click a row above)", tickers, index=default_idx)
            order_by_ticker = {str(o.get("ticker", "")).upper(): o for o in orders}
            render_detail(db_path, chosen, order_by_ticker.get(chosen, {}),
                          analysis_map.get(chosen, {}), run_id)

        # Monitor exit suggestions for open positions.
        mon = load_table(
            db_path,
            "SELECT output_json FROM agent_outputs WHERE agent='monitor' ORDER BY ts DESC LIMIT 1",
        )
        if not mon.empty:
            mexits = (_parse(mon.iloc[0]["output_json"]) or {}).get("exits", [])
            if mexits:
                st.markdown("**Exit suggestions (open positions):**")
                edf = pd.DataFrame(mexits)
                ecols = ["ticker", "action", "trigger", "reason", "confidence"]
                st.dataframe(edf[[c for c in ecols if c in edf.columns]],
                             use_container_width=True, hide_index=True)

    # --- Headline metrics from the latest P&L snapshot -------------------
    pnl = load_table(db_path, "SELECT * FROM pnl ORDER BY ts")
    col1, col2, col3, col4 = st.columns(4)
    if not pnl.empty:
        last = pnl.iloc[-1]
        col1.metric("Equity", f"${last['equity']:,.2f}")
        col2.metric("Cash", f"${last['cash']:,.2f}")
        col3.metric("Drawdown", f"{last['drawdown_pct']:.2f}%")
        col4.metric("Peak equity", f"${last['peak_equity']:,.2f}")
        st.subheader("Equity over time")
        st.line_chart(pnl.set_index("ts")[["equity", "peak_equity"]])
    else:
        st.info("No P&L snapshots recorded yet (recommend mode doesn't track P&L — use paper mode).")

    # --- Current positions ----------------------------------------------
    st.subheader("Current positions (latest snapshot)")
    positions = load_table(
        db_path,
        """SELECT ticker, shares, avg_cost, market_value, sector FROM positions
           WHERE run_id = (SELECT run_id FROM positions ORDER BY ts DESC LIMIT 1)""",
    )
    if not positions.empty:
        st.dataframe(positions, use_container_width=True, hide_index=True)
        st.bar_chart(positions.groupby("sector")["market_value"].sum())
    else:
        st.info("No open positions recorded.")

    # --- Decision / guardrail log ---------------------------------------
    st.subheader("Decision & guardrail log")
    decisions = load_table(
        db_path,
        """SELECT ts, ticker, side, action, requested_usd, approved, approved_usd,
           resized, reasons FROM decisions ORDER BY ts DESC LIMIT 100""",
    )
    if not decisions.empty:
        decisions["approved"] = decisions["approved"].map({1: "✅", 0: "❌"})
        st.dataframe(decisions, use_container_width=True, hide_index=True)
    else:
        st.info("No decisions recorded.")

    # --- Orders & fills --------------------------------------------------
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Recent orders")
        orders_tbl = load_table(
            db_path,
            """SELECT ts, mode, ticker, side, qty, notional_usd, status
               FROM orders ORDER BY ts DESC LIMIT 100""",
        )
        st.dataframe(orders_tbl, use_container_width=True, hide_index=True)
    with c2:
        st.subheader("Recent fills")
        fills = load_table(
            db_path,
            """SELECT ts, ticker, side, qty, price, notional_usd, simulated
               FROM fills ORDER BY ts DESC LIMIT 100""",
        )
        st.dataframe(fills, use_container_width=True, hide_index=True)

    # --- Audit log -------------------------------------------------------
    with st.expander("Audit log (halts, kill switch, rate limits, errors)"):
        audit = load_table(
            db_path, "SELECT ts, level, event, detail FROM audit_log ORDER BY ts DESC LIMIT 200"
        )
        st.dataframe(audit, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()

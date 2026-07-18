"""Trading System dashboard — interactive control center.

Tabs: Ideas (recommendations + per-stock drill-down), Portfolio, Settings,
Activity. The sidebar can launch **recommend-mode scans only** (swing/momentum)
— the dashboard can never place a trade; preview/live remain CLI-only. Settings
edits are written to `config.local.yaml` (merged over config.yaml at load time)
so the commented base config is never rewritten.

Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

# Make the repo root + this dir importable (streamlit runs this file directly).
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import load_config, LOCAL_CONFIG_NAME  # noqa: E402
import runner  # noqa: E402

LOCAL_CONFIG_PATH = REPO_ROOT / LOCAL_CONFIG_NAME

# Chart series colors — validated (dataviz skill): blue=equity, aqua=peak.
C_BLUE, C_AQUA = "#3987e5", "#199e70"

ACTION_BADGE = {"buy": "🟢 buy", "add": "🔵 add", "trim": "🟠 trim",
                "hold": "⚪ hold", "pass": "⚫ pass"}
ACTION_RANK = {"buy": 0, "add": 1, "trim": 2, "hold": 3, "pass": 4}

CSS = """
<style>
  .block-container {padding-top: 2.2rem; padding-bottom: 2rem;}
  [data-testid="stMetric"] {
    border: 1px solid rgba(128,128,128,.25); border-radius: 10px;
    padding: 10px 14px;
  }
  [data-testid="stMetricValue"] {font-variant-numeric: tabular-nums;}
  div[data-testid="stDataFrame"] {font-variant-numeric: tabular-nums;}
  .chip {display:inline-block; padding:2px 10px; border-radius:999px;
         border:1px solid rgba(128,128,128,.35); font-size:.78rem;
         margin-right:6px; opacity:.9;}
</style>
"""


# ---------------------------------------------------------------------------
# Data access (read-only)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=10)
def load_table(db_path: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    con = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(sql, con, params=params)
    except Exception:
        return pd.DataFrame()
    finally:
        con.close()


def _parse(json_str):
    try:
        return json.loads(json_str)
    except Exception:
        return None


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


def run_meta(db_path: str, run_id: str) -> tuple[str, str]:
    """(mode, profile) of a run, parsed from runs.notes."""
    df = load_table(db_path, "SELECT mode, notes FROM runs WHERE run_id=?", (run_id,))
    if df.empty:
        return "?", "swing"
    notes = df.iloc[0]["notes"] or ""
    profile = next((p.split("=", 1)[1] for p in notes.split() if p.startswith("profile=")), "swing")
    return df.iloc[0]["mode"], profile


def system_health(db_path: str, run_id: str) -> tuple[str, bool]:
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
# Settings overlay (config.local.yaml)
# ---------------------------------------------------------------------------
def read_overrides() -> dict:
    try:
        return yaml.safe_load(LOCAL_CONFIG_PATH.read_text()) or {}
    except Exception:
        return {}


def save_overrides(overrides: dict) -> None:
    LOCAL_CONFIG_PATH.write_text(
        "# Machine-managed by the dashboard Settings tab. Merged over config.yaml.\n"
        "# Delete this file (or use 'Reset overrides') to return to base config.\n"
        + yaml.safe_dump(overrides, sort_keys=False)
    )


# ---------------------------------------------------------------------------
# Sidebar — control center
# ---------------------------------------------------------------------------
def sidebar(cfg, db_path: str, run_id: str) -> None:
    st.sidebar.title("⚡ Control center")

    backend, have_price = system_health(db_path, run_id)
    healthy = backend not in ("offline", "unknown") and have_price
    ks_on = cfg.kill_switch_enabled
    st.sidebar.markdown(
        f'<span class="chip">🧠 {backend}</span>'
        f'<span class="chip">📶 data {"ok" if have_price else "none"}</span>'
        f'<span class="chip">mode {cfg.mode}</span>'
        f'<span class="chip">{"🛑 KILL" if ks_on else "🟢 armed"}</span>',
        unsafe_allow_html=True,
    )
    if not healthy:
        st.sidebar.warning(
            "Degraded: " +
            ("no LLM key (set ANTHROPIC_API_KEY) " if backend in ("offline", "unknown") else "") +
            ("· no market data (install yfinance or set ALPHAVANTAGE_API_KEY)" if not have_price else ""),
            icon="⚠️",
        )

    # ---- Run a scan (recommend-only, by design) --------------------------
    st.sidebar.subheader("Run a scan")
    status = runner.scan_status()
    running = status["state"] == "running"
    profile = st.sidebar.selectbox("Strategy profile", list(cfg.valid_profiles()),
                                   index=list(cfg.valid_profiles()).index(cfg.profile)
                                   if cfg.profile in cfg.valid_profiles() else 0,
                                   disabled=running)
    if st.sidebar.button("▶  Run scan", type="primary", width="stretch",
                         disabled=running,
                         help="Runs the full pipeline in recommend mode (advice only — "
                              "never places trades). Preview/live stay CLI-only."):
        res = runner.start_scan(profile)
        if res.get("started"):
            st.session_state["scan_watch"] = True
            st.rerun()
        else:
            st.sidebar.error(res.get("reason", "could not start"))

    @st.fragment(run_every="3s")
    def scan_monitor():
        stt = runner.scan_status()
        if stt["state"] == "running":
            st.caption(f"⏳ {runner.job_label(stt)} · {stt['elapsed_s']}s elapsed")
            st.code(runner.read_log(10) or "starting…", language=None)
        elif stt["state"] == "finished":
            if st.session_state.get("scan_watch"):
                st.session_state["scan_watch"] = False
                load_table.clear()
                st.rerun(scope="app")
            icon = "✅" if stt.get("ok") else "⚠️"
            st.caption(f"{icon} last job: {runner.job_label(stt)} · {stt.get('started', '')}")
            if not stt.get("ok"):
                with st.expander("last job log"):
                    st.code(runner.read_log(25), language=None)

    with st.sidebar:
        scan_monitor()

    if st.sidebar.button("↻  Refresh data", width="stretch"):
        load_table.clear()
        st.rerun()

    # ---- Kill switch ------------------------------------------------------
    st.sidebar.subheader("Kill switch")
    ks_file = REPO_ROOT / cfg.raw.get("kill_switch_file", "KILL_SWITCH")
    pinned = bool(cfg.raw.get("kill_switch", False))
    if ks_on:
        st.sidebar.error("ALL trading halted.", icon="🛑")
        if pinned:
            st.sidebar.caption("Pinned by `kill_switch: true` in config — release there.")
        else:
            sure = st.sidebar.checkbox("I want to resume trading", key="ks_release_ok")
            if st.sidebar.button("Release kill switch", disabled=not sure,
                                 width="stretch"):
                ks_file.unlink(missing_ok=True)
                st.rerun()
    else:
        if st.sidebar.button("🛑  ENGAGE KILL SWITCH", width="stretch",
                             help="Instantly halts all trading in every mode. "
                                  "Recommend scans still work (they never trade)."):
            ks_file.write_text(f"kill switch engaged via dashboard {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
            st.rerun()

    st.sidebar.caption("Scans and deep research run in read-only modes only — the dashboard "
                       "can never place a trade. Trading (preview/live) is CLI-only. "
                       "API keys live in `.env` and are never shown here.")


# ---------------------------------------------------------------------------
# Ideas tab
# ---------------------------------------------------------------------------
def ideas_tab(db_path: str, obj: dict, run_id: str, ts: str) -> None:
    if not obj:
        st.info("No recommendations yet — run a scan from the sidebar.")
        return
    mode, profile = run_meta(db_path, run_id)
    st.caption(f"Latest **{profile}** scan · {mode} mode · {ts}")

    mv = str(obj.get("market_view", ""))
    if mv and "fallback" in mv.lower():
        st.caption("ℹ️ Agents used deterministic heuristics for this run (no LLM reply).")
    elif mv:
        st.markdown(f"**Market view:** {mv}")

    orders = obj.get("orders", []) or []
    amap = latest_analysis_map(db_path)
    if not orders:
        st.info("The latest scan produced no per-stock output.")
        return

    rows = []
    for o in orders:
        t = str(o.get("ticker", "")).upper()
        a = amap.get(t, {})
        act = str(o.get("action", "")).lower()
        rows.append({
            "ticker": t,
            "action": ACTION_BADGE.get(act, act),
            "composite": a.get("composite_score", o.get("_score")),
            "tech": a.get("technical_score"),
            "fund": a.get("fundamental_score"),
            "sent": a.get("sentiment_score"),
            "setup": (a.get("swing_setup") or "—").replace("none", "—"),
            "conf": o.get("confidence"),
            "target $": o.get("target_dollar_amount") or None,
            "stop": o.get("suggested_stop_loss"),
            "take profit": o.get("take_profit"),
            "_r": ACTION_RANK.get(act, 9),
        })
    df = (pd.DataFrame(rows)
          .sort_values(["_r", "composite"], ascending=[True, False])
          .drop(columns="_r").reset_index(drop=True))

    score_col = lambda label: st.column_config.ProgressColumn(  # noqa: E731
        label, min_value=0, max_value=100, format="%d")
    st.dataframe(
        df, width="stretch", hide_index=True,
        column_config={
            "ticker": st.column_config.TextColumn("ticker", width="small"),
            "composite": score_col("composite"),
            "tech": score_col("tech"),
            "fund": score_col("fund"),
            "sent": score_col("sent"),
            "conf": st.column_config.NumberColumn("conf", format="%d"),
            "target $": st.column_config.NumberColumn("target $", format="$%.0f"),
            "stop": st.column_config.NumberColumn("stop", format="$%.2f"),
            "take profit": st.column_config.NumberColumn("take profit", format="$%.2f"),
        },
    )

    # Monitor exit suggestions (open positions).
    mon = load_table(
        db_path,
        "SELECT output_json FROM agent_outputs WHERE agent='monitor' ORDER BY ts DESC LIMIT 1",
    )
    if not mon.empty:
        mexits = (_parse(mon.iloc[0]["output_json"]) or {}).get("exits", [])
        if mexits:
            st.markdown("**Exit suggestions (open positions):**")
            edf = pd.DataFrame(mexits)
            ecols = [c for c in ("ticker", "action", "trigger", "reason", "confidence")
                     if c in edf.columns]
            st.dataframe(edf[ecols], width="stretch", hide_index=True)

    # ---- drill-down -------------------------------------------------------
    st.subheader("🔍 Why this stock?")
    tickers = df["ticker"].tolist()
    chosen = st.pills("Inspect", tickers, default=tickers[0] if tickers else None,
                      label_visibility="collapsed")
    if chosen:
        order_by_ticker = {str(o.get("ticker", "")).upper(): o for o in orders}
        render_detail(db_path, chosen, order_by_ticker.get(chosen, {}),
                      amap.get(chosen, {}), run_id)


def render_detail(db_path: str, ticker: str, order: dict, analysis: dict, run_id: str) -> None:
    research = research_for(db_path, ticker)
    tech = research.get("technicals", {}) or {}
    fund = research.get("fundamentals", {}) or {}
    news = research.get("news_sentiment", {}) or {}
    dq = research.get("data_quality", {}) or {}
    gr = guardrail_for(db_path, ticker, run_id)

    action = str(order.get("action", "—")).upper()
    composite = analysis.get("composite_score", order.get("_score", "—"))
    setup = analysis.get("swing_setup", "—")
    st.markdown(f"### {ticker} — **{action}** · composite **{composite}** · "
                f"setup *{setup}* · conf {order.get('confidence', '—')}")
    thesis = analysis.get("one_line_thesis") or order.get("rationale")
    if thesis:
        st.markdown(f"> {thesis}")

    tabs = st.tabs(["Why", "Technicals", "Fundamentals", "News", "Scoring", "Guardrail", "Raw"])

    with tabs[0]:
        if order.get("rationale"):
            st.markdown(f"**Decision rationale:** {order['rationale']}")
        for r in (analysis.get("key_risks") or []):
            st.markdown(f"- {r}")
        if order.get("suggested_stop_loss") or order.get("take_profit"):
            st.markdown(f"**Plan:** stop {_fmt(order.get('suggested_stop_loss'), money=True)} · "
                        f"target {_fmt(order.get('take_profit'), money=True)} · "
                        f"by {order.get('max_hold_until', '—')}")
        if dq.get("missing_fields"):
            st.caption("Missing data: " + ", ".join(dq["missing_fields"]))

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
        st.caption(f"MACD {_fmt(macd.get('macd'))} · signal {_fmt(macd.get('signal'))} · "
                   f"hist {_fmt(macd.get('histogram'))}")

    with tabs[2]:
        c = st.columns(4)
        c[0].metric("Revenue TTM", _fmt(fund.get("revenue_ttm"), money=True))
        c[1].metric("Rev growth", _fmt(fund.get("revenue_growth_yoy"), pct=True))
        c[2].metric("EPS TTM", _fmt(fund.get("eps_ttm")))
        c[3].metric("EPS growth", _fmt(fund.get("eps_growth_yoy"), pct=True))
        c = st.columns(4)
        c[0].metric("P/E", _fmt(fund.get("pe_ratio")))
        c[1].metric("P/S", _fmt(fund.get("ps_ratio")))
        c[2].metric("D/E", _fmt(fund.get("debt_to_equity")))
        c[3].metric("Next earnings", fund.get("next_earnings_date") or "—")

    with tabs[3]:
        st.markdown(f"**Aggregate sentiment:** {_fmt(news.get('aggregate_score'))} "
                    f"({news.get('aggregate_label', '—')}) · "
                    f"{news.get('article_count', 0)} articles · "
                    f"source {news.get('source') or 'none'}")
        if news.get("summary"):
            st.markdown(news["summary"])
        for h in (news.get("headlines") or []):
            title, url = h.get("title") or "(untitled)", h.get("url")
            line = f"- [{title}]({url})" if url else f"- {title}"
            score = h.get("sentiment_score")
            if h.get("sentiment_label") or score is not None:
                line += f"  *({h.get('sentiment_label','')} {('' if score is None else f'{score:+.2f}')})*"
            st.markdown(line)
        if not news.get("headlines"):
            st.caption("No headlines for this run.")

    with tabs[4]:
        c = st.columns(4)
        c[0].metric("Composite", _fmt(analysis.get("composite_score")))
        c[1].metric("Technical", _fmt(analysis.get("technical_score")))
        c[2].metric("Fundamental", _fmt(analysis.get("fundamental_score")))
        c[3].metric("Sentiment", _fmt(analysis.get("sentiment_score")))
        bd = analysis.get("score_breakdown") or {}
        for grp in ("technical", "fundamental"):
            if isinstance(bd.get(grp), dict):
                st.caption(f"{grp.title()}: " + ", ".join(f"{k}={v}" for k, v in bd[grp].items()))
        if isinstance(bd.get("weights"), dict):
            st.caption("Weights: " + ", ".join(f"{k}={v}" for k, v in bd["weights"].items()))

    with tabs[5]:
        if gr:
            verdict = "✅ approved" if gr.get("approved") else "❌ rejected"
            if gr.get("resized"):
                verdict += " (resized)"
            st.markdown(f"**Guardrail verdict:** {verdict} — requested "
                        f"{_fmt(gr.get('requested_usd'), money=True)} → approved "
                        f"{_fmt(gr.get('approved_usd'), money=True)}")
            for r in (_parse(gr.get("reasons")) or []):
                st.markdown(f"- {r}")
        else:
            st.caption("No guardrail record — this name was hold/pass (no order proposed).")

    with tabs[6]:
        st.json({"research": research, "analysis": analysis, "decision": order})


# ---------------------------------------------------------------------------
# Deep dive tab — saved single-ticker research reports (read-only)
# ---------------------------------------------------------------------------
def deep_dive_tab(db_path: str) -> None:
    # ---- research bar: run a deep dive right from here (read-only mode) ----
    status = runner.scan_status()
    running = status["state"] == "running"
    c1, c2 = st.columns([4, 1])
    ticker_in = c1.text_input(
        "Research a stock", placeholder="Type a ticker — e.g. NVDA (lowercase fine)",
        label_visibility="collapsed", disabled=running, key="deep_dive_ticker")
    if c2.button("🔎 Research", type="primary", width="stretch", disabled=running,
                 help="Runs a read-only deep-research briefing on this ticker "
                      "(explain mode — nothing is traded). Takes a minute or two."):
        res = runner.start_explain(ticker_in)
        if res.get("started"):
            st.session_state["scan_watch"] = True
            st.rerun()
        else:
            st.error(res.get("reason", "could not start"))
    if running:
        st.info(f"⏳ {runner.job_label(status)} running ({status['elapsed_s']}s) — "
                "the report will appear here when it finishes (watch the sidebar log).")
    st.caption("Briefings are saved and re-readable below, newest first. "
               "CLI equivalent: `python orchestrator.py --mode explain --ticker NVDA`")

    tickers_df = load_table(
        db_path,
        "SELECT ticker, MAX(ts) AS latest FROM agent_outputs "
        "WHERE agent='explain' AND ticker IS NOT NULL GROUP BY ticker ORDER BY latest DESC",
    )
    if tickers_df.empty:
        st.info("No deep-research reports yet — type a ticker above and hit **Research**.")
        return

    tickers = tickers_df["ticker"].tolist()
    chosen = st.pills("Ticker", tickers, default=tickers[0], label_visibility="collapsed")
    if not chosen:
        return
    reports = load_table(
        db_path,
        "SELECT ts, output_json FROM agent_outputs WHERE agent='explain' AND ticker=? "
        "ORDER BY ts DESC LIMIT 20",
        (chosen,),
    )
    if reports.empty:
        st.info("No reports for this ticker.")
        return
    labels = [f"{row['ts'][:16].replace('T', ' ')} UTC" for _, row in reports.iterrows()]
    idx = 0
    if len(labels) > 1:
        idx = labels.index(st.selectbox("Report", labels, index=0))
    report = _parse(reports.iloc[idx]["output_json"]) or {}
    render_explain_report(report)


def render_explain_report(report: dict) -> None:
    snap = report.get("snapshot", {}) or {}
    price, mcap = snap.get("price"), snap.get("market_cap")
    st.markdown(f"### {report.get('ticker', '?')}"
                + (f" — {snap.get('name')}" if snap.get("name") else "")
                + f" · {snap.get('sector', '')}")
    chips = []
    if isinstance(price, (int, float)):
        chips.append(f"${price:,.2f}")
    if isinstance(mcap, (int, float)):
        chips.append(f"mkt cap ${mcap/1e9:,.1f}B")
    chips.append(f"as of {report.get('as_of', '—')}")
    st.caption(" · ".join(chips))

    v = report.get("verdict") or {}
    if v.get("action"):
        action = str(v["action"]).upper()
        line = (f"**🎯 VERDICT: {action}** · confidence {v.get('confidence', '—')} · "
                f"{v.get('profile', 'swing')} profile")
        if v.get("rationale"):
            line += f"\n\n{v['rationale']}"
        kind = {"buy": st.success, "add": st.success,
                "sell": st.error, "trim": st.error, "avoid": st.error}.get(
            str(v["action"]).lower(), st.info)
        kind(line)
        if v.get("reasons"):
            st.caption(" · ".join(v["reasons"][:4]))
        plan = v.get("suggested_plan")
        if plan:
            st.caption(f"Suggested plan (informational only): stop ${plan.get('stop')} · "
                       f"target ${plan.get('target')} · by {plan.get('max_hold_until')}")

    if snap.get("summary"):
        st.markdown(snap["summary"])

    pa = report.get("price_action", {}) or {}
    rets = pa.get("returns", {}) or {}
    cols = st.columns(5)
    for col, (label, key) in zip(cols, [("1d", "d1"), ("5d", "d5"), ("1m", "m1"),
                                        ("3m", "m3"), ("YTD", "ytd")]):
        v = rets.get(key)
        col.metric(label, f"{v:+.1f}%" if isinstance(v, (int, float)) else "—")
    if pa.get("summary"):
        st.caption(pa["summary"])

    wim = report.get("why_it_moved", {}) or {}
    st.markdown(f"**📰 Why it moved:** {wim.get('summary', '—')}")
    for d in (wim.get("drivers") or []):
        st.markdown(f"- {d.get('claim', '')} — *“{d.get('headline', '')}”*"
                    + (f" ({d.get('date')})" if d.get("date") else ""))

    e = report.get("earnings", {}) or {}
    if e.get("next_date"):
        flag = " ⚠️ **event risk inside the swing horizon**" if e.get("event_risk") else ""
        st.markdown(f"**🗓 Earnings:** {e['next_date']}"
                    + (f" (in ~{e['days_until']}d)" if e.get("days_until") is not None else "")
                    + flag)
    else:
        st.markdown("**🗓 Earnings:** date unavailable")

    f = report.get("fundamentals", {}) or {}
    fparts = []
    for k, label in (("pe_ratio", "P/E"), ("ps_ratio", "P/S"),
                     ("eps_growth_yoy", "EPS growth %"), ("debt_to_equity", "D/E")):
        v = f.get(k)
        fparts.append(f"{label} {round(v, 2) if isinstance(v, (int, float)) else 'n/a'}")
    fcf = f.get("free_cash_flow")
    fparts.append(f"FCF {'$' + format(fcf, ',.0f') if isinstance(fcf, (int, float)) else 'n/a'}")
    st.markdown("**🧾 Fundamentals:** " + " · ".join(fparts))
    if f.get("note"):
        st.caption(f["note"])

    scenarios = report.get("scenarios") or []
    if scenarios:
        st.markdown("**🔮 Scenarios** *(conditional levels — not a forecast)*")
        cols = st.columns(len(scenarios))
        icons = {"bull": "🟢", "base": "⚪", "bear": "🔴"}
        for col, s in zip(cols, scenarios):
            name = str(s.get("name", "")).lower()
            tl = s.get("target_level")
            with col:
                st.markdown(f"{icons.get(name, '•')} **{name.upper()}**"
                            + (f" → ${tl:,.2f}" if isinstance(tl, (int, float)) else ""))
                st.caption(f"if {s.get('condition', '?')}")
                if s.get("narrative"):
                    st.caption(s["narrative"])
                st.caption(f"confirm: {s.get('confirm', '—')}")
                st.caption(f"invalidate: {s.get('invalidate', '—')}")

    c1, c2 = st.columns(2)
    with c1:
        if report.get("risks"):
            st.markdown("**⚠️ Key risks**")
            for r in report["risks"][:6]:
                st.markdown(f"- {r}")
    with c2:
        if report.get("watch_next"):
            st.markdown("**👀 Watch next**")
            for w in report["watch_next"][:5]:
                st.markdown(f"- {w}")

    pos = report.get("position")
    if pos is None:
        st.caption("💼 Position: no account connected when this report was generated.")
    elif pos.get("held"):
        st.markdown(f"**💼 Your position:** {pos.get('shares')} sh @ "
                    f"${pos.get('avg_cost')} (unrealized "
                    f"{pos.get('unrealized_pnl_pct', '—')}%)")
    elif pos.get("held") is False:
        st.caption("💼 Position: not held at report time.")
    if isinstance(pos, dict) and pos.get("trade_plan"):
        tp = pos["trade_plan"]
        st.caption(f"Stored plan: stop ${tp.get('stop_loss')} · target "
                   f"${tp.get('take_profit')} · by {tp.get('max_hold_until')}")

    if report.get("disclaimer"):
        st.caption(f"*{report['disclaimer']}*")
    with st.expander("Raw report JSON"):
        st.json(report)


# ---------------------------------------------------------------------------
# Portfolio tab
# ---------------------------------------------------------------------------
def portfolio_tab(db_path: str) -> None:
    pnl = load_table(db_path, "SELECT * FROM pnl ORDER BY ts")
    if pnl.empty:
        st.info("No portfolio data yet — equity, positions and P&L appear here once "
                "your real account is connected (recommend reads it read-only; "
                "preview/live trade it).")
        return
    last = pnl.iloc[-1]
    c = st.columns(4)
    c[0].metric("Equity", f"${last['equity']:,.2f}")
    c[1].metric("Cash", f"${last['cash']:,.2f}")
    c[2].metric("Drawdown", f"{last['drawdown_pct']:.2f}%")
    c[3].metric("Peak equity", f"${last['peak_equity']:,.2f}")
    st.line_chart(pnl.set_index("ts")[["equity", "peak_equity"]],
                  color=[C_BLUE, C_AQUA])

    positions = load_table(
        db_path,
        """SELECT ticker, shares, avg_cost, market_value, sector FROM positions
           WHERE run_id = (SELECT run_id FROM positions ORDER BY ts DESC LIMIT 1)""",
    )
    if not positions.empty:
        st.subheader("Positions")
        st.dataframe(positions, width="stretch", hide_index=True)
        st.bar_chart(positions.groupby("sector")["market_value"].sum(), color=C_BLUE)
    else:
        st.caption("No open positions recorded.")


# ---------------------------------------------------------------------------
# Settings tab (writes config.local.yaml)
# ---------------------------------------------------------------------------
def settings_tab(cfg) -> None:
    st.caption(f"Edits are saved to `{LOCAL_CONFIG_NAME}` and merged over `config.yaml` "
               "(your commented base file is never modified). API keys live in `.env` "
               "and are never shown or edited here.")
    overrides = read_overrides()

    # ---- General ----------------------------------------------------------
    with st.form("general"):
        st.subheader("General")
        c1, c2 = st.columns(2)
        modes = ["recommend", "preview", "live"]
        mode = c1.selectbox("Default mode (CLI runs)", modes, index=modes.index(cfg.mode))
        profiles = list(cfg.valid_profiles())
        profile = c2.selectbox("Default profile", profiles,
                               index=profiles.index(cfg.profile) if cfg.profile in profiles else 0)
        if mode == "live":
            st.warning("`live` places REAL orders when run from the CLI. Dashboard scans "
                       "always stay recommend-only.", icon="⚠️")

        st.subheader("Watchlist")
        universe = st.multiselect("Universe (deselect to remove)", options=cfg.universe,
                                  default=cfg.universe)
        added = st.text_input("Add tickers (comma-separated)", placeholder="e.g. TSLA, PLTR")

        st.subheader("Discovery")
        d = cfg.discovery
        c1, c2, c3 = st.columns(3)
        d_on = c1.toggle("Dynamic discovery", value=bool(d.get("dynamic_discovery", True)))
        d_max = c2.number_input("Max discovered", 0, 25, int(d.get("max_discovered", 5)))
        d_vol = c3.number_input("Min avg volume", 0, value=int(d.get("min_avg_volume", 500000)),
                                step=100000)
        c1, c2 = st.columns(2)
        d_minp = c1.number_input("Min price $", 0.0, value=float(d.get("min_price", 2)))
        d_maxp = c2.number_input("Max price $", 1.0, value=float(d.get("max_price", 500)))

        st.subheader("Research & sizing")
        r = cfg.research
        c1, c2, c3 = st.columns(3)
        r_llm = c1.toggle("Per-ticker LLM notes (slower)",
                          value=bool(r.get("llm_enrichment", False)))
        r_conc = c2.number_input("Research concurrency", 1, 20, int(r.get("concurrency", 5)))
        hypo = c3.number_input("Hypothetical cash $ (recommend)", 100.0,
                               value=float(cfg.recommend.get("hypothetical_cash", 10000.0)),
                               step=500.0)

        if st.form_submit_button("💾 Save settings", type="primary"):
            new_universe = universe + [t.strip().upper() for t in added.split(",")
                                       if t.strip()] if added else universe
            overrides.update({
                "mode": mode,
                "profile": profile,
                "universe": list(dict.fromkeys(new_universe)),
                "discovery": {**overrides.get("discovery", {}),
                              "dynamic_discovery": d_on, "max_discovered": int(d_max),
                              "min_avg_volume": int(d_vol), "min_price": float(d_minp),
                              "max_price": float(d_maxp)},
                "research": {"llm_enrichment": r_llm, "concurrency": int(r_conc)},
                "recommend": {"hypothetical_cash": float(hypo)},
            })
            save_overrides(overrides)
            load_table.clear()
            st.success(f"Saved to {LOCAL_CONFIG_NAME}.")
            st.rerun()

    # ---- Hard risk limits ---------------------------------------------------
    with st.form("risk"):
        st.subheader("🛡️ Hard risk limits")
        st.warning("These are your HARD safety limits, enforced deterministically on every "
                   "order. Changing them changes your safety budget.", icon="🛡️")
        rk = cfg.raw.get("risk", {})
        c1, c2, c3 = st.columns(3)
        max_positions = c1.number_input("Max positions", 1, 100, int(rk.get("max_positions", 15)))
        max_pos_pct = c2.number_input("Max position % of equity", 0.01, 1.0,
                                      float(rk.get("max_position_pct", 0.15)), step=0.01)
        max_sector_pct = c3.number_input("Max sector %", 0.05, 1.0,
                                         float(rk.get("max_sector_pct", 0.40)), step=0.05)
        c1, c2, c3 = st.columns(3)
        per_trade = c1.number_input("Per-trade max $", 10.0,
                                    value=float(rk.get("per_trade_max_usd", 500)), step=50.0)
        daily_trades = c2.number_input("Daily max trades", 1, 100,
                                       int(rk.get("daily_max_trades", 5)))
        cash_floor = c3.number_input("Min cash reserve %", 0.0, 0.9,
                                     float(rk.get("min_cash_reserve_pct", 0.10)), step=0.05)
        c1, c2 = st.columns(2)
        dd_halt = c1.number_input("Drawdown halt %", 0.02, 0.9,
                                  float(rk.get("max_account_drawdown_halt_pct", 0.15)), step=0.01)
        min_trade = c2.number_input("Min trade $", 1.0, value=float(rk.get("min_trade_usd", 50)))
        no_trade = st.text_input("No-trade list (comma-separated)",
                                 value=", ".join(rk.get("no_trade_list", []) or []))
        confirm = st.checkbox("I understand this changes my hard safety limits")
        if st.form_submit_button("💾 Save risk limits"):
            if not confirm:
                st.error("Tick the confirmation to change hard limits.")
            else:
                overrides["risk"] = {
                    **overrides.get("risk", {}),
                    "max_positions": int(max_positions),
                    "max_position_pct": float(max_pos_pct),
                    "max_sector_pct": float(max_sector_pct),
                    "per_trade_max_usd": float(per_trade),
                    "daily_max_trades": int(daily_trades),
                    "min_cash_reserve_pct": float(cash_floor),
                    "max_account_drawdown_halt_pct": float(dd_halt),
                    "min_trade_usd": float(min_trade),
                    "no_trade_list": [t.strip().upper() for t in no_trade.split(",") if t.strip()],
                }
                save_overrides(overrides)
                load_table.clear()
                st.success(f"Risk limits saved to {LOCAL_CONFIG_NAME}.")
                st.rerun()

    # ---- Reset --------------------------------------------------------------
    if LOCAL_CONFIG_PATH.exists():
        st.divider()
        with st.expander(f"Current overrides ({LOCAL_CONFIG_NAME})"):
            st.code(LOCAL_CONFIG_PATH.read_text(), language="yaml")
        sure = st.checkbox("Really reset all dashboard overrides to base config")
        if st.button("🗑 Reset all overrides", disabled=not sure):
            LOCAL_CONFIG_PATH.unlink(missing_ok=True)
            load_table.clear()
            st.rerun()


# ---------------------------------------------------------------------------
# Activity tab
# ---------------------------------------------------------------------------
def activity_tab(db_path: str) -> None:
    st.subheader("Decision & guardrail log")
    decisions = load_table(
        db_path,
        """SELECT ts, ticker, side, action, requested_usd, approved, approved_usd,
           resized, reasons FROM decisions ORDER BY ts DESC LIMIT 200""",
    )
    if not decisions.empty:
        decisions["approved"] = decisions["approved"].map({1: "✅", 0: "❌"})
        st.dataframe(decisions, width="stretch", hide_index=True)
    else:
        st.caption("No decisions recorded yet.")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Orders")
        st.dataframe(load_table(
            db_path,
            "SELECT ts, mode, ticker, side, qty, notional_usd, status "
            "FROM orders ORDER BY ts DESC LIMIT 100"),
            width="stretch", hide_index=True)
    with c2:
        st.subheader("Fills")
        st.dataframe(load_table(
            db_path,
            "SELECT ts, ticker, side, qty, price, notional_usd "
            "FROM fills ORDER BY ts DESC LIMIT 100"),
            width="stretch", hide_index=True)

    with st.expander("Audit log (halts, kill switch, rate limits, errors)"):
        st.dataframe(load_table(
            db_path,
            "SELECT ts, level, event, detail FROM audit_log ORDER BY ts DESC LIMIT 300"),
            width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="Trading System", page_icon="📈", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    cfg = load_config()
    db_path = str(cfg.db_path)
    obj, run_id, ts = latest_decision(db_path)

    sidebar(cfg, db_path, run_id)

    st.title("📈 Trading System")
    _, profile = run_meta(db_path, run_id) if run_id else ("?", cfg.profile)
    st.markdown(
        f'<span class="chip">latest scan: {profile}</span>'
        f'<span class="chip">{ts or "no runs yet"}</span>'
        f'<span class="chip">default mode: {cfg.mode}</span>',
        unsafe_allow_html=True,
    )

    tab_ideas, tab_deep, tab_portfolio, tab_settings, tab_activity = st.tabs(
        ["📋 Ideas", "🔎 Deep dive", "💼 Portfolio", "⚙️ Settings", "🧾 Activity"])
    with tab_ideas:
        ideas_tab(db_path, obj, run_id, ts)
    with tab_deep:
        deep_dive_tab(db_path)
    with tab_portfolio:
        portfolio_tab(db_path)
    with tab_settings:
        settings_tab(cfg)
    with tab_activity:
        activity_tab(db_path)


if __name__ == "__main__":
    main()

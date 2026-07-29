"""Render one run into a dated markdown briefing (``reports/YYYY-MM-DD.md``).

This is the pull-to-push step: a scheduled run otherwise leaves its output in
SQLite and the dashboard, both of which have to be remembered. The briefing
answers the two standing questions directly — *what should I do with each stock
I hold*, and *what is worth buying* — and is written to a file the scheduler can
point a notification at.

Read-only. It opens the database, renders, and writes a file; it never trades,
never mutates a table, and never calls an LLM.

Sources, and why each:
  * ``positions``      — the holdings the verdicts must cover.
  * ``agent_outputs``  (agent='decision') — the AUTHORITATIVE per-ticker verdict
    list. The ``decisions`` table only stores *actionable* rows, so hold/pass
    verdicts exist only inside the decision agent's ``orders`` payload.
  * ``agent_outputs``  (agent='analysis') — composite scores, thesis, risks; and
    the previous run's copy, for the score delta that reveals a decaying thesis.
  * ``pnl`` / ``orders`` — equity, drawdown, and anything needing human review.

Run standalone:  python -m reporting.brief [--run-id RUN] [--out-dir reports]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from server.format import DASH, fmt_money, fmt_num, fmt_pct, fmt_shares, fmt_ts

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO / "storage" / "trading.db"
_DEFAULT_OUT = _REPO / "reports"

# The decision agent falls back to a deterministic policy when the LLM call
# fails. It says so in market_view; surfacing that is the difference between a
# briefing you can act on and one that is quietly just a ranking by score.
_FALLBACK_MARKER = "deterministic fallback"

# Analysis emits a flat 50 for every component when it has no data to work with.
_NEUTRAL_SCORE = 50


@dataclass
class _Holding:
    ticker: str
    shares: float
    avg_cost: float
    market_value: float
    sector: str | None = None
    action: str | None = None          # None => no verdict recorded for it
    rationale: str | None = None
    score: float | None = None
    prev_score: float | None = None
    price: float | None = None
    risks: list[str] = field(default_factory=list)

    @property
    def cost_basis(self) -> float:
        return (self.avg_cost or 0.0) * (self.shares or 0.0)

    @property
    def pnl_usd(self) -> float | None:
        if not self.market_value or not self.cost_basis:
            return None
        return self.market_value - self.cost_basis

    @property
    def pnl_pct(self) -> float | None:
        basis = self.cost_basis
        if not basis:
            return None
        return (self.market_value - basis) / basis


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _latest_run(conn: sqlite3.Connection, mode: str | None = None) -> sqlite3.Row | None:
    sql = "select * from runs"
    args: tuple = ()
    if mode:
        sql += " where mode = ?"
        args = (mode,)
    sql += " order by started_ts desc limit 1"
    return conn.execute(sql, args).fetchone()


def _agent_payload(conn: sqlite3.Connection, run_id: str, agent: str):
    row = conn.execute(
        "select output_json from agent_outputs where run_id = ? and agent = ? "
        "order by id desc limit 1", (run_id, agent)).fetchone()
    if not row or not row["output_json"]:
        return None
    try:
        return json.loads(row["output_json"])
    except (ValueError, TypeError):
        return None


def _analysis_by_ticker(payload) -> dict[str, dict]:
    """Analysis output is a list of per-ticker dicts; index it."""
    if not isinstance(payload, list):
        return {}
    return {str(d.get("ticker", "")).upper(): d for d in payload if isinstance(d, dict)}


def _verdicts_by_ticker(payload) -> dict[str, dict]:
    """Per-ticker verdicts live in the decision payload's ``orders`` list — the
    only place hold/pass survive (the decisions table keeps actionable rows)."""
    if not isinstance(payload, dict):
        return {}
    out = {}
    for o in payload.get("orders", []) or []:
        if isinstance(o, dict) and o.get("ticker"):
            out[str(o["ticker"]).upper()] = o
    return out


def _exits_by_ticker(payload) -> dict[str, dict]:
    """Sells come from the MONITOR agent, not the decision agent.

    The decision agent reduces risk with ``trim`` and explicitly "does not place
    outright sells" — stop-loss and take-profit exits are the monitor's job and
    live only in its ``exits`` payload. A brief that reads only the decision
    output therefore reports "hold" for a position that just breached its stop.
    """
    if not isinstance(payload, dict):
        return {}
    out = {}
    for e in payload.get("exits", []) or []:
        if isinstance(e, dict) and e.get("ticker"):
            t = str(e["ticker"]).upper()
            # Keep the strongest signal if a ticker appears more than once
            # (the deterministic and LLM paths can both emit one).
            if out.get(t, {}).get("action") == "exit_full":
                continue
            out[t] = e
    return out


def _previous_run_id(conn: sqlite3.Connection, run: sqlite3.Row) -> str | None:
    row = conn.execute(
        "select run_id from runs where started_ts < ? and mode = ? "
        "order by started_ts desc limit 1",
        (run["started_ts"], run["mode"])).fetchone()
    return row["run_id"] if row else None


def _holdings(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "select * from positions where run_id = ? and shares > 0 order by ticker",
        (run_id,)).fetchall()


def _fpct(frac: float | None, *, signed: bool = False) -> str:
    """Render a FRACTION (0.15) as a percentage ("15.0%").

    ``server.format.fmt_pct`` expects values already in percent units, while
    equity/P&L ratios in this codebase are stored as fractions — so the scale
    conversion has to happen here.
    """
    if frac is None:
        return DASH
    return fmt_pct(frac * 100.0, signed=signed)


def _delta(cur: float | None, prev: float | None) -> str:
    if cur is None or prev is None:
        return ""
    d = cur - prev
    if abs(d) < 0.5:
        return " (flat)"
    return f" ({d:+.0f})"


def _pnl_cell(h: _Holding, available: bool = True) -> str:
    if not available or h.pnl_usd is None:
        return DASH
    return f"{fmt_money(h.pnl_usd)} / {_fpct(h.pnl_pct, signed=True)}"


def _cost_basis_available(holdings: list[_Holding]) -> bool:
    """True only if avg_cost looks like a real entry price.

    The broker read can populate avg_cost as market_value / shares — i.e. the
    CURRENT price — in which case cost basis equals market value for every
    position and P&L is identically zero. Reporting "$0.00 / +0.0%" across a
    book that is sitting below its stops is worse than reporting nothing, so
    this detects the degenerate case and the column is suppressed instead.
    """
    priced = [h for h in holdings if h.market_value and h.cost_basis]
    if not priced:
        return False
    return any(abs(h.cost_basis - h.market_value) > 0.01 for h in priced)


def _action_label(action: str | None) -> str:
    if not action:
        return "**NO VERDICT**"
    a = action.lower()
    return {"buy": "BUY", "add": "ADD", "trim": "TRIM",
            "sell": "SELL", "hold": "hold", "pass": "pass"}.get(a, a.upper())


def _build_holdings(conn, run_id, analysis, verdicts, prev_analysis,
                    exits: dict[str, dict] | None = None) -> list[_Holding]:
    exits = exits or {}
    out: list[_Holding] = []
    for row in _holdings(conn, run_id):
        t = str(row["ticker"]).upper()
        a = analysis.get(t, {})
        v = verdicts.get(t, {})
        x = exits.get(t, {})

        # A monitor exit OVERRIDES the decision verdict: a breached stop is more
        # urgent than "hold", and it is the only place a sell is ever expressed.
        if x:
            action = "sell" if x.get("action") == "exit_full" else "trim"
            rationale = x.get("reason") or v.get("rationale")
            trigger = x.get("trigger")
            if trigger:
                rationale = f"**{trigger}** — {rationale}"
        else:
            action, rationale = v.get("action"), v.get("rationale")

        out.append(_Holding(
            ticker=t,
            shares=row["shares"] or 0.0,
            avg_cost=row["avg_cost"] or 0.0,
            market_value=row["market_value"] or 0.0,
            sector=row["sector"] or a.get("_sector"),
            action=action,
            rationale=rationale,
            score=a.get("composite_score"),
            prev_score=(prev_analysis.get(t) or {}).get("composite_score"),
            price=a.get("_price") or x.get("current_price"),
            risks=list(a.get("key_risks") or []),
        ))
    # Exits first, then worst score: what needs acting on should be read first.
    _urgency = {"sell": 0, "trim": 1, "add": 2, "buy": 2}
    out.sort(key=lambda h: (_urgency.get(str(h.action or "").lower(), 3),
                            h.score if h.score is not None else 999))
    return out


def _degraded_reasons(analysis: dict, decision) -> list[str]:
    reasons = []
    if isinstance(decision, dict):
        mv = str(decision.get("market_view", ""))
        if _FALLBACK_MARKER in mv.lower():
            reasons.append(
                "The decision agent fell back to its deterministic policy — the LLM call did "
                "not return usable output, so today's actions are a ranking by composite score, "
                "not reasoned allocation.")
    if analysis:
        scores = [d.get("composite_score") for d in analysis.values()]
        scores = [s for s in scores if s is not None]
        if scores and all(abs(s - _NEUTRAL_SCORE) < 1e-6 for s in scores):
            reasons.append(
                "Every composite score is exactly 50 (the neutral default), which means research "
                "got no usable market data. Scores below are not meaningful.")
    return reasons


def _render(conn: sqlite3.Connection, run: sqlite3.Row) -> str:
    run_id = run["run_id"]
    analysis = _analysis_by_ticker(_agent_payload(conn, run_id, "analysis"))
    decision = _agent_payload(conn, run_id, "decision")
    verdicts = _verdicts_by_ticker(decision)
    exits = _exits_by_ticker(_agent_payload(conn, run_id, "monitor"))

    prev_id = _previous_run_id(conn, run)
    prev_analysis = _analysis_by_ticker(
        _agent_payload(conn, prev_id, "analysis")) if prev_id else {}

    holdings = _build_holdings(conn, run_id, analysis, verdicts, prev_analysis, exits)
    held = {h.ticker for h in holdings}

    started = run["started_ts"]
    day = (started or "")[:10] or datetime.now(timezone.utc).date().isoformat()
    L: list[str] = []

    L.append(f"# Daily brief — {day}")
    L.append("")
    L.append(f"*{fmt_ts(started)} · run `{run_id}` · mode **{run['mode']}** · "
             f"{run['notes'] or ''}*")
    L.append("")

    # ---- degraded-run banner -------------------------------------------
    for reason in _degraded_reasons(analysis, decision):
        L.append(f"> ⚠️ **Degraded run.** {reason}")
        L.append("")

    # ---- account snapshot ----------------------------------------------
    pnl = conn.execute(
        "select * from pnl where run_id = ? order by id desc limit 1", (run_id,)).fetchone()
    if pnl:
        dd = pnl["drawdown_pct"]
        L.append(f"**Account** · equity {fmt_money(pnl['equity'])} · "
                 f"cash {fmt_money(pnl['cash'])} · "
                 f"drawdown {_fpct(dd)} from peak")
        L.append("")

    # ---- 1. holdings ----------------------------------------------------
    L.append(f"## Your positions ({len(holdings)})")
    L.append("")
    if not holdings:
        L.append("_No open positions recorded for this run._")
        L.append("")
        L.append("> If you do hold stock, the account read returned nothing — check the "
                 "Robinhood connection before trusting the ideas below.")
        L.append("")
    else:
        has_basis = _cost_basis_available(holdings)
        L.append("| Ticker | Action | Score | Shares | Value | P&L | Why |")
        L.append("|---|---|---|---:|---:|---:|---|")
        for h in holdings:
            score = (f"{fmt_num(h.score, 0)}{_delta(h.score, h.prev_score)}"
                     if h.score is not None else DASH)
            why = h.rationale or (
                "_No verdict was produced for this holding in this run._"
                if h.action is None else DASH)
            L.append(
                f"| **{h.ticker}** | {_action_label(h.action)} | {score} | "
                f"{fmt_shares(h.shares)} | {fmt_money(h.market_value)} | "
                f"{_pnl_cell(h, has_basis)} | {why} |")
        L.append("")
        if not has_basis:
            L.append("> **P&L is unavailable.** The account read reports `avg_cost` as "
                     "market value ÷ shares — the *current* price, not what you paid — so "
                     "every position would compute to exactly $0.00. The column is blanked "
                     "rather than showing a flat book that isn't real.")
            L.append("")

        missing = [h.ticker for h in holdings if h.action is None]
        if missing:
            L.append(f"> ⚠️ **{len(missing)} holding(s) got no verdict this run:** "
                     f"{', '.join(missing)}. Every position you own should receive one — "
                     "if this persists, the decision agent's output contract is dropping "
                     "non-actionable holdings.")
            L.append("")

        risky = [h for h in holdings if h.risks]
        if risky:
            L.append("**Flagged risks on your holdings**")
            L.append("")
            for h in risky:
                L.append(f"- **{h.ticker}** — {'; '.join(h.risks)}")
            L.append("")

    # ---- 2. new ideas ---------------------------------------------------
    ideas = [(t, v) for t, v in verdicts.items()
             if t not in held and str(v.get("action", "")).lower() in ("buy", "add")]
    ideas.sort(key=lambda kv: (analysis.get(kv[0], {}).get("composite_score") or 0),
               reverse=True)

    L.append(f"## New ideas ({len(ideas)})")
    L.append("")
    if not ideas:
        scanned = len(analysis)
        L.append(f"_Nothing actionable today — {scanned} name(s) scanned, none cleared the "
                 "buy threshold or there was no room within the risk limits._")
        L.append("")
    else:
        L.append("| Ticker | Score | Size | Stop | Target | Thesis |")
        L.append("|---|---|---:|---:|---:|---|")
        for t, v in ideas:
            a = analysis.get(t, {})
            L.append(
                f"| **{t}** | {fmt_num(a.get('composite_score'), 0)} | "
                f"{fmt_money(v.get('target_dollar_amount'))} | "
                f"{fmt_money(v.get('suggested_stop_loss'))} | "
                f"{fmt_money(v.get('take_profit'))} | "
                f"{v.get('rationale') or a.get('one_line_thesis') or DASH} |")
        L.append("")
        passed = sum(1 for t, v in verdicts.items()
                     if str(v.get("action", "")).lower() == "pass")
        if passed:
            L.append(f"_{passed} other name(s) were scanned and passed over._")
            L.append("")

    # ---- 3. attention ---------------------------------------------------
    attention: list[str] = []

    # An exit signal for a ticker that is not in the account cannot be acted on
    # and usually means the monitor invented or mangled a symbol. Worth seeing:
    # it is a signal you would otherwise assume covered one of your holdings.
    phantom = sorted(t for t in exits if t not in held)
    if phantom:
        attention.append(
            f"**Exit signal(s) for ticker(s) you do not hold: {', '.join(phantom)}.** "
            "These cannot be acted on and point at a bad symbol from the monitor agent — "
            "check whether a real holding was meant.")

    review = conn.execute(
        "select ticker, ts, detail from orders where status = 'needs_review'").fetchall()
    for r in review:
        attention.append(
            f"**{r['ticker']}** has an order in `needs_review` from {fmt_ts(r['ts'])} — the "
            "executor could not confirm whether it filled. It will not be retried "
            "automatically; reconcile it against your broker by hand.")

    resized = conn.execute(
        "select ticker, reasons from decisions where run_id = ? and resized = 1",
        (run_id,)).fetchall()
    for r in resized:
        try:
            why = "; ".join(json.loads(r["reasons"]) or [])
        except (ValueError, TypeError):
            why = str(r["reasons"] or "")
        attention.append(f"**{r['ticker']}** was resized by the risk layer — {why}")

    if (_REPO / "KILL_SWITCH").exists():
        attention.append("**Kill switch is ENGAGED** — no orders will be placed in any mode "
                         "until the `KILL_SWITCH` file is removed.")

    if pnl and pnl["drawdown_pct"] is not None and pnl["drawdown_pct"] >= 0.15:
        attention.append(
            f"**Drawdown halt territory** — {_fpct(pnl['drawdown_pct'])} off the peak. "
            "New buys are blocked; risk-reducing sells still allowed.")

    L.append("## Needs your attention")
    L.append("")
    if attention:
        for item in attention:
            L.append(f"- {item}")
    else:
        L.append("_Nothing flagged._")
    L.append("")

    L.append("---")
    L.append("")
    L.append("*Generated from a read-only run. Nothing here was traded, and none of it is "
             "financial advice — the scores and rationales are model output, so read the "
             "reasoning rather than the label.*")
    L.append("")
    return "\n".join(L)


def render_brief(db_path: str | Path = _DEFAULT_DB, run_id: str | None = None,
                 out_dir: str | Path = _DEFAULT_OUT, mode: str | None = None) -> Path | None:
    """Render ``run_id`` (default: the most recent run, optionally of ``mode``)
    to ``out_dir/YYYY-MM-DD.md``. Returns the path written, or None if there is
    no run to render."""
    conn = _connect(db_path)
    try:
        if run_id:
            run = conn.execute("select * from runs where run_id = ?", (run_id,)).fetchone()
        else:
            run = _latest_run(conn, mode)
        if run is None:
            return None
        text = _render(conn, run)
    finally:
        conn.close()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    day = (run["started_ts"] or "")[:10] or datetime.now(timezone.utc).date().isoformat()
    path = out_dir / f"{day}.md"
    path.write_text(text, encoding="utf-8")
    return path


def summary_line(db_path: str | Path = _DEFAULT_DB, run_id: str | None = None,
                 mode: str | None = None) -> str:
    """A one-line summary suitable for a desktop notification body."""
    conn = _connect(db_path)
    try:
        run = (conn.execute("select * from runs where run_id = ?", (run_id,)).fetchone()
               if run_id else _latest_run(conn, mode))
        if run is None:
            return "No run found."
        rid = run["run_id"]
        analysis = _analysis_by_ticker(_agent_payload(conn, rid, "analysis"))
        decision = _agent_payload(conn, rid, "decision")
        verdicts = _verdicts_by_ticker(decision)
        exits = _exits_by_ticker(_agent_payload(conn, rid, "monitor"))
        holdings = _build_holdings(conn, rid, analysis, verdicts, {}, exits)
        held = {h.ticker for h in holdings}

        sells = sum(1 for h in holdings if str(h.action or "").lower() == "sell")
        acts = sum(1 for h in holdings
                   if str(h.action or "").lower() in ("add", "trim", "buy"))
        ideas = sum(1 for t, v in verdicts.items()
                    if t not in held and str(v.get("action", "")).lower() in ("buy", "add"))
        parts = [f"{len(holdings)} held"]
        if sells:
            parts.append(f"🔴 {sells} SELL")
        parts += [f"{acts} other action{'s' if acts != 1 else ''}",
                  f"{ideas} new idea{'s' if ideas != 1 else ''}"]
        if _degraded_reasons(analysis, decision):
            parts.append("⚠️ degraded")
        return " · ".join(parts)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a run into a markdown briefing")
    ap.add_argument("--run-id", default=None, help="run to render (default: latest)")
    ap.add_argument("--mode", default=None,
                    help="when picking the latest run, restrict to this mode")
    ap.add_argument("--db", default=str(_DEFAULT_DB))
    ap.add_argument("--out-dir", default=str(_DEFAULT_OUT))
    ap.add_argument("--summary", action="store_true",
                    help="print a one-line summary instead of the file path")
    args = ap.parse_args()

    if args.summary:
        print(summary_line(args.db, args.run_id, args.mode))
        return 0

    path = render_brief(args.db, args.run_id, args.out_dir, args.mode)
    if path is None:
        print("No run found to render.")
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

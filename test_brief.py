"""Tests for the daily markdown briefing.

The load-bearing guarantee: **every stock you hold appears in the brief with a
verdict**, and when a verdict is missing the brief says so loudly rather than
quietly omitting the row. That is the whole point of the report, and it is
exactly what a change to the decision agent's output contract could silently
break — so it is asserted here.

Run:  python -m pytest test_brief.py -q
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from reporting.brief import render_brief

_SCHEMA = """
create table runs (run_id text, started_ts text, finished_ts text, mode text, notes text);
create table positions (id integer primary key, ts text, run_id text, ticker text,
    shares real, avg_cost real, market_value real, sector text, account text);
create table agent_outputs (id integer primary key, ts text, run_id text, agent text,
    ticker text, model text, input_json text, output_json text, raw_text text);
create table decisions (id integer primary key, ts text, run_id text, ticker text,
    side text, action text, requested_usd real, confidence real, approved integer,
    approved_usd real, resized integer, reasons text, checks_json text, rationale text);
create table pnl (id integer primary key, ts text, run_id text, equity real, cash real,
    buying_power real, peak_equity real, drawdown_pct real, realized_pnl real,
    unrealized_pnl real, account text);
create table orders (id integer primary key, ts text, run_id text, mode text, ticker text,
    side text, order_type text, qty real, limit_price real, notional_usd real,
    status text, broker_order_id text, detail text);
"""

RUN = "20260729T120000Z-test"
PREV = "20260728T120000Z-test"


def _make_db(tmp_path, *, verdicts, holdings, analysis=None, exits=None):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    c.executescript(_SCHEMA)
    for rid, ts in ((PREV, "2026-07-28T12:00:00+00:00"), (RUN, "2026-07-29T12:00:00+00:00")):
        c.execute("insert into runs values (?,?,?,?,?)",
                  (rid, ts, ts, "recommend", "backend=stub"))
    for t, shares, avg, mv in holdings:
        c.execute("insert into positions (run_id,ticker,shares,avg_cost,market_value,sector,"
                  "account) values (?,?,?,?,?,?,?)",
                  (RUN, t, shares, avg, mv, "Tech", "individual"))
    analysis = analysis or [
        {"ticker": t, "composite_score": 70, "_price": 100.0,
         "one_line_thesis": f"{t} thesis", "key_risks": []}
        for t, *_ in holdings
    ]
    c.execute("insert into agent_outputs (run_id,agent,output_json) values (?,?,?)",
              (RUN, "analysis", json.dumps(analysis)))
    # previous run: same names, 12 points higher, so the delta is visible
    c.execute("insert into agent_outputs (run_id,agent,output_json) values (?,?,?)",
              (PREV, "analysis", json.dumps(
                  [dict(a, composite_score=(a["composite_score"] or 0) + 12) for a in analysis])))
    c.execute("insert into agent_outputs (run_id,agent,output_json) values (?,?,?)",
              (RUN, "decision", json.dumps({"market_view": "Constructive.", "orders": verdicts})))
    if exits is not None:
        c.execute("insert into agent_outputs (run_id,agent,output_json) values (?,?,?)",
                  (RUN, "monitor", json.dumps({"exits": exits, "holds": [], "notes": ""})))
    c.commit()
    c.close()
    return db


def _render(tmp_path, **kw):
    db = _make_db(tmp_path, **kw)
    out = render_brief(db, run_id=RUN, out_dir=tmp_path / "reports")
    return out.read_text()


def test_every_holding_appears_with_its_verdict(tmp_path):
    text = _render(
        tmp_path,
        holdings=[("AAPL", 10, 100.0, 1200.0), ("NVDA", 5, 200.0, 900.0)],
        verdicts=[{"ticker": "AAPL", "action": "hold", "rationale": "thesis intact"},
                  {"ticker": "NVDA", "action": "trim", "rationale": "over position cap"}],
    )
    assert "## Your positions (2)" in text
    # Both names, and crucially the non-actionable 'hold' one, are present.
    assert "**AAPL**" in text and "thesis intact" in text
    assert "**NVDA**" in text and "over position cap" in text
    assert "hold" in text and "TRIM" in text
    assert "NO VERDICT" not in text


def test_missing_verdict_is_loud_not_silent(tmp_path):
    """If the decision agent stops emitting non-actionable holdings, the brief
    must flag it — silently dropping the row is the failure mode to prevent."""
    text = _render(
        tmp_path,
        holdings=[("AAPL", 10, 100.0, 1200.0), ("NVDA", 5, 200.0, 900.0)],
        verdicts=[{"ticker": "NVDA", "action": "trim", "rationale": "over cap"}],
    )
    assert "**AAPL**" in text                     # still listed
    assert "NO VERDICT" in text                   # and marked
    assert "1 holding(s) got no verdict" in text  # and summarised
    assert "AAPL" in text.split("got no verdict")[1][:80]


def test_pnl_and_score_delta_render(tmp_path):
    text = _render(
        tmp_path,
        holdings=[("AAPL", 10, 100.0, 1200.0)],   # cost 1000 -> +200 / +20%
        verdicts=[{"ticker": "AAPL", "action": "hold", "rationale": "ok"}],
    )
    assert "$200.00" in text and "+20.0%" in text
    assert "(-12)" in text          # 70 now vs 82 last run


def test_degraded_run_banner_on_llm_fallback(tmp_path):
    db = _make_db(tmp_path, holdings=[("AAPL", 1, 1.0, 1.0)],
                  verdicts=[{"ticker": "AAPL", "action": "hold"}])
    c = sqlite3.connect(db)
    c.execute("update agent_outputs set output_json = ? where run_id = ? and agent = 'decision'",
              (json.dumps({"market_view": "Deterministic fallback policy (no LLM backend).",
                           "orders": []}), RUN))
    c.commit(); c.close()
    text = render_brief(db, run_id=RUN, out_dir=tmp_path / "r").read_text()
    assert "Degraded run" in text and "deterministic policy" in text


def test_degraded_run_banner_on_all_neutral_scores(tmp_path):
    text = _render(
        tmp_path,
        holdings=[("AAPL", 1, 1.0, 1.0)],
        verdicts=[{"ticker": "AAPL", "action": "hold"}],
        analysis=[{"ticker": "AAPL", "composite_score": 50, "key_risks": []}],
    )
    assert "Degraded run" in text and "exactly 50" in text


def test_new_ideas_exclude_names_already_held(tmp_path):
    text = _render(
        tmp_path,
        holdings=[("AAPL", 10, 100.0, 1200.0)],
        verdicts=[{"ticker": "AAPL", "action": "add", "rationale": "adding"},
                  {"ticker": "MSFT", "action": "buy", "rationale": "new name",
                   "target_dollar_amount": 30.0}],
        analysis=[{"ticker": "AAPL", "composite_score": 70, "key_risks": []},
                  {"ticker": "MSFT", "composite_score": 75, "key_risks": []}],
    )
    ideas = text.split("## New ideas")[1].split("## Needs")[0]
    assert "MSFT" in ideas
    assert "AAPL" not in ideas       # held names belong in the positions table only
    assert "## New ideas (1)" in text


def test_empty_holdings_warns_about_account_read(tmp_path):
    text = _render(tmp_path, holdings=[],
                   verdicts=[{"ticker": "MSFT", "action": "buy"}])
    assert "No open positions recorded" in text
    assert "account read returned nothing" in text


def test_monitor_exit_overrides_a_decision_hold(tmp_path):
    """Sells come ONLY from the monitor agent — the decision agent reduces risk
    with trim and never emits an outright sell. A brief that reads only the
    decision output reports "hold" for a position that just breached its stop,
    which is the most consequential thing it could get wrong."""
    text = _render(
        tmp_path,
        holdings=[("MU", 0.2, 1000.0, 200.0)],
        verdicts=[{"ticker": "MU", "action": "hold", "rationale": "avoid churn"}],
        exits=[{"ticker": "MU", "action": "exit_full", "trigger": "stop_loss",
                "reason": "price $773 <= stop $953", "current_price": 773.0}],
    )
    assert "SELL" in text
    assert "stop_loss" in text and "773" in text
    assert "avoid churn" not in text        # the stale hold rationale is replaced


def test_partial_exit_renders_as_trim(tmp_path):
    text = _render(
        tmp_path,
        holdings=[("VYX", 10.0, 7.0, 78.0)],
        verdicts=[{"ticker": "VYX", "action": "hold", "rationale": "x"}],
        exits=[{"ticker": "VYX", "action": "exit_partial", "trigger": "take_profit",
                "reason": "reached target"}],
    )
    assert "TRIM" in text and "take_profit" in text


def test_exit_for_unheld_ticker_is_flagged(tmp_path):
    """A mangled symbol from the monitor (observed live: 'GOOGLEL' for GOOGL)
    produces a sell signal that cannot be acted on. It must not pass silently."""
    text = _render(
        tmp_path,
        holdings=[("GOOGL", 1.0, 100.0, 100.0)],
        verdicts=[{"ticker": "GOOGL", "action": "hold"}],
        exits=[{"ticker": "GOOGLEL", "action": "exit_full", "trigger": "stop_loss",
                "reason": "bad symbol"}],
    )
    assert "GOOGLEL" in text
    assert "do not hold" in text


def test_pnl_suppressed_when_avg_cost_is_really_current_price(tmp_path):
    """avg_cost == market_value / shares means the broker read gave the current
    price, not a cost basis, so every P&L computes to exactly $0. Reporting a
    flat book that isn't real is worse than reporting nothing."""
    text = _render(
        tmp_path,
        holdings=[("MU", 0.2, 1000.0, 200.0), ("AAPL", 0.5, 300.0, 150.0)],
        verdicts=[{"ticker": "MU", "action": "hold"}, {"ticker": "AAPL", "action": "hold"}],
    )
    assert "P&L is unavailable" in text
    assert "$0.00 / +0.0%" not in text


def test_pnl_shown_when_cost_basis_is_real(tmp_path):
    text = _render(
        tmp_path,
        holdings=[("AAPL", 10.0, 100.0, 1200.0)],   # basis 1000 != value 1200
        verdicts=[{"ticker": "AAPL", "action": "hold"}],
    )
    assert "P&L is unavailable" not in text
    assert "$200.00" in text and "+20.0%" in text


def test_no_run_returns_none(tmp_path):
    db = _make_db(tmp_path, holdings=[], verdicts=[])
    assert render_brief(db, run_id="does-not-exist", out_dir=tmp_path / "r") is None


# --------------------------------------------------------------------------- #
# The two display bugs from the 2026-09 briefs
# --------------------------------------------------------------------------- #
def _with_pnl(db, drawdown_pct_stored: float):
    c = sqlite3.connect(db)
    c.execute("insert into pnl (run_id, equity, cash, buying_power, peak_equity, "
              "drawdown_pct, account) values (?,?,?,?,?,?,?)",
              (RUN, 258.74, 152.05, 152.05, 282.46, drawdown_pct_stored, "individual"))
    c.commit(); c.close()


def test_drawdown_is_stored_in_percent_and_rendered_once(tmp_path):
    """pnl.drawdown_pct holds 8.397 for an 8.4% drawdown; the brief printed
    "839.7%" and a 'buys are blocked' banner for two weeks."""
    db = _make_db(tmp_path, holdings=[("NTAP", 0.23, 194.37, 45.4)],
                  verdicts=[{"ticker": "NTAP", "action": "hold"}])
    _with_pnl(db, 8.397)
    text = render_brief(db, run_id=RUN, out_dir=tmp_path / "r").read_text()
    assert "drawdown 8.4% from peak" in text
    assert "839" not in text
    assert "Drawdown halt territory" not in text


def test_halt_banner_only_at_the_configured_limit(tmp_path):
    db = _make_db(tmp_path, holdings=[("NTAP", 0.23, 194.37, 45.4)],
                  verdicts=[{"ticker": "NTAP", "action": "hold"}])
    _with_pnl(db, 16.0)   # 16% > the 15% halt
    text = render_brief(db, run_id=RUN, out_dir=tmp_path / "r").read_text()
    assert "Drawdown halt territory" in text and "16.0%" in text


def test_partial_exit_promoted_to_full_renders_as_sell(tmp_path):
    """The monitor said exit_partial, the guardrail row says exit_full (the
    remainder would have been dust): the brief must say SELL, not TRIM."""
    db = _make_db(tmp_path, holdings=[("HPE", 0.0313, 48.08, 1.75)],
                  verdicts=[{"ticker": "HPE", "action": "hold"}],
                  exits=[{"ticker": "HPE", "action": "exit_partial", "trigger": "take_profit",
                          "reason": "at target", "shares": 0.0156}])
    c = sqlite3.connect(db)
    c.execute("insert into decisions (run_id, ticker, side, action, approved, approved_usd, "
              "resized, reasons) values (?,?,?,?,?,?,?,?)",
              (RUN, "HPE", "SELL", "exit_full", 1, 1.75, 0, '["approved"]'))
    c.commit(); c.close()
    text = render_brief(db, run_id=RUN, out_dir=tmp_path / "r").read_text()
    assert "| **HPE** | SELL |" in text
    assert "closing in full" in text


def test_each_holding_is_listed_once_even_with_two_snapshots(tmp_path):
    """A trading run snapshots positions before AND after executing. The brief
    must show the last snapshot per name, not both."""
    db = _make_db(tmp_path, holdings=[("SCCO", 0.30799, 211.05, 65.0)],
                  verdicts=[{"ticker": "SCCO", "action": "hold"}])
    c = sqlite3.connect(db)
    # the post-run snapshot, with the real market value
    c.execute("insert into positions (run_id,ticker,shares,avg_cost,market_value,sector,"
              "account) values (?,?,?,?,?,?,?)",
              (RUN, "SCCO", 0.30799, 211.05, 58.07, "Tech", "individual"))
    c.commit(); c.close()
    text = render_brief(db, run_id=RUN, out_dir=tmp_path / "r").read_text()
    assert text.count("**SCCO**") == 1
    assert "$58.07" in text and "$65.00" not in text
    assert "positions (1)" in text

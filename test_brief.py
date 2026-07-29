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


def _make_db(tmp_path, *, verdicts, holdings, analysis=None):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    c.executescript(_SCHEMA)
    for rid, ts in ((PREV, "2026-07-28T12:00:00+00:00"), (RUN, "2026-07-29T12:00:00+00:00")):
        c.execute("insert into runs values (?,?,?,?,?)",
                  (rid, ts, ts, "recommend", "profile=swing"))
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


def test_no_run_returns_none(tmp_path):
    db = _make_db(tmp_path, holdings=[], verdicts=[])
    assert render_brief(db, run_id="does-not-exist", out_dir=tmp_path / "r") is None

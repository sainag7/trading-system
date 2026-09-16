"""Offline tests for the exit / sizing plumbing fixed on 2026-09-15.

NO network, NO API keys. Each case reproduces a defect read straight out of the
live ledger:

  * The Monitor's take-profit partial had no memory, so it halved HPE on 9/04,
    9/10 and 9/14 and tried a fourth time on 9/15 (a $0.97 order the broker
    refused). A +29% winner became a $1.50 stub through exit logic alone.
  * A partial exit could leave a remainder worth less than a sandwich.
  * Held names dropped out of research the day after they were bought, so the
    Monitor scored them 50 by default and could never fire a thesis break.
  * ``trade_plans`` was shared across accounts: the individual book's advice
    run trailed its own GOOGL (avg cost $368.68) into the row the agentic book
    read for a GOOGL bought at $344.94 — a stop 0.65% below entry.
  * Plans were never deleted on a full exit, so a rebuy inherited a stale
    entry date / price / peak.
  * NVDA was sold and rebought the next day three times in two weeks.

Run:  python -m pytest test_exit_discipline.py -q
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.monitor_agent import _deterministic_exits, _drop_repeat_take_profit
from config import load_config
from execution.executor import OrderResult
from risk.guardrails import AccountState, GuardrailResult, OrderIntent, Position, Side
from storage.db import Database


STRATEGY = {"exit_below_score": 38, "graded_stops": True}


def _pos(**kw) -> dict:
    base = dict(ticker="HPE", shares=0.25, avg_cost=48.08, current_price=62.09,
                market_value=15.5, unrealized_pnl_pct=29.1, stop_loss=53.39,
                take_profit=57.69, max_hold_until="2026-11-02", score=70,
                above_sma200=True, ret_3m=10.0, tp_taken=False)
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Take-profit fires once
# --------------------------------------------------------------------------- #
def test_take_profit_fires_when_not_yet_taken():
    exits = _deterministic_exits([_pos()], STRATEGY)
    assert len(exits) == 1
    assert exits[0]["trigger"] == "take_profit"
    assert exits[0]["action"] == "exit_partial"
    assert exits[0]["shares"] == pytest.approx(0.125)


def test_take_profit_does_not_refire_once_banked():
    """The HPE sequence: price stays above target for a week. Day two onward
    must be a hold — the remainder rides the trailing stop only."""
    assert _deterministic_exits([_pos(tp_taken=True)], STRATEGY) == []


def test_a_stop_hit_still_closes_a_position_whose_take_profit_was_banked():
    """tp_taken must switch off the take-profit rule and NOTHING else."""
    exits = _deterministic_exits(
        [_pos(tp_taken=True, current_price=50.0, unrealized_pnl_pct=-2.0,
              above_sma200=False, ret_3m=-5.0)], STRATEGY)
    assert len(exits) == 1
    assert exits[0]["trigger"] == "stop_loss"
    assert exits[0]["action"] == "exit_full"


def test_model_cannot_refire_a_banked_take_profit_through_the_union():
    """On 9/10 the LLM sold HPE for being 'within 1 ATR' of the target. Once the
    partial is banked, a model take_profit exit on that name is dropped; other
    triggers and other names are untouched."""
    llm_exits = [
        {"ticker": "HPE", "action": "exit_partial", "trigger": "take_profit",
         "reason": "near target"},
        {"ticker": "HPE", "action": "exit_full", "trigger": "thesis_break",
         "reason": "bad earnings"},
        {"ticker": "NTAP", "action": "exit_partial", "trigger": "take_profit",
         "reason": "at target"},
    ]
    kept = _drop_repeat_take_profit(
        llm_exits, [_pos(tp_taken=True), _pos(ticker="NTAP", tp_taken=False)])
    assert [(e["ticker"], e["trigger"]) for e in kept] == [
        ("HPE", "thesis_break"), ("NTAP", "take_profit")]


# --------------------------------------------------------------------------- #
# Orchestrator plumbing: dust partials, plan lifecycle, cooldown, universe
# --------------------------------------------------------------------------- #
def _orch(tmp: str, account: str = "agentic"):
    from orchestrator import Orchestrator
    from recommend_check import StubProvider
    cfg = load_config()
    cfg.set_mode("recommend")
    cfg.raw.setdefault("discovery", {})["dynamic_discovery"] = False
    cfg.raw.setdefault("execution", {})["read_live_account"] = False
    cfg.raw["storage"]["db_path"] = str(Path(tmp) / "trading.db")
    cfg.apply_account(account)
    return Orchestrator(cfg, provider=StubProvider())


def _account(equity=260.0, **positions) -> AccountState:
    return AccountState(
        equity=equity, cash=0.0, buying_power=0.0, peak_equity=equity,
        positions={t: Position(t, shares=sh, avg_cost=cost, market_value=mv)
                   for t, (sh, cost, mv) in positions.items()})


def test_partial_exit_leaving_dust_is_promoted_to_a_full_exit():
    """HPE on 9/14: 0.0626 shares at $56.33, 'sell half'. Half leaves $1.76 —
    under the $13 floor on a $260 book — so the whole position goes."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        acct = _account(HPE=(0.062603, 48.08, 3.53))
        intents = orch._exits_to_intents(
            [{"ticker": "HPE", "action": "exit_partial", "shares": 0.031301,
              "trigger": "take_profit", "reason": "at target",
              "current_price": 56.33}], acct)
        (i,) = intents
        assert i.action == "exit_full" and i.shares is None
        assert i.trigger == "take_profit"
        assert "minimum position" in i.rationale
        rows = orch.db.query("select event from audit_log where event='partial_promoted_to_full'")
        assert rows


def test_partial_exit_that_leaves_a_real_position_stays_partial():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        acct = _account(NTAP=(0.5, 194.37, 100.0))
        (i,) = orch._exits_to_intents(
            [{"ticker": "NTAP", "action": "exit_partial", "shares": 0.25,
              "trigger": "take_profit", "reason": "at target",
              "current_price": 233.0}], acct)
        assert i.action == "exit_partial" and i.shares == 0.25


def _res(intent: OrderIntent, shares: float) -> GuardrailResult:
    return GuardrailResult(intent=intent, approved=True, approved_usd=shares * intent.price,
                           approved_shares=shares, resized=False, reasons=["approved"],
                           checks=[])


def test_take_profit_partial_flags_the_plan_and_a_full_exit_deletes_it():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        acct_no = orch.cfg.account_number
        orch.db.upsert_trade_plan(ticker="HPE", run_id="r1", entry_price=48.08,
                                  stop_loss=44.0, take_profit=57.69,
                                  max_hold_until="2026-11-02", thesis="t",
                                  peak_price=48.08, account=acct_no)
        # 1) the take-profit partial fills -> tp_taken, plan kept
        partial = OrderIntent(ticker="HPE", side=Side.SELL, action="exit_partial",
                              shares=0.125, price=62.09, trigger="take_profit")
        orch._settle_plan_after_sell(partial, _res(partial, 0.125), pre_shares=0.25,
                                     result=OrderResult(ok=True, status="filled",
                                                        filled_qty=0.125, fill_price=62.09))
        plan = orch.db.get_trade_plans(account=acct_no)["HPE"]
        assert plan["tp_taken"] == 1
        # 2) the remainder is stopped out in full -> plan deleted
        full = OrderIntent(ticker="HPE", side=Side.SELL, action="exit_full",
                           shares=None, price=50.0, trigger="stop_loss")
        orch._settle_plan_after_sell(full, _res(full, 0.125), pre_shares=0.125,
                                     result=OrderResult(ok=True, status="filled",
                                                        filled_qty=0.125, fill_price=50.0))
        assert "HPE" not in orch.db.get_trade_plans(account=acct_no)
        events = [r["event"] for r in orch.db.query("select event from audit_log")]
        assert "take_profit_banked" in events and "trade_plan_closed" in events


def test_a_partial_that_empties_the_position_also_deletes_the_plan():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        acct_no = orch.cfg.account_number
        orch.db.upsert_trade_plan(ticker="X", run_id="r1", entry_price=10.0,
                                  account=acct_no)
        i = OrderIntent(ticker="X", side=Side.SELL, action="exit_partial",
                        shares=1.0, price=10.0, trigger="stop_trail")
        orch._settle_plan_after_sell(i, _res(i, 1.0), pre_shares=1.0,
                                     result=OrderResult(ok=True, status="filled",
                                                        filled_qty=1.0, fill_price=10.0))
        assert "X" not in orch.db.get_trade_plans(account=acct_no)


def test_rebuy_cooldown_excludes_recent_exits_but_not_take_profit_partials():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        db, acct_no = orch.db, orch.cfg.account_number
        orch.cfg.raw["strategy"]["rebuy_cooldown_days"] = 3

        def sold(ticker, trigger, days_ago):
            tid, _, _ = db.begin_trade(client_order_id=f"{ticker}:{days_ago}", run_id="r",
                                       mode="live", ticker=ticker, side="SELL", qty=1.0,
                                       price=10.0, notional=10.0, account=acct_no)
            db.finish_trade(tid, "filled", price=10.0, qty=1.0,
                            detail={"trigger": trigger, "action": "exit_full"})
            ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
            db.query("update trades set ts=? where id=?", (ts, tid))

        sold("NVDA", "stop_loss", 1)       # sold yesterday -> cooling
        sold("FCX", "stop_loss", 10)       # long ago -> free
        sold("HPE", "take_profit", 1)      # banked a partial -> still held, free
        acct = _account(HPE=(0.03, 48.0, 1.8))
        assert orch._cooldown_tickers(acct) == {"NVDA"}

        orch.cfg.raw["strategy"]["rebuy_cooldown_days"] = 0
        assert orch._cooldown_tickers(acct) == set()


def test_held_names_are_appended_to_the_research_universe():
    from orchestrator import _universe_with_held
    acct = _account(SCCO=(0.3, 211.0, 58.0), NTAP=(0.23, 194.0, 45.0),
                    NVDA=(0.0, 0.0, 0.0))            # a zero-share ghost is skipped
    universe, added = _universe_with_held(["AAPL", "NTAP"], acct)
    assert universe == ["AAPL", "NTAP", "SCCO"]
    assert added == ["SCCO"]


# --------------------------------------------------------------------------- #
# Per-account trade plans
# --------------------------------------------------------------------------- #
def test_trade_plans_are_isolated_per_account():
    """The GOOGL case: two accounts, one ticker. Trailing the individual book's
    plan must not move the agentic book's stop."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        db.upsert_trade_plan(ticker="GOOGL", run_id="a", entry_price=344.94,
                             stop_loss=323.47, take_profit=413.92, peak_price=344.94,
                             account="AGENTIC")
        db.update_trail("GOOGL", stop_loss=342.70, peak_price=368.68, account="INDIVIDUAL")
        plan = db.get_trade_plans(account="AGENTIC")["GOOGL"]
        assert plan["stop_loss"] == pytest.approx(323.47)
        assert plan["peak_price"] == pytest.approx(344.94)
        assert db.get_trade_plans(account="INDIVIDUAL") == {}
        # And the other account can hold its own plan for the same name.
        db.upsert_trade_plan(ticker="GOOGL", run_id="b", entry_price=368.68,
                             stop_loss=350.0, account="INDIVIDUAL")
        assert db.get_trade_plans(account="INDIVIDUAL")["GOOGL"]["stop_loss"] == 350.0
        db.delete_trade_plan("GOOGL", account="INDIVIDUAL")
        assert "GOOGL" in db.get_trade_plans(account="AGENTIC")


def test_legacy_single_key_table_is_rebuilt_and_attributed_by_run():
    """A pre-existing DB with the old ticker-keyed table: rows survive the
    rebuild and are attributed to the account of the run that wrote them."""
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE trade_plans (ticker TEXT PRIMARY KEY, entry_date TEXT,
                entry_price REAL, stop_loss REAL, take_profit REAL, max_hold_until TEXT,
                thesis TEXT, peak_price REAL, run_id TEXT, updated_ts TEXT);
            INSERT INTO trade_plans VALUES ('NVDA','2026-08-26',212.03,213.78,278.58,
                '2026-11-03','t',231.5,'run-agentic','x');
            INSERT INTO trade_plans VALUES ('ORPHAN','2026-08-26',1,1,1,'2026-11-03','t',1,
                'run-unknown','x');
            CREATE TABLE pnl (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                run_id TEXT, account TEXT, equity REAL, cash REAL, buying_power REAL,
                peak_equity REAL, drawdown_pct REAL, realized_pnl REAL, unrealized_pnl REAL);
            INSERT INTO pnl(ts, run_id, account, equity) VALUES ('t','run-agentic','AG1',100);
        """)
        conn.commit(); conn.close()

        db = Database(path)
        plans = db.get_trade_plans(account="AG1")
        assert plans["NVDA"]["entry_price"] == pytest.approx(212.03)
        assert plans["NVDA"]["tp_taken"] == 0
        assert "ORPHAN" not in plans and "ORPHAN" in db.get_trade_plans(account=None)
        # Idempotent: opening again is a no-op.
        Database(path).get_trade_plans(account="AG1")["NVDA"]


def test_monitor_only_trails_a_plan_the_account_owns():
    """The individual (advice-only) book has positions but no plans; the Monitor
    still gets derived stop levels, but nothing is written."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp, account="individual")
        acct = _account(GOOGL=(0.2714, 368.68, 94.05))
        out = orch._positions_for_monitor(acct)
        assert out[0]["ticker"] == "GOOGL" and out[0]["stop_loss"] is not None
        assert out[0]["tp_taken"] is False
        assert orch.db.query("select count(*) as n from trade_plans")[0]["n"] == 0

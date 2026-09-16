"""Offline tests for the post-close review agent.

NO network, NO API keys, NO model. The scorecard is deterministic Python over a
seeded ledger; the model layer is exercised only through its offline fallback
and its output normaliser; parameter proposals are checked against the bounds
that make auto-tuning safe.

Run:  python -m pytest test_review.py -q
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents import review_agent as ra
from storage.db import Database

ACCT = "AGENTIC1"
TODAY = date(2026, 9, 15)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
class FlatProvider:
    """Daily bars for a handful of tickers with known paths."""

    def __init__(self):
        self.calls = 0
        self.paths = {
            # HPE rises steadily: 48 -> 62 over the window
            "HPE": [48 + i * 0.5 for i in range(60)],
            # NVDA drifts down 232 -> 205
            "NVDA": [232 - i * 0.45 for i in range(60)],
            # FCX: stopped out then rips (whipsaw)
            "FCX": [77 - i * 1.0 for i in range(8)] + [70 + i * 1.5 for i in range(52)],
            "AAPL": [230 + (i % 5) for i in range(60)],
        }

    def get_daily_series(self, ticker, lookback=100):
        self.calls += 1
        path = self.paths.get(ticker.upper())
        if not path:
            return None
        start = date(2026, 8, 1)
        return [{"date": (start + timedelta(days=i)).isoformat(), "open": p, "high": p + 1,
                 "low": p - 1, "close": p, "volume": 1_000} for i, p in enumerate(path)]


def _db(tmp) -> Database:
    return Database(Path(tmp) / "t.db")


def _run(db, run_id, day: str, *, equity: float, decision_orders=None, analysis=None,
         monitor_exits=None, mode="live"):
    db.start_run(run_id, mode, account=ACCT)
    db.query("update runs set started_ts=? where run_id=?", (f"{day}T14:05:00+00:00", run_id))
    db.snapshot_pnl(run_id, equity=equity, cash=0.0, buying_power=0.0, peak_equity=equity,
                    drawdown_pct=0.0, account=ACCT)
    if analysis is not None:
        db.log_agent_output(run_id, "analysis", output_obj=analysis)
    if decision_orders is not None:
        db.log_agent_output(run_id, "decision",
                            output_obj={"market_view": "v", "orders": decision_orders})
    if monitor_exits is not None:
        db.log_agent_output(run_id, "monitor",
                            output_obj={"exits": monitor_exits, "holds": []})


def _trade(db, run_id, day: str, ticker, side, qty, price, *, status="filled", detail=None):
    tid, _, _ = db.begin_trade(client_order_id=f"{run_id}:{ticker}:{side}:{qty}",
                               run_id=run_id, mode="live", ticker=ticker, side=side,
                               qty=qty, price=price, notional=qty * price, account=ACCT)
    db.finish_trade(tid, status, price=price, qty=qty, detail=detail or {})
    db.query("update trades set ts=? where id=?", (f"{day}T14:10:00+00:00", tid))
    return tid


def seed_ledger(db):
    """A miniature of the real September: one oversized loser, one small winner
    halved twice, one whipsawed stop, a dust buy, a next-day rebuy, and a pass."""
    ana = lambda t, s, setup="none": {"ticker": t, "composite_score": s, "swing_setup": setup}
    # 09-03: buy NVDA (big, score 91) and HPE (small, score 72); pass on AAPL (74)
    _run(db, "r1", "2026-09-03", equity=280.0,
         analysis=[ana("NVDA", 91, "breakout"), ana("HPE", 72, "pullback-in-uptrend"),
                   ana("AAPL", 74)],
         decision_orders=[
             {"ticker": "NVDA", "action": "buy", "confidence": 85, "suggested_stop_loss": 213.0,
              "target_dollar_amount": 140.0},
             {"ticker": "HPE", "action": "buy", "confidence": 70, "suggested_stop_loss": 44.0,
              "target_dollar_amount": 12.0}])
    _trade(db, "r1", "2026-09-03", "NVDA", "BUY", 0.6, 232.0, detail={"action": "buy"})
    _trade(db, "r1", "2026-09-03", "HPE", "BUY", 0.25, 48.0, detail={"action": "buy"})
    # 09-04: HPE take-profit partial (legacy: no trigger tag; monitor output has it)
    _run(db, "r2", "2026-09-04", equity=282.0,
         monitor_exits=[{"ticker": "HPE", "action": "exit_partial", "trigger": "take_profit"}])
    _trade(db, "r2", "2026-09-04", "HPE", "SELL", 0.125, 52.5)
    # 09-08: buy FCX
    _run(db, "r3", "2026-09-08", equity=280.0, analysis=[ana("FCX", 72, "breakout")],
         decision_orders=[{"ticker": "FCX", "action": "buy", "confidence": 65,
                           "suggested_stop_loss": 70.26}])
    _trade(db, "r3", "2026-09-08", "FCX", "BUY", 0.58, 77.67, detail={"action": "buy"})
    # 09-10: HPE take-profit AGAIN (the re-fire) + a dust buy of GOOGL
    _run(db, "r4", "2026-09-10", equity=264.0,
         monitor_exits=[{"ticker": "HPE", "action": "exit_partial", "trigger": "take_profit"}])
    _trade(db, "r4", "2026-09-10", "HPE", "SELL", 0.0625, 56.0)
    _trade(db, "r4", "2026-09-10", "GOOGL", "BUY", 0.005, 345.0, detail={"action": "buy"})
    # 09-11: FCX stopped out; rejected order too
    _run(db, "r5", "2026-09-11", equity=268.0,
         monitor_exits=[{"ticker": "FCX", "action": "exit_full", "trigger": "stop_loss"}])
    _trade(db, "r5", "2026-09-11", "FCX", "SELL", 0.58, 72.0,
           detail={"trigger": "stop_loss", "action": "exit_full"})
    _trade(db, "r5", "2026-09-11", "HPE", "SELL", 0.01, 56.0, status="rejected",
           detail={"detail": "Fractional orders must be at least $1."})
    # 09-15: NVDA stop-out, then rebought next day (09-16 is future; use same-window rebuy)
    _run(db, "r6", "2026-09-15", equity=258.0,
         monitor_exits=[{"ticker": "NVDA", "action": "exit_full", "trigger": "stop_loss"}])
    _trade(db, "r6", "2026-09-15", "NVDA", "SELL", 0.6, 213.0,
           detail={"trigger": "stop_loss", "action": "exit_full"})
    _trade(db, "r6", "2026-09-15", "NVDA", "BUY", 0.1, 214.0, detail={"action": "buy"})
    # latest positions snapshot: NVDA rebuy + HPE stub + GOOGL dust
    from risk.guardrails import Position
    db.snapshot_positions("r6", {
        "NVDA": Position("NVDA", 0.1, 214.0, 21.4), "HPE": Position("HPE", 0.0625, 48.0, 3.5),
        "GOOGL": Position("GOOGL", 0.005, 345.0, 1.7)}, account=ACCT)


# --------------------------------------------------------------------------- #
# FIFO round trips
# --------------------------------------------------------------------------- #
def test_fifo_round_trips_one_leg_per_sell_with_weighted_cost():
    trades = [
        {"ticker": "X", "side": "BUY", "qty": 1.0, "price": 10.0, "ts": "2026-09-01", "run_id": "a"},
        {"ticker": "X", "side": "BUY", "qty": 1.0, "price": 20.0, "ts": "2026-09-02", "run_id": "b"},
        {"ticker": "X", "side": "SELL", "qty": 1.5, "price": 30.0, "ts": "2026-09-05", "run_id": "c",
         "detail": {"trigger": "take_profit", "action": "exit_partial"}},
    ]
    legs, open_lots = ra.fifo_round_trips(trades)
    (leg,) = legs
    # 1.0 @ 10 + 0.5 @ 20 = cost 20 for 1.5 shares -> avg 13.33; proceeds 45
    assert leg["cost_basis"] == pytest.approx(20.0)
    assert leg["proceeds"] == pytest.approx(45.0)
    assert leg["pnl_usd"] == pytest.approx(25.0)
    assert leg["pnl_pct"] == pytest.approx(125.0)
    assert leg["hold_days"] == 4 and leg["entry_run_id"] == "a" and leg["exit_run_id"] == "c"
    assert leg["exit_trigger"] == "take_profit" and not leg["closed_position"]
    assert open_lots["X"][0]["qty"] == pytest.approx(0.5)


def test_fifo_ignores_sells_with_no_recorded_lot():
    legs, lots = ra.fifo_round_trips([
        {"ticker": "Y", "side": "SELL", "qty": 1, "price": 5, "ts": "2026-09-01"}])
    assert legs == [] and lots == {}


def test_fifo_reproduces_the_real_september_total():
    """The figures the plan was written from: 13 legs, -$11.61, NVDA -$13.13."""
    real = [
        ("2026-08-31", "NVDA", "SELL", 0.151569, 219.47), ("2026-08-31", "AMZN", "SELL", 0.130809, 260.47),
        ("2026-08-31", "NVDA", "BUY", 0.252126, 219.81), ("2026-08-31", "DAR", "BUY", 0.548438, 64.31),
        ("2026-08-31", "AMZN", "BUY", 0.058055, 260.27), ("2026-09-01", "AMZN", "SELL", 0.058055, 254.07),
        ("2026-09-01", "ASND", "BUY", 0.144851, 263.65), ("2026-09-01", "HNGE", "BUY", 0.317296, 91.87),
        ("2026-09-02", "DAR", "SELL", 0.548438, 67.53), ("2026-09-02", "PBR", "BUY", 0.723752, 20.38),
        ("2026-09-03", "NVDA", "SELL", 0.252126, 227.97), ("2026-09-03", "ASND", "SELL", 0.144851, 268.43),
        ("2026-09-03", "HNGE", "SELL", 0.317296, 90.01), ("2026-09-03", "NVDA", "BUY", 0.109694, 227.91),
        ("2026-09-03", "HPE", "BUY", 0.250415, 48.08), ("2026-09-04", "PBR", "SELL", 0.723752, 20.29),
        ("2026-09-04", "HPE", "SELL", 0.125208, 52.48), ("2026-09-04", "NVDA", "BUY", 0.603058, 232.15),
        ("2026-09-08", "SCCO", "BUY", 0.30799, 211.05), ("2026-09-08", "FCX", "BUY", 0.581821, 77.67),
        ("2026-09-10", "HPE", "SELL", 0.062604, 56.13), ("2026-09-11", "FCX", "SELL", 0.581821, 72.0),
        ("2026-09-11", "NTAP", "BUY", 0.233574, 194.37), ("2026-09-14", "HPE", "SELL", 0.031301, 56.33),
        ("2026-09-14", "GOOGL", "BUY", 0.005102, 344.94), ("2026-09-15", "NVDA", "SELL", 0.712752, 213.08),
    ]
    trades = [{"ticker": t, "side": s, "qty": q, "price": p, "ts": d} for d, t, s, q, p in real]
    legs, lots = ra.fifo_round_trips(trades)
    attributable = [l for l in legs if l["cost_basis"] > 0]
    assert len(attributable) == 11          # the two 08-31 sells had no ledger lot
    assert sum(l["pnl_usd"] for l in attributable) == pytest.approx(-11.61, abs=0.02)
    nvda = [l for l in attributable if l["ticker"] == "NVDA"]
    assert nvda[-1]["pnl_usd"] == pytest.approx(-13.13, abs=0.02)
    assert set(lots) == {"HPE", "SCCO", "NTAP", "GOOGL"}


# --------------------------------------------------------------------------- #
# The scorecard
# --------------------------------------------------------------------------- #
def test_scorecard_end_to_end_on_the_seeded_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        seed_ledger(db)
        sc = ra.build_scorecard(db, FlatProvider(), ACCT, window_days=60, today=TODAY,
                                min_position_usd=13.0)
        json.dumps(sc)   # must serialise (no inf/NaN, no Row objects)

        legs = {(l["ticker"], l["exit_date"]): l for l in sc["closed_legs"]}
        assert sc["n_closed"] == 4
        nvda = legs[("NVDA", "2026-09-15")]
        assert nvda["pnl_usd"] == pytest.approx(0.6 * (213.0 - 232.0))
        assert nvda["entry_score"] == 91 and nvda["entry_score_bucket"] == "80+"
        assert nvda["entry_setup"] == "breakout" and nvda["entry_confidence"] == 85
        assert nvda["size_pct_of_equity"] == pytest.approx(100 * 0.6 * 232 / 280, abs=0.1)
        assert nvda["size_bucket"] == "25-50%"
        # R-multiple: risk at entry = (232-213)/232 = 8.19%; pnl = -8.19% -> ~ -1R
        assert nvda["r_multiple"] == pytest.approx(-1.0, abs=0.05)
        assert nvda["exit_trigger"] == "stop_loss"
        # legacy HPE sells get their trigger from the monitor output
        assert legs[("HPE", "2026-09-04")]["exit_trigger"] == "take_profit"
        assert legs[("HPE", "2026-09-10")]["exit_trigger"] == "take_profit"
        # MFE/MAE are computed from the bars
        assert nvda["mae_pct"] is not None and nvda["mae_pct"] < 0
        fcx = legs[("FCX", "2026-09-11")]
        assert fcx["ret_10d_after_exit"] is not None and fcx["ret_10d_after_exit"] > 5

        agg = sc["aggregates"]
        assert agg["all"]["n"] == 4
        assert agg["by_exit_trigger"]["take_profit"]["n"] == 2
        assert agg["by_exit_trigger"]["stop_loss"]["n"] == 2
        assert agg["whipsaw"]["stop_outs"] == 2 and agg["whipsaw"]["whipsawed"] >= 1
        assert "FCX" in agg["whipsaw"]["tickers"]
        assert agg["by_score_bucket"]["80+"]["total_pnl_usd"] < 0
        assert agg["by_score_bucket"]["70-80"]["n"] == 3          # HPE ×2, FCX
        assert agg["by_ticker"]["HPE"]["total_pnl_usd"] > 0
        assert agg["by_ticker"]["HPE"]["win_rate"] == 100.0
        assert agg["by_size_bucket"]["<10%"]["n"] == 2            # the two HPE legs

        b = sc["behaviour"]
        assert b["take_profit_refires"] == {"HPE": 2}
        # both the $12 HPE open and the $1.73 GOOGL open sit under the $13 floor
        assert [d["ticker"] for d in b["dust_orders"]] == ["HPE", "GOOGL"]
        assert b["broker_rejections"] and b["broker_rejections"][0]["ticker"] == "HPE"
        assert b["rebuys_within_5d"] == [{"ticker": "NVDA", "sold": "2026-09-15",
                                          "rebought": "2026-09-15"}]
        assert b["concentration"]["top"] == "NVDA"
        assert set(b["unscored_holdings_latest_run"]) == set()   # r6 had no analysis -> skipped

        calls = sc["calls"]
        assert calls["by_action"]["pass"]["n"] == 1        # AAPL on 09-03
        assert calls["by_action"]["buy"]["n"] == 3
        assert calls["buys_by_score_bucket"]["80+"]["n"] == 1
        # forward returns exist for the 09-03 calls (bars run to end of Sept)
        assert calls["by_action"]["buy"]["r5_n"] >= 2

        opens = {p["ticker"]: p for p in sc["open_positions"]}
        assert set(opens) == {"NVDA", "HPE", "GOOGL"}
        assert opens["HPE"]["shares"] == pytest.approx(0.0625)
        assert opens["HPE"]["unrealized_pct"] is not None


def test_scorecard_without_a_provider_still_builds():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        seed_ledger(db)
        sc = ra.build_scorecard(db, None, ACCT, window_days=60, today=TODAY)
        assert sc["n_closed"] == 4
        assert all(l["mfe_pct"] is None for l in sc["closed_legs"])
        assert sc["calls"]["by_action"]["buy"]["r5_n"] == 0


def test_scorecard_is_scoped_to_the_account():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        seed_ledger(db)
        sc = ra.build_scorecard(db, None, "OTHER", window_days=60, today=TODAY)
        assert sc["n_closed"] == 0 and sc["closed_legs"] == []


def test_series_forward_and_excursions():
    s = ra._Series([{"date": f"2026-09-{d:02d}", "close": c, "high": c + 1, "low": c - 1}
                    for d, c in zip(range(1, 11), [10, 11, 12, 13, 14, 15, 16, 17, 18, 19])])
    fwd = s.forward("2026-09-01", (1, 5, 20))
    assert fwd["r1"] == pytest.approx(10.0) and fwd["r5"] == pytest.approx(50.0)
    assert fwd["r20"] is None
    mfe, mae = s.excursions("2026-09-02", 11.0, "2026-09-04")
    assert mfe == pytest.approx((14 / 11 - 1) * 100, abs=0.01)
    assert mae == pytest.approx((10 / 11 - 1) * 100, abs=0.01)
    assert s.close_at("2026-09-03", 2) == 14


def test_day_context_collects_the_days_decisions_exits_and_trades():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        seed_ledger(db)
        ctx = ra.day_context(db, ACCT, "2026-09-11")
        assert ctx["runs"] == 1
        assert ctx["exits"][0]["ticker"] == "FCX"
        statuses = {(t["ticker"], t["status"]) for t in ctx["trades"]}
        assert ("HPE", "rejected") in statuses and ("FCX", "filled") in statuses
        assert ra.day_context(db, ACCT, "2026-01-01") == {"date": "2026-01-01", "runs": 0}


# --------------------------------------------------------------------------- #
# Model output handling
# --------------------------------------------------------------------------- #
def test_normalize_output_coerces_and_bounds():
    out = ra._normalize_output({
        "day_summary": "x" * 2000,
        "lessons": [
            {"id": "Size Discipline!", "text": "cap names", "confidence": "150",
             "applies_to": "monitor", "status": "weird"},
            {"text": ""},                      # dropped
            {"id": "ok", "text": "t", "confidence": -5, "applies_to": ["decision", "bogus"]},
        ],
        "parameter_proposals": [
            {"key": "strategy.stop_atr_mult", "proposed": "3.0", "n_trades": "7"},
            {"key": "strategy.min_score_to_buy", "proposed": "abc"},   # dropped
        ],
        "playbook": "p",
    })
    assert len(out["day_summary"]) == 1200          # no boundary to fall back to
    assert ra._clip("One sentence. Two sentence. Three", 30) == "One sentence. Two sentence."
    assert ra._clip("para one\npara two\npara three", 20) == "para one\npara two"
    assert [l["id"] for l in out["lessons"]] == ["size-discipline", "ok"]
    assert out["lessons"][0]["confidence"] == 100 and out["lessons"][0]["status"] == "new"
    assert out["lessons"][0]["applies_to"] == ["monitor"]
    assert out["lessons"][1]["confidence"] == 0 and out["lessons"][1]["applies_to"] == ["decision"]
    assert out["parameter_proposals"] == [
        {"key": "strategy.stop_atr_mult", "proposed": 3.0, "evidence": "", "n_trades": 7}]


def test_offline_fallback_produces_a_review_with_no_lessons(monkeypatch):
    async def none(*a, **k):
        return None, ""
    monkeypatch.setattr(ra, "generate_json", none)
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        seed_ledger(db)
        sc = ra.build_scorecard(db, None, ACCT, window_days=60, today=TODAY)
        out, raw = asyncio.run(ra.run_review_llm(sc, {}, [], None, {}, "m", db=db, run_id="rv"))
        assert out["lessons"] == [] and out["parameter_proposals"] == []
        assert "4 closed" in out["day_summary"]
        assert raw == "(offline fallback)"
        events = [r["event"] for r in db.query("select event from audit_log")]
        assert "review_llm_fallback" in events
        assert db.query("select count(*) as n from agent_outputs where agent='review'")[0]["n"] == 1


# --------------------------------------------------------------------------- #
# Lessons store
# --------------------------------------------------------------------------- #
def test_lessons_upsert_reinforce_retire_and_ordering():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        c = db.upsert_lessons(ACCT, [
            {"id": "a", "text": "A", "confidence": 40, "applies_to": ["decision"]},
            {"id": "b", "text": "B", "confidence": 80, "applies_to": ["monitor"]},
        ], source_run_id="r1")
        assert c == {"new": 2, "reinforced": 0, "retired": 0, "skipped": 0}
        c = db.upsert_lessons(ACCT, [
            {"id": "a", "text": "A2", "confidence": 90, "status": "reinforced"},
            {"id": "b", "status": "retired", "text": "B"},
            {"id": "zzz", "status": "retired", "text": "never existed"},
        ])
        assert c == {"new": 0, "reinforced": 1, "retired": 1, "skipped": 0}
        active = db.active_lessons(ACCT)
        assert [l["id"] for l in active] == ["a"]
        assert active[0]["text"] == "A2" and active[0]["times_reinforced"] == 1
        assert active[0]["applies_to"] == ["decision"]
        assert db.active_lessons("OTHER") == []
        # Retired stays retired: the model re-emitting "b" does not resurrect it.
        c = db.upsert_lessons(ACCT, [{"id": "b", "text": "B again", "status": "reinforced",
                                      "confidence": 99}])
        assert c["skipped"] == 1 and [l["id"] for l in db.active_lessons(ACCT)] == ["a"]
        assert db.retire_lesson("a", ACCT) and db.active_lessons(ACCT) == []
        assert len(db.all_lessons(ACCT)) == 2


def test_latest_playbook_comes_from_the_latest_review():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        assert db.latest_playbook(ACCT) is None
        db.save_review(run_id="r1", account=ACCT, review_date="2026-09-14", window_days=60,
                       model="m", scorecard={}, output={"playbook": "old"}, markdown="")
        db.save_review(run_id="r2", account=ACCT, review_date="2026-09-15", window_days=60,
                       model="m", scorecard={"n_closed": 4}, output={"playbook": "new"},
                       markdown="# x")
        assert db.latest_playbook(ACCT) == "new"
        rev = db.latest_review(ACCT)
        assert rev["scorecard"] == {"n_closed": 4} and rev["output"]["playbook"] == "new"
        assert [r["review_date"] for r in db.review_history(ACCT)] == ["2026-09-15", "2026-09-14"]


# --------------------------------------------------------------------------- #
# Parameter proposals — bounds, step, gate, whitelist
# --------------------------------------------------------------------------- #
TUNABLE = {
    "strategy.stop_atr_mult": {"min": 1.5, "max": 4.0, "step": 0.5},
    "strategy.min_score_to_buy": {"min": 60, "max": 80, "step": 5},
}
RAW = {"strategy": {"stop_atr_mult": 2.5, "min_score_to_buy": 65}, "risk": {"max_position_pct": 0.35}}


def test_bound_proposal_clamps_to_range_and_one_step():
    assert ra.bound_proposal("strategy.stop_atr_mult", 3.0, 2.5, TUNABLE) == (3.0, None)
    assert ra.bound_proposal("strategy.stop_atr_mult", 4.0, 2.5, TUNABLE) == (3.0, None)   # one step
    assert ra.bound_proposal("strategy.stop_atr_mult", 0.5, 2.5, TUNABLE) == (2.0, None)   # one step down
    assert ra.bound_proposal("strategy.stop_atr_mult", 9.0, 3.5, TUNABLE) == (4.0, None)   # max
    v, why = ra.bound_proposal("strategy.stop_atr_mult", 2.6, 2.5, TUNABLE)
    assert v == 2.5 and "no change" in why                                                 # snaps to grid
    assert ra.bound_proposal("strategy.min_score_to_buy", 72, 65, TUNABLE) == (70.0, None)


def test_bound_proposal_rejects_non_whitelisted_and_risk_keys():
    _, why = ra.bound_proposal("risk.max_position_pct", 1.0, 0.35, TUNABLE)
    assert "not a tunable key" in why
    _, why = ra.bound_proposal("risk.max_position_pct", 1.0, 0.35,
                               {"risk.max_position_pct": {"min": 0, "max": 1, "step": 1}})
    assert "outside the strategy: block" in why


def test_apply_proposals_gate_whitelist_and_write():
    written = []

    def write(patch):
        written.append(patch)
        return {"ok": True}

    props = [{"key": "strategy.stop_atr_mult", "proposed": 3.0, "n_trades": 14, "evidence": "e"},
             {"key": "risk.max_position_pct", "proposed": 1.0},
             {"key": "strategy.min_score_to_buy", "proposed": 65}]
    # Under the gate: recorded, not written.
    recs = ra.apply_proposals(props, cfg_raw=RAW, tunable=TUNABLE, n_closed=13, min_closed=20,
                              auto_apply=True, write=write)
    assert [r["status"] for r in recs] == ["proposed", "rejected", "rejected"]
    assert "gated" in recs[0]["reason"] and recs[0]["applied"] == 3.0
    assert written == []
    # Over the gate with auto_apply: written within bounds.
    recs = ra.apply_proposals(props[:1], cfg_raw=RAW, tunable=TUNABLE, n_closed=25,
                              min_closed=20, auto_apply=True, write=write)
    assert recs[0]["status"] == "applied" and written == [{"strategy": {"stop_atr_mult": 3.0}}]
    # auto_apply off: recorded only.
    recs = ra.apply_proposals(props[:1], cfg_raw=RAW, tunable=TUNABLE, n_closed=25,
                              min_closed=20, auto_apply=False, write=write)
    assert recs[0]["status"] == "proposed" and len(written) == 1
    # A failed write is a rejection, never a silent success.
    recs = ra.apply_proposals(props[:1], cfg_raw=RAW, tunable=TUNABLE, n_closed=25,
                              min_closed=20, auto_apply=True,
                              write=lambda p: {"ok": False, "error": "boom"})
    assert recs[0]["status"] == "rejected" and "boom" in recs[0]["reason"]


def test_apply_proposals_round_trips_through_the_config_overlay(monkeypatch):
    """The real writer: server.config_io.apply_overrides -> config.local.yaml."""
    from server import config_io
    from config import load_config
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(config_io, "LOCAL_CONFIG_PATH", Path(tmp) / "config.local.yaml")
        monkeypatch.setattr("config.LOCAL_CONFIG_NAME", "does-not-exist.yaml")
        recs = ra.apply_proposals(
            [{"key": "strategy.stop_atr_mult", "proposed": 3.0, "n_trades": 30}],
            cfg_raw=load_config().raw, tunable=TUNABLE, n_closed=30, min_closed=20,
            auto_apply=True, write=config_io.apply_overrides)
        assert recs[0]["status"] == "applied"
        assert config_io.read_overrides() == {"strategy": {"stop_atr_mult": 3.0}}


def test_proposals_persist_and_can_be_marked():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        pid = db.save_proposal(run_id="r", account=ACCT, key="strategy.stop_atr_mult",
                               current=2.5, proposed=3.0, applied=3.0, evidence="e",
                               n_trades=13, status="proposed", reason="gated")
        assert db.proposals(ACCT)[0]["status"] == "proposed"
        db.mark_proposal(pid, "applied", applied=3.0, reason="applied by hand")
        p = db.get_proposal(pid)
        assert p["status"] == "applied" and p["reason"] == "applied by hand"


# --------------------------------------------------------------------------- #
# Orchestrator wiring
# --------------------------------------------------------------------------- #
def test_lessons_payload_is_absent_when_empty_and_bounded_when_present():
    from orchestrator import Orchestrator
    from recommend_check import StubProvider
    from config import load_config
    with tempfile.TemporaryDirectory() as tmp:
        cfg = load_config()
        cfg.set_mode("recommend")
        cfg.raw["storage"]["db_path"] = str(Path(tmp) / "t.db")
        cfg.apply_account("agentic")
        orch = Orchestrator(cfg, provider=StubProvider())
        assert orch._lessons_payload() is None
        orch.db.upsert_lessons(cfg.account_number, [
            {"id": f"l{i}", "text": f"lesson {i}", "confidence": i * 5} for i in range(12)])
        pl = orch._lessons_payload()
        assert len(pl["items"]) == 8                       # bounded
        assert pl["items"][0]["text"] == "lesson 11"       # most confident first
        assert pl["playbook"] == ""


def test_review_brief_renders_every_section():
    from reporting.review_brief import render_review
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        seed_ledger(db)
        sc = ra.build_scorecard(db, FlatProvider(), ACCT, window_days=60, today=TODAY,
                                min_position_usd=13.0)
        out = {"day_summary": "S", "what_worked": ["w"], "what_didnt": ["d"],
               "lessons": [{"id": "a", "text": "L", "evidence": "E", "confidence": 55,
                            "applies_to": ["decision"], "status": "new"}],
               "playbook": "P"}
        md = render_review(sc, out, [{"key": "strategy.stop_atr_mult", "current": 2.5,
                                      "proposed": 3.0, "applied": 3.0, "status": "proposed",
                                      "reason": "gated"}],
                           account_label="agentic", day="2026-09-15",
                           lesson_counts={"new": 1, "reinforced": 0, "retired": 0})
        for needle in ("# Review — 2026-09-15", "## Scorecard", "By entry score",
                       "What the agent's calls did next", "## Closed trades", "**NVDA**",
                       "## Behaviour", "Take-profit fired more than once on: HPE ×2",
                       "## What worked", "## Lessons", "**[NEW]** L", "evidence: E",
                       "## Parameter proposals", "`strategy.stop_atr_mult`", "**proposed**",
                       "## Playbook", "P"):
            assert needle in md, needle

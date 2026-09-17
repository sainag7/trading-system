"""Offline tests for the Monitor's graded-exit rules — the dust-churn fix.

NO network, NO API keys. Background: HPE was half-trimmed on 2026-09-04, 09-10
and 09-14 down to a $1.76 stub, and on 09-14 that stub was sold to buy $1.76 of
GOOGL — a market-order round trip for no change in exposure. Two rules stop it:

  * ``strategy.min_position_usd`` — a partial whose remainder would be dust, or
    any exit on a position that already is dust, closes in full.
  * ``scaled_out`` — "trim to half and keep trailing the rest" means ONE
    scale-out; the next stop hit closes the remainder.

And one invariant: a rule's full exit is never downgraded to a partial by the
model.

Run:  python -m pytest test_monitor_exits.py -q
"""
from __future__ import annotations

import asyncio

import pytest

from agents import monitor_agent
from agents.monitor_agent import _deterministic_exits, run_monitor

STRATEGY = {"graded_stops": True, "exit_below_score": 38, "min_position_usd": 10}


def _pos(ticker="HPE", shares=0.0626, price=56.0, stop=56.5, take=62.0, **kw):
    """A healthy uptrend pullback sitting ON its trailing stop: above the
    200-SMA, positive 3m return, modest P&L — the shape that qualifies for a
    graded half-trim. Override ``stop``/``price`` for the other branches."""
    p = {"ticker": ticker, "shares": shares, "current_price": price,
         "stop_loss": stop, "take_profit": take, "max_hold_until": "2026-12-31",
         "score": 70, "above_sma200": True, "ret_3m": 12.0,
         "unrealized_pnl_pct": 4.0, "scaled_out": False}
    p.update(kw)
    return p


def _only(exits, ticker):
    got = [e for e in exits if e["ticker"] == ticker]
    assert len(got) == 1, exits
    return got[0]


# --------------------------------------------------------------------------- #
# The floor
# --------------------------------------------------------------------------- #
def test_stop_hit_on_a_dust_position_exits_in_full():
    # 0.0626 sh @ $56 = $3.51 — the 2026-09-10 stub. Half of dust is still dust.
    e = _only(_deterministic_exits([_pos()], STRATEGY), "HPE")
    assert e["action"] == "exit_full" and e["shares"] is None
    assert e["trigger"] == "stop_loss"


def test_pullback_whose_remainder_would_be_dust_exits_in_full():
    # $16 position: a half-trim leaves $8 < $10 floor -> close the whole thing.
    e = _only(_deterministic_exits([_pos(shares=0.2857)], STRATEGY), "HPE")
    assert e["action"] == "exit_full"
    assert "stub" in e["reason"]


def test_pullback_with_a_healthy_remainder_still_scales_out_to_half():
    # $56 position: half leaves $28, comfortably above the floor.
    e = _only(_deterministic_exits([_pos(shares=1.0)], STRATEGY), "HPE")
    assert e["action"] == "exit_partial"
    assert e["trigger"] == "stop_trail"
    assert e["shares"] == pytest.approx(0.5)


def test_take_profit_on_a_small_position_takes_the_whole_gain():
    e = _only(_deterministic_exits([_pos(shares=0.2857, price=60.0, stop=50.0, take=58.0)],
                                   STRATEGY), "HPE")
    assert e["trigger"] == "take_profit"
    assert e["action"] == "exit_full" and e["shares"] is None


def test_take_profit_on_a_healthy_position_still_trails_the_remainder():
    e = _only(_deterministic_exits([_pos(shares=1.0, price=60.0, stop=50.0, take=58.0)],
                                   STRATEGY), "HPE")
    assert e["trigger"] == "take_profit"
    assert e["action"] == "exit_partial"


def test_no_floor_configured_preserves_the_old_behaviour():
    no_floor = {**STRATEGY, "min_position_usd": 0}
    e = _only(_deterministic_exits([_pos()], no_floor), "HPE")
    assert e["action"] == "exit_partial"     # dust, but nobody asked us to care


def test_per_position_floor_overrides_the_strategy_default():
    e = _only(_deterministic_exits([_pos(shares=1.0, min_position_usd=100)],
                                   STRATEGY), "HPE")
    assert e["action"] == "exit_full"        # $56 book, $28 remainder < $100


# --------------------------------------------------------------------------- #
# One scale-out, then done
# --------------------------------------------------------------------------- #
def test_second_stop_hit_after_a_scale_out_closes_the_remainder():
    e = _only(_deterministic_exits([_pos(shares=1.0, scaled_out=True)], STRATEGY),
              "HPE")
    assert e["action"] == "exit_full" and e["shares"] is None
    assert "already scaled out" in e["reason"]


def test_broken_trend_is_a_full_exit_regardless():
    e = _only(_deterministic_exits([_pos(shares=1.0, above_sma200=False)], STRATEGY),
              "HPE")
    assert e["action"] == "exit_full"
    assert "cut losses" in e["reason"]


def test_a_holding_above_its_stop_is_left_alone():
    assert _deterministic_exits([_pos(shares=1.0, price=55.0, stop=50.0)],
                                STRATEGY) == []


# --------------------------------------------------------------------------- #
# The model cannot downgrade a hard exit
# --------------------------------------------------------------------------- #
def _patch_llm(monkeypatch, payload):
    async def fake(system, user, model, **kw):
        return payload, "raw"
    monkeypatch.setattr(monitor_agent, "generate_json", fake)


def test_llm_partial_cannot_downgrade_a_deterministic_full_exit(monkeypatch):
    _patch_llm(monkeypatch, {"exits": [
        {"ticker": "HPE", "action": "exit_partial", "shares": 0.03,
         "trigger": "stop_trail", "reason": "model: just trim"}]})
    out = asyncio.run(run_monitor([_pos()], STRATEGY, "m"))
    e = _only(out["exits"], "HPE")
    assert e["action"] == "exit_full" and e["shares"] is None
    assert e["trigger"] == "stop_loss"       # the rule's verdict, not the model's


def test_llm_may_still_add_an_exit_the_rules_missed(monkeypatch):
    _patch_llm(monkeypatch, {"exits": [
        {"ticker": "NVDA", "action": "exit_full",
         "trigger": "thesis_break", "reason": "guidance cut"}]})
    healthy = _pos("NVDA", shares=1.0, price=230.0, stop=200.0, take=280.0)
    out = asyncio.run(run_monitor([healthy], STRATEGY, "m"))
    e = _only(out["exits"], "NVDA")
    assert e["action"] == "exit_full" and e["shares"] is None


def test_llm_partial_stands_when_the_rules_also_say_partial(monkeypatch):
    _patch_llm(monkeypatch, {"exits": [
        {"ticker": "HPE", "action": "exit_partial", "shares": 0.4,
         "trigger": "stop_trail", "reason": "model: trim"}]})
    out = asyncio.run(run_monitor([_pos(shares=1.0)], STRATEGY, "m"))
    e = _only(out["exits"], "HPE")
    assert e["action"] == "exit_partial"
    assert e["shares"] == 0.4                # the model's size is respected

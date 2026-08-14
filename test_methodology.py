"""Offline tests for the momentum+quality methodology: factor scoring,
cross-sectional ranking, ATR/trailing risk, and the forward-looking verdict.

Run:  python -m pytest test_methodology.py -q
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

# Force the hermetic offline path so run_analysis's LLM enrichment is a no-op.
from agents import llm as _llm
_llm._SDK_OK = False
_llm._ANTHROPIC_OK = False

from agents.analysis_agent import (              # noqa: E402
    _momentum_factor, _quality_factor, _value_factor, run_analysis)
from agents.decision_agent import _plan_levels, _vol_size_factor   # noqa: E402
from agents.explain_agent import (               # noqa: E402
    _forward_view, _scenario_probs, _scenarios, _expected_value_pct)
from storage.db import Database                  # noqa: E402


def _tech(**kw):
    base = {"price": 100, "atr20": 3.0, "atr20_pct": 3.0,
            "distance_from_sma50_pct": None, "distance_from_sma200_pct": None,
            "distance_from_52w_high_pct": None, "ret_12_1": None, "ret_3m": None,
            "ret_6m": None, "above_sma200": None}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Factor scoring
# --------------------------------------------------------------------------- #
def test_momentum_factor_orders_uptrend_above_downtrend():
    up = _tech(distance_from_sma50_pct=8, distance_from_sma200_pct=15,
               distance_from_52w_high_pct=-3, ret_12_1=40, ret_3m=12, ret_6m=25)
    down = _tech(distance_from_sma50_pct=-20, distance_from_sma200_pct=-30,
                 distance_from_52w_high_pct=-45, ret_12_1=-35, ret_3m=-15, ret_6m=-28)
    assert _momentum_factor(up) > 70
    assert _momentum_factor(down) < 35
    assert _momentum_factor(up) > _momentum_factor(down)


def test_quality_factor_rewards_margins_low_debt():
    good = {"profit_margin": 30, "operating_margin": 35, "gross_margin": 65,
            "debt_to_equity": 0.2, "free_cash_flow": 1e9}
    bad = {"profit_margin": -5, "operating_margin": -2, "gross_margin": 15,
           "debt_to_equity": 3.5, "free_cash_flow": -1e8}
    assert _quality_factor(good) > 70
    assert _quality_factor(good) > _quality_factor(bad)


def test_value_factor_rewards_cheap_and_upside():
    cheap = {"forward_pe": 12, "ps_ratio": 2, "analyst_upside_pct": 30}
    rich = {"forward_pe": 55, "ps_ratio": 18, "analyst_upside_pct": -15}
    assert _value_factor(cheap) > _value_factor(rich)


def test_cross_sectional_ranks_strong_above_weak():
    def research(t, **kw):
        return {"ticker": t, "technicals": _tech(**kw), "fundamentals": {},
                "news_sentiment": {}, "_sector": "Tech", "_price": 100}
    strong = research("STRONG", distance_from_sma50_pct=10, distance_from_sma200_pct=20,
                      distance_from_52w_high_pct=-2, ret_12_1=45, ret_3m=15, above_sma200=True)
    weak = research("WEAK", distance_from_sma50_pct=-25, distance_from_sma200_pct=-35,
                    distance_from_52w_high_pct=-50, ret_12_1=-40, ret_3m=-20, above_sma200=False)
    mid = research("MID", distance_from_sma50_pct=0, distance_from_sma200_pct=2,
                   distance_from_52w_high_pct=-15, ret_12_1=5, ret_3m=1, above_sma200=True)
    items = asyncio.run(run_analysis([strong, weak, mid], "test",
                                     {"methodology": "momentum_quality"}))
    rank = {it["ticker"]: it["rank"] for it in items}
    assert rank["STRONG"] < rank["WEAK"]            # lower rank = better
    strong_it = next(it for it in items if it["ticker"] == "STRONG")
    assert strong_it["composite_score"] > 55
    assert "factor_percentiles" in strong_it["score_breakdown"]


# --------------------------------------------------------------------------- #
# ATR-based risk (stops / sizing)
# --------------------------------------------------------------------------- #
def test_plan_levels_caps_loss_and_holds_r_multiple():
    strat = {"stop_atr_mult": 2.5, "max_loss_pct": 0.10,
             "target_r_multiple": 2.5, "max_holding_days": 60}
    # High ATR (8): a 2.5x stop would be 20% wide -> capped at the 10% max loss.
    stop, take, _ = _plan_levels(100.0, 8.0, strat)
    assert abs(stop - 90.0) < 0.01
    assert abs((take - 100.0) / (100.0 - stop) - 2.5) < 0.01
    # Low ATR (1): the ATR stop (2.5%) binds, well inside the cap.
    stop2, _, _ = _plan_levels(100.0, 1.0, strat)
    assert abs(stop2 - 97.5) < 0.01
    # No ATR: fall back to the max-loss stop.
    stop3, _, _ = _plan_levels(100.0, None, strat)
    assert abs(stop3 - 90.0) < 0.01


def test_vol_size_factor_shrinks_high_vol_names():
    assert _vol_size_factor(3) == 1.0
    assert _vol_size_factor(4) == 1.0
    assert _vol_size_factor(8) <= 0.5          # ~half size at 8% ATR
    assert _vol_size_factor(40) == 0.3         # floored


# --------------------------------------------------------------------------- #
# Trailing stop (chandelier) — raise only, never lower
# --------------------------------------------------------------------------- #
def test_update_trail_raises_but_never_lowers():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "t.db"))
        db.upsert_trade_plan(ticker="AAA", run_id="r", entry_price=100,
                             stop_loss=90, peak_price=100)
        db.update_trail("AAA", stop_loss=95, peak_price=110)      # ratchet up
        p = db.get_trade_plans()["AAA"]
        assert p["stop_loss"] == 95 and p["peak_price"] == 110
        db.update_trail("AAA", stop_loss=92, peak_price=105)      # lower -> ignored
        p = db.get_trade_plans()["AAA"]
        assert p["stop_loss"] == 95 and p["peak_price"] == 110
        db.close()


# --------------------------------------------------------------------------- #
# Forward-looking verdict + scenarios
# --------------------------------------------------------------------------- #
def test_forward_view_lean_direction():
    up = _tech(above_sma200=True, ret_3m=12)
    bull = _forward_view(100, up, {"analyst_upside_pct": 25},
                         {"stop": 90, "target": 125, "risk_reward_r": 2.5})
    assert bull["lean"] == "bullish"
    assert bull["risk_reward_r"] == 2.5 and bull["analyst_upside_pct"] == 25
    down = _forward_view(100, _tech(above_sma200=False, ret_3m=-15),
                         {"analyst_upside_pct": -10}, None)
    assert down["lean"] == "bearish"


def test_scenario_probs_tilt_and_expected_value():
    up = _tech(above_sma200=True, ret_3m=10)
    probs = _scenario_probs(up)
    assert probs["bull"] > probs["bear"]
    assert abs(sum(probs.values()) - 1.0) < 0.02
    down = _scenario_probs(_tech(above_sma200=False, ret_3m=-12))
    assert down["bear"] > down["bull"]
    scen = _scenarios(100.0, {"resistance": 110, "support": 95}, 3.0, up)
    assert all("probability" in s for s in scen)
    assert _expected_value_pct(100.0, scen) is not None

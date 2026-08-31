"""Offline tests for trade-plan integrity: the time-stop and the recorded fill.

NO network, NO API keys. These cover two defects that both cost real money:

  * ``max_hold_until`` was taken verbatim from the model, which emitted dates in
    the PAST — so the Monitor time-stopped every position out the next morning
    and the book churned daily.
  * Fill prices were never confirmed (``place_equity_order`` returns on
    acceptance, not execution), so the pre-trade REFERENCE price was booked as
    the cost basis and the stop was anchored to it.

Run:  python -m pytest test_trade_plan.py -q
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.decision_agent import _normalize, _until, _valid_until
from config import load_config
from execution.executor import Executor, OrderResult
from risk.guardrails import OrderIntent, Side


STRATEGY = {"max_holding_days": 60, "stop_atr_mult": 2.5, "target_r_multiple": 2.5}


def _today() -> date:
    return datetime.now(timezone.utc).date()


# --------------------------------------------------------------------------- #
# max_hold_until validation — the churn bug
# --------------------------------------------------------------------------- #
def test_past_date_is_replaced():
    """The exact value observed in production: ten months in the past."""
    out, rejected = _valid_until("2025-10-24", STRATEGY)
    assert rejected == "2025-10-24"
    assert out == _until(STRATEGY)
    assert date.fromisoformat(out) > _today()


def test_date_beyond_the_horizon_is_replaced():
    too_far = (_today() + timedelta(days=365)).isoformat()
    out, rejected = _valid_until(too_far, STRATEGY)
    assert rejected == too_far
    assert date.fromisoformat(out) <= _today() + timedelta(days=60)


def test_unparseable_date_is_replaced():
    out, rejected = _valid_until("next tuesday", STRATEGY)
    assert rejected == "next tuesday"
    assert date.fromisoformat(out) > _today()


def test_today_is_rejected_as_an_immediate_time_stop():
    """A same-day stop would exit on the very next run — the churn we're fixing."""
    out, rejected = _valid_until(_today().isoformat(), STRATEGY)
    assert rejected is not None
    assert date.fromisoformat(out) > _today()


def test_valid_in_window_date_is_preserved():
    good = (_today() + timedelta(days=30)).isoformat()
    out, rejected = _valid_until(good, STRATEGY)
    assert (out, rejected) == (good, None)


def test_missing_date_falls_back_without_flagging_a_rejection():
    out, rejected = _valid_until(None, STRATEGY)
    assert rejected is None                      # absent is not "bad model output"
    assert date.fromisoformat(out) > _today()


def test_normalize_sanitises_a_past_date_end_to_end():
    order = {"ticker": "NVDA", "action": "buy", "price": 212.03,
             "suggested_stop_loss": 206.9, "take_profit": 269.65,
             "max_hold_until": "2025-10-24"}
    out = _normalize(order, {}, STRATEGY)
    assert out["_rejected_max_hold_until"] == "2025-10-24"
    assert date.fromisoformat(out["max_hold_until"]) > _today()


def test_normalize_leaves_non_actionable_orders_alone():
    out = _normalize({"ticker": "NVDA", "action": "hold"}, {}, STRATEGY)
    assert out["_rejected_max_hold_until"] is None


# --------------------------------------------------------------------------- #
# Re-anchoring the stop/target to the real fill
# --------------------------------------------------------------------------- #
def _orch(tmp: str):
    from orchestrator import Orchestrator
    from recommend_check import StubProvider
    cfg = load_config()
    cfg.set_mode("recommend")
    cfg.raw.setdefault("discovery", {})["dynamic_discovery"] = False
    cfg.raw.setdefault("execution", {})["read_live_account"] = False
    cfg.raw["storage"]["db_path"] = str(Path(tmp) / "trading.db")
    return Orchestrator(cfg, provider=StubProvider())


def _intent(stop=206.9, take=269.65):
    return OrderIntent(ticker="NVDA", side=Side.BUY, action="buy", usd_amount=34.0,
                       price=212.03, stop_loss=stop, take_profit=take)


def test_reanchor_preserves_the_intended_risk_distance():
    """The production case: sized off $212.03, filled at $224.32."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        intent = _intent()
        stop, take = orch._reanchor_levels(intent, 212.03, 224.32)
        assert stop == pytest.approx(218.89, abs=0.01)
        # Risk as a PERCENTAGE of entry is what must survive the slippage.
        before = (212.03 - 206.9) / 212.03
        after = (224.32 - stop) / 224.32
        assert after == pytest.approx(before, rel=1e-3)
        # ...and so does the reward:risk ratio.
        assert (take - 224.32) / (224.32 - stop) == pytest.approx(
            (269.65 - 212.03) / (212.03 - 206.9), rel=1e-3)


def test_reanchor_is_a_noop_without_a_confirmed_fill():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        intent = _intent()
        assert orch._reanchor_levels(intent, 212.03, 0.0) == (206.9, 269.65)


def test_reanchor_tolerates_missing_levels():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        intent = _intent(stop=None, take=None)
        assert orch._reanchor_levels(intent, 212.03, 224.32) == (None, None)


# --------------------------------------------------------------------------- #
# Fill confirmation polling
# --------------------------------------------------------------------------- #
class _StubBroker:
    """Returns a scripted sequence of get_order_fill payloads."""

    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0

    async def get_order_fill(self, order_id):
        self.calls += 1
        return self.states[min(self.calls - 1, len(self.states) - 1)]


def _executor(tmp: str, broker, **cfg):
    from storage.db import Database
    db = Database(str(Path(tmp) / "trading.db"))
    exec_cfg = {"fill_poll_attempts": 3, "fill_poll_interval_seconds": 0}
    exec_cfg.update(cfg)
    return Executor(broker=broker, mode="live", db=db, run_id="test-run",
                    exec_cfg=exec_cfg, kill_switch_check=lambda: False)


def _submitted() -> OrderResult:
    return OrderResult(ok=True, status="submitted", filled_qty=0.0, fill_price=0.0,
                       broker_order_id="oid-1", detail={})


def test_poll_records_the_confirmed_fill():
    with tempfile.TemporaryDirectory() as tmp:
        broker = _StubBroker({"state": "filled", "filled_qty": 0.1513, "fill_price": 224.32})
        ex = _executor(tmp, broker)
        res = asyncio.run(ex._confirm_fill(_submitted(), ticker="NVDA"))
        assert res.status == "filled" and res.ok
        assert res.fill_price == pytest.approx(224.32)
        assert res.filled_qty == pytest.approx(0.1513)


def test_poll_waits_through_a_pending_state():
    with tempfile.TemporaryDirectory() as tmp:
        broker = _StubBroker(
            {"state": "queued", "filled_qty": None, "fill_price": None},
            {"state": "filled", "filled_qty": 0.1513, "fill_price": 224.32},
        )
        ex = _executor(tmp, broker)
        res = asyncio.run(ex._confirm_fill(_submitted(), ticker="NVDA"))
        assert res.status == "filled"
        assert broker.calls == 2


def test_poll_never_invents_a_price_when_still_pending():
    """Exhausting the attempts must leave the cost basis unset, not guessed."""
    with tempfile.TemporaryDirectory() as tmp:
        broker = _StubBroker({"state": "queued", "filled_qty": None, "fill_price": None})
        ex = _executor(tmp, broker)
        res = asyncio.run(ex._confirm_fill(_submitted(), ticker="NVDA"))
        assert res.status == "submitted"
        assert res.fill_price == 0.0
        assert broker.calls == 3


def test_poll_marks_a_cancelled_order_as_not_ok():
    with tempfile.TemporaryDirectory() as tmp:
        broker = _StubBroker({"state": "cancelled", "filled_qty": None, "fill_price": None})
        ex = _executor(tmp, broker)
        res = asyncio.run(ex._confirm_fill(_submitted(), ticker="NVDA"))
        assert not res.ok and res.status == "cancelled"


def test_poll_disabled_by_config():
    with tempfile.TemporaryDirectory() as tmp:
        broker = _StubBroker({"state": "filled", "filled_qty": 1.0, "fill_price": 10.0})
        ex = _executor(tmp, broker, fill_poll_attempts=0)
        res = asyncio.run(ex._confirm_fill(_submitted(), ticker="NVDA"))
        assert res.status == "submitted" and broker.calls == 0


def test_poll_skipped_for_a_rejected_send():
    """A send the broker refused has nothing to confirm."""
    with tempfile.TemporaryDirectory() as tmp:
        broker = _StubBroker({"state": "filled", "filled_qty": 1.0, "fill_price": 10.0})
        ex = _executor(tmp, broker)
        rejected = OrderResult(ok=False, status="rejected", broker_order_id=None)
        res = asyncio.run(ex._confirm_fill(rejected, ticker="NVDA"))
        assert res.status == "rejected" and broker.calls == 0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

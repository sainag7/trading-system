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
from risk.guardrails import AccountState, OrderIntent, Side


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


# --------------------------------------------------------------------------- #
# The system must not sell and re-buy the same name in one cycle
# --------------------------------------------------------------------------- #
def _buy_order(ticker="NVDA", usd=25.0):
    return {"ticker": ticker, "action": "buy", "side": "BUY",
            "target_dollar_amount": usd, "price": 227.91, "confidence": 70}


def test_buy_is_dropped_when_the_monitor_is_exiting_that_ticker():
    """The 2026-08-31 / 09-03 case: Monitor sells NVDA, agent re-buys it same run."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        acct = AccountState(equity=177.0, cash=37.0, buying_power=161.0,
                            peak_equity=177.0, positions={})
        intents = orch._decision_to_intents([_buy_order("NVDA")], acct,
                                            exiting={"NVDA"})
        assert intents == []


def test_buys_for_other_tickers_survive_the_exit_filter():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        acct = AccountState(equity=177.0, cash=37.0, buying_power=161.0,
                            peak_equity=177.0, positions={})
        intents = orch._decision_to_intents(
            [_buy_order("NVDA"), _buy_order("HPE", 30.0)], acct, exiting={"NVDA"})
        assert [i.ticker for i in intents] == ["HPE"]


def test_no_exits_means_nothing_is_filtered():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        acct = AccountState(equity=177.0, cash=37.0, buying_power=161.0,
                            peak_equity=177.0, positions={})
        intents = orch._decision_to_intents([_buy_order("NVDA")], acct, exiting=set())
        assert [i.ticker for i in intents] == ["NVDA"]


# --------------------------------------------------------------------------- #
# deployable_cash follows the broker's buying power, not settled cash
# --------------------------------------------------------------------------- #
def test_deployable_cash_uses_buying_power_not_settled_cash():
    """Post-upgrade, unsettled proceeds must reach the decision agent's budget."""
    from orchestrator import _account_summary

    class _Limits:
        min_cash_reserve_pct = 0.0

    acct = AccountState(equity=177.0, cash=37.04, buying_power=161.96,
                        peak_equity=177.0, positions={})
    summary = _account_summary(acct, _Limits())
    assert summary["deployable_cash"] == pytest.approx(161.96, abs=0.01)
    assert summary["cash"] == pytest.approx(37.04, abs=0.01)   # still reported truthfully


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))


# --------------------------------------------------------------------------- #
# Sells fund the same cycle's buys
# --------------------------------------------------------------------------- #
from risk.guardrails import GuardrailResult  # noqa: E402


def _approved(intent, usd, shares) -> GuardrailResult:
    return GuardrailResult(intent=intent, approved=True, approved_usd=usd,
                           approved_shares=shares, resized=False,
                           reasons=[], checks=[])


def _exit_intent(ticker="HPE", shares=0.125207, price=48.0):
    return OrderIntent(ticker=ticker, side=Side.SELL, action="sell",
                       shares=shares, price=price, protective_exit=True)


def test_pending_exit_proceeds_sums_only_sells():
    from orchestrator import _pending_exit_proceeds
    intents = [_exit_intent("HPE", 0.5, 40.0), _exit_intent("NVDA", 0.25, 200.0)]
    assert _pending_exit_proceeds(intents) == pytest.approx(20.0 + 50.0)
    assert _pending_exit_proceeds([]) == 0.0
    assert _pending_exit_proceeds(None) == 0.0


def test_pending_exit_proceeds_values_a_full_exit_at_the_held_size():
    """The 2026-09-15 shape: an exit_full intent carries shares=None (sell all),
    and it was being summed as $0 — a $150 NVDA stop read as $0.97 of proceeds."""
    from orchestrator import _account_summary, _pending_exit_proceeds
    from risk.guardrails import Position

    acct = AccountState(equity=278.85, cash=0.18, buying_power=0.18, peak_equity=282.0,
                        positions={"NVDA": Position("NVDA", 0.712752, 231.5, 150.36),
                                   "HPE": Position("HPE", 0.031302, 48.24, 1.51)})
    full = OrderIntent(ticker="NVDA", side=Side.SELL, action="exit_full",
                       shares=None, price=210.96, protective_exit=True)
    part = _exit_intent("HPE", 0.015651, 62.09)
    assert _pending_exit_proceeds([full, part], acct) == pytest.approx(
        0.712752 * 210.96 + 0.015651 * 62.09)
    # Without an account to resolve against, a None-share exit is still $0 —
    # never a guess.
    assert _pending_exit_proceeds([full]) == 0.0
    summary = _account_summary(acct, None, [full, part])
    assert summary["expected_exit_proceeds"] == pytest.approx(151.33, abs=0.01)
    assert summary["deployable_cash_after_exits"] == pytest.approx(151.51, abs=0.01)


def test_account_summary_reports_what_the_exits_will_free():
    """A fully-invested book reads $0 deployable — the agent must still be told
    that this cycle's exits are about to fund it."""
    from orchestrator import _account_summary

    class _L:
        min_cash_reserve_pct = 0.0

    acct = AccountState(equity=275.50, cash=0.0, buying_power=0.0,
                        peak_equity=275.50, positions={})
    s = _account_summary(acct, _L(), [_exit_intent("HPE", 0.125207, 48.0)])
    assert s["deployable_cash"] == 0.0
    assert s["expected_exit_proceeds"] == pytest.approx(6.01, abs=0.01)
    assert s["deployable_cash_after_exits"] == pytest.approx(6.01, abs=0.01)


def test_account_summary_without_exits_is_unchanged():
    from orchestrator import _account_summary

    class _L:
        min_cash_reserve_pct = 0.0

    acct = AccountState(equity=100.0, cash=40.0, buying_power=40.0,
                        peak_equity=100.0, positions={})
    s = _account_summary(acct, _L())
    assert s["expected_exit_proceeds"] == 0.0
    assert s["deployable_cash_after_exits"] == s["deployable_cash"] == 40.0


class _StagedHarness:
    """Records _execute calls and serves a scripted post-exit account read."""

    def __init__(self, orch, after_account):
        self.orch, self.after = orch, after_account
        self.calls = []
        orch._execute = self._execute            # type: ignore[method-assign]
        orch._read_account = self._read          # type: ignore[method-assign]

    async def _execute(self, broker, approved, account, **kw):
        self.calls.append([r.intent.ticker for r in approved])

    async def _read(self, broker):
        return self.after


def _plan(orch, approved, account):
    from orchestrator import CyclePlan
    return CyclePlan(account=account, broker=object(), approved=approved)


def test_staged_execution_buys_with_the_cash_the_exits_freed():
    """The 2026-09-09 shape: $0 buying power, an exit pending, a buy that could
    not have been placed before the sell filled."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        orch.cfg.apply_account("agentic")
        broke = AccountState(equity=275.0, cash=0.0, buying_power=0.0,
                             peak_equity=275.0, positions={})
        flush = AccountState(equity=275.0, cash=60.0, buying_power=60.0,
                             peak_equity=275.0, positions={})
        approved = [_approved(_exit_intent(), 6.01, 0.125207),
                    _approved(OrderIntent(ticker="NVDA", side=Side.BUY, action="buy",
                                          usd_amount=50.0, price=227.91), 50.0, 0.219)]
        h = _StagedHarness(orch, flush)
        asyncio.run(orch._execute_staged(_plan(orch, approved, broke)))
        assert h.calls[0] == ["HPE"], h.calls          # exits first, alone
        assert h.calls[1] == ["NVDA"], h.calls         # buys after the re-read


def test_staged_execution_skips_buys_when_the_reread_fails():
    """The exits already succeeded; guessing the balance is the unsafe move."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        orch.cfg.apply_account("agentic")
        broke = AccountState(equity=275.0, cash=0.0, buying_power=0.0,
                             peak_equity=275.0, positions={})
        approved = [_approved(_exit_intent(), 6.01, 0.125207),
                    _approved(OrderIntent(ticker="NVDA", side=Side.BUY, action="buy",
                                          usd_amount=50.0, price=227.91), 50.0, 0.219)]
        h = _StagedHarness(orch, None)               # re-read returns nothing
        asyncio.run(orch._execute_staged(_plan(orch, approved, broke)))
        assert h.calls == [["HPE"]]                  # sells ran, buys did not


def test_staged_execution_is_single_pass_when_there_is_nothing_to_sequence():
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        orch.cfg.apply_account("agentic")
        acct = AccountState(equity=275.0, cash=100.0, buying_power=100.0,
                            peak_equity=275.0, positions={})
        approved = [_approved(OrderIntent(ticker="NVDA", side=Side.BUY, action="buy",
                                          usd_amount=50.0, price=227.91), 50.0, 0.219)]
        h = _StagedHarness(orch, acct)
        asyncio.run(orch._execute_staged(_plan(orch, approved, acct)))
        assert h.calls == [["NVDA"]]                 # one pass, no extra read


# --------------------------------------------------------------------------- #
# Budgets floor; a stage-1 rejection still gets a second look
# --------------------------------------------------------------------------- #
from risk.guardrails import CheckOutcome  # noqa: E402


def test_account_summary_floors_the_budget_rather_than_rounding_up():
    """2026-09-10: $3.507702 was reported as $3.51, the agent proposed exactly
    $3.51, and the guardrails rejected it for spending $0.0023 that never
    existed. A budget must never overstate the money on hand."""
    from orchestrator import _account_summary

    class _L:
        min_cash_reserve_pct = 0.0

    acct = AccountState(equity=264.20, cash=0.0, buying_power=0.0,
                        peak_equity=280.0, positions={})
    # The live figures: 0.062604 shares of HPE at $56.03 = $3.5077021200000003.
    s = _account_summary(acct, _L(), [_exit_intent("HPE", 0.062604, 56.03)])
    assert s["expected_exit_proceeds"] == 3.50           # NOT 3.51
    assert s["deployable_cash_after_exits"] == 3.50


def test_every_budget_figure_floors_in_the_same_direction():
    from orchestrator import _account_summary

    class _L:
        min_cash_reserve_pct = 0.0

    acct = AccountState(equity=100.0, cash=0.0, buying_power=12.99999,
                        peak_equity=100.0, positions={})
    s = _account_summary(acct, _L(), [_exit_intent("X", 1.0, 7.999999)])
    assert s["deployable_cash"] == 12.99
    assert s["expected_exit_proceeds"] == 7.99
    assert s["deployable_cash_after_exits"] == 20.99


def _rejected(intent, failed_check: str) -> GuardrailResult:
    return GuardrailResult(
        intent=intent, approved=False, approved_usd=0.0, approved_shares=0.0,
        resized=False, reasons=["rejected"],
        checks=[CheckOutcome(failed_check, False, "no room")],
    )


def _ssl_buy(usd: float = 3.51):
    return OrderIntent(ticker="SSL", side=Side.BUY, action="buy",
                       usd_amount=usd, price=14.02, sector="Materials")


def test_stage_one_cash_rejection_gets_a_second_look_after_the_exits():
    """The 2026-09-10 failure mode: SSL was rejected at stage 1 for being $0.0023
    short, so it never entered plan.approved — `buys` was empty, and the whole
    two-stage mechanism switched itself off. The rejection disabled the very
    machinery built to prevent it. (The exit here frees enough to clear the
    minimum-position floor and the $5 minimum trade; the sub-floor case is the
    next test.)"""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        orch.cfg.apply_account("agentic")
        broke = AccountState(equity=264.20, cash=0.0, buying_power=0.0,
                             peak_equity=264.20, positions={})
        flush = AccountState(equity=264.20, cash=20.0077021200000003,
                             buying_power=20.0077021200000003,
                             peak_equity=264.20, positions={})
        exit_res = _approved(_exit_intent(), 20.0077021200000003, 0.35)
        plan = _plan(orch, [exit_res], broke)
        plan.results = [exit_res, _rejected(_ssl_buy(20.01), "min_cash_reserve_pct")]
        h = _StagedHarness(orch, flush)
        asyncio.run(orch._execute_staged(plan))
        assert h.calls[0] == ["HPE"], h.calls          # exits first
        assert h.calls[1] == ["SSL"], h.calls          # revived after the re-read


def test_revived_buy_under_the_minimum_position_is_dropped_not_placed():
    """The same 09-10 shape, but the exit only frees $3.51 — 1.3% of the book.
    The carry-forward still gets its second look, and the floors then drop it:
    a $3.51 SSL position can never matter, so the cash waits."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        orch.cfg.apply_account("agentic")
        broke = AccountState(equity=264.20, cash=0.0, buying_power=0.0,
                             peak_equity=264.20, positions={})
        flush = AccountState(equity=264.20, cash=3.5077021200000003,
                             buying_power=3.5077021200000003,
                             peak_equity=264.20, positions={})
        exit_res = _approved(_exit_intent(), 3.5077021200000003, 0.062604)
        plan = _plan(orch, [exit_res], broke)
        plan.results = [exit_res, _rejected(_ssl_buy(), "min_cash_reserve_pct")]
        h = _StagedHarness(orch, flush)
        asyncio.run(orch._execute_staged(plan))
        assert h.calls == [["HPE"]], h.calls           # exit placed, dust buy not
        rows = orch.db.query(
            "select approved, reasons from decisions where ticker='SSL' order by id desc limit 1")
        assert rows and rows[0]["approved"] == 0
        assert "minimum" in rows[0]["reasons"]


def test_a_rejection_cash_cannot_fix_is_not_carried_forward():
    """A no-trade-list block has nothing to do with money — re-running it would
    only add a duplicate decision row saying the same thing."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        orch.cfg.apply_account("agentic")
        broke = AccountState(equity=264.20, cash=0.0, buying_power=0.0,
                             peak_equity=264.20, positions={})
        flush = AccountState(equity=264.20, cash=60.0, buying_power=60.0,
                             peak_equity=264.20, positions={})
        exit_res = _approved(_exit_intent(), 6.01, 0.125207)
        plan = _plan(orch, [exit_res], broke)
        plan.results = [exit_res, _rejected(_ssl_buy(), "no_trade_list")]
        h = _StagedHarness(orch, flush)
        asyncio.run(orch._execute_staged(plan))
        assert h.calls == [["HPE"]]                    # single pass, no revival


# --------------------------------------------------------------------------- #
# trade_plans lifecycle — a plan must not outlive its position
# --------------------------------------------------------------------------- #
def _seed_plan(orch, ticker="HPE", account="806", **kw):
    orch.db.upsert_trade_plan(ticker=ticker, run_id="seed", entry_price=48.0,
                              stop_loss=44.0, take_profit=58.0,
                              max_hold_until="2026-12-31", thesis="t",
                              peak_price=48.0, account=account, **kw)


def _settle(orch, ticker, *, sold, pre_shares, action="exit_full", trigger="stop_loss",
            shares=None):
    """Drive _settle_plan_after_sell the way _execute does."""
    from execution.executor import OrderResult
    intent = OrderIntent(ticker=ticker, side=Side.SELL, action=action, shares=shares,
                         price=50.0, trigger=trigger)
    res = GuardrailResult(intent=intent, approved=True, approved_usd=sold * 50.0,
                          approved_shares=sold, resized=False, reasons=["approved"],
                          checks=[])
    orch._settle_plan_after_sell(intent, res, pre_shares,
                                 OrderResult(ok=True, status="filled", filled_qty=sold,
                                             fill_price=50.0))


def test_full_exit_deletes_the_plan():
    """Seven of twelve plan rows were stale on 2026-09-15 because nothing ever
    deleted one. A confirmed sell of the whole position must remove it."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        orch.cfg.apply_account("agentic")
        acct = orch.cfg.account_number
        _seed_plan(orch, account=acct)
        _settle(orch, "HPE", sold=0.125207, pre_shares=0.125207)
        assert "HPE" not in orch.db.get_trade_plans(account=acct)
        events = [r["event"] for r in orch.db.query(
            "SELECT event FROM audit_log WHERE run_id=?", (orch.run_id,))]
        assert "trade_plan_closed" in events


def test_partial_exit_keeps_the_plan_and_marks_it_scaled_out():
    """A half-trim on a trailing stop leaves a live position: the plan stays,
    flagged so the next stop hit closes the remainder instead of halving it
    again. A take-profit partial flags tp_taken instead."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        orch.cfg.apply_account("agentic")
        acct = orch.cfg.account_number
        _seed_plan(orch, account=acct)
        _settle(orch, "HPE", sold=0.062604, pre_shares=0.125207, action="exit_partial",
                trigger="stop_trail", shares=0.062604)
        plan = orch.db.get_trade_plans(account=acct)["HPE"]
        assert plan["scaled_out"] == 1 and plan["tp_taken"] == 0
        assert plan["stop_loss"] == 44.0          # levels untouched
        _seed_plan(orch, "NTAP", account=acct)
        _settle(orch, "NTAP", sold=0.1, pre_shares=0.2, action="exit_partial",
                trigger="take_profit", shares=0.1)
        plan = orch.db.get_trade_plans(account=acct)["NTAP"]
        assert plan["tp_taken"] == 1 and plan["scaled_out"] == 0


def test_full_exit_tolerates_broker_share_rounding():
    """The broker reports holdings to 6dp; a sell sized from that figure must
    still read as a full exit, not a partial that leaves a 1e-7 share stub."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        orch.cfg.apply_account("agentic")
        acct = orch.cfg.account_number
        _seed_plan(orch, account=acct)
        _settle(orch, "HPE", sold=0.125207, pre_shares=0.1252071, action="exit_partial",
                trigger="stop_trail", shares=0.125207)
        assert "HPE" not in orch.db.get_trade_plans(account=acct)


def test_prune_only_touches_the_matching_account():
    """A run on the individual book must never discard the agentic book's plans
    just because the individual account does not hold those names."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        _seed_plan(orch, "HPE", account="806")
        _seed_plan(orch, "SCCO", account="806")
        _seed_plan(orch, "LEGACY", account=None)
        # Pruning against the OTHER account: nothing of 806's is touched.
        assert orch.db.prune_trade_plans(held=["ORCL"], account="750") == []
        assert set(orch.db.get_trade_plans(account="806")) == {"HPE", "SCCO"}
        # Pruning against 806 with only SCCO held: HPE goes, LEGACY (no
        # account) is never auto-pruned, SCCO stays.
        assert orch.db.prune_trade_plans(held=["SCCO"], account="806") == ["HPE"]
        assert set(orch.db.get_trade_plans(account="806")) == {"SCCO"}
        assert set(orch.db.get_trade_plans(account=None)) == {"LEGACY"}
        # No account means no pruning at all.
        assert orch.db.prune_trade_plans(held=[], account=None) == []


def test_get_trade_plans_is_account_scoped():
    """The individual account's Monitor must not read the agentic account's
    stops for a ticker both happen to hold — and a legacy row (no account) is
    not visible to either: it belongs to single-account mode only."""
    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        _seed_plan(orch, "NVDA", account="806")
        _seed_plan(orch, "OLD", account=None)
        assert set(orch.db.get_trade_plans(account="806")) == {"NVDA"}
        assert set(orch.db.get_trade_plans(account="750")) == set()
        assert set(orch.db.get_trade_plans()) == {"OLD"}


def test_finalize_prunes_against_a_populated_read_only():
    """An empty account read is the model UNDER-reporting (the read-sanity gate
    exists for exactly this), so finalize must not treat it as 'sold out'."""
    from risk.guardrails import Position

    with tempfile.TemporaryDirectory() as tmp:
        orch = _orch(tmp)
        orch.cfg.apply_account("agentic")
        acct_no = orch.cfg.account_number
        _seed_plan(orch, "HPE", account=acct_no)
        _seed_plan(orch, "SCCO", account=acct_no)
        orch.notifier.send_digest = lambda *a, **k: None   # type: ignore[method-assign]

        reads = [
            AccountState(equity=1.0, cash=1.0, buying_power=1.0, peak_equity=1.0,
                         positions={}),
            AccountState(equity=60.0, cash=0.0, buying_power=0.0, peak_equity=60.0,
                         positions={"SCCO": Position("SCCO", 0.3, 200.0, 60.0)}),
        ]

        async def _read(broker):
            return reads.pop(0)
        orch._read_account = _read                      # type: ignore[method-assign]

        asyncio.run(orch._finalize_run(broker=object()))   # empty read
        assert set(orch.db.get_trade_plans(account=acct_no)) == {"HPE", "SCCO"}
        asyncio.run(orch._finalize_run(broker=object()))   # SCCO only
        assert set(orch.db.get_trade_plans(account=acct_no)) == {"SCCO"}

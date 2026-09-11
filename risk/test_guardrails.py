"""Unit tests for the deterministic risk guardrails.

Run from the repo root:  pytest risk/test_guardrails.py -v

These tests are the safety contract of the whole system. They cover every hard
limit and its boundary values, plus the module's reject-favoring semantics:

  * per_trade_max_usd, max_position_pct, max_sector_pct, min_cash_reserve_pct
    and buying power are all caps on SIZE: an oversized buy is TRIMMED to the
    room left under them, and only a cap with no room left rejects. Each of
    those has a paired "no room at all -> still rejected" case, so trimming can
    never become a way to smuggle a zero-headroom order through.
  * The kill switch rejects EVERYTHING; the drawdown halt rejects buys but allows
    risk-reducing sells; the no-trade list blocks buys but allows exits.
  * Every decision is emitted to the optional audit sink.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow running as `pytest risk/test_guardrails.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import RiskLimits  # noqa: E402
from risk.guardrails import (  # noqa: E402
    AccountState,
    OrderIntent,
    Position,
    Side,
    sweep_to_budget,
    validate_batch,
    validate_order,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def default_limits(**overrides) -> RiskLimits:
    base = dict(
        max_positions=15,
        max_position_pct=0.15,
        max_sector_pct=0.40,
        per_trade_max_usd=500.0,
        daily_max_trades=5,
        min_cash_reserve_pct=0.10,
        max_account_drawdown_halt_pct=0.15,
        min_trade_usd=50.0,
        allow_fractional_shares=True,
        no_trade_list=(),
    )
    base.update(overrides)
    return RiskLimits.from_dict(base)


def account(
    equity=10_000.0,
    cash=10_000.0,
    buying_power=None,
    peak_equity=None,
    positions=None,
) -> AccountState:
    return AccountState(
        equity=equity,
        cash=cash,
        buying_power=buying_power if buying_power is not None else cash,
        peak_equity=peak_equity if peak_equity is not None else equity,
        positions=positions or {},
    )


def buy(ticker="AAPL", usd=300.0, price=100.0, sector="Technology", **kw) -> OrderIntent:
    return OrderIntent(
        ticker=ticker, side=Side.BUY, action="buy",
        usd_amount=usd, price=price, sector=sector, **kw
    )


def sell(ticker="AAPL", shares=None, price=100.0, **kw) -> OrderIntent:
    return OrderIntent(
        ticker=ticker, side=Side.SELL, action="sell",
        shares=shares, price=price, **kw
    )


def pos(ticker="AAPL", shares=10, avg_cost=100, market_value=1_000, sector="Technology"):
    return {ticker: Position(ticker, shares=shares, avg_cost=avg_cost,
                             market_value=market_value, sector=sector)}


# ===========================================================================
# Happy path
# ===========================================================================
def test_clean_buy_is_approved_unchanged():
    res = validate_order(buy(usd=300), account(), default_limits())
    assert res.approved and not res.resized
    assert res.approved_usd == pytest.approx(300.0)
    assert res.approved_shares == pytest.approx(3.0)


# ===========================================================================
# per_trade_max_usd  (the only RESIZE)
# ===========================================================================
def test_per_trade_cap_resizes_down():
    res = validate_order(buy(usd=800), account(equity=100_000, cash=100_000),
                         default_limits(per_trade_max_usd=500))
    assert res.approved and res.resized
    assert res.approved_usd == pytest.approx(500.0)
    assert any("per-trade cap" in r for r in res.reasons)


def test_per_trade_exactly_at_limit_is_not_resized():
    res = validate_order(buy(usd=500), account(equity=100_000, cash=100_000),
                         default_limits(per_trade_max_usd=500))
    assert res.approved and not res.resized
    assert res.approved_usd == pytest.approx(500.0)


def test_per_trade_one_cent_over_resizes():
    res = validate_order(buy(usd=500.01), account(equity=100_000, cash=100_000),
                         default_limits(per_trade_max_usd=500))
    assert res.approved and res.resized
    assert res.approved_usd == pytest.approx(500.0)


def test_per_trade_pct_binds_when_tighter_than_usd_ceiling():
    """On a small book the equity-scaled pct is what caps the order."""
    res = validate_order(buy(usd=80), account(equity=100, cash=100),
                         default_limits(per_trade_max_usd=500,
                                        per_trade_max_pct=0.30,
                                        max_position_pct=1.0,
                                        min_trade_usd=1))
    assert res.approved and res.resized
    assert res.approved_usd == pytest.approx(30.0)


def test_per_trade_usd_binds_when_tighter_than_pct():
    """On a large book the absolute dollar ceiling is what caps the order."""
    res = validate_order(buy(usd=800), account(equity=100_000, cash=100_000),
                         default_limits(per_trade_max_usd=500,
                                        per_trade_max_pct=0.30))
    assert res.approved and res.resized
    assert res.approved_usd == pytest.approx(500.0)


def test_per_trade_pct_scales_with_equity():
    """The cap grows with the account — no manual edit needed as it compounds."""
    limits = default_limits(per_trade_max_usd=500, per_trade_max_pct=0.30,
                            max_position_pct=1.0, min_trade_usd=1)
    res = validate_order(buy(usd=1000), account(equity=1000, cash=1000), limits)
    assert res.approved_usd == pytest.approx(300.0)


def test_per_trade_pct_defaults_to_noop():
    """Configs that never set the pct keep pure per_trade_max_usd behaviour."""
    res = validate_order(buy(usd=800), account(equity=100_000, cash=100_000),
                         default_limits(per_trade_max_usd=500))
    assert res.approved_usd == pytest.approx(500.0)


# ===========================================================================
# max_position_pct  (TRIM to the room left; reject only when there is none)
# ===========================================================================
def test_position_pct_over_cap_is_trimmed():
    # 15% of 10k = $1,500 cap. Request $2,000 -> TRIM to $1,500, don't discard.
    res = validate_order(
        buy(usd=2000, price=100),
        account(equity=10_000, cash=10_000),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.approved and res.resized
    assert res.approved_usd == pytest.approx(1_500.0)
    assert any("position cap" in r for r in res.reasons)


def test_position_pct_full_leaves_no_room_and_rejects():
    # Already holding the whole $1,500 cap — nothing left to trim to.
    res = validate_order(
        buy(usd=500, price=100),
        account(equity=10_000, cash=10_000,
                positions=pos("AAPL", shares=15, market_value=1_500)),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.rejected
    assert any("position cap" in r for r in res.reasons)


def test_position_pct_exactly_at_cap_is_approved():
    res = validate_order(
        buy(usd=1500, price=100),
        account(equity=10_000, cash=10_000),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.approved and not res.resized
    assert res.approved_usd == pytest.approx(1_500.0)


def test_position_pct_existing_holding_trims_the_add():
    # Hold $1,200 of AAPL; cap $1,500. A $500 add -> trimmed to the $300 of room.
    res = validate_order(
        buy(usd=500, price=100),
        account(equity=10_000, cash=10_000,
                positions=pos("AAPL", shares=12, market_value=1_200)),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.approved and res.resized
    assert res.approved_usd == pytest.approx(300.0)
    assert any("position cap" in r for r in res.reasons)


def test_position_pct_add_within_cap_approved():
    # Hold $1,000 of AAPL; cap $1,500. A $400 add -> $1,400 <= cap -> APPROVE.
    res = validate_order(
        buy(usd=400, price=100, ticker="AAPL"),
        account(equity=10_000, cash=10_000,
                positions=pos("AAPL", shares=10, market_value=1_000)),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.approved and not res.resized
    assert res.approved_usd == pytest.approx(400.0)


# ===========================================================================
# max_sector_pct  (TRIM to the room left; reject only when there is none)
# ===========================================================================
def test_sector_pct_over_cap_is_trimmed():
    # 40% of 10k = $4,000 sector cap. Already $3,800 Tech; a $500 buy trims to $200.
    res = validate_order(
        buy(ticker="AAPL", usd=500, price=100, sector="Technology"),
        account(equity=10_000, cash=10_000,
                positions=pos("MSFT", shares=10, avg_cost=380, market_value=3_800)),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.approved and res.resized
    assert res.approved_usd == pytest.approx(200.0)
    assert any("sector cap" in r for r in res.reasons)


def test_sector_pct_full_leaves_no_room_and_rejects():
    # Tech already at the full $4,000 cap — nothing left to trim to.
    res = validate_order(
        buy(ticker="AAPL", usd=500, price=100, sector="Technology"),
        account(equity=10_000, cash=10_000,
                positions=pos("MSFT", shares=10, avg_cost=400, market_value=4_000)),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.rejected
    assert any("sector cap" in r for r in res.reasons)


def test_sector_pct_exactly_at_cap_approved():
    # $3,800 Tech + $200 = $4,000 == cap -> APPROVE.
    res = validate_order(
        buy(ticker="AAPL", usd=200, price=100, sector="Technology"),
        account(equity=10_000, cash=10_000,
                positions=pos("MSFT", shares=10, avg_cost=380, market_value=3_800)),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.approved and not res.resized
    assert res.approved_usd == pytest.approx(200.0)


def test_sector_cap_isolated_per_sector():
    # A health-care buy is unaffected by a full tech sector.
    res = validate_order(
        buy(ticker="UNH", usd=300, price=100, sector="Health Care"),
        account(equity=40_000, cash=40_000,
                positions=pos("MSFT", shares=10, avg_cost=400, market_value=4_000)),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.approved and not res.resized
    assert res.approved_usd == pytest.approx(300.0)


# ===========================================================================
# min_cash_reserve_pct  (TRIM to the balance above the floor)
#
# The floor is measured against buying_power, not the settled `cash` field —
# see the comment in validate_order. On limited margin Robinhood reports
# cash: 0.00 against a healthy buying power, and measuring the reserve on
# `cash` would reject every buy the account could actually afford.
# ===========================================================================
def test_cash_reserve_breach_is_trimmed_to_the_floor():
    # equity 10k, 10% floor = $1,000, buying power 1,200 -> $200 deployable.
    # A $500 buy is trimmed to $200 rather than discarded.
    res = validate_order(
        buy(usd=500, price=100),
        account(equity=10_000, cash=1_200, buying_power=1_200),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.approved and res.resized
    assert res.approved_usd == pytest.approx(200.0)
    assert any("reserve floor" in r for r in res.reasons)


def test_cash_reserve_exactly_at_floor_approved():
    # buying power 1,500; $500 buy -> exactly $1,000 == floor -> APPROVE untouched.
    res = validate_order(
        buy(usd=500, price=100),
        account(equity=10_000, cash=1_500, buying_power=1_500),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.approved and not res.resized
    assert res.approved_usd == pytest.approx(500.0)


def test_cash_reserve_blocks_when_already_below_floor():
    # Below the floor already: headroom is negative, so there is nothing to
    # trim to and the order is still rejected outright.
    res = validate_order(
        buy(usd=100, price=100),
        account(equity=10_000, cash=800, buying_power=800),
        default_limits(per_trade_max_usd=10_000),
    )
    assert res.rejected
    assert any("reserve floor" in r for r in res.reasons)


def test_cash_reserve_measured_against_buying_power_not_settled_cash():
    """Limited margin: proceeds are spendable before they settle.

    Robinhood reports settled `cash` as 0.00 while buying_power carries the
    unsettled proceeds. Measuring the reserve against `cash` rejected every buy
    the account could comfortably afford — the same stranding bug that was
    already fixed in sweep_to_budget().
    """
    res = validate_order(
        buy(usd=100, price=100),
        account(equity=1_000, cash=0.0, buying_power=200.0),
        default_limits(per_trade_max_usd=10_000, min_cash_reserve_pct=0.10),
    )
    assert res.approved and not res.resized      # floor $100, room $100
    assert res.approved_usd == pytest.approx(100.0)


# ===========================================================================
# buying power  (TRIM to what is spendable)
# ===========================================================================
def test_buying_power_insufficient_is_trimmed():
    res = validate_order(
        buy(usd=500, price=100),
        account(equity=100_000, cash=100_000, buying_power=250),
        default_limits(per_trade_max_usd=10_000, min_cash_reserve_pct=0.0,
                       max_position_pct=1.0),
    )
    assert res.approved and res.resized
    assert res.approved_usd == pytest.approx(250.0)
    assert any("buying power" in r for r in res.reasons)


def test_no_buying_power_at_all_is_rejected():
    res = validate_order(
        buy(usd=500, price=100),
        account(equity=100_000, cash=0.0, buying_power=0.0),
        default_limits(per_trade_max_usd=10_000, min_cash_reserve_pct=0.0,
                       max_position_pct=1.0),
    )
    assert res.rejected
    assert any("buying power" in r for r in res.reasons)


# ===========================================================================
# max_positions
# ===========================================================================
def test_max_positions_blocks_new_name():
    positions = {
        f"T{i}": Position(f"T{i}", shares=1, avg_cost=10, market_value=10, sector="Technology")
        for i in range(15)
    }
    res = validate_order(
        buy(ticker="NEW", usd=100, price=100),
        account(equity=100_000, cash=100_000, positions=positions),
        default_limits(),
    )
    assert res.rejected
    assert any("max positions" in r for r in res.reasons)


def test_max_positions_allows_add_to_existing():
    positions = {
        f"T{i}": Position(f"T{i}", shares=1, avg_cost=10, market_value=10, sector="Technology")
        for i in range(15)
    }
    res = validate_order(
        buy(ticker="T0", usd=100, price=100, sector="Technology"),
        account(equity=100_000, cash=100_000, positions=positions),
        default_limits(),
    )
    assert res.approved


def test_max_positions_none_means_unlimited():
    """`max_positions: null` lets the decision agent choose the book's size."""
    positions = {
        f"T{i}": Position(f"T{i}", shares=1, avg_cost=10, market_value=10, sector="Technology")
        for i in range(15)
    }
    res = validate_order(
        buy(ticker="NEW", usd=100, price=100),
        account(equity=100_000, cash=100_000, positions=positions),
        default_limits(max_positions=None),
    )
    assert res.approved
    assert any(c.name == "max_positions" and "unlimited" in c.detail for c in res.checks)


# ===========================================================================
# daily_max_trades  (executed + pending)
# ===========================================================================
def test_daily_trade_limit_blocks_buy():
    res = validate_order(buy(usd=100), account(), default_limits(daily_max_trades=5),
                         trades_today=5)
    assert res.rejected
    assert any("daily trade limit" in r for r in res.reasons)


def test_daily_trade_limit_blocks_sell_too():
    res = validate_order(sell(shares=5), account(positions=pos()),
                         default_limits(daily_max_trades=5), trades_today=5)
    assert res.rejected


def test_daily_trade_limit_one_below_is_allowed():
    res = validate_order(buy(usd=100), account(), default_limits(daily_max_trades=5),
                         trades_today=4)
    assert res.approved


def test_protective_exit_bypasses_daily_cap():
    # A stop-loss sell must fire even when the daily cap is already reached.
    res = validate_order(
        sell(shares=10, price=100, protective_exit=True),
        account(positions=pos(shares=10)),
        default_limits(daily_max_trades=5), trades_today=5,
    )
    assert res.approved


def test_non_protective_sell_still_blocked_by_daily_cap():
    # A discretionary (take-profit/trim) sell is still subject to the cap.
    res = validate_order(
        sell(shares=10, price=100),  # protective_exit defaults False
        account(positions=pos(shares=10)),
        default_limits(daily_max_trades=5), trades_today=5,
    )
    assert res.rejected


def test_protective_exits_do_not_consume_daily_budget():
    # Cap is 1; the buy fills it, yet both protective exits still go through and
    # do not block each other (they don't consume a daily slot).
    intents = [
        buy(ticker="AAPL", usd=100, price=100),
        sell(ticker="MSFT", shares=5, price=100, protective_exit=True),
        sell(ticker="NVDA", shares=5, price=100, protective_exit=True),
    ]
    acct = account(equity=100_000, cash=100_000,
                   positions={**pos("MSFT", shares=5), **pos("NVDA", shares=5)})
    results = validate_batch(intents, acct, default_limits(
        daily_max_trades=1, max_position_pct=1.0, max_sector_pct=1.0, per_trade_max_usd=10_000))
    assert results[0].approved          # buy consumes the single slot
    assert results[1].approved and results[2].approved  # protective exits exempt


# ===========================================================================
# no_trade_list
# ===========================================================================
def test_no_trade_list_blocks_buy():
    res = validate_order(buy(ticker="TSLA", usd=100, price=100),
                         account(), default_limits(no_trade_list=["TSLA"]))
    assert res.rejected
    assert any("no-trade list" in r for r in res.reasons)


def test_no_trade_list_is_case_insensitive():
    res = validate_order(buy(ticker="tsla", usd=100, price=100),
                         account(), default_limits(no_trade_list=["TSLA"]))
    assert res.rejected


def test_no_trade_list_still_allows_selling_to_exit():
    res = validate_order(
        sell(ticker="TSLA", shares=10, price=100),
        account(positions=pos("TSLA", shares=10, sector="Consumer Discretionary")),
        default_limits(no_trade_list=["TSLA"]),
    )
    assert res.approved


# ===========================================================================
# drawdown halt
# ===========================================================================
def test_drawdown_halt_blocks_buys():
    res = validate_order(
        buy(usd=100, price=100),
        account(equity=8_400, cash=8_400, peak_equity=10_000),
        default_limits(max_account_drawdown_halt_pct=0.15),
    )
    assert res.rejected and res.account_halted


def test_drawdown_halt_still_allows_sells():
    res = validate_order(
        sell(shares=10, price=84),
        account(equity=8_400, cash=0, peak_equity=10_000,
                positions=pos("AAPL", shares=10, market_value=840)),
        default_limits(max_account_drawdown_halt_pct=0.15),
    )
    assert res.approved and res.account_halted  # halt flag still surfaced


def test_drawdown_exactly_at_limit_halts():
    res = validate_order(
        buy(usd=100, price=100),
        account(equity=8_500, cash=8_500, peak_equity=10_000),
        default_limits(max_account_drawdown_halt_pct=0.15),
    )
    assert res.rejected and res.account_halted


def test_drawdown_just_below_limit_allows_buy():
    res = validate_order(
        buy(usd=100, price=100),
        account(equity=8_600, cash=8_600, peak_equity=10_000),
        default_limits(max_account_drawdown_halt_pct=0.15),
    )
    assert res.approved


def test_no_peak_history_does_not_halt():
    res = validate_order(
        buy(usd=100, price=100),
        account(equity=5_000, cash=5_000, peak_equity=0.0),
        default_limits(),
    )
    assert res.approved


# ===========================================================================
# KILL SWITCH (full)
# ===========================================================================
def test_kill_switch_blocks_buy():
    res = validate_order(buy(usd=100), account(), default_limits(), kill_switch=True)
    assert res.rejected and res.kill_switch_active


def test_kill_switch_blocks_sell():
    res = validate_order(sell(shares=10), account(positions=pos()), default_limits(),
                         kill_switch=True)
    assert res.rejected and res.kill_switch_active


def test_kill_switch_rejects_entire_batch():
    intents = [
        buy(ticker="AAPL", usd=300, price=100),
        sell(ticker="MSFT", shares=5, price=100),
        buy(ticker="NVDA", usd=200, price=50),
    ]
    acct = account(equity=100_000, cash=100_000,
                   positions=pos("MSFT", shares=10, market_value=1_000))
    results = validate_batch(intents, acct, default_limits(), kill_switch=True)
    assert len(results) == 3
    assert all(r.rejected and r.kill_switch_active for r in results)
    assert all(r.approved_usd == 0 and r.approved_shares == 0 for r in results)


# ===========================================================================
# sells
# ===========================================================================
def test_sell_without_position_is_rejected():
    res = validate_order(sell(ticker="AAPL", shares=5), account(), default_limits())
    assert res.rejected
    assert any("no open position" in r for r in res.reasons)


def test_sell_more_than_held_is_clamped():
    res = validate_order(sell(shares=25, price=100), account(positions=pos(shares=10)),
                         default_limits())
    assert res.approved and res.resized
    assert res.approved_shares == pytest.approx(10.0)  # cannot short / oversell


def test_sell_full_position_when_shares_unspecified():
    res = validate_order(sell(shares=None, price=100),
                         account(positions=pos(shares=7, market_value=700)), default_limits())
    assert res.approved
    assert res.approved_shares == pytest.approx(7.0)


def test_sell_zero_shares_is_rejected():
    res = validate_order(sell(shares=0, price=100), account(positions=pos(shares=10)),
                         default_limits())
    assert res.rejected


def test_sell_negative_shares_is_rejected():
    res = validate_order(sell(shares=-5, price=100), account(positions=pos(shares=10)),
                         default_limits())
    assert res.rejected


# ===========================================================================
# malformed / sizing / fractional shares / min trade
# ===========================================================================
def test_fractional_shares_allowed():
    res = validate_order(buy(usd=250, price=100), account(equity=100_000, cash=100_000),
                         default_limits(per_trade_max_usd=10_000, allow_fractional_shares=True))
    assert res.approved
    assert res.approved_shares == pytest.approx(2.5)


def test_fractional_buy_quantized_to_8_decimals():
    # Reproduces the live rejection: 24.77 / 333.07 = 0.07439590524937507 (17 dp);
    # Robinhood rejects a quantity with more than 8 decimal places.
    from decimal import Decimal
    res = validate_order(buy(usd=24.77, price=333.07),
                         account(equity=100_000, cash=100_000),
                         default_limits(per_trade_max_usd=25, min_trade_usd=1,
                                        allow_fractional_shares=True))
    assert res.approved
    decimals = -Decimal(str(res.approved_shares)).as_tuple().exponent
    assert decimals <= 8, res.approved_shares
    # Floored (never up) and the notional is recomputed to match the tradeable size.
    assert res.approved_shares <= 24.77 / 333.07 + 1e-12
    assert res.approved_usd == pytest.approx(res.approved_shares * 333.07)


def test_whole_shares_floor_when_fractional_disabled():
    res = validate_order(buy(usd=250, price=100), account(equity=100_000, cash=100_000),
                         default_limits(per_trade_max_usd=10_000, allow_fractional_shares=False))
    assert res.approved
    assert res.approved_shares == 2  # floor(250/100)
    assert res.approved_usd == pytest.approx(200.0)


def test_below_min_trade_is_rejected():
    res = validate_order(buy(usd=40, price=100), account(), default_limits(min_trade_usd=50))
    assert res.rejected
    assert any("minimum trade" in r for r in res.reasons)


def test_missing_price_rejected():
    res = validate_order(buy(usd=100, price=0.0), account(), default_limits())
    assert res.rejected
    assert any("price" in r for r in res.reasons)


def test_negative_notional_rejected():
    res = validate_order(buy(usd=-100, price=100), account(), default_limits())
    assert res.rejected


def test_negative_shares_buy_rejected():
    intent = OrderIntent(ticker="AAPL", side=Side.BUY, shares=-3, price=100, sector="Technology")
    res = validate_order(intent, account(equity=100_000, cash=100_000), default_limits())
    assert res.rejected


def test_zero_equity_rejected():
    res = validate_order(buy(usd=100, price=100), account(equity=0.0, cash=0.0), default_limits())
    assert res.rejected


def test_buy_with_no_notional_rejected():
    intent = OrderIntent(ticker="AAPL", side=Side.BUY, usd_amount=0.0, shares=None, price=100)
    res = validate_order(intent, account(), default_limits())
    assert res.rejected


def test_buy_sized_by_shares_when_usd_absent():
    intent = OrderIntent(ticker="AAPL", side=Side.BUY, shares=2, price=100, sector="Technology")
    res = validate_order(intent, account(equity=100_000, cash=100_000),
                         default_limits(per_trade_max_usd=10_000))
    assert res.approved
    assert res.approved_usd == pytest.approx(200.0)


# ===========================================================================
# precedence: which breach is reported first
# ===========================================================================
def test_per_trade_resize_then_position_trim():
    # $2,000 request, per-trade $500 -> trimmed to $500; the position cap leaves
    # only $300 of room -> trimmed again, to $300. Caps compose, tightest wins.
    res = validate_order(
        buy(ticker="AAPL", usd=2000, price=100, sector="Technology"),
        account(equity=2_000, cash=2_000),  # 15% of 2k = $300 position cap
        default_limits(per_trade_max_usd=500, min_cash_reserve_pct=0.0),
    )
    assert res.approved and res.resized
    assert res.approved_usd == pytest.approx(300.0)
    assert any("position cap" in r for r in res.reasons)


# ===========================================================================
# batch validation accumulates state
# ===========================================================================
def test_batch_daily_limit_cuts_off_after_n_trades():
    intents = [buy(ticker=f"T{i}", usd=100, price=100, sector="Technology") for i in range(7)]
    acct = account(equity=1_000_000, cash=1_000_000)
    results = validate_batch(intents, acct, default_limits(
        daily_max_trades=5, max_position_pct=1.0, max_sector_pct=1.0, per_trade_max_usd=10_000))
    assert sum(1 for r in results if r.approved) == 5  # 6th and 7th blocked


def test_batch_cumulative_cash_reserve_trims_then_stops():
    # buying power 2,000, equity 10k, floor $1,000 -> $1,000 deployable across
    # the batch. Two buys fit whole, the third is trimmed to the last $200, and
    # the rest are rejected with nothing left. The batch never exceeds the floor.
    intents = [buy(ticker=f"T{i}", usd=400, price=100, sector=f"S{i}") for i in range(5)]
    acct = account(equity=10_000, cash=2_000, buying_power=2_000)
    results = validate_batch(intents, acct, default_limits(
        per_trade_max_usd=10_000, min_cash_reserve_pct=0.10,
        max_position_pct=1.0, max_sector_pct=1.0))
    approved = [r for r in results if r.approved]
    total = sum(r.approved_usd for r in approved)
    assert [round(r.approved_usd, 2) for r in approved] == [400.0, 400.0, 200.0]
    assert total == pytest.approx(1_000.0)
    assert total <= 1_000.0 + 1e-6
    assert results[3].rejected and results[4].rejected


def test_batch_cumulative_sector_cap_trims_second():
    # Two tech buys that each fit but together breach the 40% sector cap: the
    # second is trimmed to the remaining $1,000 rather than dropped.
    intents = [
        buy(ticker="AAPL", usd=3000, price=100, sector="Technology"),
        buy(ticker="MSFT", usd=3000, price=100, sector="Technology"),
    ]
    acct = account(equity=10_000, cash=10_000)
    results = validate_batch(intents, acct, default_limits(
        per_trade_max_usd=10_000, max_position_pct=1.0, max_sector_pct=0.40))
    assert results[0].approved and not results[0].resized
    assert results[1].approved and results[1].resized
    assert results[1].approved_usd == pytest.approx(1_000.0)
    total_tech = sum(r.approved_usd for r in results if r.approved)
    assert total_tech <= 4_000.0 + 1e-6


# ===========================================================================
# audit logging
# ===========================================================================
def test_audit_logs_approved():
    events = []
    validate_order(buy(usd=100), account(), default_limits(),
                   audit=lambda lvl, ev, det: events.append((lvl, ev)))
    assert ("INFO", "guardrail_approved") in events


def test_audit_logs_modified_on_resize():
    events = []
    validate_order(buy(usd=900), account(equity=100_000, cash=100_000),
                   default_limits(per_trade_max_usd=500),
                   audit=lambda lvl, ev, det: events.append((lvl, ev)))
    assert ("INFO", "guardrail_modified") in events


def test_audit_logs_rejected():
    # No buying power at all: nothing to trim to, so this really is a rejection.
    events = []
    validate_order(buy(usd=2000, price=100),
                   account(equity=10_000, cash=0.0, buying_power=0.0),
                   default_limits(per_trade_max_usd=10_000, min_cash_reserve_pct=0.0),
                   audit=lambda lvl, ev, det: events.append((lvl, ev)))
    assert ("WARN", "guardrail_rejected") in events


def test_audit_logs_kill_switch_halt():
    events = []
    validate_order(buy(usd=100), account(), default_limits(), kill_switch=True,
                   audit=lambda lvl, ev, det: events.append((lvl, ev)))
    assert ("HALT", "guardrail_kill_switch") in events


def test_audit_logs_drawdown_halt():
    events = []
    validate_order(buy(usd=100, price=100),
                   account(equity=8_000, cash=8_000, peak_equity=10_000),
                   default_limits(), audit=lambda lvl, ev, det: events.append((lvl, ev)))
    assert ("HALT", "guardrail_drawdown_halt") in events


def test_audit_logs_one_event_per_batch_order():
    events = []
    intents = [buy(ticker="AAPL", usd=100, price=100), buy(ticker="MSFT", usd=100, price=100)]
    validate_batch(intents, account(equity=100_000, cash=100_000), default_limits(),
                   audit=lambda lvl, ev, det: events.append((lvl, ev)))
    assert len(events) == 2


# ===========================================================================
# determinism contract
# ===========================================================================
def test_validation_is_pure_and_repeatable():
    args = (buy(usd=2000, price=100), account(equity=10_000, cash=10_000),
            default_limits(per_trade_max_usd=10_000))
    r1 = validate_order(*args)
    r2 = validate_order(*args)
    assert r1.approved == r2.approved
    assert r1.approved_usd == pytest.approx(r2.approved_usd)
    assert r1.reasons == r2.reasons


# ===========================================================================
# sweep_to_budget — deploy idle cash across the approved buys
# ===========================================================================
def _sweep_limits(**overrides) -> RiskLimits:
    """The agentic-account shape: every percentage ceiling off, no position cap."""
    base = dict(
        max_positions=None, max_position_pct=1.0, max_sector_pct=1.0,
        per_trade_max_usd=500.0, per_trade_max_pct=1.0, min_cash_reserve_pct=0.0,
        min_trade_usd=1.0, daily_max_trades=5, sweep_cash_to_buys=True,
    )
    base.update(overrides)
    return default_limits(**base)


def test_sweep_deploys_the_idle_remainder():
    """The 2026-08-28 case: $103.21 book, two $34 buys, $35.21 left idle."""
    acct = account(equity=103.21, cash=103.21)
    limits = _sweep_limits()
    results = validate_batch(
        [buy(ticker="NVDA", usd=34.0, price=212.03, sector="Technology"),
         buy(ticker="AMZN", usd=34.0, price=259.945, sector="Consumer")],
        acct, limits,
    )
    assert sum(r.approved_usd for r in results) == pytest.approx(68.0, abs=0.01)

    sweep_to_budget(results, acct, limits)
    total = sum(r.approved_usd for r in results)
    # Fractional flooring leaves at most a few cents unspent.
    assert total == pytest.approx(103.21, abs=0.05)
    assert all(r.resized for r in results)
    assert all(any(c.name == "cash_sweep" for c in r.checks) for r in results)


def test_sweep_preserves_relative_conviction_weighting():
    """Scaling is proportional: the smaller position stays the smaller one."""
    acct = account(equity=300.0, cash=300.0)
    limits = _sweep_limits()
    results = validate_batch(
        [buy(ticker="AAA", usd=60.0, price=10.0, sector="A"),
         buy(ticker="BBB", usd=30.0, price=10.0, sector="B")],
        acct, limits,
    )
    sweep_to_budget(results, acct, limits)
    big, small = results[0].approved_usd, results[1].approved_usd
    assert sum((big, small)) == pytest.approx(300.0, abs=0.05)
    assert big == pytest.approx(2 * small, rel=0.02)   # 2:1 ratio preserved


def test_sweep_is_a_noop_without_approved_buys():
    """Proposing nothing is how the agent holds cash — that must survive."""
    acct = account(equity=100.0, cash=100.0)
    limits = _sweep_limits()
    results = validate_batch([], acct, limits)
    assert sweep_to_budget(results, acct, limits) == []


def test_sweep_never_revives_a_rejected_order():
    acct = account(equity=100.0, cash=100.0)
    limits = _sweep_limits(no_trade_list=("BAD",))
    results = validate_batch([buy(ticker="BAD", usd=20.0, price=10.0)], acct, limits)
    assert results[0].rejected
    sweep_to_budget(results, acct, limits)
    assert results[0].rejected
    assert results[0].approved_usd == pytest.approx(0.0)


def test_sweep_respects_per_trade_cap():
    """Headroom is bounded by per_trade_max_usd even with cash to spare."""
    acct = account(equity=1_000.0, cash=1_000.0)
    limits = _sweep_limits(per_trade_max_usd=100.0)
    results = validate_batch(
        [buy(ticker="AAA", usd=50.0, price=10.0, sector="A")], acct, limits)
    sweep_to_budget(results, acct, limits)
    assert results[0].approved_usd == pytest.approx(100.0, abs=0.01)


def test_sweep_respects_max_position_pct():
    acct = account(equity=1_000.0, cash=1_000.0)
    limits = _sweep_limits(max_position_pct=0.20)
    results = validate_batch(
        [buy(ticker="AAA", usd=50.0, price=10.0, sector="A")], acct, limits)
    sweep_to_budget(results, acct, limits)
    assert results[0].approved_usd == pytest.approx(200.0, abs=0.01)


def test_sweep_honours_the_cash_reserve():
    acct = account(equity=1_000.0, cash=1_000.0)
    # per_trade cap lifted so the RESERVE is what binds, not the dollar ceiling.
    limits = _sweep_limits(min_cash_reserve_pct=0.10, per_trade_max_usd=10_000.0)
    results = validate_batch(
        [buy(ticker="AAA", usd=100.0, price=10.0, sector="A")], acct, limits)
    sweep_to_budget(results, acct, limits)
    assert results[0].approved_usd == pytest.approx(900.0, abs=0.01)


def test_sweep_spends_unsettled_buying_power_on_limited_margin():
    """After the limited-margin upgrade, buying_power includes unsettled proceeds.

    The real 2026-09-03 state: $177 book, only $37.04 settled, $124.92 still
    settling. Spendable must follow the BROKER's buying power, not settled cash —
    taking the smaller of the two is what stranded ~70% of the account.
    """
    acct = account(equity=177.0, cash=37.04, buying_power=161.96)
    limits = _sweep_limits()
    results = validate_batch(
        [buy(ticker="AAA", usd=25.0, price=10.0, sector="A")], acct, limits)
    sweep_to_budget(results, acct, limits)
    assert results[0].approved_usd == pytest.approx(161.96, abs=0.05)
    # The old min(cash, buying_power) would have capped this at the settled $37.
    assert results[0].approved_usd > 37.04


def test_sweep_unchanged_on_a_cash_account():
    """Regression: on a cash account buying_power already excludes unsettled
    funds, so dropping the min() must not change behaviour there."""
    acct = account(equity=100.0, cash=100.0, buying_power=100.0)
    limits = _sweep_limits()
    results = validate_batch(
        [buy(ticker="AAA", usd=20.0, price=10.0, sector="A")], acct, limits)
    sweep_to_budget(results, acct, limits)
    assert results[0].approved_usd == pytest.approx(100.0, abs=0.01)


def test_sweep_still_honours_the_reserve_against_buying_power():
    acct = account(equity=200.0, cash=50.0, buying_power=200.0)
    limits = _sweep_limits(min_cash_reserve_pct=0.10, per_trade_max_usd=10_000.0)
    results = validate_batch(
        [buy(ticker="AAA", usd=20.0, price=10.0, sector="A")], acct, limits)
    sweep_to_budget(results, acct, limits)
    assert results[0].approved_usd == pytest.approx(180.0, abs=0.01)   # 200 - 10%


def test_sweep_does_not_spend_same_batch_sell_proceeds():
    """A sell in THIS batch has not filled yet, so it cannot fund a buy in it.

    (Under limited margin the proceeds would settle instantly, but the budget is
    read at the start of the cycle — a failed or partial exit would otherwise
    leave the buy overcommitted.)
    """
    acct = account(equity=200.0, cash=50.0,
                   positions=pos("HELD", shares=10, market_value=150, sector="A"))
    limits = _sweep_limits()
    results = validate_batch(
        [sell(ticker="HELD", shares=10, price=15.0),
         buy(ticker="AAA", usd=20.0, price=10.0, sector="B")],
        acct, limits,
    )
    sweep_to_budget(results, acct, limits)
    the_buy = next(r for r in results if r.intent.side == Side.BUY)
    # Budget is the $50 already settled, NOT $50 + $150 of sale proceeds.
    assert the_buy.approved_usd == pytest.approx(50.0, abs=0.01)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ===========================================================================
# Share precision — the broker accepts at most MAX_SHARE_DECIMALS places
# ===========================================================================
def test_sizing_never_exceeds_the_brokers_share_precision():
    """Every path that produces a share count must stay inside the limit."""
    from risk.guardrails import MAX_SHARE_DECIMALS
    from decimal import Decimal

    def dp(x):
        return max(0, -Decimal(str(x)).as_tuple().exponent)

    for price in (337.93, 1787.32, 19.7, 0.51, 227.91):
        res = validate_order(buy(usd=88.93, price=price), account(), default_limits())
        assert dp(res.approved_shares) <= MAX_SHARE_DECIMALS, (price, res.approved_shares)


def test_floor_shares_reproduces_the_live_rejection():
    """2026-09-09: a half-position trim of HPE computed 0.0626035 (7 dp) and the
    broker refused it with HTTP 400. It must floor to 6 dp."""
    from risk.guardrails import floor_shares
    assert floor_shares(0.125207 / 2) == 0.062603


def test_floor_shares_never_rounds_a_sell_above_the_holding():
    """Rounding to nearest could land above shares held and be an oversell."""
    from risk.guardrails import floor_shares
    for held in (0.125207, 0.999999, 1.0000004, 0.0000019):
        assert floor_shares(held) <= held


# ===========================================================================
# Regression: the 2026-09-10 SSL rejection
# ===========================================================================
def test_a_buy_two_tenths_of_a_cent_over_the_cash_is_trimmed_not_dropped():
    """The exact numbers from the live run that prompted this change.

    The Monitor sold $3.5077021200000003 of HPE. The budget was reported to the
    decision agent as $3.51 (``round`` rounding UP), so it proposed a $3.51 SSL
    buy — and the guardrails rejected it for being $0.0023 short, printing the
    now-infamous "would drop cash to $-0.00, below the reserve floor $0.00".

    A fifth of a cent must never cost a whole trade.
    """
    proceeds = 3.5077021200000003
    res = validate_order(
        buy(ticker="SSL", usd=3.51, price=14.02, sector="Materials"),
        account(equity=264.20, cash=proceeds, buying_power=proceeds),
        default_limits(per_trade_max_usd=500.0, per_trade_max_pct=1.0,
                       max_position_pct=1.0, max_sector_pct=1.0,
                       min_cash_reserve_pct=0.0, min_trade_usd=1.0,
                       max_positions=None),
    )
    assert res.approved, res.reasons
    assert res.resized
    assert res.approved_usd <= proceeds + 1e-9      # never spends what isn't there
    assert res.approved_usd == pytest.approx(proceeds, abs=0.02)


def test_a_trim_never_lands_above_the_cap_that_caused_it():
    """Property check: whatever binds, the approved size stays under it."""
    for cash in (0.51, 3.5077021200000003, 12.34, 99.999999, 250.0):
        res = validate_order(
            buy(ticker="SSL", usd=1_000.0, price=14.02, sector="Materials"),
            account(equity=1_000.0, cash=cash, buying_power=cash),
            default_limits(per_trade_max_usd=10_000.0, per_trade_max_pct=1.0,
                           max_position_pct=1.0, max_sector_pct=1.0,
                           min_cash_reserve_pct=0.0, min_trade_usd=0.5,
                           max_positions=None),
        )
        if res.approved:
            assert res.approved_usd <= cash + 1e-9, (cash, res.approved_usd)


def test_a_trimmed_size_still_respects_the_share_precision():
    from risk.guardrails import MAX_SHARE_DECIMALS
    from decimal import Decimal

    res = validate_order(
        buy(ticker="SSL", usd=3.51, price=14.02, sector="Materials"),
        account(equity=264.20, cash=3.5077021200000003,
                buying_power=3.5077021200000003),
        default_limits(per_trade_max_usd=500.0, per_trade_max_pct=1.0,
                       max_position_pct=1.0, max_sector_pct=1.0,
                       min_cash_reserve_pct=0.0, min_trade_usd=1.0,
                       max_positions=None),
    )
    exponent = Decimal(str(res.approved_shares)).as_tuple().exponent
    assert max(0, -exponent) <= MAX_SHARE_DECIMALS


def test_a_trim_below_the_minimum_trade_is_still_rejected():
    """Trimming must not smuggle a dust order past min_trade_usd."""
    res = validate_order(
        buy(usd=500, price=100),
        account(equity=10_000, cash=10.0, buying_power=10.0),
        default_limits(per_trade_max_usd=10_000, min_cash_reserve_pct=0.0,
                       max_position_pct=1.0, min_trade_usd=50.0),
    )
    assert res.rejected
    assert any("minimum trade" in r for r in res.reasons)

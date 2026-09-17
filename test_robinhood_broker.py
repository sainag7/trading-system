"""Offline tests for the direct Robinhood MCP broker — NO network.

Feeds the broker the real payload shapes captured from the live tools
(get_portfolio / get_equity_positions / get_equity_quotes / get_equity_orders)
and asserts the account contract, the order-state mapping, and that every
ambiguous outcome is surfaced as needs_review rather than guessed.

Run:  python -m pytest test_robinhood_broker.py -q
"""
from __future__ import annotations

import asyncio

from data.robinhood_mcp import RobinhoodMCPError
from execution.executor import (
    RobinhoodMCPBroker, account_json, order_from_payload, _next_cursor,
)
from risk.guardrails import Side

# Real captured shapes (trimmed) — the `data` half of the tool envelope.
_PORTFOLIO = {"total_value": "263.83956345", "equity_value": "184.54956345",
              "cash": "79.29", "currency": "USD",
              "buying_power": {"buying_power": "79.2900",
                               "unleveraged_buying_power": "79.2900"}}
_POSITIONS = {"positions": [
    {"symbol": "NTAP", "quantity": "0.472713", "average_buy_price": "193.290000",
     "shares_available_for_sells": "0.472713", "type": "long"},
    {"symbol": "GOOGL", "quantity": "0.262285", "average_buy_price": "346.260000",
     "shares_available_for_sells": "0.262285", "type": "long"},
]}
_QUOTES = {"results": [
    {"quote": {"symbol": "NTAP", "last_trade_price": "200.000000", "previous_close": "199"},
     "close": {"symbol": "NTAP", "price": "199"}},
    {"quote": {"symbol": "GOOGL", "last_trade_price": "350.000000", "previous_close": "349"},
     "close": {"symbol": "GOOGL", "price": "349"}},
]}
_FILLED_ORDER = {
    "id": "6aabf498-2cec-4965-8587-5fd0bccf6a0a", "symbol": "SCCO", "side": "sell",
    "type": "market", "state": "filled", "quantity": "0.307990",
    "cumulative_quantity": "0.307990", "price": None, "average_price": "196.340100",
    "dollar_based_amount": None, "time_in_force": "gfd", "market_hours": "regular_hours",
}


class FakeRH:
    def __init__(self, responses: dict | None = None, error: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.error = error

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if self.error:
            raise RobinhoodMCPError(self.error)
        r = self.responses[tool]
        return r(args) if callable(r) else r


def _broker(rh):
    return RobinhoodMCPBroker(rh, account_number="806813184")


# --------------------------------------------------------------------------- #
# Account read
# --------------------------------------------------------------------------- #
def test_get_account_assembles_cash_positions_and_market_value():
    rh = FakeRH({"get_portfolio": _PORTFOLIO, "get_equity_positions": _POSITIONS,
                 "get_equity_quotes": _QUOTES})
    acct = asyncio.run(_broker(rh).get_account(peak_equity=0.0, sectors={"NTAP": "Technology"}))
    assert acct is not None
    assert acct.cash == 79.29 and acct.buying_power == 79.29
    assert set(acct.positions) == {"NTAP", "GOOGL"}
    ntap = acct.positions["NTAP"]
    assert ntap.shares == 0.472713 and ntap.avg_cost == 193.29
    assert abs(ntap.market_value - 0.472713 * 200.0) < 1e-9
    assert ntap.sector == "Technology"
    assert abs(acct.equity - (79.29 + 0.472713 * 200 + 0.262285 * 350)) < 1e-6
    # Every read is scoped to THIS account.
    assert all(a.get("account_number") == "806813184"
               for t, a in rh.calls if t != "get_equity_quotes")


def test_get_account_follows_pagination():
    pages = [
        {"positions": [_POSITIONS["positions"][0]], "next": "https://x/?cursor=abc"},
        {"positions": [_POSITIONS["positions"][1]]},
    ]
    rh = FakeRH({"get_portfolio": _PORTFOLIO,
                 "get_equity_positions": lambda a: pages[1] if a.get("cursor") == "abc" else pages[0],
                 "get_equity_quotes": _QUOTES})
    acct = asyncio.run(_broker(rh).get_account())
    assert set(acct.positions) == {"NTAP", "GOOGL"}
    assert [a.get("cursor") for t, a in rh.calls if t == "get_equity_positions"] == [None, "abc"]


def test_get_account_transport_failure_returns_none_with_hint():
    b = _broker(FakeRH(error="401 unauthorized"))
    assert asyncio.run(b.get_account()) is None
    assert b.last_read_failure["kind"] == "no_response"
    assert "robinhood_mcp login" in b.last_read_failure["hint"]


def test_missing_quote_values_position_at_cost_never_drops_it():
    parsed = account_json(_PORTFOLIO, _POSITIONS["positions"], {"NTAP": 200.0})
    by = {p["ticker"]: p for p in parsed["positions"]}
    assert abs(by["GOOGL"]["market_value"] - 0.262285 * 346.26) < 1e-9


def test_next_cursor_variants():
    assert _next_cursor({"cursor": "c1"}) == "c1"
    assert _next_cursor({"next": "https://api/x?limit=5&cursor=c2"}) == "c2"
    assert _next_cursor({"positions": []}) is None
    assert _next_cursor(None) is None


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
def test_order_from_payload_maps_broker_fields():
    o = order_from_payload(_FILLED_ORDER)
    assert o["order_id"] == "6aabf498-2cec-4965-8587-5fd0bccf6a0a"
    assert o["state"] == "filled"
    assert o["filled_qty"] == 0.30799 and o["fill_price"] == 196.3401
    # Tolerates a wrapper and rejects non-orders.
    assert order_from_payload({"order": _FILLED_ORDER})["state"] == "filled"
    assert order_from_payload({"detail": "nope"}) is None
    assert order_from_payload("text") is None


def test_place_order_sends_exactly_one_scoped_order():
    rh = FakeRH({"place_equity_order": lambda a: {"id": "o1", "state": "confirmed", **a}})
    res = asyncio.run(_broker(rh).place_order(
        ticker="aapl", side=Side.BUY, qty=2.0, limit_price=337.934, ref_id="ref-1"))
    (tool, args), = rh.calls
    assert tool == "place_equity_order"
    assert args == {"account_number": "806813184", "symbol": "AAPL", "side": "buy",
                    "type": "limit", "market_hours": "regular_hours", "time_in_force": "gfd",
                    "quantity": "2", "limit_price": "337.93", "ref_id": "ref-1"}
    assert res.ok and res.status == "submitted" and res.broker_order_id == "o1"
    assert res.fill_price == 0.0 and res.detail["fill_price_reported"] is False


def test_market_dollar_order_has_no_limit_price():
    rh = FakeRH({"place_equity_order": lambda a: {"id": "o2", "state": "queued"}})
    asyncio.run(_broker(rh).place_order(
        ticker="AAPL", side=Side.BUY, qty=0.3, limit_price=100.0,
        order_type="market", dollar_amount=30.0))
    args = rh.calls[0][1]
    assert args["dollar_amount"] == "30.00" and "limit_price" not in args
    assert "quantity" not in args


def test_filled_order_reports_broker_fill():
    rh = FakeRH({"place_equity_order": _FILLED_ORDER})
    res = asyncio.run(_broker(rh).place_order(
        ticker="SCCO", side=Side.SELL, qty=0.30799, limit_price=196.0, order_type="market"))
    assert res.ok and res.status == "filled"
    assert res.filled_qty == 0.30799 and res.fill_price == 196.3401


def test_rejected_and_unknown_states_are_never_treated_as_sent():
    rej = FakeRH({"place_equity_order": {"id": "o3", "state": "rejected", "reason": "x"}})
    res = asyncio.run(_broker(rej).place_order(ticker="A", side=Side.BUY, qty=1, limit_price=1))
    assert not res.ok and res.status == "rejected"

    odd = FakeRH({"place_equity_order": {"id": "o4", "state": "pending_review_by_mars"}})
    res = asyncio.run(_broker(odd).place_order(ticker="A", side=Side.BUY, qty=1, limit_price=1))
    assert not res.ok and res.status == "needs_review"

    junk = FakeRH({"place_equity_order": {"detail": "not an order"}})
    res = asyncio.run(_broker(junk).place_order(ticker="A", side=Side.BUY, qty=1, limit_price=1))
    assert not res.ok and res.status == "needs_review"


def test_transport_failure_on_place_is_needs_review_not_retry():
    rh = FakeRH(error="connection reset")
    res = asyncio.run(_broker(rh).place_order(ticker="A", side=Side.BUY, qty=1, limit_price=1))
    assert not res.ok and res.status == "needs_review"
    assert len(rh.calls) == 1                      # never blind-retried


def test_get_order_fill_reads_back_by_id():
    rh = FakeRH({"get_equity_orders": lambda a: {"orders": [_FILLED_ORDER]}
                 if a.get("order_id") == _FILLED_ORDER["id"] else {"orders": []}})
    fill = asyncio.run(_broker(rh).get_order_fill(_FILLED_ORDER["id"]))
    assert fill == {"state": "filled", "filled_qty": 0.30799, "fill_price": 196.3401}
    assert asyncio.run(_broker(rh).get_order_fill("nope")) == {}
    assert asyncio.run(_broker(FakeRH(error="x")).get_order_fill("o")) == {}

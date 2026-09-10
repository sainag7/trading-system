"""Unit tests for order-quantity quantization to the broker's share precision.

The limit is Robinhood's: at most MAX_SHARE_DECIMALS (6) decimal places on a
fractional quantity. These assertions derive it rather than restating a number —
hard-coding 8 here is precisely how the code drifted away from the broker.

Run:  python -m pytest test_order_quantize.py -q
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from execution.executor import RobinhoodMCPBroker, Side, _quantize_shares, _fmt_qty
from risk.guardrails import MAX_SHARE_DECIMALS


def _decimals(x: float) -> int:
    return max(0, -Decimal(str(x)).as_tuple().exponent)


def test_quantize_floors_the_live_failure_case():
    # The exact quantity Robinhood rejected.
    q = _quantize_shares(0.07439590524937507)
    assert q == 0.074395                        # floored to the broker's precision
    assert _decimals(q) <= MAX_SHARE_DECIMALS
    assert q <= 0.07439590524937507             # never rounds up


def test_quantize_various():
    assert _quantize_shares(1.0) == 1.0
    assert _quantize_shares(2.5) == 2.5
    assert _quantize_shares(0.123456789) == 0.123456     # 7th dp dropped, not rounded
    assert _quantize_shares(3e-9) == 0.0                 # below the precision floors to zero
    # Any input comes out within the broker's precision.
    for v in (0.07439590524937507, 0.1, 0.999999999, 12.3456789012, 0.000000019):
        assert _decimals(_quantize_shares(v)) <= MAX_SHARE_DECIMALS, v


def _instruction_for(qty: float) -> str:
    """Capture the exact instruction place_order() hands to the MCP, without
    touching the network: _ask is the only outbound seam."""
    broker = RobinhoodMCPBroker(
        mcp_url="http://test.invalid/mcp", model="test-model",
        account_number="123456789",
    )
    captured: dict[str, str] = {}

    async def fake_ask(instruction, tools, max_turns=6):
        captured["instruction"] = instruction
        return {"status": "submitted", "order_id": "x", "filled_qty": qty,
                "fill_price": 1.0}, instruction

    broker._ask = fake_ask
    asyncio.run(broker.place_order(
        ticker="AAPL", side=Side.BUY, qty=qty, limit_price=337.93,
    ))
    return captured["instruction"]


def test_instruction_text_never_exceeds_the_share_precision():
    """Regression for the live HTTP 400: Robinhood rejected an order because the
    QUANTITY IN THE INSTRUCTION had 17 decimals. Quantizing the float is not
    enough on its own — what reaches the broker is this rendered string, so the
    guarantee has to be asserted here, not only on _quantize_shares' return."""
    # The exact quantity Robinhood rejected on 2026-07-27.
    text = _instruction_for(0.07434944237918216)
    m = re.search(r"for ([0-9.]+) shares of", text)
    assert m, f"quantity not found in instruction: {text!r}"
    rendered = m.group(1)
    assert _decimals(float(rendered)) <= MAX_SHARE_DECIMALS, rendered
    assert rendered == "0.074349"
    # The raw un-quantized float must not appear anywhere in the instruction.
    assert "0.07434944237918216" not in text


def test_instruction_quantity_is_never_scientific_notation():
    # A tiny fractional order must still render as a plain decimal — "1.9e-08"
    # would be rejected by the broker's quantity parser.
    text = _instruction_for(0.000000019)
    m = re.search(r"for ([0-9.eE+-]+) shares of", text)
    assert m and "e" not in m.group(1).lower(), text


class _FakeDB:
    """Minimal stand-in recording what execute_order persists."""

    def __init__(self):
        self.orders: list[dict] = []
        self.trades: list[dict] = []
        self.finished: list[dict] = []
        self.fills: list[dict] = []

    def audit(self, *a, **k):
        pass

    def log_order(self, run_id, mode, **k):
        self.orders.append(k)
        return len(self.orders)

    def begin_trade(self, **k):
        self.trades.append(k)
        return len(self.trades), True, None

    def finish_trade(self, *a, **k):
        self.finished.append(k)

    def update_order_status(self, *a, **k):
        pass

    def log_fill(self, *a, **k):
        self.fills.append(k)


class _RecordingBroker:
    def __init__(self):
        self.qty = None
        self.dollar_amount = None
        self.order_type = None

    async def place_order(self, *, ticker, side, qty, limit_price, order_type,
                          ref_id=None, dollar_amount=None):
        self.qty = qty
        self.dollar_amount = dollar_amount
        self.order_type = order_type
        from execution.executor import OrderResult
        return OrderResult(ok=True, status="submitted", filled_qty=qty,
                           fill_price=limit_price, broker_order_id="test")


def test_execute_order_quantizes_before_broker_and_db():
    """The DB rows from the failed 2026-07-27 run stored a 17-decimal quantity,
    which is how we know un-quantized values were reaching the broker. Lock in
    that execute_order quantizes FIRST, so the broker call, the orders row and
    the trades row all agree on the tradeable quantity."""
    from execution.executor import Executor

    db, broker = _FakeDB(), _RecordingBroker()
    ex = Executor(broker, "live", db, "run-test", {"order_type": "limit"},
                  kill_switch_check=lambda: False)

    asyncio.run(ex.execute_order(
        ticker="AAPL", side=Side.BUY, qty=0.07434944237918216,
        ref_price=337.93, notional=25.0,
    ))

    assert broker.qty == 0.074349, broker.qty
    assert _decimals(broker.qty) <= MAX_SHARE_DECIMALS
    assert db.orders[0]["qty"] == 0.074349
    assert db.trades[0]["qty"] == 0.074349


def test_fmt_qty_clean_string():
    assert _fmt_qty(0.07439590524937507) == "0.074395"    # no trailing zeros
    assert _fmt_qty(1.0) == "1"                            # whole shares
    assert _fmt_qty(2.50000000) == "2.5"
    assert _fmt_qty(0.123456789) == "0.123456"
    assert _fmt_qty(0.0) == "0"
    # Never scientific notation, always within the broker's precision.
    s = _fmt_qty(0.000000019)                              # 1.9e-8
    assert "e" not in s.lower()
    assert _decimals(float(s)) <= MAX_SHARE_DECIMALS


# ---------------------------------------------------------------------------
# Market-order spend protection and fill honesty
# ---------------------------------------------------------------------------
def _run_order(cfg, *, side=Side.BUY, qty=0.07434944237918216, ref_price=337.93,
               notional=25.0):
    from execution.executor import Executor
    db, broker = _FakeDB(), _RecordingBroker()
    ex = Executor(broker, "live", db, "run-test", cfg, kill_switch_check=lambda: False)
    asyncio.run(ex.execute_order(ticker="AAPL", side=side, qty=qty,
                                 ref_price=ref_price, notional=notional))
    return db, broker


def test_fractional_buy_is_sent_as_capped_dollar_amount():
    """A fractional qty can only go out as a market order, which has no price
    protection. The spend must instead be bounded by sending the approved
    notional, so a price move between sizing and fill cannot overspend."""
    _db, broker = _run_order({"order_type": "limit"})
    assert broker.order_type == "market"          # fractional forces market
    assert broker.dollar_amount == 25.0           # spend capped at the approved notional


def test_fractional_sell_still_sends_share_quantity():
    """A sell must move an exact share count (closing a position); its risk is
    smaller proceeds, not an overspend, so it keeps quantity semantics."""
    _db, broker = _run_order({"order_type": "limit"}, side=Side.SELL)
    assert broker.dollar_amount is None
    assert broker.qty == 0.074349


def test_whole_share_order_keeps_limit_protection():
    _db, broker = _run_order({"order_type": "limit"}, qty=3.0, notional=1013.79)
    assert broker.order_type == "limit"
    assert broker.dollar_amount is None


def test_unreported_fill_price_is_never_replaced_by_reference_price():
    """A 'submitted' response carrying no fill price must not have the stale
    prefetch price recorded as the fill — that invents a cost basis and produces
    P&L against a price nothing traded at."""
    broker = RobinhoodMCPBroker(mcp_url="http://test.invalid/mcp", model="test-model",
                                account_number="123456789")

    async def fake_ask(instruction, tools, max_turns=6):
        return {"status": "submitted", "order_id": "x"}, instruction

    broker._ask = fake_ask
    res = asyncio.run(broker.place_order(ticker="AAPL", side=Side.BUY, qty=2.0,
                                         limit_price=337.93, order_type="market"))
    assert res.fill_price == 0.0                      # NOT 337.93
    assert res.detail["fill_price_reported"] is False
    assert res.filled_qty == 0.0                      # 'submitted' is not a fill
    assert res.detail["filled_qty_reported"] is False


def test_unconfirmed_fill_does_not_write_a_fills_row():
    """No broker-reported fill -> no fabricated fills row and no invented
    price/qty overwriting the trades row."""
    from execution.executor import Executor, OrderResult

    class _NoFillBroker:
        async def place_order(self, **k):
            return OrderResult(ok=True, status="submitted", filled_qty=0.0,
                               fill_price=0.0, broker_order_id="x",
                               detail={"fill_price_reported": False})

    db = _FakeDB()
    ex = Executor(_NoFillBroker(), "live", db, "run-test", {"order_type": "limit"},
                  kill_switch_check=lambda: False)
    asyncio.run(ex.execute_order(ticker="AAPL", side=Side.BUY, qty=2.0,
                                 ref_price=337.93, notional=675.86))
    assert db.fills == []                              # nothing filled -> no fill row
    assert db.finished[0]["price"] is None             # do not overwrite with a guess
    assert db.finished[0]["qty"] is None


def test_reported_fill_is_recorded_faithfully():
    from execution.executor import Executor, OrderResult

    class _FilledBroker:
        async def place_order(self, **k):
            return OrderResult(ok=True, status="filled", filled_qty=2.0,
                               fill_price=340.10, broker_order_id="x")

    db = _FakeDB()
    ex = Executor(_FilledBroker(), "live", db, "run-test", {"order_type": "limit"},
                  kill_switch_check=lambda: False)
    asyncio.run(ex.execute_order(ticker="AAPL", side=Side.BUY, qty=2.0,
                                 ref_price=337.93, notional=675.86))
    assert db.finished[0]["price"] == 340.10           # the real fill, not the ref
    assert db.fills[0]["price"] == 340.10

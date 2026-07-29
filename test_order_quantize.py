"""Unit tests for order-quantity quantization to Robinhood's 8-decimal limit.

Run:  python -m pytest test_order_quantize.py -q
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from execution.executor import RobinhoodMCPBroker, Side, _quantize_shares, _fmt_qty


def _decimals(x: float) -> int:
    return max(0, -Decimal(str(x)).as_tuple().exponent)


def test_quantize_floors_the_live_failure_case():
    # The exact quantity Robinhood rejected.
    q = _quantize_shares(0.07439590524937507)
    assert q == 0.0743959                       # floored to 8 dp
    assert _decimals(q) <= 8
    assert q <= 0.07439590524937507             # never rounds up


def test_quantize_various():
    assert _quantize_shares(1.0) == 1.0
    assert _quantize_shares(2.5) == 2.5
    assert _quantize_shares(0.123456789) == 0.12345678   # 9th dp dropped, not rounded
    assert _quantize_shares(3e-9) == 0.0                 # below 1e-8 floors to zero
    # Any input comes out with <= 8 decimal places.
    for v in (0.07439590524937507, 0.1, 0.999999999, 12.3456789012, 0.000000019):
        assert _decimals(_quantize_shares(v)) <= 8, v


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


def test_instruction_text_never_exceeds_8_decimals():
    """Regression for the live HTTP 400: Robinhood rejected an order because the
    QUANTITY IN THE INSTRUCTION had 17 decimals. Quantizing the float is not
    enough on its own — what reaches the broker is this rendered string, so the
    guarantee has to be asserted here, not only on _quantize_shares' return."""
    # The exact quantity Robinhood rejected on 2026-07-27.
    text = _instruction_for(0.07434944237918216)
    m = re.search(r"for ([0-9.]+) shares of", text)
    assert m, f"quantity not found in instruction: {text!r}"
    rendered = m.group(1)
    assert _decimals(float(rendered)) <= 8, rendered
    assert rendered == "0.07434944"
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

    def audit(self, *a, **k):
        pass

    def log_order(self, run_id, mode, **k):
        self.orders.append(k)
        return len(self.orders)

    def begin_trade(self, **k):
        self.trades.append(k)
        return len(self.trades), True, None

    def finish_trade(self, *a, **k):
        pass

    def update_order_status(self, *a, **k):
        pass

    def log_fill(self, *a, **k):
        pass


class _RecordingBroker:
    def __init__(self):
        self.qty = None

    async def place_order(self, *, ticker, side, qty, limit_price, order_type, ref_id=None):
        self.qty = qty
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

    assert broker.qty == 0.07434944, broker.qty
    assert _decimals(broker.qty) <= 8
    assert db.orders[0]["qty"] == 0.07434944
    assert db.trades[0]["qty"] == 0.07434944


def test_fmt_qty_clean_string():
    assert _fmt_qty(0.07439590524937507) == "0.0743959"   # no trailing zeros
    assert _fmt_qty(1.0) == "1"                            # whole shares
    assert _fmt_qty(2.50000000) == "2.5"
    assert _fmt_qty(0.123456789) == "0.12345678"
    assert _fmt_qty(0.0) == "0"
    # Never scientific notation, always <= 8 decimals.
    s = _fmt_qty(0.000000019)                              # 1.9e-8
    assert "e" not in s.lower()
    assert _decimals(float(s)) <= 8

"""Unit tests for order-quantity quantization to Robinhood's 8-decimal limit.

Run:  python -m pytest test_order_quantize.py -q
"""
from __future__ import annotations

from decimal import Decimal

from execution.executor import _quantize_shares, _fmt_qty


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

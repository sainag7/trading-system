"""Deterministic technical indicators.

Pure functions over price/volume series — no I/O, no LLM, no global state. Given
the same inputs they always return the same output, exactly like
:mod:`risk.guardrails`. This is deliberate: the Research Agent computes every
technical number here rather than asking a model for it, so a figure is either
derived from real data or ``None`` — never invented.

Conventions:
  * ``closes`` / ``highs`` / ``lows`` are chronological lists, **oldest first,
    newest last**, matching :meth:`data.providers.DataProvider.get_daily_series`.
  * Each function returns ``None`` (or a dict of ``None`` for MACD) when there is
    not enough data, so the caller can flag the field as missing.
  * RSI and ATR use Wilder's smoothing (the standard); MACD uses EMAs.
"""
from __future__ import annotations


def sma(values: list[float], n: int) -> float | None:
    """Simple moving average of the last ``n`` values."""
    if not values or len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def ema_series(values: list[float], n: int) -> list[float]:
    """Exponential moving average series, seeded with the SMA of the first ``n``.

    Returns a list of length ``len(values) - n + 1`` (empty if too short).
    """
    if not values or len(values) < n or n <= 0:
        return []
    k = 2.0 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: list[float], n: int) -> float | None:
    """Last EMA value, or ``None`` if there isn't enough data."""
    series = ema_series(values, n)
    return series[-1] if series else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Relative Strength Index (Wilder), 0..100."""
    if not closes or len(closes) < n + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, float | None]:
    """MACD line, signal line and histogram.

    macd = EMA(fast) - EMA(slow); signal = EMA(signal) of the macd line.
    Returns ``{"macd": .., "signal": .., "histogram": ..}`` with ``None`` for any
    component that can't be computed yet.
    """
    none = {"macd": None, "signal": None, "histogram": None}
    if not closes or len(closes) < slow + signal:
        return none
    fast_s = ema_series(closes, fast)
    slow_s = ema_series(closes, slow)
    if not fast_s or not slow_s:
        return none
    # Align the two EMA series on their (newest) tails before subtracting.
    length = min(len(fast_s), len(slow_s))
    macd_line = [f - s for f, s in zip(fast_s[-length:], slow_s[-length:])]
    sig_s = ema_series(macd_line, signal)
    macd_val = macd_line[-1]
    if not sig_s:
        return {"macd": round(macd_val, 4), "signal": None, "histogram": None}
    sig_val = sig_s[-1]
    return {
        "macd": round(macd_val, 4),
        "signal": round(sig_val, 4),
        "histogram": round(macd_val - sig_val, 4),
    }


def atr(
    highs: list[float], lows: list[float], closes: list[float], n: int = 14
) -> float | None:
    """Average True Range (Wilder smoothing) over ``n`` periods."""
    if not closes or len(closes) < n + 1:
        return None
    if len(highs) != len(closes) or len(lows) != len(closes):
        return None
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr_val = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        atr_val = (atr_val * (n - 1) + trs[i]) / n
    return atr_val

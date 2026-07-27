"""Shared display formatting for the web app.

The database stores every ``ts`` as a full-precision UTC ISO-8601 string
(``2026-07-24T11:00:00.123456+00:00`` — see ``storage/db._now``). Rendering that
verbatim is the dashboard's single biggest readability problem. Every API
response routes its timestamps through :func:`fmt_ts` here so the frontend only
ever displays clean, market-local strings like ``Jul 24, 2026 · 11:00 AM ET``.

Money/percent helpers live here too so numbers are formatted once, server-side,
and the frontend never renders a raw float.
"""
from __future__ import annotations

from datetime import datetime

from market import ET

DASH = "—"


def parse_ts(value) -> datetime | None:
    """Parse a stored ISO timestamp into an aware datetime (UTC assumed when the
    string carries no offset). Returns ``None`` for empty/garbage input so callers
    can fall back to a dash rather than crashing on bad data."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    # Accept a trailing 'Z' (Python < 3.11 fromisoformat rejects it).
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Last resort: date-only or space-separated forms.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_ts(value, style: str = "full") -> str:
    """Format a stored UTC ISO timestamp in Eastern (exchange) time.

    Styles:
      * ``full``     → ``Jul 24, 2026 · 11:00 AM ET``  (headers, detail views)
      * ``datetime`` → ``Jul 24, 11:00 AM``            (dense tables — no year/tz)
      * ``date``     → ``Jul 24, 2026``
      * ``time``     → ``11:00 AM ET``
      * ``iso``      → the original string (rarely needed; machine use)

    Returns ``—`` when the value can't be parsed, so a bad row never shows a raw
    ISO blob or throws.
    """
    if style == "iso":
        return str(value) if value else DASH
    dt = parse_ts(value)
    if dt is None:
        return DASH
    et = dt.astimezone(ET)
    # %-I / %-l give a non-zero-padded hour on macOS/Linux; guard for portability.
    hour12 = et.strftime("%I").lstrip("0") or "12"
    minute = et.strftime("%M")
    ampm = et.strftime("%p")
    mon_day = f"{et.strftime('%b')} {et.day}"          # "Jul 24"
    clock = f"{hour12}:{minute} {ampm}"                 # "11:00 AM"
    if style == "date":
        return f"{mon_day}, {et.year}"
    if style == "time":
        return f"{clock} ET"
    if style == "datetime":
        return f"{mon_day}, {clock}"
    # full
    return f"{mon_day}, {et.year} · {clock} ET"


def fmt_day(value) -> str:
    """Format a CALENDAR date (``%Y-%m-%d`` fields like entry_date / max_hold_until
    / next_earnings_date) as ``Jul 24, 2026`` — no timezone conversion, because a
    plain date is not an instant and must not shift a day at midnight UTC."""
    if not value:
        return DASH
    s = str(value).strip()[:10]
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        # Fall back to instant parsing for anything that carries a time.
        return fmt_ts(value, "date")
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def fmt_money(value, decimals: int = 2) -> str:
    """``1234.5`` → ``$1,234.50`` (or ``$1,235`` with ``decimals=0``)."""
    if value is None or _is_nan(value):
        return DASH
    try:
        return f"${float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return DASH


def fmt_pct(value, decimals: int = 1, signed: bool = False) -> str:
    """A percentage that is ALREADY in percent units (``12.3`` → ``12.3%``)."""
    if value is None or _is_nan(value):
        return DASH
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DASH
    sign = "+" if signed and v > 0 else ""
    return f"{sign}{v:.{decimals}f}%"


def fmt_num(value, decimals: int = 2) -> str:
    if value is None or _is_nan(value):
        return DASH
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return DASH


def fmt_shares(value) -> str:
    """Share counts: trim trailing zeros (``3.0`` → ``3``, ``1.2500`` → ``1.25``)."""
    if value is None or _is_nan(value):
        return DASH
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return DASH


def _is_nan(v) -> bool:
    return isinstance(v, float) and v != v

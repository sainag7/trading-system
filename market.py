"""US equity market calendar — a tiny, dependency-free trading-day / market-hours
gate.

This is a **daily-cadence** system that is scheduled once per trading day
(launchd/cron only know weekdays, not exchange holidays). The orchestrator calls
:func:`is_trading_day` before any live/preview cycle so a firing on a weekend or
NYSE holiday cleanly no-ops instead of attempting to trade a closed market.

No third-party calendar dependency: NYSE full-day closures are a short, slow-
moving list. Keep ``NYSE_HOLIDAYS`` extended a year or two ahead. If a date is
missing the only downside is a scheduled run on a closed day — the broker simply
rejects/queues orders and the run still places nothing unsafe.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# NYSE full-day closures (regular-session holidays). Observed dates already
# account for weekend shifts. Extend annually.
NYSE_HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed; Jul 4 is a Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving Day
    date(2026, 12, 25),  # Christmas Day
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # Martin Luther King Jr. Day
    date(2027, 2, 15),   # Washington's Birthday
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed; Jun 19 is a Saturday)
    date(2027, 7, 5),    # Independence Day (observed; Jul 4 is a Sunday)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving Day
    date(2027, 12, 24),  # Christmas Day (observed; Dec 25 is a Saturday)
})

# Regular trading session in ET (ignores early-close half-days — those still
# trade, so a once-daily mid-morning run is unaffected).
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def now_et() -> datetime:
    """Current time in US/Eastern (exchange local time)."""
    return datetime.now(ET)


def is_trading_day(d: date | datetime | None = None) -> bool:
    """True if ``d`` (ET date; defaults to today) is a NYSE trading day —
    a weekday that is not a full-day exchange holiday."""
    if d is None:
        d = now_et()
    if isinstance(d, datetime):
        d = d.astimezone(ET).date() if d.tzinfo else d.date()
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def is_market_open(dt: datetime | None = None) -> bool:
    """True if the regular NYSE session is open at ``dt`` (defaults to now, ET)."""
    dt = (dt or now_et()).astimezone(ET)
    if not is_trading_day(dt):
        return False
    return MARKET_OPEN <= dt.time() < MARKET_CLOSE

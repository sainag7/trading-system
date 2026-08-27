"""Dynamic stock discovery (screener).

Runs BEFORE the research pipeline and produces **the entire universe** for a
run. There is no fixed watchlist in config.yaml — every ticker the agents look
at comes from here, using FREE sources only:

  * yfinance predefined screeners (most_actives / day_gainers)
  * Yahoo Finance's public trending endpoint
  * Reddit r/wallstreetbets + r/stocks "hot" JSON (public, no auth)

Candidates are de-duplicated, excluded against the no-trade list, then passed
through a **yfinance-only** pre-filter (price band, average volume, and an
annualised-volatility ceiling) so it costs **zero Alpha Vantage quota**. The top
N by recent momentum are returned.

The single public entry point is :func:`discover_candidates`. It is fully
defensive: every external call is wrapped, and any failure (or no network)
results in an empty list. Because there is no fallback watchlist, an empty
result means the orchestrator HALTS the cycle and trades nothing — fail-closed
by design. It logs what it found and why each candidate was kept/dropped.
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

import market

try:
    import requests
except Exception:  # pragma: no cover - optional
    requests = None  # type: ignore

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional
    yf = None  # type: ignore

# A browser-like UA — Reddit and Yahoo reject the default urllib/requests UA.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 swing-trading-screener/1.0"
)

# Candidate ticker pattern (2-5 uppercase letters; cashtags handled separately).
_TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")

# Common ALL-CAPS words that look like tickers but aren't, so Reddit mentions
# don't flood the candidate list. (Real tickers shadowed by a few of these —
# e.g. AI/EV/PT — are an acceptable miss; the pre-filter drops bogus ones too.)
_STOPWORDS = {
    "DD", "YOLO", "DYOR", "NFA", "HODL", "FOMO", "TLDR", "TL", "DR", "EDIT",
    "IMO", "IMHO", "LOL", "LMAO", "WTF", "OMG", "AH", "PM", "AM", "EOD", "EOY",
    "YTD", "ATH", "ATL", "ER", "EPS", "PE", "PT", "ITM", "OTM", "FD", "FDS",
    "CALL", "CALLS", "PUT", "PUTS", "BUY", "SELL", "HOLD", "LONG", "SHORT",
    "BULL", "BEAR", "RED", "MOON", "WSB", "RH", "TA", "OP", "MF", "GUH",
    "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "SEC", "FED", "FOMC", "CPI",
    "GDP", "USA", "US", "UK", "EU", "AI", "EV", "PR", "USD", "Q",
    "THE", "AND", "FOR", "ARE", "NOT", "YOU", "ALL", "NEW", "NOW", "BIG",
    "CAN", "GET", "OUT", "WAY", "ITS", "WILL", "JUST", "LIKE", "THIS", "THAT",
    "GO", "ON", "IN", "AT", "BE", "DO", "IF", "OR", "SO", "UP", "TO", "IT",
}

# Cap on candidates we hit yfinance for, to bound runtime. Must be comfortably
# larger than discovery.max_discovered — the price-band and volume pre-filter
# typically drops a fifth of what the sources return.
_MAX_EXAMINED = 60


# ---------------------------------------------------------------------------
# Sources (each best-effort; never raises)
# ---------------------------------------------------------------------------
def _yf_screeners() -> list[str]:
    """yfinance predefined screeners. The API differs across versions, so try a
    few shapes and degrade quietly."""
    if yf is None:
        return []
    out: list[str] = []
    for key in ("most_actives", "day_gainers"):
        try:
            res = None
            if hasattr(yf, "screen"):
                res = yf.screen(key)
            elif hasattr(yf, "Screener"):
                s = yf.Screener()
                try:
                    s.set_predefined_body(key)
                except Exception:
                    pass
                res = getattr(s, "response", None)
            quotes = []
            if isinstance(res, dict):
                quotes = res.get("quotes") or (
                    res.get("finance", {}).get("result", [{}])[0].get("quotes", [])
                    if res.get("finance") else []
                )
            for q in quotes:
                sym = q.get("symbol") if isinstance(q, dict) else None
                if sym:
                    out.append(sym)
        except Exception:
            continue
    return out


def _yahoo_trending(limit: int = 25) -> list[str]:
    """Yahoo Finance public trending endpoint."""
    if requests is None:
        return []
    for host in ("query1", "query2"):
        try:
            url = f"https://{host}.finance.yahoo.com/v1/finance/trending/US?count={limit}"
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
            result = (r.json().get("finance", {}) or {}).get("result", [])
            if result:
                syms = [q.get("symbol") for q in result[0].get("quotes", []) if q.get("symbol")]
                if syms:
                    return syms
        except Exception:
            continue
    return []


def _reddit_mentions(limit_posts: int = 50) -> list[str]:
    """Uppercase ticker-looking mentions from r/wallstreetbets + r/stocks 'hot'."""
    if requests is None:
        return []
    counts: dict[str, int] = {}
    for sub in ("wallstreetbets", "stocks"):
        try:
            url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit_posts}"
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
            posts = (r.json().get("data", {}) or {}).get("children", [])
            for p in posts:
                d = p.get("data", {}) or {}
                text = f"{d.get('title', '')} {d.get('selftext', '')}"
                for m in _TICKER_RE.findall(text):
                    if m in _STOPWORDS:
                        continue
                    counts[m] = counts.get(m, 0) + 1
        except Exception:
            continue
    return [t for t, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)]


# ---------------------------------------------------------------------------
# Pre-filter (yfinance ONLY — no Alpha Vantage quota cost)
# ---------------------------------------------------------------------------
_MOMENTUM_BARS = 21   # ~1 trading month — the window the momentum ranking uses

# A live symbol's most recent bar is today's partial session or the previous
# trading day. Anything older means the symbol has stopped printing trades.
# Counted in NYSE trading days, so weekends and exchange holidays cost nothing.
_MAX_STALE_TRADING_DAYS = 2


def _trading_days_since(d: date) -> int:
    """NYSE trading days from ``d`` (exclusive) to today ET (inclusive)."""
    today = market.now_et().date()
    n, cur = 0, d + timedelta(days=1)
    while cur <= today:
        if market.is_trading_day(cur):
            n += 1
        cur += timedelta(days=1)
    return n


def _passes_filter(ticker: str, min_vol: float, min_p: float, max_p: float,
                   max_vol_ann: float):
    """Return ``(ok, reason, momentum_pct)``. Uses only yfinance.

    Fetches 3 months so annualised volatility has enough samples to be stable
    (~21 bars is far too noisy for a std-dev estimate), but still measures
    momentum over the last ~21 bars so the ranking keeps its 1-month meaning.
    """
    if yf is None:
        return False, "yfinance unavailable", None
    try:
        hist = yf.Ticker(ticker).history(period="3mo")
    except Exception:
        return False, "fetch error", None
    if hist is None or getattr(hist, "empty", True):
        return False, "no price data", None
    try:
        # Build (date, close, volume) together. Filtering Close and Volume in
        # separate passes desynchronises them whenever a row has a NaN price but
        # a real volume (or vice versa), silently pairing each close with some
        # other day's volume.
        bars = [
            (idx.date(), float(c), float(v))
            for idx, c, v in zip(hist.index, hist["Close"].tolist(),
                                 hist["Volume"].tolist())
            if c == c and v == v
        ]
    except Exception:
        return False, "bad data", None
    if not bars:
        return False, "no price data", None

    # Liveness. yfinance pads a delisted/halted ticker forward with zero-volume
    # bars repeating its final close. Those bars have zero variance, so a dead
    # ticker measures as ultra-low-volatility and ranks BETTER the longer it has
    # been dead — while the broker cannot trade it at all. Trim the padding and
    # judge recency on the last genuinely traded bar.
    padded = 0
    while bars and bars[-1][2] == 0:
        bars.pop()
        padded += 1
    if not bars:
        return False, "no traded bars", None
    # One trailing zero-volume bar can be a pre-open or partial session; a run of
    # them is padding over a symbol that has stopped trading.
    if padded >= 2:
        return False, f"{padded} zero-volume bars since {bars[-1][0]} — not trading", None
    stale = _trading_days_since(bars[-1][0])
    if stale > _MAX_STALE_TRADING_DAYS:
        return False, f"last traded {bars[-1][0]} ({stale} trading days stale)", None

    closes = [c for _, c, _ in bars]
    vols = [v for _, _, v in bars]
    price = closes[-1]
    # Volume over the recent window, matching the pre-3mo behaviour.
    recent_vols = vols[-_MOMENTUM_BARS:]
    avg_vol = sum(recent_vols) / len(recent_vols)
    # Momentum over the last ~month, unchanged in meaning by the longer fetch.
    mom_closes = closes[-_MOMENTUM_BARS:]
    momentum = ((mom_closes[-1] / mom_closes[0] - 1) * 100
                if len(mom_closes) > 1 and mom_closes[0] else 0.0)

    if not (min_p <= price <= max_p):
        return False, f"price ${price:,.2f} outside ${min_p:g}-${max_p:g}", None
    if avg_vol < min_vol:
        return False, f"avg vol {avg_vol/1e3:,.0f}k < {min_vol/1e3:,.0f}k", None

    # Annualised volatility from the full 3-month window.
    rets = [closes[i] / closes[i - 1] - 1
            for i in range(1, len(closes)) if closes[i - 1]]
    ann_vol = None
    if len(rets) > 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        ann_vol = math.sqrt(var) * math.sqrt(252)
        if max_vol_ann > 0 and ann_vol > max_vol_ann:
            return False, (f"annualised vol {ann_vol*100:,.0f}% > "
                           f"{max_vol_ann*100:,.0f}%"), None

    vol_txt = f", vol {ann_vol*100:,.0f}%" if ann_vol is not None else ""
    return True, (f"price ${price:,.2f}, avg vol {avg_vol/1e6:,.1f}M, "
                  f"1mo {momentum:+.1f}%{vol_txt}"), momentum


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def discover_candidates(cfg, provider, n: int = 5) -> list[str]:
    """Return up to ``n`` trending tickers — the whole universe for this run.

    Args:
        cfg: the global :class:`config.Config` (uses ``.discovery`` and
            ``.risk.no_trade_list``).
        provider: the :class:`data.providers.DataProvider`; its ``.cache`` is
            reused for a daily cache when present.
        n: max tickers to return.

    Never raises — returns ``[]`` on any failure. An empty result halts the
    cycle upstream (there is no watchlist to fall back to).
    """
    try:
        disc = cfg.discovery or {}
    except Exception:
        disc = {}
    # No fixed universe exists, so turning this off does not fall back to a
    # watchlist — it stops the system from researching (and trading) anything.
    if not disc.get("dynamic_discovery", False):
        return []
    n = int(n or disc.get("max_discovered", 20))

    cache = getattr(provider, "cache", None)
    cache_key = f"discovery_candidates_n{n}"
    if cache is not None:
        try:
            cached = cache.get(cache_key)
        except Exception:
            cached = None
        if cached is not None:
            print(f"\n🔎 Discovery: reusing today's cached candidates {cached}")
            return list(cached)

    try:
        no_trade = set(cfg.risk.no_trade_list)
    except Exception:
        no_trade = set()

    print("\n🔎 Discovery: scanning free sources for trending tickers...")
    raw: list[str] = []
    if disc.get("use_yfinance_screeners", True):
        s = _yf_screeners()
        print(f"   yfinance screeners: {len(s)} symbols")
        raw += s
    if disc.get("use_yahoo_trending", True):
        s = _yahoo_trending()
        print(f"   yahoo trending:     {len(s)} symbols")
        raw += s
    if disc.get("use_reddit", True):
        s = _reddit_mentions()
        print(f"   reddit mentions:    {len(s)} symbols")
        raw += s

    # Normalize, dedupe (preserve order), exclude the no-trade list.
    seen: set[str] = set()
    candidates: list[str] = []
    for sym in raw:
        t = str(sym).upper().strip().lstrip("$")
        if not t.isalpha() or not (1 <= len(t) <= 5):
            continue
        if t in no_trade or t in seen:
            continue
        seen.add(t)
        candidates.append(t)

    if not candidates:
        print("   no candidates found from any source — this run will be halted.")
        return []

    min_vol = float(disc.get("min_avg_volume", 500000))
    min_p = float(disc.get("min_price", 2))
    max_p = float(disc.get("max_price", 500))
    max_vol_ann = float(disc.get("max_annualized_vol", 0.60))

    kept: list[tuple[str, float]] = []
    for t in candidates[:_MAX_EXAMINED]:
        ok, reason, momentum = _passes_filter(t, min_vol, min_p, max_p, max_vol_ann)
        print(f"   {'kept ' if ok else 'drop '} {t}: {reason}")
        if ok:
            kept.append((t, momentum if momentum is not None else 0.0))

    if not kept:
        print("   no candidates passed the pre-filter — this run will be halted.")
        return []

    kept.sort(key=lambda kv: kv[1], reverse=True)  # strongest momentum first
    result = [t for t, _ in kept[:n]]
    print(f"   ➕ universe for this run — {len(result)} ticker(s): {result}")
    if cache is not None:
        try:
            cache.set(cache_key, result)
        except Exception:
            pass
    return result

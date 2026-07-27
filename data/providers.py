"""Market-data providers with aggressive caching and rate-limit protection.

The free Alpha Vantage tier is **25 requests/day**. A single careless run can
exhaust it, so this module is built around three defences:

  1. **On-disk cache** keyed by request, with a TTL (default 24h — we trade on a
     daily cadence, so intraday refetches are pointless). A cached value never
     spends quota.
  2. **A persistent daily request budget** stored in the cache dir. Once the
     budget is spent, Alpha Vantage calls are refused for the rest of the day
     and we transparently fall back to yfinance.
  3. **Self-throttling** — a minimum delay between live Alpha Vantage calls so a
     burst can't trip the per-minute limit.

Public surface (all return plain dicts/values, never raise on network failure —
they return ``None``/empty and the caller decides what to do):

  * ``DataProvider.get_quote(ticker)``        -> latest price + day stats
  * ``DataProvider.get_daily_series(ticker)`` -> recent daily OHLCV
  * ``DataProvider.get_fundamentals(ticker)`` -> sector, market cap, PE, etc.
  * ``DataProvider.get_macro()``              -> FRED macro snapshot (optional)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional fallback
    yf = None  # type: ignore


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------
class DiskCache:
    """A trivial JSON file cache with per-entry TTL."""

    def __init__(self, cache_dir: str | Path, ttl_minutes: int = 1440):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_minutes * 60

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return self.dir / f"{safe}.json"

    def get(self, key: str) -> Any | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            payload = json.loads(p.read_text())
        except Exception:
            return None
        if time.time() - payload.get("_cached_at", 0) > self.ttl_seconds:
            return None
        return payload.get("value")

    def set(self, key: str, value: Any) -> None:
        p = self._path(key)
        p.write_text(json.dumps({"_cached_at": time.time(), "value": value}))


# ---------------------------------------------------------------------------
# Alpha Vantage daily budget tracker (persisted across runs)
# ---------------------------------------------------------------------------
class DailyBudget:
    """Tracks Alpha Vantage requests used today; persisted to the cache dir."""

    def __init__(self, cache_dir: str | Path, daily_budget: int = 25):
        self.path = Path(cache_dir) / "_av_budget.json"
        self.daily_budget = daily_budget

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                pass
        return {"date": "", "used": 0}

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def used(self) -> int:
        state = self._load()
        return state["used"] if state.get("date") == self._today() else 0

    def remaining(self) -> int:
        return max(0, self.daily_budget - self.used())

    def can_spend(self) -> bool:
        return self.remaining() > 0

    def spend(self, n: int = 1) -> None:
        state = self._load()
        if state.get("date") != self._today():
            state = {"date": self._today(), "used": 0}
        state["used"] += n
        self.path.write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
@dataclass
class ProviderConfig:
    cache_dir: str = ".cache"
    cache_ttl_minutes: int = 1440
    av_api_key: str | None = None
    av_daily_budget: int = 25
    av_min_seconds_between_calls: int = 13
    use_yfinance_fallback: bool = True
    use_fred: bool = True
    fred_api_key: str | None = None
    sector_overrides: dict[str, str] | None = None


class DataProvider:
    """Unified market-data access with caching + graceful fallback."""

    AV_URL = "https://www.alphavantage.co/query"
    FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(self, cfg: ProviderConfig, audit=None):
        self.cfg = cfg
        self.cache = DiskCache(cfg.cache_dir, cfg.cache_ttl_minutes)
        self.budget = DailyBudget(cfg.cache_dir, cfg.av_daily_budget)
        self.sector_overrides = {k.upper(): v for k, v in (cfg.sector_overrides or {}).items()}
        self._last_av_call = 0.0
        # Optional callback (run_id, level, event, detail) for the audit log.
        self._audit = audit

    # -- internal helpers --------------------------------------------------
    def _log(self, level: str, event: str, detail: Any = None) -> None:
        if self._audit:
            try:
                self._audit(level, event, detail)
            except Exception:
                pass

    def _throttle_av(self) -> None:
        elapsed = time.time() - self._last_av_call
        wait = self.cfg.av_min_seconds_between_calls - elapsed
        if wait > 0:
            time.sleep(wait)

    def _alpha_vantage(self, params: dict) -> dict | None:
        """Make a budgeted, throttled, cached Alpha Vantage call."""
        if requests is None or not self.cfg.av_api_key:
            return None
        cache_key = "av_" + "_".join(f"{k}-{v}" for k, v in sorted(params.items())
                                     if k != "apikey")
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        if not self.budget.can_spend():
            self._log("WARN", "alphavantage_budget_exhausted",
                      {"used": self.budget.used(), "budget": self.cfg.av_daily_budget})
            return None

        self._throttle_av()
        params = {**params, "apikey": self.cfg.av_api_key}
        try:
            resp = requests.get(self.AV_URL, params=params, timeout=20)
            self._last_av_call = time.time()
            self.budget.spend(1)
            data = resp.json()
        except Exception as e:  # network / parse error — never raise to caller
            self._log("ERROR", "alphavantage_request_failed", {"error": str(e)})
            return None

        # Alpha Vantage signals throttling/limits inside a 200 response body.
        if any(k in data for k in ("Note", "Information", "Error Message")):
            self._log("WARN", "alphavantage_limit_or_error", data)
            return None

        self.cache.set(cache_key, data)
        return data

    # -- yfinance fallback -------------------------------------------------
    def _yf_quote(self, ticker: str) -> dict | None:
        if yf is None or not self.cfg.use_yfinance_fallback:
            return None
        cache_key = f"yfq_{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            if hist is None or hist.empty:
                return None
            last = hist.iloc[-1]
            prev = hist.iloc[-2] if len(hist) > 1 else last
            quote = {
                "ticker": ticker,
                "price": float(last["Close"]),
                "prev_close": float(prev["Close"]),
                "day_high": float(last["High"]),
                "day_low": float(last["Low"]),
                "volume": float(last["Volume"]),
                "source": "yfinance",
            }
            self.cache.set(cache_key, quote)
            return quote
        except Exception as e:  # pragma: no cover - network dependent
            self._log("ERROR", "yfinance_quote_failed", {"ticker": ticker, "error": str(e)})
            return None

    # -- public API --------------------------------------------------------
    def get_quote(self, ticker: str) -> dict | None:
        """Latest price + simple day stats. AV first, yfinance fallback."""
        ticker = ticker.upper()
        data = self._alpha_vantage({"function": "GLOBAL_QUOTE", "symbol": ticker})
        if data and "Global Quote" in data and data["Global Quote"]:
            q = data["Global Quote"]
            try:
                return {
                    "ticker": ticker,
                    "price": float(q.get("05. price", 0) or 0),
                    "prev_close": float(q.get("08. previous close", 0) or 0),
                    "day_high": float(q.get("03. high", 0) or 0),
                    "day_low": float(q.get("04. low", 0) or 0),
                    "volume": float(q.get("06. volume", 0) or 0),
                    "change_pct": float((q.get("10. change percent", "0") or "0").rstrip("%")),
                    "source": "alphavantage",
                }
            except (TypeError, ValueError):
                pass
        return self._yf_quote(ticker)

    def get_daily_series(self, ticker: str, lookback: int = 100) -> list[dict] | None:
        """Recent daily OHLCV, newest last. Used for trend/momentum/indicators.

        ``lookback`` > 100 pulls the *full* history (still a single AV request) so
        long indicators such as the 200-day SMA can be computed; the result is
        sliced back to ``lookback`` rows. A value <= 100 uses the lighter
        ``compact`` payload.
        """
        ticker = ticker.upper()
        outsize = "full" if lookback > 100 else "compact"
        data = self._alpha_vantage(
            {"function": "TIME_SERIES_DAILY", "symbol": ticker, "outputsize": outsize}
        )
        series = (data or {}).get("Time Series (Daily)")
        if series:
            rows = []
            for date in sorted(series.keys())[-lookback:]:
                v = series[date]
                rows.append({
                    "date": date,
                    "open": float(v["1. open"]),
                    "high": float(v["2. high"]),
                    "low": float(v["3. low"]),
                    "close": float(v["4. close"]),
                    "volume": float(v["5. volume"]),
                })
            return rows
        # yfinance fallback
        if yf is not None and self.cfg.use_yfinance_fallback:
            cache_key = f"yfd_{ticker}_{lookback}"
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
            try:
                # Enough history for a 200-day SMA when a long lookback is asked.
                period = "2y" if lookback > 150 else f"{max(lookback, 60)}d"
                hist = yf.Ticker(ticker).history(period=period)
                rows = [
                    {"date": str(idx.date()), "open": float(r["Open"]),
                     "high": float(r["High"]), "low": float(r["Low"]),
                     "close": float(r["Close"]), "volume": float(r["Volume"])}
                    for idx, r in hist.iterrows()
                ][-lookback:]
                self.cache.set(cache_key, rows)
                return rows
            except Exception:  # pragma: no cover
                return None
        return None

    def get_news_sentiment(self, ticker: str, days: int = 14, limit: int = 50) -> dict:
        """News + per-article sentiment over the last ``days`` (Alpha Vantage).

        Uses AV's NEWS_SENTIMENT endpoint, which returns numeric sentiment scores
        per article. Falls back to yfinance headlines (no scores) when AV is
        unavailable or its quota is spent. Always returns a dict; never raises.
        """
        ticker = ticker.upper()
        # Anchor ``time_from`` to midnight so the cache key is stable within a day.
        frm = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%dT0000")
        data = self._alpha_vantage({
            "function": "NEWS_SENTIMENT", "tickers": ticker,
            "time_from": frm, "sort": "LATEST", "limit": str(limit),
        })
        feed = (data or {}).get("feed")
        if isinstance(feed, list) and feed:
            scores: list[float] = []
            articles: list[dict] = []
            for a in feed:
                ts = next(
                    (s for s in a.get("ticker_sentiment", [])
                     if str(s.get("ticker", "")).upper() == ticker),
                    None,
                )
                score = _to_float((ts or {}).get("ticker_sentiment_score"))
                if score is None:
                    score = _to_float(a.get("overall_sentiment_score"))
                label = (ts or {}).get("ticker_sentiment_label") or a.get("overall_sentiment_label")
                if score is not None:
                    scores.append(score)
                articles.append({
                    "title": a.get("title"),
                    "source": a.get("source"),
                    "time_published": a.get("time_published"),
                    "url": a.get("url"),
                    "sentiment_score": score,
                    "sentiment_label": label,
                    "relevance": _to_float((ts or {}).get("relevance_score")),
                })
            return {
                "source": "alphavantage",
                "window_days": days,
                "article_count": len(feed),
                "aggregate_score": round(sum(scores) / len(scores), 4) if scores else None,
                "articles": articles,
            }
        return self._yf_news(ticker, days)

    def _yf_news(self, ticker: str, days: int) -> dict:
        """Headline-only fallback (yfinance has no per-article sentiment score)."""
        empty = {"source": None, "window_days": days, "article_count": 0,
                 "aggregate_score": None, "articles": []}
        if yf is None or not self.cfg.use_yfinance_fallback:
            return empty
        cache_key = f"yfnews_{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            raw = yf.Ticker(ticker).news or []
        except Exception:  # pragma: no cover - network dependent
            return empty
        articles = []
        for n in raw:
            # yfinance has shifted this shape across versions; read both layouts.
            c = n.get("content", n)
            pub = c.get("providerPublishTime") or c.get("pubDate")
            when = None
            if isinstance(pub, (int, float)):
                when = datetime.fromtimestamp(pub, tz=timezone.utc).isoformat()
            elif pub:
                when = str(pub)
            articles.append({
                "title": c.get("title"),
                "source": (c.get("provider") or {}).get("displayName")
                if isinstance(c.get("provider"), dict) else c.get("publisher"),
                "time_published": when,
                "url": (c.get("canonicalUrl") or {}).get("url")
                if isinstance(c.get("canonicalUrl"), dict) else c.get("link"),
                "sentiment_score": None,
                "sentiment_label": None,
                "relevance": None,
            })
        result = {"source": "yfinance", "window_days": days,
                  "article_count": len(articles), "aggregate_score": None,
                  "articles": articles}
        self.cache.set(cache_key, result)
        return result

    # Fields all callers can rely on existing (value or None). Margins and growth
    # are expressed as **percent** (e.g. 25.0 == 25%); debt_to_equity is a ratio.
    _FUNDAMENTAL_FIELDS = (
        "revenue_ttm", "revenue_growth_yoy", "eps_ttm", "eps_growth_yoy",
        "gross_margin", "operating_margin", "profit_margin", "pe_ratio",
        "ps_ratio", "debt_to_equity", "free_cash_flow", "next_earnings_date",
        "market_cap", "beta", "analyst_target", "week52_high", "week52_low",
        "industry", "name", "description",
    )

    def get_fundamentals(self, ticker: str) -> dict:
        """Fundamentals for one ticker, merged from yfinance + Alpha Vantage.

        yfinance is queried **first** because it is free and covers the fields AV
        OVERVIEW lacks (debt/equity, free cash flow, next earnings date), which
        keeps the scarce 25/day AV budget for price history and news. AV OVERVIEW
        then fills any remaining gaps. The sector override always wins so the
        sector-concentration guardrail is never wrong due to an API gap.
        """
        ticker = ticker.upper()
        result: dict[str, Any] = {"ticker": ticker, "sector": self.sector_overrides.get(ticker)}
        for f in self._FUNDAMENTAL_FIELDS:
            result[f] = None
        result["source"] = None

        yf_data = self._yf_fundamentals(ticker)
        if yf_data:
            for k, v in yf_data.items():
                if k == "sector":
                    continue  # override-aware sector handled below
                if result.get(k) is None and v is not None:
                    result[k] = v
            if result["sector"] is None:
                result["sector"] = yf_data.get("sector")
            result["source"] = "yfinance"

        # AV OVERVIEW only when core valuation fields are still missing.
        if result.get("pe_ratio") is None or result.get("revenue_ttm") is None:
            av = self._av_fundamentals(ticker)
            if av:
                for k, v in av.items():
                    if k == "sector":
                        continue
                    if result.get(k) is None and v is not None:
                        result[k] = v
                if result["sector"] is None:
                    result["sector"] = av.get("sector")
                result["source"] = result["source"] or "alphavantage"

        result["sector"] = result.get("sector") or "Unknown"
        return result

    def _yf_fundamentals(self, ticker: str) -> dict | None:
        if yf is None or not self.cfg.use_yfinance_fallback:
            return None
        cache_key = f"yff_{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
        except Exception:  # pragma: no cover - network dependent
            return None
        out = {
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            # Company identity/summary — sourced from the provider so downstream
            # narrative never has to reach for model memory (anti-hallucination).
            "name": info.get("shortName") or info.get("longName"),
            "description": (str(info.get("longBusinessSummary"))[:600]
                            if info.get("longBusinessSummary") else None),
            "revenue_ttm": _to_float(info.get("totalRevenue")),
            "revenue_growth_yoy": _pct(info.get("revenueGrowth")),
            "eps_ttm": _to_float(info.get("trailingEps")),
            "eps_growth_yoy": _pct(info.get("earningsGrowth")),
            "gross_margin": _pct(info.get("grossMargins")),
            "operating_margin": _pct(info.get("operatingMargins")),
            "profit_margin": _pct(info.get("profitMargins")),
            "pe_ratio": _to_float(info.get("trailingPE")),
            "ps_ratio": _to_float(info.get("priceToSalesTrailing12Months")),
            # yfinance reports debt/equity as a percent (150 == 1.5x) -> ratio.
            "debt_to_equity": (lambda d: d / 100 if d is not None else None)(
                _to_float(info.get("debtToEquity"))
            ),
            "free_cash_flow": _to_float(info.get("freeCashflow")),
            "market_cap": _to_float(info.get("marketCap")),
            "beta": _to_float(info.get("beta")),
            "analyst_target": _to_float(info.get("targetMeanPrice")),
            "week52_high": _to_float(info.get("fiftyTwoWeekHigh")),
            "week52_low": _to_float(info.get("fiftyTwoWeekLow")),
            "next_earnings_date": self._yf_next_earnings(t),
        }
        self.cache.set(cache_key, out)
        return out

    @staticmethod
    def _yf_next_earnings(ticker_obj) -> str | None:
        """Best-effort next earnings date from yfinance (shape varies by version)."""
        try:
            cal = getattr(ticker_obj, "calendar", None)
            if isinstance(cal, dict):
                dates = cal.get("Earnings Date")
                if isinstance(dates, (list, tuple)) and dates:
                    return str(dates[0])
                if dates:
                    return str(dates)
        except Exception:  # pragma: no cover - network dependent
            pass
        return None

    def _av_fundamentals(self, ticker: str) -> dict | None:
        data = self._alpha_vantage({"function": "OVERVIEW", "symbol": ticker})
        if not (data and data.get("Symbol")):
            return None
        revenue = _to_float(data.get("RevenueTTM"))
        gross_profit = _to_float(data.get("GrossProfitTTM"))
        gross_margin = (gross_profit / revenue * 100) if (gross_profit and revenue) else None
        return {
            "sector": data.get("Sector"),
            "industry": data.get("Industry"),
            "name": data.get("Name"),
            "description": (str(data.get("Description"))[:600]
                            if data.get("Description") else None),
            "revenue_ttm": revenue,
            "revenue_growth_yoy": _pct(data.get("QuarterlyRevenueGrowthYOY")),
            "eps_ttm": _to_float(data.get("EPS")),
            "eps_growth_yoy": _pct(data.get("QuarterlyEarningsGrowthYOY")),
            "gross_margin": round(gross_margin, 2) if gross_margin is not None else None,
            "operating_margin": _pct(data.get("OperatingMarginTTM")),
            "profit_margin": _pct(data.get("ProfitMargin")),
            "pe_ratio": _to_float(data.get("PERatio")),
            "ps_ratio": _to_float(data.get("PriceToSalesRatioTTM")),
            "market_cap": _to_float(data.get("MarketCapitalization")),
            "beta": _to_float(data.get("Beta")),
            "analyst_target": _to_float(data.get("AnalystTargetPrice")),
            "week52_high": _to_float(data.get("52WeekHigh")),
            "week52_low": _to_float(data.get("52WeekLow")),
        }

    def get_macro(self) -> dict:
        """Lightweight macro snapshot from FRED (optional, cached daily)."""
        if not (self.cfg.use_fred and self.cfg.fred_api_key and requests):
            return {}
        snapshot: dict[str, Any] = {}
        series = {"fed_funds": "FEDFUNDS", "cpi": "CPIAUCSL", "unemployment": "UNRATE",
                  "ten_year": "DGS10"}
        for label, sid in series.items():
            cache_key = f"fred_{sid}"
            cached = self.cache.get(cache_key)
            if cached is not None:
                snapshot[label] = cached
                continue
            try:
                resp = requests.get(self.FRED_URL, params={
                    "series_id": sid, "api_key": self.cfg.fred_api_key,
                    "file_type": "json", "sort_order": "desc", "limit": 1,
                }, timeout=20)
                obs = resp.json().get("observations", [])
                val = _to_float(obs[0]["value"]) if obs else None
                snapshot[label] = val
                self.cache.set(cache_key, val)
            except Exception:  # pragma: no cover
                snapshot[label] = None
        return snapshot

    def sector_for(self, ticker: str) -> str:
        """Cheap sector lookup honouring overrides (used by the guardrails)."""
        ticker = ticker.upper()
        if ticker in self.sector_overrides:
            return self.sector_overrides[ticker]
        return self.get_fundamentals(ticker).get("sector", "Unknown")


def _to_float(v: Any) -> float | None:
    try:
        if v in (None, "None", "-", ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v: Any) -> float | None:
    """Convert a fraction (0.25) to a rounded percent (25.0)."""
    f = _to_float(v)
    return round(f * 100, 2) if f is not None else None


def build_provider(config, audit=None) -> DataProvider:
    """Construct a :class:`DataProvider` from the global :class:`config.Config`."""
    data_cfg = config.data
    av = data_cfg.get("alphavantage", {})
    pcfg = ProviderConfig(
        cache_dir=data_cfg.get("cache_dir", ".cache"),
        cache_ttl_minutes=data_cfg.get("cache_ttl_minutes", 1440),
        av_api_key=config.env("ALPHAVANTAGE_API_KEY"),
        av_daily_budget=av.get("daily_request_budget", 25),
        av_min_seconds_between_calls=av.get("min_seconds_between_calls", 13),
        use_yfinance_fallback=data_cfg.get("use_yfinance_fallback", True),
        use_fred=data_cfg.get("use_fred", True),
        fred_api_key=config.env("FRED_API_KEY"),
        sector_overrides=config.sectors,
    )
    inner = DataProvider(pcfg, audit=audit)
    # When enabled, front the Alpha-Vantage/yfinance provider with Robinhood's
    # real-time, quota-free market data for quotes/series/fundamentals. Alpha
    # Vantage is left only for news sentiment (Robinhood has no news tool); FRED
    # still supplies macro. Falls back to `inner` on any Robinhood miss.
    if data_cfg.get("use_robinhood_data", False):
        from data.robinhood_provider import RobinhoodDataProvider
        model = data_cfg.get("robinhood_data_model", "claude-haiku-4-5-20251001")
        return RobinhoodDataProvider(
            inner, model=model, audit=audit,
            historical_days=int(data_cfg.get("robinhood_historical_days", 400)))
    return inner

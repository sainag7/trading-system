"""Offline tests for the Alpha Vantage throttle — the budget-burning race.

NO network, NO API keys. Background: 107 of the 115 Alpha Vantage errors in the
fortnight to 2026-09-15 were AV's *per-second* rejection, not the daily quota.
The research agent fans out across threads; each read the same stale
``_last_av_call``, fired at once, got rejected — and each rejection still spent
a unit of the 25/day budget. By the time the burst was over the budget was
gone and every later ticker logged ``alphavantage_budget_exhausted`` and fell
back to yfinance headlines. Calls are now serialised under a lock.

Run:  python -m pytest test_providers.py -q
"""
from __future__ import annotations

import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from data import providers
from data.providers import DataProvider, ProviderConfig


class _FakeRequests:
    """Records call times; answers with a scripted JSON body."""

    def __init__(self, body):
        self.body = body
        self.times: list[float] = []
        self._lock = threading.Lock()

    def get(self, url, params=None, timeout=None):
        with self._lock:
            self.times.append(time.monotonic())
        body = self.body
        class _Resp:
            def json(self_inner):
                return body
        return _Resp()


def _provider(tmp, fake, interval=0.05, budget=25, audit=None):
    providers.requests = fake                         # module-level injection
    cfg = ProviderConfig(cache_dir=tmp, av_api_key="k", av_daily_budget=budget,
                         av_min_seconds_between_calls=interval)
    return DataProvider(cfg, audit=audit)


def _news_call(p, ticker):
    return p._alpha_vantage({"function": "NEWS_SENTIMENT", "tickers": ticker})


def test_concurrent_calls_are_serialised_at_the_configured_spacing():
    with tempfile.TemporaryDirectory() as tmp:
        fake = _FakeRequests({"feed": []})
        p = _provider(tmp, fake, interval=0.05)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda t: _news_call(p, t), ["A", "B", "C", "D", "E"]))
        assert len(fake.times) == 5
        gaps = [b - a for a, b in zip(fake.times, fake.times[1:])]
        # Every consecutive pair is at least the interval apart (small slack for
        # the sleep's own resolution). Before the lock these were all ~0.
        assert all(g >= 0.045 for g in gaps), gaps
        assert p.budget.used() == 5


def test_a_successful_body_is_cached_and_costs_one_unit():
    with tempfile.TemporaryDirectory() as tmp:
        fake = _FakeRequests({"feed": [{"title": "x"}]})
        p = _provider(tmp, fake, interval=0)
        assert _news_call(p, "NVDA")["feed"]
        assert _news_call(p, "NVDA")["feed"]            # cache hit
        assert len(fake.times) == 1
        assert p.budget.used() == 1


def test_avs_daily_limit_reply_exhausts_the_local_budget():
    """When AV's own counter says the day is spent, ours agrees immediately,
    so the remaining tickers skip straight to the fallback instead of each
    sleeping through the throttle to collect the same rejection."""
    events = []
    with tempfile.TemporaryDirectory() as tmp:
        fake = _FakeRequests({"Information": "We have detected your API key as X and "
                              "our standard API rate limit is 25 requests per day."})
        p = _provider(tmp, fake, interval=0, audit=lambda l, e, d: events.append(e))
        assert _news_call(p, "NVDA") is None
        assert p.budget.remaining() == 0
        assert _news_call(p, "AMZN") is None
        assert len(fake.times) == 1                     # second call never sent
        assert events.count("alphavantage_limit_or_error") == 1
        assert events.count("alphavantage_budget_exhausted") == 1


def test_a_per_second_reply_costs_only_its_own_unit():
    """The per-second message (which the lock now prevents) must not be
    mistaken for the daily limit."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _FakeRequests({"Note": "Thank you for using Alpha Vantage! Please consider "
                              "spreading out your free API requests more sparingly "
                              "(1 request per second)."})
        p = _provider(tmp, fake, interval=0)
        assert _news_call(p, "NVDA") is None
        assert p.budget.used() == 1
        assert p.budget.remaining() == 24


def test_budget_exhausted_is_audited_once_per_provider():
    """172 identical rows in a fortnight said nothing the first one did not."""
    events = []
    with tempfile.TemporaryDirectory() as tmp:
        fake = _FakeRequests({"feed": []})
        p = _provider(tmp, fake, interval=0, budget=2,
                      audit=lambda l, e, d: events.append(e))
        for t in ["A", "B", "C", "D", "E", "F"]:
            _news_call(p, t)
        assert len(fake.times) == 2                     # budget of 2 honoured
        assert events.count("alphavantage_budget_exhausted") == 1

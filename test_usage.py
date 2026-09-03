"""Unit tests for LLM token/cost accounting.

Run:  python -m pytest test_usage.py -q
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from agents import llm, pricing
from storage.db import Database


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
def test_dated_model_id_resolves_to_its_base_price():
    """config.yaml pins `claude-haiku-4-5-20251001`. Longest-prefix lookup must
    price it as haiku rather than dropping it into "unpriced"."""
    assert pricing.rates_for("claude-haiku-4-5-20251001") == (1.00, 5.00)
    assert pricing.rates_for("claude-haiku-4-5") == (1.00, 5.00)


def test_longest_prefix_wins():
    """`claude-opus-5` must not be shadowed by a shorter overlapping key."""
    assert pricing.rates_for("claude-sonnet-5") == (2.00, 10.00)
    assert pricing.rates_for("claude-sonnet-4-6") == (3.00, 15.00)
    assert pricing.rates_for("claude-opus-5") == (5.00, 25.00)


def test_unknown_model_is_unpriced_not_free():
    """An unpriced model must report None so the UI can say "unknown". Returning
    0.0 would silently understate spend and look like the tracker is working."""
    cost, source = pricing.cost_usd("some-new-model", 10_000, 10_000)
    assert cost is None
    assert source == pricing.UNPRICED


def test_token_and_cache_math():
    # 1M in @ $1 + 1M out @ $5
    assert pricing.cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000)[0] == 6.00
    # cache WRITE is 1.25x the input rate
    assert pricing.cost_usd("claude-haiku-4-5", 0, 0, 1_000_000, 0)[0] == 1.25
    # cache READ is 0.10x the input rate (the ~90% saving)
    assert round(pricing.cost_usd("claude-haiku-4-5", 0, 0, 0, 1_000_000)[0], 6) == 0.10
    # a call with no tokens costs nothing but is still "computed", not "unpriced"
    assert pricing.cost_usd("claude-haiku-4-5") == (0.0, pricing.COMPUTED)


# --------------------------------------------------------------------------- #
# Capture — the sink must never be able to break a run
# --------------------------------------------------------------------------- #
def test_a_raising_sink_never_escapes():
    """Cost accounting is observability. A broken sink (locked DB, schema drift)
    must not propagate into the trading pipeline."""
    def boom(_rec):
        raise RuntimeError("sink exploded")

    llm.set_usage_sink(boom)
    try:
        llm._emit_usage(model="m", backend="b")   # must not raise
    finally:
        llm.set_usage_sink(None)


def test_no_sink_is_a_noop():
    llm.set_usage_sink(None)
    llm._emit_usage(model="m", backend="b")


def _capture():
    recs: list[dict] = []
    llm.set_usage_sink(recs.append)
    return recs


def test_context_tags_the_record_and_nests():
    recs = _capture()
    try:
        with llm.usage_context(agent="research", run_id="r1"):
            llm._emit_usage(model="m", backend="b")
            with llm.usage_context(agent="explain"):     # inner overrides
                llm._emit_usage(model="m", backend="b")
        llm._emit_usage(model="m", backend="b")          # outside -> untagged
    finally:
        llm.set_usage_sink(None)
    assert recs[0]["agent"] == "research" and recs[0]["run_id"] == "r1"
    assert recs[1]["agent"] == "explain" and recs[1]["run_id"] == "r1"
    assert "agent" not in recs[2]


def test_context_propagates_into_concurrent_tasks():
    """Research gathers many tickers concurrently. Each task copies the context
    at creation, so every concurrent call must carry the right agent label."""
    recs = _capture()

    async def one(_i):
        await asyncio.sleep(0)
        llm._emit_usage(model="m", backend="b")

    async def main():
        with llm.usage_context(agent="research"):
            await asyncio.gather(*(one(i) for i in range(5)))

    try:
        asyncio.run(main())
    finally:
        llm.set_usage_sink(None)
    assert len(recs) == 5
    assert all(r["agent"] == "research" for r in recs)


# --------------------------------------------------------------------------- #
# Backend record shaping
# --------------------------------------------------------------------------- #
class _Usage:
    input_tokens = 1200
    output_tokens = 340
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 800


class _Resp:
    usage = _Usage()
    model = "claude-haiku-4-5-20251001"


def test_messages_api_record_is_computed_from_tokens():
    recs = _capture()
    try:
        llm._emit_api_usage(_Resp(), "claude-haiku-4-5-20251001", 0.0)
    finally:
        llm.set_usage_sink(None)
    r = recs[0]
    assert r["backend"] == "anthropic_api"
    assert r["cost_source"] == pricing.COMPUTED
    # 1200*$1/M + 340*$5/M + 800*$1/M*0.1
    assert round(r["cost_usd"], 8) == round(0.0012 + 0.0017 + 0.00008, 8)
    assert r["ok"] == 1


def test_served_model_is_billed_not_the_requested_one():
    """Under a server-side fallback the response names a different model; cost
    must follow what actually served the request."""
    class Served(_Resp):
        model = "claude-opus-5"

    recs = _capture()
    try:
        llm._emit_api_usage(Served(), "claude-haiku-4-5", 0.0)
    finally:
        llm.set_usage_sink(None)
    assert recs[0]["model"] == "claude-opus-5"


class _ResultMessage:
    usage = {"input_tokens": 5000, "output_tokens": 900,
             "cache_creation_input_tokens": 100, "cache_read_input_tokens": 4000}
    total_cost_usd = 0.0421
    num_turns = 6
    duration_ms = 22000
    is_error = False


def test_agent_sdk_prefers_its_own_reported_cost():
    recs = _capture()
    try:
        llm._emit_sdk_usage(_ResultMessage(), "claude-haiku-4-5", 0.0, ok=True)
    finally:
        llm.set_usage_sink(None)
    r = recs[0]
    assert r["backend"] == "claude_agent_sdk"
    assert r["cost_usd"] == 0.0421
    assert r["cost_source"] == pricing.SDK_REPORTED
    assert r["num_turns"] == 6


def test_agent_sdk_falls_back_to_computing_when_cost_absent():
    class NoCost(_ResultMessage):
        total_cost_usd = None

    recs = _capture()
    try:
        llm._emit_sdk_usage(NoCost(), "claude-haiku-4-5", 0.0, ok=True)
    finally:
        llm.set_usage_sink(None)
    assert recs[0]["cost_source"] == pricing.COMPUTED
    assert recs[0]["cost_usd"] > 0


# --------------------------------------------------------------------------- #
# Persistence + aggregation
# --------------------------------------------------------------------------- #
def _db() -> Database:
    return Database(Path(tempfile.mkdtemp()) / "t.db")


def test_rows_persist_and_aggregate_by_agent():
    db = _db()
    db.log_llm_usage(agent="research", model="claude-haiku-4-5", backend="anthropic_api",
                     input_tokens=1000, output_tokens=100, cost_usd=0.0015,
                     cost_source="computed", ok=1)
    db.log_llm_usage(agent="research", model="claude-haiku-4-5", backend="anthropic_api",
                     input_tokens=2000, output_tokens=200, cost_usd=0.0030,
                     cost_source="computed", ok=1)
    db.log_llm_usage(agent="decision", model="claude-sonnet-4-6", backend="anthropic_api",
                     input_tokens=500, output_tokens=50, cost_usd=0.0022,
                     cost_source="computed", ok=1)
    by_agent = {r["agent"]: r for r in db.usage_grouped("agent", 30)}
    assert by_agent["research"]["calls"] == 2
    assert by_agent["research"]["input_tokens"] == 3000
    assert round(by_agent["research"]["cost_usd"], 6) == 0.0045
    assert by_agent["decision"]["calls"] == 1
    # ordered by spend, biggest first
    assert [r["agent"] for r in db.usage_grouped("agent", 30)][0] == "research"
    db.close()


def test_failed_calls_are_counted_not_dropped():
    """A max-turns failure burns real tokens; leaving it out would under-report
    exactly the spend worth noticing."""
    db = _db()
    db.log_llm_usage(agent="execution", model="m", backend="claude_agent_sdk",
                     ok=0, cost_source="unpriced", error="max turns")
    t = db.usage_totals(30)
    assert t["calls"] == 1
    assert t["failed"] == 1
    db.close()


def test_unpriced_cost_is_null_and_counted_separately():
    """A NULL cost means unknown. It must not be summed in as zero, and the UI
    needs the count so it can say the total is incomplete."""
    db = _db()
    db.log_llm_usage(agent="a", model="mystery", backend="anthropic_api",
                     input_tokens=9999, cost_usd=None, cost_source="unpriced", ok=1)
    db.log_llm_usage(agent="a", model="claude-haiku-4-5", backend="anthropic_api",
                     input_tokens=1000, cost_usd=0.001, cost_source="computed", ok=1)
    t = db.usage_totals(30)
    assert t["unpriced_calls"] == 1
    assert round(t["cost_usd"], 6) == 0.001      # the unknown row adds nothing
    assert t["input_tokens"] == 10999            # but its TOKENS still count
    db.close()


def test_daily_grouping_and_reject_bad_group_column():
    db = _db()
    db.log_llm_usage(agent="a", model="m", backend="b", cost_usd=0.5, ok=1)
    daily = db.usage_daily(30)
    assert len(daily) == 1 and daily[0]["calls"] == 1
    # group-by column is interpolated into SQL, so it must be allow-listed
    try:
        db.usage_grouped("agent; DROP TABLE llm_usage", 30)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    db.close()


def test_usage_for_run_scopes_to_one_run():
    db = _db()
    db.log_llm_usage(run_id="r1", agent="a", model="m", backend="b", cost_usd=1.0, ok=1)
    db.log_llm_usage(run_id="r2", agent="a", model="m", backend="b", cost_usd=2.0, ok=1)
    assert db.usage_for_run("r1")["cost_usd"] == 1.0
    assert db.usage_for_run("r2")["calls"] == 1
    db.close()

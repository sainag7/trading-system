"""Offline check of explain mode — NO network, NO API keys.

Verifies the single-ticker deep-research pipeline end-to-end:
  A. Report produced + persisted for a ticker NOT in the configured universe,
     with ZERO trading side effects (no fills/trades/orders/plans/pnl/positions).
  B. EMPTY news payload → the report says "no clear catalyst found in available
     news" and cites nothing (the anti-hallucination guarantee).
  C. Ticker validation: missing / malformed --ticker fails cleanly.

Run:  python explain_check.py
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from config import load_config
from orchestrator import Orchestrator
from recommend_check import StubProvider

# Hermetic: force the deterministic fallback even when LLM SDKs are installed.
from agents import llm as _llm
_llm._SDK_OK = False
_llm._ANTHROPIC_OK = False

from agents.explain_agent import NO_CATALYST  # noqa: E402


class NoNewsStub(StubProvider):
    """Same market data, but the news feed returns nothing."""

    def get_news_sentiment(self, ticker, days=14, limit=50):
        return {"source": "stub", "window_days": days, "article_count": 0,
                "aggregate_score": None, "articles": []}


def _fresh_config(tmp: str):
    config = load_config()
    config.set_mode("explain")
    config.raw.setdefault("discovery", {})["dynamic_discovery"] = False
    config.raw.setdefault("execution", {})["read_live_account"] = False
    config.raw["storage"]["db_path"] = str(Path(tmp) / "trading.db")
    # Prove universe-independence: research AAPL while it is NOT in the universe.
    config.raw["universe"] = ["MSFT", "NVDA"]
    return config


async def case_a() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = _fresh_config(tmp)
        orch = Orchestrator(config, provider=StubProvider())
        await orch.run_explain("AAPL")

        db = orch.db
        counts = {t: db.query(f"SELECT COUNT(*) n FROM {t}")[0]["n"]
                  for t in ("fills", "trades", "orders", "trade_plans", "positions", "pnl")}
        reports = db.get_explain_reports("AAPL")
        run = db.query("SELECT mode FROM runs ORDER BY started_ts DESC LIMIT 1")[0]["mode"]

        print("\n----- EXPLAIN CHECK [A: report + zero side effects] -----")
        print(f"reports saved:   {len(reports)} (expect 1)")
        print(f"run mode:        {run} (expect explain)")
        print(f"side effects:    {counts} (expect all 0)")

        assert run == "explain"
        assert len(reports) == 1, "one report row expected in agent_outputs"
        assert all(v == 0 for v in counts.values()), f"trading side effects! {counts}"

        r = reports[0]["report"]
        for key in ("snapshot", "price_action", "why_it_moved", "earnings",
                    "fundamentals", "scenarios", "risks", "watch_next", "disclaimer"):
            assert key in r, f"report missing section {key!r}"
        assert db.get_explain_tickers() == ["AAPL"]
        # Scenario levels must be concrete numbers derived from series+ATR.
        assert len(r["scenarios"]) == 3
        for s in r["scenarios"]:
            assert isinstance(s.get("target_level"), (int, float)), s
        # Returns computed from the daily series.
        rets = r["price_action"]["returns"]
        assert isinstance(rets.get("m1"), (int, float))
        # Stub has news, so the catalyst section must cite actual stub headlines.
        assert r["why_it_moved"]["catalyst_found"] is True
        assert all("AAPL" in d["headline"] for d in r["why_it_moved"]["drivers"])
        # Verdict: stub composite (~72) ≥ buy threshold, not held ⇒ BUY with a plan.
        v = r["verdict"]
        price = r["snapshot"]["price"]
        print(f"verdict: {v['action']} conf={v['confidence']} plan={v['suggested_plan']}")
        assert v["action"] == "buy", v
        assert isinstance(v["confidence"], int) and 5 <= v["confidence"] <= 95
        assert v["suggested_plan"] and v["suggested_plan"]["stop"] < price < v["suggested_plan"]["target"]
        assert v.get("rationale")
        print("report schema, scenario levels, returns, citations, and verdict verified")


async def case_b() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = _fresh_config(tmp)
        orch = Orchestrator(config, provider=NoNewsStub())
        await orch.run_explain("AAPL")
        r = orch.db.get_explain_reports("AAPL")[0]["report"]
        wim = r["why_it_moved"]
        print("\n----- EXPLAIN CHECK [B: empty news → no invented catalyst] -----")
        print(f"summary:        {wim.get('summary')!r}")
        print(f"catalyst_found: {wim.get('catalyst_found')} · drivers: {len(wim.get('drivers', []))}")
        assert wim.get("summary") == NO_CATALYST
        assert wim.get("catalyst_found") is False
        assert wim.get("drivers") == []


def case_c() -> None:
    import subprocess, sys
    print("\n----- EXPLAIN CHECK [C: ticker validation] -----")
    for argv, why in ((["--mode", "explain"], "missing ticker"),
                      (["--mode", "explain", "--ticker", "NV1D$"], "malformed ticker")):
        p = subprocess.run([sys.executable, "orchestrator.py", *argv],
                           capture_output=True, text=True)
        ok = p.returncode != 0 and "error:" in (p.stderr + p.stdout)
        print(f"{why}: exit={p.returncode} (expect non-zero with clear error) -> {'OK' if ok else 'FAIL'}")
        assert ok, (why, p.stdout, p.stderr)


def case_d() -> None:
    """Pure-function verdict mapping matrix (deterministic recommendation)."""
    from agents.explain_agent import _verdict
    strat = {"min_score_to_buy": 65, "min_score_to_add": 70,
             "trim_below_score": 45, "exit_below_score": 35}
    cases = [
        (True, 30, "sell"), (True, 42, "trim"), (True, 80, "add"), (True, 55, "hold"),
        (False, 80, "buy"), (False, 30, "avoid"), (False, 55, "watch"),
        (None, 80, "buy"),   # unknown position treated as not held (reason noted)
    ]
    print("\n----- EXPLAIN CHECK [D: verdict mapping matrix] -----")
    for held, score, expect in cases:
        v = _verdict(100.0, score, held, strat)
        print(f"held={held!s:5} score={score:3} -> {v['action']:5} conf={v['confidence']}")
        assert v["action"] == expect, (held, score, v["action"], expect)
    # Event risk must lower conviction vs the same setup without it.
    base = _verdict(100.0, 80, False, strat)["confidence"]
    risky = _verdict(100.0, 80, False, strat, event_risk=True)["confidence"]
    print(f"event-risk dampener: {base} -> {risky}")
    assert risky < base
    # buy/add carry a plan; watch/sell do not.
    assert _verdict(100.0, 80, False, strat)["suggested_plan"] is not None
    assert _verdict(100.0, 55, False, strat)["suggested_plan"] is None


async def main() -> int:
    await case_a()
    await case_b()
    case_c()
    case_d()
    print("\n✅ EXPLAIN CHECK PASSED — briefing produced, grounded, verdict mapped, "
          "and side-effect free.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

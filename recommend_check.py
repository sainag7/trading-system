"""Offline check of recommend mode — NO network, NO API keys.

Verifies the recommend pipeline runs end-to-end and has ZERO trading side
effects: no fills, no trades, no trade_plans, and no hypothetical P&L/positions
snapshots polluting the dashboard — while still logging decisions + agent
outputs so the dashboard can show advice.

Run:  python recommend_check.py
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from config import load_config
from orchestrator import Orchestrator

# Hermetic offline check: force the deterministic fallback even when the Claude
# Agent SDK / anthropic packages are installed, so this never makes a network /
# LLM call and stays fast and deterministic.
from agents import llm as _llm
_llm._SDK_OK = False
_llm._ANTHROPIC_OK = False


class StubProvider:
    """Deterministic offline market data so the pipeline runs without network."""

    PRICES = {
        "AAPL": 190.0, "MSFT": 410.0, "NVDA": 120.0, "GOOGL": 175.0, "AMZN": 185.0,
        "META": 500.0, "JPM": 200.0, "V": 280.0, "UNH": 490.0, "XOM": 110.0,
        "CAT": 330.0, "COST": 850.0,
    }
    SECTORS = {
        "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
        "GOOGL": "Communication Services", "AMZN": "Consumer Discretionary",
        "META": "Communication Services", "JPM": "Financials", "V": "Financials",
        "UNH": "Health Care", "XOM": "Energy", "CAT": "Industrials",
        "COST": "Consumer Staples",
    }

    def get_quote(self, ticker):
        p = self.PRICES.get(ticker.upper())
        return {"ticker": ticker.upper(), "price": p, "prev_close": p, "source": "stub"} if p else None

    def get_daily_series(self, ticker, lookback=100):
        p = self.PRICES.get(ticker.upper())
        if not p:
            return None
        # A gently rising series so the deterministic trend logic sees an uptrend.
        return [
            {"date": f"2026-01-{(i % 28) + 1:02d}", "open": p * (0.8 + i * 0.002),
             "high": p * (0.82 + i * 0.002), "low": p * (0.78 + i * 0.002),
             "close": p * (0.8 + i * 0.002), "volume": 1_000_000}
            for i in range(90)
        ]

    def get_fundamentals(self, ticker):
        p = self.PRICES.get(ticker.upper(), 0)
        return {"ticker": ticker.upper(), "sector": self.SECTORS.get(ticker.upper(), "Unknown"),
                "pe_ratio": 25.0, "ps_ratio": 6.0, "eps_ttm": 8.0,
                "revenue_ttm": 1.0e11, "profit_margin": 22.0, "gross_margin": 44.0,
                "operating_margin": 30.0, "debt_to_equity": 1.2, "free_cash_flow": 2.0e10,
                "revenue_growth_yoy": 10.0, "eps_growth_yoy": 12.0,
                "next_earnings_date": "2026-07-30", "beta": 1.1, "market_cap": 2.0e12,
                "industry": "Stub Industry", "week52_high": p * 1.05, "week52_low": p * 0.7,
                "source": "stub"}

    def get_news_sentiment(self, ticker, days=14, limit=50):
        return {"source": "stub", "window_days": days, "article_count": 2,
                "aggregate_score": 0.18,
                "articles": [
                    {"title": f"{ticker.upper()} update", "source": "Stub Wire",
                     "time_published": "20260612T120000", "url": "https://example.com/1",
                     "sentiment_score": 0.2, "sentiment_label": "Somewhat-Bullish",
                     "relevance": 0.9},
                    {"title": f"{ticker.upper()} analyst note", "source": "Stub Wire",
                     "time_published": "20260611T120000", "url": "https://example.com/2",
                     "sentiment_score": 0.16, "sentiment_label": "Neutral", "relevance": 0.8},
                ]}

    def get_macro(self):
        return {"fed_funds": 4.5, "ten_year": 4.2, "cpi": 3.1, "unemployment": 4.0}

    def sector_for(self, ticker):
        return self.SECTORS.get(ticker.upper(), "Unknown")


# Discovery is the ONLY source of tickers now, and it hits the network. Feed the
# pipeline the stub universe instead. Note we leave `dynamic_discovery` ON: with
# no fixed watchlist, turning it off halts the run by design, so this stub is
# what keeps the check exercising the full pipeline. The orchestrator imports
# `discover_candidates` at call time, so patching the module attribute works.
from data import screener as _screener
_screener.discover_candidates = lambda cfg, provider, n=20: list(StubProvider.PRICES)


async def run_recommend_check() -> None:
    """Run one full recommend cycle offline and assert zero trading side effects,
    plus that the configured exit parameters reach the decision agent."""
    import json

    with tempfile.TemporaryDirectory() as tmp:
        config = load_config()
        config.set_mode("recommend")
        config.raw.setdefault("execution", {})["read_live_account"] = False
        config.raw["storage"]["db_path"] = str(Path(tmp) / "trading.db")
        config.raw.setdefault("recommend", {})["hypothetical_cash"] = 10_000.0
        # Lower the buy bar so the offline policy actually recommends something.
        config.raw["strategy"]["min_score_to_buy"] = 55

        orch = Orchestrator(config, provider=StubProvider())
        await orch.run_cycle()

        db = orch.db
        fills = db.query("SELECT COUNT(*) n FROM fills")[0]["n"]
        trades = db.query("SELECT COUNT(*) n FROM trades")[0]["n"]
        plans = db.query("SELECT COUNT(*) n FROM trade_plans")[0]["n"]
        pnl = db.query("SELECT COUNT(*) n FROM pnl")[0]["n"]
        positions = db.query("SELECT COUNT(*) n FROM positions")[0]["n"]
        decisions = db.query("SELECT COUNT(*) n FROM decisions")[0]["n"]
        agents = db.query("SELECT COUNT(*) n FROM agent_outputs")[0]["n"]

        print("\n----- RECOMMEND CHECK -----")
        print(f"decisions logged: {decisions}")
        print(f"agent_outputs:    {agents}")
        print(f"fills:            {fills} (expect 0)")
        print(f"trades:           {trades} (expect 0)")
        print(f"trade_plans:      {plans} (expect 0)")
        print(f"pnl snapshots:    {pnl} (expect 0 — hypothetical book is never snapshotted)")
        print(f"position rows:    {positions} (expect 0)")

        assert agents > 0, "recommend should still log agent outputs"
        assert decisions > 0, "recommend should log guardrail-annotated decisions"
        assert fills == 0, "recommend must place NO fills"
        assert trades == 0, "recommend must write NO trades"
        assert plans == 0, "recommend must write NO trade plans"
        assert pnl == 0, "hypothetical equity must NOT be snapshotted into pnl"
        assert positions == 0, "hypothetical positions must NOT be snapshotted"

        # The configured exit parameters must reach the decision agent end-to-end.
        # Stops are now VOLATILITY-based: stop = price - k*ATR, floored so the loss
        # never exceeds max_loss_pct, and the target sits at a fixed reward:risk
        # (target_r_multiple). Assert those invariants rather than a flat percent.
        row = db.query(
            "SELECT input_json, output_json FROM agent_outputs "
            "WHERE agent='decision' ORDER BY ts DESC LIMIT 1")[0]
        payload = json.loads(row["input_json"])
        strat = payload.get("strategy", {})
        assert strat.get("max_holding_days") == 60, strat
        target_r = strat.get("target_r_multiple", 2.5)
        max_loss = strat.get("max_loss_pct", strat.get("default_stop_loss_pct", 0.10))
        orders = (json.loads(row["output_json"]) or {}).get("orders", [])
        buys = [o for o in orders
                if o.get("action") == "buy" and o.get("price")
                and o.get("suggested_stop_loss") and o.get("take_profit")]
        assert buys, "run should propose at least one buy with a plan"
        for o in buys:
            price, stop, take = o["price"], o["suggested_stop_loss"], o["take_profit"]
            assert stop < price < take, (o["ticker"], stop, price, take)
            # Loss is capped: the stop never sits below the max-loss floor.
            assert stop >= price * (1 - max_loss) - 0.01, (o["ticker"], stop / price)
            # Reward:risk equals the configured R-multiple.
            r = (take - price) / (price - stop)
            assert abs(r - target_r) < 0.05, (o["ticker"], r)
        print(f"exit parameters verified: ATR stop capped at {max_loss:.0%} loss, "
              f"target ~{target_r:g}R")


async def main() -> int:
    await run_recommend_check()
    print("\n✅ RECOMMEND CHECK PASSED — ran end-to-end with zero trading "
          "side effects.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

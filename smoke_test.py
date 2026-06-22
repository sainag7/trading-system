"""End-to-end smoke test of the paper-mode pipeline — NO network, NO API keys.

Injects a deterministic stub data provider so the full chain
(Research -> Analysis -> Decision -> Risk -> Execution -> Monitor) runs offline
and produces simulated fills. Verifies the audit DB captured orders & fills and
that no guardrail was violated.

Run:  python smoke_test.py
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from config import load_config
from orchestrator import Orchestrator

# Hermetic offline smoke test: force the deterministic fallback even when the
# Claude Agent SDK / anthropic packages are installed, so this never makes a
# network / LLM call and stays fast and deterministic.
from agents import llm as _llm
_llm._SDK_OK = False
_llm._ANTHROPIC_OK = False


class StubProvider:
    """Deterministic offline market data so paper fills actually happen."""

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


async def main() -> int:
    # Isolated temp dir so we don't touch the real DB / paper account.
    with tempfile.TemporaryDirectory() as tmp:
        config = load_config()
        config.set_mode("paper")
        # Keep this offline test deterministic — no network discovery.
        config.raw.setdefault("discovery", {})["dynamic_discovery"] = False
        config.raw["storage"]["db_path"] = str(Path(tmp) / "trading.db")
        config.raw["paper"]["state_file"] = str(Path(tmp) / "paper_account.json")
        config.raw["paper"]["starting_cash"] = 10_000.0
        # Lower the buy bar so the deterministic offline policy actually trades
        # against the stub's modest uptrend (real runs use the config default 65).
        config.raw["strategy"]["min_score_to_buy"] = 55

        orch = Orchestrator(config, provider=StubProvider())
        await orch.run_cycle()

        db = orch.db
        orders = db.query("SELECT ticker, side, qty, notional_usd, status FROM orders")
        fills = db.query("SELECT ticker, side, qty, price FROM fills")
        decisions = db.query("SELECT ticker, approved, approved_usd, resized FROM decisions")
        agent_rows = db.query("SELECT agent, COUNT(*) n FROM agent_outputs GROUP BY agent")

        print("\n----- SMOKE TEST RESULTS -----")
        print(f"agent_outputs: {{ {', '.join(f'{r['agent']}:{r['n']}' for r in agent_rows)} }}")
        print(f"decisions logged: {len(decisions)}")
        print(f"orders logged:    {len(orders)}")
        print(f"fills logged:     {len(fills)}")
        for o in orders:
            print(f"   order: {o['side']} {o['qty']:g} {o['ticker']} "
                  f"${o['notional_usd']:.2f} -> {o['status']}")

        # --- assertions -------------------------------------------------
        acct = orch._build_broker().get_account()
        approved = [d for d in decisions if d["approved"]]
        assert agent_rows, "no agent outputs were logged"
        assert len(approved) > 0, "expected at least one approved order in paper mode"
        assert len(fills) == len(approved), "every approved paper order should fill"
        # No position may exceed 15% of equity (the key guardrail) after fills.
        for t, p in acct.positions.items():
            pct = p.market_value / acct.equity if acct.equity else 0
            assert pct <= 0.15 + 1e-6, f"{t} is {pct:.1%} of equity — guardrail breached!"
        # Daily trade cap respected.
        assert len(fills) <= config.risk.daily_max_trades, "daily trade cap breached"
        print("\n✅ SMOKE TEST PASSED — pipeline ran end-to-end and guardrails held.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

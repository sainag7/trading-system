"""Offline check of recommend mode — NO network, NO API keys.

Verifies the recommend pipeline runs end-to-end and has ZERO trading side effects:
no fills, no trades, no trade_plans, and the paper-account file is never created —
while still logging decisions + agent outputs so the dashboard can show advice.

Run:  python recommend_check.py
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from config import load_config
from orchestrator import Orchestrator
from smoke_test import StubProvider

# Hermetic offline check: force the deterministic fallback even when the Claude
# Agent SDK / anthropic packages are installed, so this never makes a network /
# LLM call and stays fast and deterministic.
from agents import llm as _llm
_llm._SDK_OK = False
_llm._ANTHROPIC_OK = False


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config = load_config()
        config.set_mode("recommend")
        # Keep this offline check deterministic — no network discovery.
        config.raw.setdefault("discovery", {})["dynamic_discovery"] = False
        paper_state = Path(tmp) / "paper_account.json"
        config.raw["storage"]["db_path"] = str(Path(tmp) / "trading.db")
        config.raw["paper"]["state_file"] = str(paper_state)
        config.raw["paper"]["starting_cash"] = 10_000.0
        # Lower the buy bar so the offline policy actually recommends something.
        config.raw["strategy"]["min_score_to_buy"] = 55

        orch = Orchestrator(config, provider=StubProvider())
        await orch.run_cycle()

        db = orch.db
        fills = db.query("SELECT COUNT(*) n FROM fills")[0]["n"]
        trades = db.query("SELECT COUNT(*) n FROM trades")[0]["n"]
        plans = db.query("SELECT COUNT(*) n FROM trade_plans")[0]["n"]
        decisions = db.query("SELECT COUNT(*) n FROM decisions")[0]["n"]
        agents = db.query("SELECT COUNT(*) n FROM agent_outputs")[0]["n"]

        print("\n----- RECOMMEND CHECK -----")
        print(f"decisions logged: {decisions}")
        print(f"agent_outputs:    {agents}")
        print(f"fills:            {fills} (expect 0)")
        print(f"trades:           {trades} (expect 0)")
        print(f"trade_plans:      {plans} (expect 0)")
        print(f"paper account file created: {paper_state.exists()} (expect False)")

        assert agents > 0, "recommend should still log agent outputs"
        assert decisions > 0, "recommend should log guardrail-annotated decisions"
        assert fills == 0, "recommend must place NO fills"
        assert trades == 0, "recommend must write NO trades"
        assert plans == 0, "recommend must write NO trade plans"
        assert not paper_state.exists(), "recommend must NOT touch the paper account"
        print("\n✅ RECOMMEND CHECK PASSED — advice produced, zero trading side effects.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

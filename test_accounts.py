"""Offline tests for multi-account routing + autonomous-trading safety gates.

NO network, NO API keys. Covers:
  * Config.apply_account — per-account risk overlay + account-number resolution.
  * Orchestrator._trading_safety_halt — the equity guard and the read-sanity gate.
  * Broker construction routes to the selected account number.

Run:  python -m pytest test_accounts.py -q
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from config import load_config
from orchestrator import Orchestrator
from risk.guardrails import AccountState, Position

# Importing recommend_check forces the hermetic offline LLM fallback and gives us
# a deterministic StubProvider (no network) for constructing an Orchestrator.
from recommend_check import StubProvider  # noqa: E402


def _cfg(tmp: str, mode: str = "recommend"):
    cfg = load_config()
    cfg.set_mode(mode)
    cfg.raw.setdefault("discovery", {})["dynamic_discovery"] = False
    cfg.raw.setdefault("execution", {})["read_live_account"] = False
    cfg.raw["storage"]["db_path"] = str(Path(tmp) / "trading.db")
    return cfg


def _orch(cfg):
    return Orchestrator(cfg, provider=StubProvider())


# --------------------------------------------------------------------------- #
# Config.apply_account
# --------------------------------------------------------------------------- #
def test_agentic_overlay_applies_100dollar_risk():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        base_min_trade = cfg.risk.min_trade_usd            # base = 50
        active = cfg.apply_account("agentic")
        r = cfg.risk
        # The $100 overlay must take effect for the agentic account.
        assert r.min_trade_usd == 1.0, r.min_trade_usd
        # Sizing on this account is the decision agent's call: EVERY percentage
        # ceiling is off. A ceiling the agent exceeds does not trim the order, it
        # rejects it outright (only the per-trade cap resizes), so a cap here
        # would drop high-conviction ideas rather than place them smaller.
        assert r.per_trade_max_pct == 1.0, r.per_trade_max_pct
        assert r.max_position_pct == 1.0, r.max_position_pct
        assert r.min_cash_reserve_pct == 0.0, r.min_cash_reserve_pct
        # Discovered tickers often have no sector, so they all collapse into one
        # "Unknown" bucket; the sector cap is disabled here on purpose.
        assert r.max_sector_pct == 1.0, r.max_sector_pct
        # No position-count cap either: the decision agent chooses how many names
        # to hold. A fixed count was read as portfolio shape and became a sizing
        # anchor (equity/max_positions), stranding cash whenever fewer candidates
        # qualified than there were slots.
        assert r.max_positions is None, r.max_positions
        # So the remainder cannot sit idle, approved buys are scaled up to use
        # the deployable balance.
        assert r.sweep_cash_to_buys is True
        assert min(r.per_trade_max_usd, r.per_trade_max_pct * 100.0) == 100.0
        # Routing metadata is resolved.
        assert active["role"] == "agentic"
        assert cfg.account_number == cfg.accounts["agentic"]["number"]
        assert active["max_equity_guard"] == 500
        assert base_min_trade == 50.0  # sanity: base really was the strict floor


def test_individual_keeps_base_limits():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        cfg.apply_account("individual")
        # No per-account risk overlay -> strict base limits unchanged.
        assert cfg.risk.min_trade_usd == 50.0
        assert cfg.risk.per_trade_max_usd == 500.0
        assert cfg.risk.max_position_pct == 0.15
        assert cfg.account_number == cfg.accounts["individual"]["number"]
        assert (cfg.active_account or {}).get("max_equity_guard") is None


def test_unknown_account_role_raises():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        try:
            cfg.apply_account("bogus")
        except ValueError:
            return
        raise AssertionError("apply_account('bogus') should have raised ValueError")


# --------------------------------------------------------------------------- #
# Orchestrator._trading_safety_halt — equity guard
# --------------------------------------------------------------------------- #
def _acct(equity, cash, positions=None):
    return AccountState(equity=equity, cash=cash, buying_power=cash,
                        peak_equity=equity, positions=positions or {})


def test_equity_guard_halts_when_account_too_rich():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp, mode="live")
        cfg.apply_account("agentic")           # max_equity_guard = 500
        orch = _orch(cfg)
        # A $3k read on the agentic slot == mis-route -> must halt.
        halt = orch._trading_safety_halt(_acct(3313.0, 39.99))
        assert halt is not None
        assert halt[0] == "trading_equity_guard"


def test_equity_guard_passes_for_real_100dollar_book():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp, mode="live")
        cfg.apply_account("agentic")
        orch = _orch(cfg)
        # $100 all-cash, no positions, no prior snapshot -> safe to trade.
        assert orch._trading_safety_halt(_acct(100.0, 100.0)) is None


# --------------------------------------------------------------------------- #
# Orchestrator._trading_safety_halt — read-sanity gate
# --------------------------------------------------------------------------- #
def test_read_sanity_skips_when_positions_vanish():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp, mode="live")
        cfg.apply_account("agentic")
        orch = _orch(cfg)
        acct_no = cfg.account_number
        # Prior run recorded $80 of position value ($100 equity, $20 cash) for
        # THIS account, under a different run id.
        orch.db.snapshot_pnl("prev-run", equity=100.0, cash=20.0, buying_power=20.0,
                             peak_equity=100.0, drawdown_pct=0.0, account=acct_no)
        # This read shows 0 positions -> likely under-report -> skip trading.
        halt = orch._trading_safety_halt(_acct(100.0, 100.0))
        assert halt is not None
        assert halt[0] == "trading_read_sanity_skip"


def test_read_sanity_passes_when_positions_present():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp, mode="live")
        cfg.apply_account("agentic")
        orch = _orch(cfg)
        acct_no = cfg.account_number
        orch.db.snapshot_pnl("prev-run", equity=100.0, cash=20.0, buying_power=20.0,
                             peak_equity=100.0, drawdown_pct=0.0, account=acct_no)
        held = {"NVDA": Position(ticker="NVDA", shares=1.0, avg_cost=80.0,
                                 market_value=80.0, sector="Technology")}
        # Read DOES include a position -> not a suspect read.
        assert orch._trading_safety_halt(_acct(100.0, 20.0, held)) is None


def test_read_sanity_ignores_other_accounts_history():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp, mode="live")
        cfg.apply_account("agentic")
        orch = _orch(cfg)
        # A prior snapshot for a DIFFERENT account must not gate this one.
        orch.db.snapshot_pnl("prev-run", equity=3000.0, cash=40.0, buying_power=40.0,
                             peak_equity=3000.0, drawdown_pct=0.0, account="999999999")
        assert orch._trading_safety_halt(_acct(100.0, 100.0)) is None


# --------------------------------------------------------------------------- #
# Broker routing + per-account peak equity
# --------------------------------------------------------------------------- #
def test_brokers_carry_selected_account_number():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp, mode="live")
        cfg.apply_account("agentic")
        orch = _orch(cfg)
        assert orch._build_broker().account_number == cfg.account_number
        assert orch._robinhood_broker().account_number == cfg.account_number


def test_peak_equity_is_scoped_per_account():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        orch = _orch(cfg)
        db = orch.db
        db.snapshot_pnl("r1", equity=3000.0, cash=40.0, buying_power=40.0,
                        peak_equity=3000.0, drawdown_pct=0.0, account="750268922")
        db.snapshot_pnl("r2", equity=100.0, cash=100.0, buying_power=100.0,
                        peak_equity=100.0, drawdown_pct=0.0, account="806813184")
        # Each account's high-water mark is isolated.
        assert db.get_peak_equity(fallback=0.0, account="806813184") == 100.0
        assert db.get_peak_equity(fallback=0.0, account="750268922") == 3000.0
        # Global (legacy) view still sees the max across all accounts.
        assert db.get_peak_equity(fallback=0.0) == 3000.0

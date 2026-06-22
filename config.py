"""Centralised configuration loading for the trading system.

Loads ``config.yaml`` once and exposes typed accessors. Environment variables
(from ``.env``) are layered on top for secrets and for a ``--mode`` override.

This module intentionally has **no** trading logic — it only reads config so
that every other module gets a single, consistent view of the limits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv is optional at import time (tests don't need it)
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv missing is non-fatal
    pass

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

VALID_MODES = ("recommend", "paper", "preview", "live")


@dataclass(frozen=True)
class RiskLimits:
    """Hard, deterministic risk limits. Mirrors the ``risk:`` block in config.

    These are passed into :mod:`risk.guardrails`. The guardrail layer treats
    them as inviolable numeric ceilings — it never calls an LLM and never
    relaxes a limit at runtime.
    """

    max_positions: int = 15
    max_position_pct: float = 0.15
    max_sector_pct: float = 0.40
    per_trade_max_usd: float = 500.0
    daily_max_trades: int = 5
    min_cash_reserve_pct: float = 0.10
    max_account_drawdown_halt_pct: float = 0.15
    min_trade_usd: float = 50.0
    allow_fractional_shares: bool = True
    no_trade_list: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RiskLimits":
        no_trade = tuple(str(t).upper() for t in d.get("no_trade_list", []))
        return cls(
            max_positions=int(d.get("max_positions", 15)),
            max_position_pct=float(d.get("max_position_pct", 0.15)),
            max_sector_pct=float(d.get("max_sector_pct", 0.40)),
            per_trade_max_usd=float(d.get("per_trade_max_usd", 500.0)),
            daily_max_trades=int(d.get("daily_max_trades", 5)),
            min_cash_reserve_pct=float(d.get("min_cash_reserve_pct", 0.10)),
            max_account_drawdown_halt_pct=float(
                d.get("max_account_drawdown_halt_pct", 0.15)
            ),
            min_trade_usd=float(d.get("min_trade_usd", 50.0)),
            allow_fractional_shares=bool(d.get("allow_fractional_shares", True)),
            no_trade_list=no_trade,
        )


@dataclass
class Config:
    """In-memory view of ``config.yaml`` plus environment-derived secrets."""

    raw: dict[str, Any]
    path: Path

    # -- top level ---------------------------------------------------------
    @property
    def mode(self) -> str:
        # Precedence: env override (TRADING_MODE) > config.yaml. The CLI flag is
        # applied by the orchestrator after construction via set_mode().
        mode = os.getenv("TRADING_MODE") or self.raw.get("mode", "paper")
        mode = str(mode).lower()
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode {mode!r}; expected one of {VALID_MODES}")
        return mode

    def set_mode(self, mode: str) -> None:
        mode = str(mode).lower()
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode {mode!r}; expected one of {VALID_MODES}")
        self.raw["mode"] = mode

    @property
    def kill_switch_enabled(self) -> bool:
        """True if the config flag is set OR the kill-switch file exists."""
        if bool(self.raw.get("kill_switch", False)):
            return True
        ks_file = self.raw.get("kill_switch_file", "KILL_SWITCH")
        return (REPO_ROOT / ks_file).exists()

    # -- nested blocks -----------------------------------------------------
    @property
    def risk(self) -> RiskLimits:
        return RiskLimits.from_dict(self.raw.get("risk", {}))

    @property
    def strategy(self) -> dict[str, Any]:
        return self.raw.get("strategy", {})

    @property
    def analysis(self) -> dict[str, Any]:
        return self.raw.get("analysis", {})

    @property
    def universe(self) -> list[str]:
        return [str(t).upper() for t in self.raw.get("universe", [])]

    @property
    def discovery(self) -> dict[str, Any]:
        return self.raw.get("discovery", {})

    @property
    def research(self) -> dict[str, Any]:
        return self.raw.get("research", {})

    @property
    def sectors(self) -> dict[str, str]:
        return {str(k).upper(): str(v) for k, v in self.raw.get("sectors", {}).items()}

    @property
    def models(self) -> dict[str, str]:
        return self.raw.get("models", {})

    @property
    def data(self) -> dict[str, Any]:
        return self.raw.get("data", {})

    @property
    def execution(self) -> dict[str, Any]:
        return self.raw.get("execution", {})

    @property
    def storage(self) -> dict[str, Any]:
        return self.raw.get("storage", {})

    @property
    def paper(self) -> dict[str, Any]:
        return self.raw.get("paper", {"starting_cash": 10000.0,
                                      "state_file": "storage/paper_account.json"})

    @property
    def db_path(self) -> Path:
        return REPO_ROOT / self.storage.get("db_path", "storage/trading.db")

    # -- secrets -----------------------------------------------------------
    @staticmethod
    def env(name: str, default: str | None = None) -> str | None:
        return os.getenv(name, default)


def load_config(path: str | Path | None = None) -> Config:
    """Load and return the :class:`Config` from ``config.yaml``."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, "r") as f:
        raw = yaml.safe_load(f) or {}
    return Config(raw=raw, path=cfg_path)

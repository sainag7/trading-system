"""In-process orchestrator runners for the web API.

Thin wrappers that build a ``Config`` with the same mode/account routing as
``orchestrator.main()`` and drive the orchestrator directly (no subprocess), so
the job manager can capture output and — for trading — so per-order approval can
be injected. Nothing here bypasses guardrails or the kill switch: every path goes
through ``Orchestrator``.
"""
from __future__ import annotations

from config import Config, load_config
import orchestrator as orch_mod


class ConfigError(ValueError):
    """A user-facing configuration problem (bad account, bad mode, …)."""


def prepare_config(*, mode: str,
                   account: str | None = None) -> tuple[Config, str | None, dict | None]:
    """Load config and apply mode/account exactly like ``orchestrator.main``.

    Advice modes default to the ``individual`` account, trading modes to
    ``agentic`` — so autonomous orders never touch the individual book. Trading
    modes REQUIRE a real, configured account number (matches the CLI guard)."""
    cfg = load_config()
    cfg.set_mode(mode)

    role = account
    if role is None and cfg.accounts:
        role = "agentic" if mode in ("preview", "live") else "individual"
    active = None
    if role:
        try:
            active = cfg.apply_account(role)
        except ValueError as e:
            raise ConfigError(str(e))
        number = active.get("number")
        if mode in ("preview", "live") and (not number or str(number).startswith("YOUR_")):
            raise ConfigError(
                f"account role {role!r} has no real account number configured "
                "(set accounts.<role>.number in config.local.yaml) — refusing to "
                "trade without an explicit account.")
    return cfg, role, active


async def run_scan(account: str | None = None) -> dict:
    """Recommend-mode scan (advice only — never trades)."""
    cfg, role, _ = prepare_config(mode="recommend", account=account)
    orch = orch_mod.Orchestrator(cfg)
    await orch.run_cycle()
    return {"run_id": orch.run_id, "mode": "recommend", "account": role}


async def run_research(ticker: str) -> dict:
    """Explain-mode single-ticker deep research (read-only — never trades)."""
    t = (ticker or "").strip().upper()
    if not (t.isalpha() and 1 <= len(t) <= 5):
        raise ConfigError(f"{ticker!r} doesn't look like a US equity symbol (1-5 letters)")
    cfg, _, _ = prepare_config(mode="explain")
    orch = orch_mod.Orchestrator(cfg)
    await orch.run_explain(t)
    return {"run_id": orch.run_id, "mode": "explain", "ticker": t}

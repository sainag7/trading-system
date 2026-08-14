"""Orchestrator — the main daily trading loop.

Pipeline (strict order):

    [Discovery] -> Research -> Analysis -> Decision -> Risk(guardrails)
                -> Execution -> Monitor

Run modes (``--mode`` overrides config.yaml; default ``recommend``):

    recommend  research + advice ONLY; places nothing and writes no fills/trades/
               plans. Reads the real account read-only if connected, else a
               hypothetical book. Use this to judge the agents' decisions.
    explain    deep-research briefing for ONE ticker (--ticker NVDA). Read-only:
               no discovery, no decision agent, no orders. Report persisted to
               SQLite and viewable in the dashboard's Deep dive tab.
    preview    print planned orders, require explicit confirmation, then place live
    live       place real orders via the Robinhood Trading MCP (within guardrails)

Hard safety properties enforced here:

  * The GLOBAL KILL SWITCH is checked before the cycle and again before EVERY
    order (recommend mode never trades, so it is exempt).
  * The account DRAWDOWN HALT is enforced by the guardrails; if breached, no new
    risk is opened (exits still allowed).
  * EVERY order intent passes through ``risk.guardrails`` before the executor
    can see it. There is no path from a decision to a fill that skips the risk
    layer.
  * Every agent input/output, decision, order and fill is written to SQLite.

There is ONE strategy, configured under ``strategy:`` / ``analysis:`` in
config.yaml — no profiles or per-run parameter switching.

Usage:
    python orchestrator.py --mode recommend
    python orchestrator.py --mode explain --ticker NVDA
    python orchestrator.py --mode preview
    python orchestrator.py --mode live --yes      # skip the extra live warning
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import market
from config import load_config
from storage.db import Database
from data.providers import build_provider
from risk.guardrails import (
    AccountState, OrderIntent, Side, validate_batch,
)
from execution.executor import Executor, RobinhoodMCPBroker
from execution.notifier import Notifier
from agents.research_agent import run_research
from agents.analysis_agent import run_analysis
from agents.decision_agent import run_decision
from agents.monitor_agent import run_monitor
from agents import explain_agent, llm


def _now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]


def _account_summary(acct: AccountState) -> dict:
    """Compact, LLM-friendly view of the account for the decision agent."""
    return {
        "equity": round(acct.equity, 2),
        "cash": round(acct.cash, 2),
        "buying_power": round(acct.buying_power, 2),
        "peak_equity": round(acct.peak_equity, 2),
        "drawdown_pct": round(acct.drawdown_pct() * 100, 2),
        "open_positions": acct.open_position_count(),
        "positions": {
            t: {
                "shares": round(p.shares, 4),
                "avg_cost": round(p.avg_cost, 2),
                "market_value": round(p.market_value, 2),
                "sector": p.sector,
            }
            for t, p in acct.positions.items()
        },
    }


@dataclass
class CyclePlan:
    """Everything the pipeline computes up to (and including) the guardrails,
    with NOTHING executed yet. This is the seam the dashboard uses to show
    proposed orders for per-order approval before any live send.

    ``approved`` are the guardrail results that passed (approved with shares > 0);
    ``results`` is every guardrail result (approved / resized / rejected).
    """

    account: AccountState
    broker: object | None
    approved: list = field(default_factory=list)
    results: list = field(default_factory=list)
    decision: dict = field(default_factory=dict)
    monitor_out: dict = field(default_factory=dict)
    all_intents: list = field(default_factory=list)


@dataclass
class PipelineOutcome:
    """Result of running the pipeline without executing. Either a ``halted`` run
    (a gate stopped it — already audited and finished) or a :class:`CyclePlan`."""

    halted: bool
    plan: "CyclePlan | None" = None
    halt_reason: str | None = None   # audit-event slug, e.g. "kill_switch_active"
    halt_message: str | None = None  # human-readable message


class Orchestrator:
    def __init__(self, config, *, assume_yes: bool = False, provider=None):
        self.cfg = config
        self.assume_yes = assume_yes
        self.db = Database(config.db_path)
        self.run_id = _now_run_id()
        self.mode = config.mode
        self.notifier = Notifier(
            config.raw.get("notifications", {}),
            audit=lambda lvl, ev, det: self.db.audit(self.run_id, lvl, ev, det),
        )
        # `provider` can be injected for tests; otherwise build the real one.
        self.provider = provider or build_provider(
            config, audit=lambda lvl, ev, det: self.db.audit(self.run_id, lvl, ev, det)
        )

    # -- kill switch -------------------------------------------------------
    def kill_switch_active(self) -> bool:
        # Re-read config each time so an operator flipping the flag/file mid-run
        # is honoured immediately.
        fresh = load_config(self.cfg.path)
        return fresh.kill_switch_enabled

    # -- broker selection --------------------------------------------------
    def _build_broker(self):
        # preview/live read from (and place to) the real Robinhood MCP, scoped to
        # the active account (defaults to `agentic` for trading modes).
        ex = self.cfg.execution
        return RobinhoodMCPBroker(
            mcp_url=ex.get("mcp_url"), model=self.cfg.models.get("decision_agent", "claude-sonnet-4-6"),
            token=self.cfg.env("ROBINHOOD_MCP_TOKEN"),
            account_number=self.cfg.account_number,
            audit=lambda lvl, ev, det: self.db.audit(self.run_id, lvl, ev, det),
        )

    async def _read_account(self, broker) -> AccountState | None:
        peak = self.db.get_peak_equity(fallback=0.0, account=self.cfg.account_number)
        return await broker.get_account(peak_equity=peak, sectors=self.cfg.sectors)

    async def check_broker(self) -> None:
        """Preflight the Robinhood MCP connection: read the selected account and
        print equity/cash/positions, or an actionable error. Places nothing."""
        acct = self.cfg.account_number or "default"
        print(f"🔌 Checking Robinhood MCP connection (account {acct})...")
        broker = self._build_broker()
        account = await self._read_account(broker)
        if account is not None:
            print(f"✅ Connected — account {acct}: equity ${account.equity:,.2f} · "
                  f"cash ${account.cash:,.2f} · {account.open_position_count()} positions")
            return
        fail = getattr(broker, "last_read_failure", None) or {}
        if fail.get("kind") == "no_response":
            print("❌ No data from the Robinhood MCP — the connection likely needs "
                  "re-authentication. In an interactive `claude` session started from "
                  "this repo, run `/mcp` and reconnect robinhood-trading, then retry.")
        else:
            print("❌ Got a response but no valid account data — re-run; if it "
                  "persists, reconnect robinhood-trading via `/mcp`.")
        if fail.get("error"):
            print(f"   detail: {fail['error']}")

    # -- recommend-mode account (read-only; never trades) ------------------
    def _hypothetical_account(self) -> AccountState:
        """A clean book sized from recommend.hypothetical_cash, so recommendation
        sizing is meaningful when no real account is connected."""
        cash = float(self.cfg.recommend.get("hypothetical_cash", 10000.0))
        return AccountState(equity=cash, cash=cash, buying_power=cash,
                            peak_equity=cash, positions={})

    def _live_account_enabled(self) -> bool:
        """Read the real Robinhood account when the config flag is on, or a
        non-interactive bearer token is configured."""
        return bool(self.cfg.execution.get("read_live_account", False)
                    or self.cfg.env("ROBINHOOD_MCP_TOKEN"))

    def _robinhood_broker(self) -> RobinhoodMCPBroker:
        # Read-only broker for recommend/explain, scoped to the active account
        # (defaults to `individual` for advice modes).
        return RobinhoodMCPBroker(
            mcp_url=self.cfg.execution.get("mcp_url"),
            model=self.cfg.models.get("decision_agent", "claude-sonnet-4-6"),
            token=self.cfg.env("ROBINHOOD_MCP_TOKEN"),
            account_number=self.cfg.account_number,
            audit=lambda lvl, ev, det: self.db.audit(self.run_id, lvl, ev, det),
        )

    async def _recommend_account(self) -> AccountState:
        """Use the real Robinhood account READ-ONLY when enabled and the read
        succeeds; otherwise recommend against a hypothetical book. Sets
        ``self._account_is_real`` so hypothetical numbers are never snapshotted
        into the P&L/positions tables (keeps the dashboard real)."""
        self._account_is_real = False
        if self._live_account_enabled():
            acct = await self._robinhood_broker().get_account(
                peak_equity=self.db.get_peak_equity(
                    fallback=0.0, account=self.cfg.account_number),
                sectors=self.cfg.sectors)
            if acct is not None:
                print("   (using your live Robinhood holdings, read-only)")
                self._account_is_real = True
                return acct
            self.db.audit(self.run_id, "WARN",
                          "recommend_real_read_failed_fallback_hypothetical", {})
            print("   (couldn't read Robinhood — falling back to a hypothetical book)")
        else:
            print("   (live account read disabled — using a hypothetical book)")
        return self._hypothetical_account()

    # -- autonomous-trading safety gates -----------------------------------
    def _trading_safety_halt(self, account: AccountState) -> tuple[str, dict, str] | None:
        """Pre-trade safety checks for preview/live. Returns ``None`` when it is
        safe to proceed, else ``(audit_event, detail, human_message)`` and the
        caller HALTS the cycle before any order is proposed or placed.

        Two independent gates, on top of Robinhood's own agentic_allowed server
        rule and the deterministic guardrails:
          1. Equity guard — refuse to trade an account that reads RICHER than its
             configured ceiling (a cheap structural block against mis-routing an
             order into the large individual account).
          2. Read sanity — if the account previously held position value but this
             read shows zero positions, the model-mediated read likely
             under-reported; skip trading rather than risk over-concentrating a
             name we already hold. Reads are safe to retry, so re-running is fine.
        """
        active = self.cfg.active_account or {}
        acct_no = self.cfg.account_number
        guard = active.get("max_equity_guard")
        if guard is not None and account.equity > float(guard):
            return (
                "trading_equity_guard",
                {"account": acct_no, "equity": round(account.equity, 2),
                 "max_equity_guard": float(guard)},
                f"EQUITY GUARD: account {acct_no or 'default'} reads "
                f"${account.equity:,.2f}, above its ${float(guard):,.2f} ceiling — "
                "refusing to trade (possible mis-route to the wrong account). "
                "No orders placed.",
            )
        if not account.positions:
            prior = self.db.prior_pnl_for_account(acct_no, self.run_id)
            if prior:
                prior_equity = float(prior.get("equity") or 0.0)
                prior_pos_value = prior_equity - float(prior.get("cash") or 0.0)
                if prior_pos_value > max(1.0, 0.02 * prior_equity):
                    return (
                        "trading_read_sanity_skip",
                        {"account": acct_no,
                         "prior_position_value": round(prior_pos_value, 2),
                         "this_read_positions": 0},
                        "READ SANITY: this account previously held "
                        f"~${prior_pos_value:,.2f} of positions but the current read "
                        "shows 0 — the account read likely under-reported. Skipping "
                        "trading this cycle (re-run to retry). No orders placed.",
                    )
        return None

    # -- main cycle --------------------------------------------------------
    async def build_plan(self) -> PipelineOutcome:
        """Run the pipeline up to and including the guardrails, executing NOTHING.

        Returns a :class:`PipelineOutcome`: either ``halted`` (a gate stopped the
        run — it is audited and finished here) or one carrying a :class:`CyclePlan`
        of guardrail-approved orders. In the non-halt case the run is left OPEN so
        a later :meth:`execute_approved` (dashboard) or :meth:`run_cycle` (CLI) can
        finish it. Executing nothing means the dashboard can present the proposed
        orders for per-order approval before any live send."""
        cfg = self.cfg
        db = self.db
        db.start_run(self.run_id, self.mode,
                     notes=f"backend={llm.backend_name()}")
        self._banner()

        # 0) KILL SWITCH gate (before anything trades). Recommend mode never
        # trades, so it is exempt — advice is always safe to produce.
        if self.mode != "recommend" and self.kill_switch_active():
            db.audit(self.run_id, "HALT", "kill_switch_active", {"mode": self.mode})
            print("\n🛑 KILL SWITCH ACTIVE — halting all trading. No orders will be placed.")
            db.finish_run(self.run_id)
            return PipelineOutcome(
                halted=True, halt_reason="kill_switch_active",
                halt_message="Kill switch is active — halting all trading. No orders placed.")

        # 0b) MARKET-DAY gate (trading modes only). The daily schedule fires on
        # weekdays; skip weekends and NYSE holidays so a firing on a closed market
        # cleanly no-ops instead of attempting to trade. Advice modes may run any
        # day, so they are exempt.
        if self.mode in ("preview", "live") and not market.is_trading_day():
            db.audit(self.run_id, "INFO", "market_closed_skip",
                     {"mode": self.mode, "date": market.now_et().date().isoformat()})
            print("\n📆 Market closed today (weekend/holiday) — no trading. "
                  "No orders placed.")
            db.finish_run(self.run_id)
            return PipelineOutcome(
                halted=True, halt_reason="market_closed_skip",
                halt_message="Market is closed today (weekend/holiday) — no trading.")

        # Recommend mode reads no broker it could trade through; it uses the real
        # account read-only when connected, else a hypothetical book.
        self._account_is_real = True
        if self.mode == "recommend":
            broker = None
            account = await self._recommend_account()
        else:
            broker = self._build_broker()
            account = await self._read_account(broker)
        if account is None:
            fail = getattr(broker, "last_read_failure", None) or {}
            if fail.get("kind") == "no_response":
                msg = ("Robinhood MCP returned no data across "
                       f"{fail.get('attempts', 3)} attempts — the background connection "
                       "likely needs re-authentication. In an interactive `claude` session "
                       "started from this repo, run `/mcp` and reconnect robinhood-trading, "
                       "then re-run. (Preview/live never fall back to a hypothetical book.)")
                if fail.get("error"):
                    msg += f"\n   detail: {fail['error']}"
            else:
                msg = ("Could not read the account from the Robinhood MCP "
                       f"(got a response but no valid account data across "
                       f"{fail.get('attempts', 3)} attempts). Re-run; if it persists, "
                       "reconnect robinhood-trading via `/mcp`.")
            db.audit(self.run_id, "ERROR", "account_read_failed",
                     {"mode": self.mode, **fail})
            print(f"❌ {msg}")
            db.finish_run(self.run_id)
            return PipelineOutcome(
                halted=True, halt_reason="account_read_failed",
                halt_message=msg + " No orders placed.")

        # Snapshot account state up front — REAL accounts only. A hypothetical
        # recommend book must never pollute the P&L/positions tables/dashboard.
        acct_no = self.cfg.account_number
        if self._account_is_real:
            db.snapshot_positions(self.run_id, account.positions, account=acct_no)
            db.snapshot_pnl(
                self.run_id, equity=account.equity, cash=account.cash,
                buying_power=account.buying_power, peak_equity=account.peak_equity,
                drawdown_pct=account.drawdown_pct() * 100, account=acct_no,
            )
        acct_label = ""
        if self.cfg.active_account:
            acct_label = (f" | account {self.cfg.active_account['role']}"
                          f" ({acct_no or 'default'})")
        print(f"\n📊 Account: equity ${account.equity:,.2f} | cash ${account.cash:,.2f} | "
              f"positions {account.open_position_count()} | "
              f"drawdown {account.drawdown_pct()*100:.2f}%{acct_label}")

        # ----- AUTONOMOUS-TRADING SAFETY GATES (trading modes only) -----------
        # These run before any order is proposed/placed. Recommend/explain never
        # trade, so they are exempt.
        if self.mode in ("preview", "live"):
            halt = self._trading_safety_halt(account)
            if halt is not None:
                db.audit(self.run_id, "HALT", halt[0], halt[1])
                print(f"\n🛑 {halt[2]}")
                db.finish_run(self.run_id)
                return PipelineOutcome(halted=True, halt_reason=halt[0],
                                       halt_message=halt[2])

        limits = cfg.risk
        limits_dict = cfg.raw.get("risk", {})
        strategy = cfg.strategy
        trades_today = db.trades_today()
        trades_remaining = max(0, limits.daily_max_trades - trades_today)

        # =================================================================
        # MONITOR open positions FIRST (exits are risk-reducing & time-sensitive)
        # =================================================================
        exit_intents: list[OrderIntent] = []
        monitor_out: dict = {}
        if account.positions:
            mon_positions = self._positions_for_monitor(account)
            monitor_out = await run_monitor(
                mon_positions, strategy, cfg.models.get("monitor_agent", "claude-sonnet-4-6"),
                db=db, run_id=self.run_id,
            )
            exit_intents = self._exits_to_intents(monitor_out.get("exits", []), account)
            if exit_intents:
                print(f"\n🔔 Monitor proposes {len(exit_intents)} exit(s).")

        # =================================================================
        # DYNAMIC DISCOVERY — the ONLY source of tickers. There is no fixed
        # watchlist to fall back to, so a scan that yields nothing HALTS the
        # run rather than trading a stale list of names.
        # =================================================================
        universe: list[str] = []
        if cfg.discovery.get("dynamic_discovery", False):
            try:
                from data.screener import discover_candidates
                universe = discover_candidates(
                    cfg, self.provider, cfg.discovery.get("max_discovered", 20))
                if universe:
                    db.audit(self.run_id, "INFO", "discovery_added", {"tickers": universe})
            except Exception as e:
                db.audit(self.run_id, "WARN", "discovery_failed", {"error": str(e)})
                print(f"\n⚠️  Discovery failed: {e}")
        else:
            print("\n⚠️  discovery.dynamic_discovery is OFF — there is no fixed "
                  "universe, so this run has nothing to research.")

        if not universe:
            db.audit(self.run_id, "WARN", "no_universe_halt", {})
            print("\n🛑 Market scan produced no tradable candidates. Halting this "
                  "cycle — nothing researched, nothing traded.")
            db.finish_run(self.run_id)
            return PipelineOutcome(
                halted=True, halt_reason="no_universe_halt",
                halt_message="Market scan produced no tradable candidates — "
                             "nothing researched, no orders placed.")

        # =================================================================
        # RESEARCH -> ANALYSIS -> DECISION
        # =================================================================
        print(f"\n🔬 Researching {len(universe)} tickers (data backend protects AV quota)...")
        research = await run_research(
            universe, self.provider,
            cfg.models.get("research_agent", "claude-haiku-4-5-20251001"),
            db=db, run_id=self.run_id,
            llm_enrichment=cfg.research.get("llm_enrichment", False),
            concurrency=int(cfg.research.get("concurrency", 5)),
        )
        analysis = await run_analysis(
            research, cfg.models.get("analysis_agent", "claude-sonnet-4-6"),
            strategy, weights=cfg.analysis.get("weights"),
            factor_weights=cfg.analysis.get("factor_weights"), db=db, run_id=self.run_id,
        )
        print("📈 Top composites: " + ", ".join(
            f"{a['ticker']}={a.get('composite_score','?')}({a.get('swing_setup','?')})"
            for a in analysis[:6]
        ))

        decision = await run_decision(
            analysis, _account_summary(account), limits_dict, strategy, trades_remaining,
            cfg.models.get("decision_agent", "claude-sonnet-4-6"), db=db, run_id=self.run_id,
        )
        buy_intents = self._decision_to_intents(decision.get("orders", []), account)
        print(f"🧠 Decision: {decision.get('market_view','')!r} -> {len(buy_intents)} proposed order(s)")

        # =================================================================
        # RISK GUARDRAILS — the mandatory gate. Exits validated first.
        # =================================================================
        all_intents = exit_intents + buy_intents
        approved: list = []
        results: list = []
        if all_intents:
            # Recommend never trades, so it shows normal guardrail sizing rather
            # than a "kill switch -> all rejected" view.
            kill = False if self.mode == "recommend" else self.kill_switch_active()
            results = validate_batch(
                all_intents, account, limits, trades_today,
                kill_switch=kill,
                # Guardrails write every approve/modify/reject to the audit_log table.
                audit=lambda lvl, ev, det: db.audit(self.run_id, lvl, ev, det),
            )
            print("\n🛡️  Guardrail review:")
            for res in results:
                db.log_decision(self.run_id, res)
                print("   " + res.summary())
                if res.approved and res.approved_shares > 0:
                    approved.append(res)

        # Pipeline complete — hand the guardrail-approved orders back to the
        # caller. Execution (if any) happens in run_cycle / execute_approved.
        return PipelineOutcome(halted=False, plan=CyclePlan(
            account=account, broker=broker, approved=approved, results=results,
            decision=decision, monitor_out=monitor_out, all_intents=all_intents))

    async def run_cycle(self) -> None:
        """CLI entry: build the plan, then handle each mode exactly as before —
        recommend prints advice and stops; preview/live execute the full approved
        batch (preview confirms per order via the interactive prompt)."""
        outcome = await self.build_plan()
        if outcome.halted:
            return
        plan = outcome.plan

        # =================================================================
        # RECOMMEND MODE — advice only. Emit the report and STOP: no execution,
        # no fills / trades / trade_plans, no account mutation.
        # =================================================================
        if self.mode == "recommend":
            self._print_recommendations(plan.decision, plan.monitor_out, plan.results)
            self.db.finish_run(self.run_id)
            print("\n✅ Recommendation run complete — nothing was traded.")
            return

        if not plan.all_intents:
            print("\n✅ No actions proposed this cycle. Done.")
            self.db.finish_run(self.run_id)
            return

        if not plan.approved:
            print("\n✅ Nothing cleared the guardrails. No orders sent.")
            self.db.finish_run(self.run_id)
            return

        # =================================================================
        # EXECUTION
        # =================================================================
        await self._execute(plan.broker, plan.approved, plan.account)

        # Post-execution: refresh positions, summarise the run, send the digest.
        await self._finalize_run(plan.broker)

        self.db.finish_run(self.run_id)
        print(f"\n✅ Cycle complete (run_id={self.run_id}, mode={self.mode}).")

    async def execute_approved(self, plan: CyclePlan, approved_subset: list, *,
                               confirm_callback=None) -> None:
        """Place a user-approved SUBSET of a plan's guardrail-approved orders,
        then refresh positions, summarise and finish the run.

        Used by the dashboard AFTER per-order approval. The caller must have
        already gathered any live confirmation (construct the Orchestrator with
        ``assume_yes=True``); ``confirm_callback`` may auto-approve the per-order
        preview confirmation since the UI already collected the user's choice.
        Every order still re-checks the kill switch inside the Executor and only
        guardrail-approved intents can appear in ``approved_subset``."""
        await self._execute(plan.broker, approved_subset, plan.account,
                            confirm_callback=confirm_callback)
        await self._finalize_run(plan.broker)
        self.db.finish_run(self.run_id)

    def discard_plan(self) -> None:
        """Abandon a built-but-unexecuted plan and close its run (dashboard
        'cancel' after previewing proposed orders)."""
        self.db.audit(self.run_id, "INFO", "plan_discarded", {})
        self.db.finish_run(self.run_id)

    # =====================================================================
    # EXPLAIN MODE — single-ticker deep research. READ-ONLY: no discovery,
    # no decision agent, no order intents, no guardrail/executor path.
    # =====================================================================
    async def run_explain(self, ticker: str) -> None:
        cfg, db = self.cfg, self.db
        db.start_run(self.run_id, "explain",
                     notes=f"backend={llm.backend_name()} ticker={ticker}")
        self._banner()
        print(f"\n🔎 Deep research: {ticker} "
              "(read-only — nothing is traded, no orders are planned)")

        # Research + analysis for exactly this ticker (watchlist not required —
        # sector/fundamentals resolve via the provider, not config `universe`).
        research_list = await run_research(
            [ticker], self.provider,
            cfg.models.get("research_agent", "claude-haiku-4-5-20251001"),
            db=db, run_id=self.run_id,
            llm_enrichment=cfg.research.get("llm_enrichment", False), concurrency=1,
        )
        research = research_list[0]
        analysis_items = await run_analysis(
            research_list, cfg.models.get("analysis_agent", "claude-haiku-4-5-20251001"),
            cfg.strategy, weights=cfg.analysis.get("weights"),
            factor_weights=cfg.analysis.get("factor_weights"), db=db, run_id=self.run_id,
        )
        analysis = analysis_items[0] if analysis_items else {}

        position = await self._position_context(ticker)
        series = await asyncio.to_thread(self.provider.get_daily_series, ticker, 260)
        payload = explain_agent.build_payload(
            ticker, research, analysis, series or [], cfg.strategy, position)
        report = await explain_agent.run_explain(
            payload, cfg.models.get("explain_agent", "claude-sonnet-4-6"),
            db=db, run_id=self.run_id,
        )

        self._print_degraded_warning(
            llm.backend_name() == "offline",
            (research.get("technicals") or {}).get("price") is None,
            "parts of this briefing")
        self._print_explain(report)
        db.finish_run(self.run_id)
        print(f"\n✅ Report saved (run_id={self.run_id}). Re-read it any time in the "
              "dashboard's Deep dive tab.")

    async def _position_context(self, ticker: str) -> dict | None:
        """READ-ONLY position context: real holdings (if connected) + stored plan."""
        position: dict | None = None
        if self._live_account_enabled():
            acct = await self._robinhood_broker().get_account(
                peak_equity=self.db.get_peak_equity(fallback=0.0), sectors=self.cfg.sectors)
            if acct is not None:
                held = acct.positions.get(ticker)
                if held and held.shares > 0:
                    price = self._ref_price(ticker, None)
                    position = {
                        "held": True, "shares": round(held.shares, 6),
                        "avg_cost": round(held.avg_cost, 2),
                        "market_value": round(held.market_value, 2),
                        "unrealized_pnl_pct": (round((price / held.avg_cost - 1) * 100, 2)
                                               if price and held.avg_cost else None),
                    }
                else:
                    position = {"held": False}
        plan = self.db.get_trade_plans().get(ticker)
        if plan:
            position = {**(position or {"held": None}), "trade_plan": {
                k: plan.get(k) for k in
                ("entry_date", "entry_price", "stop_loss", "take_profit",
                 "max_hold_until", "thesis")}}
        return position

    def _print_explain(self, report: dict) -> None:
        snap = report.get("snapshot", {}) or {}
        name = snap.get("name") or ""
        print("\n" + "─" * 70)
        print(f"🔎 DEEP RESEARCH — {report.get('ticker')}"
              + (f"  ({name})" if name else "")
              + f"  ·  as of {report.get('as_of', '—')}")
        print("─" * 70)
        price = snap.get("price")
        mcap = snap.get("market_cap")
        print(f"  {snap.get('sector', 'Unknown')}"
              + (f" · ${price:,.2f}" if isinstance(price, (int, float)) else "")
              + (f" · mkt cap ${mcap/1e9:,.1f}B" if isinstance(mcap, (int, float)) else ""))

        v = report.get("verdict") or {}
        if v.get("action"):
            icons = {"buy": "🟢", "add": "🔵", "hold": "⚪", "watch": "👁",
                     "trim": "🟠", "sell": "🔴", "avoid": "⛔"}
            print(f"\n  🎯 VERDICT: {icons.get(v['action'], '•')} "
                  f"{str(v['action']).upper()}  (confidence {v.get('confidence', '—')})")
            if v.get("rationale"):
                print(f"     {v['rationale']}")
            # Forward lean + estimates (rough, not a prediction).
            lean_icon = {"bullish": "📈", "bearish": "📉", "neutral": "➖"}
            fwd = []
            if v.get("lean"):
                fwd.append(f"{lean_icon.get(v['lean'], '')} lean {v['lean']}")
            if isinstance(v.get("expected_return_pct"), (int, float)):
                fwd.append(f"est. return ~{v['expected_return_pct']:+.0f}%")
            if isinstance(v.get("analyst_upside_pct"), (int, float)):
                fwd.append(f"analyst upside {v['analyst_upside_pct']:+.0f}%")
            if isinstance(v.get("expected_value_pct"), (int, float)):
                fwd.append(f"scenario EV {v['expected_value_pct']:+.0f}%")
            if v.get("risk_reward_r"):
                fwd.append(f"{v['risk_reward_r']:.1f}R")
            if fwd:
                print("     " + "  ·  ".join(fwd))
            for reason in (v.get("reasons") or [])[:4]:
                print(f"     • {reason}")
            plan = v.get("suggested_plan")
            if plan:
                print(f"     suggested plan: stop ${plan.get('stop')} / target "
                      f"${plan.get('target')} / by {plan.get('max_hold_until')} "
                      f"(informational only)")

        if snap.get("summary"):
            print(f"\n  📌 {snap['summary']}")

        pa = report.get("price_action", {}) or {}
        rets = pa.get("returns", {}) or {}
        def fp(v):  # noqa: E731
            return f"{v:+.1f}%" if isinstance(v, (int, float)) else "—"
        print(f"\n  📈 Returns: 1d {fp(rets.get('d1'))} · 5d {fp(rets.get('d5'))} · "
              f"1m {fp(rets.get('m1'))} · 3m {fp(rets.get('m3'))} · YTD {fp(rets.get('ytd'))}")
        if pa.get("summary"):
            print(f"     {pa['summary']}")

        wim = report.get("why_it_moved", {}) or {}
        print(f"\n  📰 Why it moved: {wim.get('summary', '—')}")
        for d in (wim.get("drivers") or []):
            print(f"     • {d.get('claim', '')}  — “{d.get('headline', '')}”"
                  + (f" ({d.get('date')})" if d.get("date") else ""))

        e = report.get("earnings", {}) or {}
        if e.get("next_date"):
            flag = "  ⚠️ EVENT RISK inside the swing horizon" if e.get("event_risk") else ""
            print(f"\n  🗓  Earnings: {e['next_date']}"
                  + (f" (in ~{e['days_until']}d)" if e.get("days_until") is not None else "")
                  + flag)
        else:
            print("\n  🗓  Earnings: date unavailable")
        if e.get("note"):
            print(f"     {e['note']}")

        f = report.get("fundamentals", {}) or {}
        def fv(k, label, money=False):  # noqa: E731
            v = f.get(k)
            if not isinstance(v, (int, float)):
                return f"{label} n/a"
            return f"{label} {'$' + format(v, ',.0f') if money else round(v, 2)}"
        print("\n  🧾 " + " · ".join([
            fv("pe_ratio", "P/E"), fv("forward_pe", "fwd P/E"), fv("ps_ratio", "P/S"),
            fv("eps_growth_yoy", "EPS growth%"), fv("debt_to_equity", "D/E"),
            fv("analyst_upside_pct", "analyst upside%"),
            fv("free_cash_flow", "FCF", money=True)]))
        if f.get("note"):
            print(f"     {f['note']}")

        scenarios = report.get("scenarios") or []
        if scenarios:
            print("\n  🔮 Scenarios (probability-weighted — rough estimates, not a forecast):")
            for s in scenarios:
                tl = s.get("target_level")
                prob = s.get("probability")
                prob_s = f" [~{int(round(prob * 100))}%]" if isinstance(prob, (int, float)) else ""
                print(f"     • {str(s.get('name', '')).upper():4}{prob_s} if {s.get('condition', '?')}"
                      + (f" → toward ${tl:,.2f}" if isinstance(tl, (int, float)) else ""))
                if s.get("narrative"):
                    print(f"        {s['narrative']}")
                print(f"        confirm: {s.get('confirm', '—')} | "
                      f"invalidate: {s.get('invalidate', '—')}")

        if report.get("risks"):
            print("\n  ⚠️  Key risks:")
            for r in report["risks"][:6]:
                print(f"     • {r}")
        if report.get("watch_next"):
            print("\n  👀 Watch next:")
            for w in report["watch_next"][:5]:
                print(f"     • {w}")

        pos = report.get("position")
        if pos is None:
            print("\n  💼 Your position: no account connected (read-only check skipped)")
        elif pos.get("held"):
            print(f"\n  💼 Your position: {pos.get('shares')} sh @ ${pos.get('avg_cost')} "
                  f"(unrealized {fp(pos.get('unrealized_pnl_pct'))})")
        elif pos.get("held") is False:
            print("\n  💼 Your position: not currently held")
        if isinstance(pos, dict) and pos.get("trade_plan"):
            tp = pos["trade_plan"]
            print(f"     stored plan: stop ${tp.get('stop_loss')} / target "
                  f"${tp.get('take_profit')} / by {tp.get('max_hold_until')}")

        print(f"\n  {report.get('disclaimer', '')}")

    # -- execution ---------------------------------------------------------
    async def _execute(self, broker, approved, start_account: AccountState, *,
                       confirm_callback=None) -> None:
        if self.mode == "live" and not self.assume_yes:
            self._live_warning()

        executor = Executor(
            broker=broker, mode=self.mode, db=self.db, run_id=self.run_id,
            exec_cfg=self.cfg.execution, kill_switch_check=self.kill_switch_active,
            confirm_callback=confirm_callback,
        )
        verb = {"preview": "Previewing", "live": "PLACING LIVE"}[self.mode]
        print(f"\n💸 {verb} {len(approved)} order(s):")
        for res in approved:
            intent = res.intent
            ref_price = intent.price or 0.0
            # Pre-trade share count for this name (used to reconcile unclear fills).
            held = start_account.positions.get(intent.ticker)
            pre_shares = held.shares if held else 0.0
            result = await executor.execute_order(
                ticker=intent.ticker, side=intent.side, qty=res.approved_shares,
                ref_price=ref_price, notional=res.approved_usd,
                sector=intent.sector or "Unknown", pre_shares=pre_shares,
            )
            # Record/refresh the exit plan for opened/increased positions so the
            # Monitor enforces the decision's stop / target / time-stop later.
            if result.ok and intent.side == Side.BUY and intent.action in ("buy", "add"):
                fill = result.fill_price or ref_price
                self.db.upsert_trade_plan(
                    ticker=intent.ticker, run_id=self.run_id,
                    entry_price=fill, stop_loss=intent.stop_loss,
                    take_profit=intent.take_profit, max_hold_until=intent.max_hold_until,
                    thesis=intent.rationale, peak_price=fill,  # seed the chandelier trail
                )
            tag = "✓" if result.ok else "✗"
            print(f"   {tag} {intent.side.value} {res.approved_shares:g} {intent.ticker} "
                  f"-> {result.status} @ ${result.fill_price:.2f}")

    async def _finalize_run(self, broker) -> None:
        """Refresh the positions table from the broker, summarise, and notify."""
        account = await self._read_account(broker)
        if account is not None:
            acct_no = self.cfg.account_number
            self.db.snapshot_positions(self.run_id, account.positions, account=acct_no)
            self.db.snapshot_pnl(
                self.run_id, equity=account.equity, cash=account.cash,
                buying_power=account.buying_power, peak_equity=account.peak_equity,
                drawdown_pct=account.drawdown_pct() * 100, account=acct_no,
            )
        summary = self.db.get_run_summary(self.run_id)
        print("\n📋 Run summary: "
              f"executed {summary['trades_executed']} "
              f"(buys {summary['buys']}, sells {summary['sells']}) | "
              f"bought ${summary['gross_bought']:,.2f}, sold ${summary['gross_sold']:,.2f}"
              + (f" | ⚠️ {summary['needs_review']} need review" if summary['needs_review'] else ""))
        self.notifier.send_digest(self.run_id, self.mode, summary, account)

    # -- degraded-run warning (shared by recommend + explain) ---------------
    @staticmethod
    def _print_degraded_warning(backend_offline: bool, no_data: bool,
                                noun: str, extra: str = "") -> None:
        if not (backend_offline or no_data):
            return
        print(f"\n⚠️  DEGRADED RUN — {noun} are placeholders:")
        if backend_offline:
            print("     • LLM backend offline → `pip install -r requirements.txt` "
                  "and set ANTHROPIC_API_KEY in .env")
        if no_data:
            print("     • No market data → install yfinance (no key) or set "
                  "ALPHAVANTAGE_API_KEY in .env")
        if extra:
            print(f"     {extra}")

    # -- recommendation report (recommend mode) ---------------------------
    def _print_recommendations(self, decision: dict, monitor_out: dict, results: list) -> None:
        """Readable advice digest. The full ranked view lives in the dashboard."""
        orders = decision.get("orders", []) or []
        res_by_ticker = {r.intent.ticker: r for r in results}

        # Warn loudly when the run is degraded (no LLM and/or no market data), so
        # an all-"pass" / score-50 result is never a mystery.
        scores = [o.get("_score") for o in orders if o.get("_score") is not None]
        no_data = bool(scores) and all(s == 50 for s in scores)
        self._print_degraded_warning(
            llm.backend_name() == "offline", no_data, "recommendations",
            extra="Every score defaults to ~50 (below the buy threshold), so all show PASS.")

        actionable = [o for o in orders if str(o.get("action", "")).lower() in ("buy", "add", "trim")]
        holds = [o for o in orders if str(o.get("action", "")).lower() == "hold"]
        passes = [o for o in orders if str(o.get("action", "")).lower() == "pass"]
        exits = monitor_out.get("exits", []) or []

        print("\n" + "─" * 70)
        print("📋 RECOMMENDATIONS  (advice only, nothing traded)")
        if decision.get("market_view"):
            print(f"   Market view: {decision['market_view']}")
        print("─" * 70)

        if exits:
            print("\n  Exit suggestions (open positions):")
            for e in exits:
                print(f"   • {str(e.get('action', 'exit')).upper()} {e.get('ticker')} — "
                      f"{e.get('trigger', '')}: {e.get('reason', '')}")

        if actionable:
            print("\n  New / adjust positions:")
            for o in actionable:
                t = str(o.get("ticker", "")).upper()
                res = res_by_ticker.get(t)
                size = f"${float(o.get('target_dollar_amount', 0) or 0):,.0f}"
                if res is not None and res.rejected:
                    size += " [guardrails: REJECT]"
                elif res is not None and res.resized:
                    size += f" [guardrails: → ${res.approved_usd:,.0f}]"
                print(f"   • {str(o.get('action', '')).upper():4} {t}  "
                      f"conf {o.get('confidence', '?')}  size {size}")
                stop, tp = o.get("suggested_stop_loss"), o.get("take_profit")
                if stop or tp:
                    print(f"        stop ${stop} / target ${tp} / by {o.get('max_hold_until', '-')}")
                if o.get("rationale"):
                    print(f"        {o['rationale']}")
        else:
            print("\n  No new buys/adds/trims recommended today.")

        if holds or passes:
            print(f"\n  Hold: {len(holds)}   Pass: {len(passes)}")
        print("\n  Full ranked view + history:  open the dashboard (./start.command)")

    # -- intent construction ----------------------------------------------
    def _ref_price(self, ticker: str, fallback: float | None) -> float:
        if fallback and fallback > 0:
            return float(fallback)
        q = self.provider.get_quote(ticker)
        return float(q["price"]) if q and q.get("price") else 0.0

    def _decision_to_intents(self, orders: list[dict], account: AccountState) -> list[OrderIntent]:
        intents = []
        for o in orders:
            action = str(o.get("action", "")).lower()
            if action in ("hold", "pass"):
                continue  # not an order
            ticker = str(o.get("ticker", "")).upper()
            if not ticker:
                continue
            # Side from the explicit field, else inferred from the action.
            side = Side(str(o.get("side") or ("SELL" if action == "trim" else "BUY")).upper())
            price = self._ref_price(ticker, o.get("price"))
            target_usd = float(o.get("target_dollar_amount", o.get("usd_amount", 0)) or 0)
            shares = o.get("shares")
            # For a trim, convert the target $ into shares (capped at held shares).
            if side == Side.SELL and shares is None and target_usd > 0 and price > 0:
                held = account.positions.get(ticker)
                want = target_usd / price
                shares = round(min(want, held.shares), 6) if held else round(want, 6)
            # Confidence may arrive as 0-100 (preferred) or 0-1; store as 0-1.
            conf = float(o.get("confidence", 0) or 0)
            conf = conf / 100 if conf > 1 else conf
            intents.append(OrderIntent(
                ticker=ticker, side=side, action=action,
                usd_amount=target_usd if side == Side.BUY else 0.0, shares=shares,
                price=price, sector=o.get("sector") or self.cfg.sectors.get(ticker, "Unknown"),
                confidence=conf, rationale=o.get("rationale", ""),
                stop_loss=o.get("suggested_stop_loss"), take_profit=o.get("take_profit"),
                max_hold_until=o.get("max_hold_until"),
            ))
        return intents

    # Triggers that protect capital and are therefore exempt from the daily cap.
    PROTECTIVE_TRIGGERS = {"stop_loss", "time_stop", "thesis_break"}

    def _exits_to_intents(self, exits: list[dict], account: AccountState) -> list[OrderIntent]:
        intents = []
        for e in exits:
            ticker = str(e.get("ticker", "")).upper()
            if ticker not in account.positions:
                continue
            price = self._ref_price(ticker, e.get("current_price") or e.get("price"))
            trigger = str(e.get("trigger", "")).lower()
            # exit_full -> shares None (guardrail sells all held); exit_partial carries shares.
            intents.append(OrderIntent(
                ticker=ticker, side=Side.SELL, action=e.get("action", "exit_full"),
                shares=e.get("shares"), price=price,
                sector=account.positions[ticker].sector,
                confidence=float(e.get("confidence", 0) or 0),
                rationale=f"{trigger or 'exit'}: {e.get('reason', '')}",
                protective_exit=trigger in self.PROTECTIVE_TRIGGERS,
            ))
        return intents

    def _positions_for_monitor(self, account: AccountState) -> list[dict]:
        """Assemble each open position with its STORED exit plan (absolute
        stop/target/time-stop levels + original thesis) plus fresh price and the
        latest thesis-break signals (score, sma50, volume, sentiment)."""
        strat = self.cfg.strategy
        default_stop = strat.get("max_loss_pct", strat.get("default_stop_loss_pct", 0.10))
        default_tp = strat.get("default_take_profit_pct", 0.20)
        exit_below = strat.get("exit_below_score", 35)
        use_trail = bool(strat.get("use_trailing_stop", True))
        trail_mult = float(strat.get("trail_atr_mult", 3.0))
        plans = self.db.get_trade_plans()           # decision agent's exit plans
        scores = self.db.get_latest_scores()        # previous cycle's composites
        research = self.db.get_latest_research()     # previous cycle's technicals/sentiment
        out = []
        for t, p in account.positions.items():
            price = self._ref_price(t, None)         # fresh current price
            plan = plans.get(t, {})
            tech = (research.get(t, {}) or {}).get("technicals", {}) or {}
            news = (research.get(t, {}) or {}).get("news_sentiment", {}) or {}
            atr = tech.get("atr20")
            # Absolute exit levels: prefer the stored plan, else derive defaults
            # from entry so the rules still protect an un-planned legacy position.
            stop_level = plan.get("stop_loss")
            if stop_level is None and p.avg_cost:
                stop_level = round(p.avg_cost * (1 - default_stop), 2)
            take_level = plan.get("take_profit")
            if take_level is None and p.avg_cost:
                take_level = round(p.avg_cost * (1 + default_tp), 2)

            # CHANDELIER TRAIL — ratchet the stop up with the highest price seen
            # since entry (never down). This is what stops a winner giving back its
            # gains and, crucially, a loser riding far past its stop. Persisted so
            # the level is current on the next run and in the live path.
            peak = max(x for x in (plan.get("peak_price"), plan.get("entry_price"),
                                   p.avg_cost, price) if x is not None)
            if use_trail and atr and peak:
                trailed = round(peak - trail_mult * atr, 2)
                stop_level = max(stop_level or trailed, trailed)
                self.db.update_trail(t, stop_loss=stop_level, peak_price=peak)

            out.append({
                "ticker": t, "shares": p.shares, "avg_cost": p.avg_cost,
                "current_price": price, "market_value": p.market_value, "sector": p.sector,
                "unrealized_pnl_pct": round((price / p.avg_cost - 1) * 100, 2) if p.avg_cost else None,
                "stop_loss": stop_level,
                "take_profit": take_level,
                "max_hold_until": plan.get("max_hold_until"),
                "entry_date": plan.get("entry_date"),
                "thesis": plan.get("thesis"),
                "score": scores.get(t, 50),
                "exit_below_score": exit_below,
                "atr20": atr,
                "sma50": tech.get("sma50"),
                "above_sma200": tech.get("above_sma200"),
                "ret_3m": tech.get("ret_3m"),
                "volume_vs_avg": tech.get("volume_vs_avg"),
                "sentiment_score": news.get("aggregate_score"),
            })
        return out

    # -- console UX --------------------------------------------------------
    def _banner(self) -> None:
        print("=" * 70)
        print(f" Trading System  |  mode={self.mode.upper()}  |  run={self.run_id}")
        print(f" LLM backend: {llm.backend_name()}")
        if self.mode == "recommend":
            print(" RECOMMEND MODE: advice only — nothing is traded.")
        elif self.mode == "explain":
            print(" EXPLAIN MODE: deep research briefing — read-only, nothing is traded.")
        print("=" * 70)

    def _live_warning(self) -> None:
        print("\n" + "!" * 70)
        print(" LIVE MODE: real orders will be placed with REAL money via Robinhood.")
        print(" They have passed the deterministic guardrails, but YOU are responsible.")
        print("!" * 70)
        ans = input(" Type 'I UNDERSTAND' to proceed: ").strip()
        if ans != "I UNDERSTAND":
            raise SystemExit("Live confirmation not given — aborting.")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Multi-agent swing-trading orchestrator")
    ap.add_argument("--mode", choices=["recommend", "explain", "preview", "live"],
                    help="run mode (overrides config.yaml; default recommend)")
    ap.add_argument("--ticker", default=None,
                    help="stock symbol for --mode explain (e.g. --ticker NVDA)")
    ap.add_argument("--account", default=None,
                    help="brokerage account role from config.yaml accounts: "
                         "(e.g. individual, agentic). Default: individual for "
                         "recommend/explain, agentic for preview/live.")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive LIVE confirmation (use with care)")
    ap.add_argument("--kill", action="store_true",
                    help="create the kill-switch file and exit (emergency stop)")
    ap.add_argument("--check-broker", action="store_true",
                    help="test the Robinhood MCP connection: read the account and "
                         "print equity/cash/positions (or an actionable error), then exit")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    if args.kill:
        ks = config.path.parent / config.raw.get("kill_switch_file", "KILL_SWITCH")
        ks.write_text(f"kill switch engaged {datetime.now(timezone.utc).isoformat()}\n")
        print(f"🛑 Kill switch engaged: {ks}. Delete this file to resume trading.")
        return

    if args.mode:
        config.set_mode(args.mode)

    # Select the brokerage account for this run (multi-account routing). Advice
    # modes default to `individual`, trading modes to `agentic`, so autonomous
    # orders never touch the individual book. When no accounts: block is
    # configured the system stays single-account (MCP default). This is where
    # the per-account risk overlay is applied.
    role = args.account
    if role is None and config.accounts:
        role = "agentic" if config.mode in ("preview", "live") else "individual"
    if role:
        try:
            active = config.apply_account(role)
        except ValueError as e:
            raise SystemExit(f"error: {e}")
        number = active.get("number")
        if config.mode in ("preview", "live") and (
                not number or str(number).startswith("YOUR_")):
            raise SystemExit(
                f"error: account role {role!r} has no real account number configured "
                "(set accounts.<role>.number in config.local.yaml) — refusing to "
                "trade without an explicit account.")

    orch = Orchestrator(config, assume_yes=args.yes)

    if args.check_broker:
        asyncio.run(orch.check_broker())
        return

    if config.mode == "explain":
        ticker = (args.ticker or "").strip().upper()
        if not ticker:
            raise SystemExit("error: --mode explain requires --ticker SYMBOL "
                             "(e.g. python orchestrator.py --mode explain --ticker NVDA)")
        if not (ticker.isalpha() and 1 <= len(ticker) <= 5):
            raise SystemExit(f"error: {args.ticker!r} does not look like a US equity "
                             "symbol (1-5 letters)")
        asyncio.run(orch.run_explain(ticker))
        return

    asyncio.run(orch.run_cycle())


if __name__ == "__main__":
    main()

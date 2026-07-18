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

Strategy profiles (``--profile``, orthogonal to ``--mode``; default ``swing``):
overlay short-horizon parameter sets from config.yaml ``strategy_profiles:`` —
e.g. ``momentum`` for days-to-~2-weeks quick-profit ideas. Profiles only adjust
soft strategy/analysis/discovery parameters; hard risk limits never change.

Usage:
    python orchestrator.py --mode recommend
    python orchestrator.py --profile momentum --mode recommend
    python orchestrator.py --mode explain --ticker NVDA
    python orchestrator.py --mode preview
    python orchestrator.py --mode live --yes      # skip the extra live warning
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import datetime, timezone

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
        # preview/live read from (and place to) the real Robinhood MCP.
        ex = self.cfg.execution
        return RobinhoodMCPBroker(
            mcp_url=ex.get("mcp_url"), model=self.cfg.models.get("decision_agent", "claude-sonnet-4-6"),
            token=self.cfg.env("ROBINHOOD_MCP_TOKEN"),
            audit=lambda lvl, ev, det: self.db.audit(self.run_id, lvl, ev, det),
        )

    async def _read_account(self, broker) -> AccountState | None:
        peak = self.db.get_peak_equity(fallback=0.0)
        return await broker.get_account(peak_equity=peak, sectors=self.cfg.sectors)

    # -- recommend-mode account (read-only; never trades) ------------------
    def _hypothetical_account(self) -> AccountState:
        """A clean book sized from recommend.hypothetical_cash, so recommendation
        sizing is meaningful when no real account is connected."""
        cash = float(self.cfg.recommend.get("hypothetical_cash", 10000.0))
        return AccountState(equity=cash, cash=cash, buying_power=cash,
                            peak_equity=cash, positions={})

    async def _recommend_account(self) -> AccountState:
        """Use the real Robinhood account READ-ONLY when a token is configured and
        the read succeeds; otherwise recommend against a hypothetical book.
        Sets ``self._account_is_real`` so hypothetical numbers are never
        snapshotted into the P&L/positions tables (keeps the dashboard real)."""
        self._account_is_real = False
        if self.cfg.env("ROBINHOOD_MCP_TOKEN"):
            broker = RobinhoodMCPBroker(
                mcp_url=self.cfg.execution.get("mcp_url"),
                model=self.cfg.models.get("decision_agent", "claude-sonnet-4-6"),
                token=self.cfg.env("ROBINHOOD_MCP_TOKEN"),
                audit=lambda lvl, ev, det: self.db.audit(self.run_id, lvl, ev, det),
            )
            acct = await broker.get_account(
                peak_equity=self.db.get_peak_equity(fallback=0.0), sectors=self.cfg.sectors)
            if acct is not None:
                print("   (using your live Robinhood holdings, read-only)")
                self._account_is_real = True
                return acct
            self.db.audit(self.run_id, "WARN",
                          "recommend_real_read_failed_fallback_hypothetical", {})
        print("   (no live account — recommending against a hypothetical book)")
        return self._hypothetical_account()

    # -- main cycle --------------------------------------------------------
    async def run_cycle(self) -> None:
        cfg = self.cfg
        db = self.db
        db.start_run(self.run_id, self.mode,
                     notes=f"backend={llm.backend_name()} profile={self.cfg.profile}")
        self._banner()

        # 0) KILL SWITCH gate (before anything trades). Recommend mode never
        # trades, so it is exempt — advice is always safe to produce.
        if self.mode != "recommend" and self.kill_switch_active():
            db.audit(self.run_id, "HALT", "kill_switch_active", {"mode": self.mode})
            print("\n🛑 KILL SWITCH ACTIVE — halting all trading. No orders will be placed.")
            db.finish_run(self.run_id)
            return

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
            db.audit(self.run_id, "ERROR", "account_read_failed", {"mode": self.mode})
            print("❌ Could not read the account from the broker. Aborting cycle.")
            db.finish_run(self.run_id)
            return

        # Snapshot account state up front — REAL accounts only. A hypothetical
        # recommend book must never pollute the P&L/positions tables/dashboard.
        if self._account_is_real:
            db.snapshot_positions(self.run_id, account.positions)
            db.snapshot_pnl(
                self.run_id, equity=account.equity, cash=account.cash,
                buying_power=account.buying_power, peak_equity=account.peak_equity,
                drawdown_pct=account.drawdown_pct() * 100,
            )
        print(f"\n📊 Account: equity ${account.equity:,.2f} | cash ${account.cash:,.2f} | "
              f"positions {account.open_position_count()} | drawdown {account.drawdown_pct()*100:.2f}%")

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
        # DYNAMIC DISCOVERY (additive; silently falls back to the fixed universe)
        # =================================================================
        universe = list(cfg.universe)
        if cfg.discovery.get("dynamic_discovery", False):
            try:
                from data.screener import discover_candidates
                discovered = discover_candidates(
                    cfg, self.provider, cfg.discovery.get("max_discovered", 5))
                for t in discovered:
                    if t not in universe:
                        universe.append(t)
                if discovered:
                    db.audit(self.run_id, "INFO", "discovery_added", {"tickers": discovered})
            except Exception as e:  # never break the cycle over discovery
                db.audit(self.run_id, "WARN", "discovery_failed", {"error": str(e)})

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
            strategy, weights=cfg.analysis.get("weights"), db=db, run_id=self.run_id,
        )
        print("📈 Top composites: " + ", ".join(
            f"{a['ticker']}={a.get('composite_score','?')}({a.get('swing_setup','?')})"
            for a in analysis[:6]
        ))

        decision = await run_decision(
            analysis, _account_summary(account), limits_dict, strategy, trades_remaining,
            cfg.models.get("decision_agent", "claude-opus-4-8"), db=db, run_id=self.run_id,
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

        # =================================================================
        # RECOMMEND MODE — advice only. Emit the report and STOP: no execution,
        # no fills / trades / trade_plans, no account mutation.
        # =================================================================
        if self.mode == "recommend":
            self._print_recommendations(decision, monitor_out, results)
            db.finish_run(self.run_id)
            print("\n✅ Recommendation run complete — nothing was traded.")
            return

        if not all_intents:
            print("\n✅ No actions proposed this cycle. Done.")
            db.finish_run(self.run_id)
            return

        if not approved:
            print("\n✅ Nothing cleared the guardrails. No orders sent.")
            db.finish_run(self.run_id)
            return

        # =================================================================
        # EXECUTION
        # =================================================================
        await self._execute(broker, approved, account)

        # Post-execution: refresh positions, summarise the run, send the digest.
        await self._finalize_run(broker)

        db.finish_run(self.run_id)
        print(f"\n✅ Cycle complete (run_id={self.run_id}, mode={self.mode}).")

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
            cfg.strategy, weights=cfg.analysis.get("weights"), db=db, run_id=self.run_id,
        )
        analysis = analysis_items[0] if analysis_items else {}

        position = await self._position_context(ticker)
        series = await asyncio.to_thread(self.provider.get_daily_series, ticker, 260)
        payload = explain_agent.build_payload(
            ticker, research, analysis, series or [], cfg.strategy, position,
            profile=cfg.profile)
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
        if self.cfg.env("ROBINHOOD_MCP_TOKEN"):
            broker = RobinhoodMCPBroker(
                mcp_url=self.cfg.execution.get("mcp_url"),
                model=self.cfg.models.get("decision_agent", "claude-sonnet-4-6"),
                token=self.cfg.env("ROBINHOOD_MCP_TOKEN"),
                audit=lambda lvl, ev, det: self.db.audit(self.run_id, lvl, ev, det),
            )
            acct = await broker.get_account(
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
                  f"{str(v['action']).upper()}  (confidence {v.get('confidence', '—')}, "
                  f"{v.get('profile', 'swing')} profile)")
            if v.get("rationale"):
                print(f"     {v['rationale']}")
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
            fv("pe_ratio", "P/E"), fv("ps_ratio", "P/S"),
            fv("eps_growth_yoy", "EPS growth%"), fv("debt_to_equity", "D/E"),
            fv("free_cash_flow", "FCF", money=True)]))
        if f.get("note"):
            print(f"     {f['note']}")

        scenarios = report.get("scenarios") or []
        if scenarios:
            print("\n  🔮 Scenarios (conditional levels — NOT a forecast):")
            for s in scenarios:
                tl = s.get("target_level")
                print(f"     • {str(s.get('name', '')).upper():4} if {s.get('condition', '?')}"
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
    async def _execute(self, broker, approved, start_account: AccountState) -> None:
        if self.mode == "live" and not self.assume_yes:
            self._live_warning()

        executor = Executor(
            broker=broker, mode=self.mode, db=self.db, run_id=self.run_id,
            exec_cfg=self.cfg.execution, kill_switch_check=self.kill_switch_active,
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
                self.db.upsert_trade_plan(
                    ticker=intent.ticker, run_id=self.run_id,
                    entry_price=result.fill_price or ref_price,
                    stop_loss=intent.stop_loss, take_profit=intent.take_profit,
                    max_hold_until=intent.max_hold_until, thesis=intent.rationale,
                )
            tag = "✓" if result.ok else "✗"
            print(f"   {tag} {intent.side.value} {res.approved_shares:g} {intent.ticker} "
                  f"-> {result.status} @ ${result.fill_price:.2f}")

    async def _finalize_run(self, broker) -> None:
        """Refresh the positions table from the broker, summarise, and notify."""
        account = await self._read_account(broker)
        if account is not None:
            self.db.snapshot_positions(self.run_id, account.positions)
            self.db.snapshot_pnl(
                self.run_id, equity=account.equity, cash=account.cash,
                buying_power=account.buying_power, peak_equity=account.peak_equity,
                drawdown_pct=account.drawdown_pct() * 100,
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
        print(f"📋 RECOMMENDATIONS  ({self.cfg.profile} profile — advice only, nothing traded)")
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
        print("\n  Full ranked view + history:  streamlit run dashboard/app.py")

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
        default_stop = strat.get("default_stop_loss_pct", 0.08)
        default_tp = strat.get("default_take_profit_pct", 0.20)
        exit_below = strat.get("exit_below_score", 35)
        plans = self.db.get_trade_plans()           # decision agent's exit plans
        scores = self.db.get_latest_scores()        # previous cycle's composites
        research = self.db.get_latest_research()     # previous cycle's technicals/sentiment
        out = []
        for t, p in account.positions.items():
            price = self._ref_price(t, None)         # fresh current price
            plan = plans.get(t, {})
            tech = (research.get(t, {}) or {}).get("technicals", {}) or {}
            news = (research.get(t, {}) or {}).get("news_sentiment", {}) or {}
            # Absolute exit levels: prefer the stored plan, else derive defaults
            # from entry so the rules still protect an un-planned legacy position.
            stop_level = plan.get("stop_loss")
            if stop_level is None and p.avg_cost:
                stop_level = round(p.avg_cost * (1 - default_stop), 2)
            take_level = plan.get("take_profit")
            if take_level is None and p.avg_cost:
                take_level = round(p.avg_cost * (1 + default_tp), 2)
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
                "sma50": tech.get("sma50"),
                "volume_vs_avg": tech.get("volume_vs_avg"),
                "sentiment_score": news.get("aggregate_score"),
            })
        return out

    # -- console UX --------------------------------------------------------
    def _banner(self) -> None:
        print("=" * 70)
        print(f" Trading System  |  mode={self.mode.upper()}  |  "
              f"profile={self.cfg.profile.upper()}  |  run={self.run_id}")
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
    ap.add_argument("--profile", default=None,
                    help="strategy profile from config.yaml strategy_profiles "
                         "(e.g. swing, momentum; default: config `profile:`, i.e. swing)")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive LIVE confirmation (use with care)")
    ap.add_argument("--kill", action="store_true",
                    help="create the kill-switch file and exit (emergency stop)")
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

    # Apply the strategy profile (CLI beats the config default) BEFORE anything
    # reads strategy/analysis/discovery. Profiles never touch the hard risk: limits.
    try:
        config.apply_profile(args.profile or config.profile)
    except ValueError as e:
        raise SystemExit(f"error: {e}")

    orch = Orchestrator(config, assume_yes=args.yes)

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

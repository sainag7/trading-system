"""Deterministic risk guardrails for the swing-trading system.

THIS MODULE IS THE LAST LINE OF DEFENSE BEFORE CAPITAL IS DEPLOYED.

Rules of this module:
  * It is **plain, deterministic Python**. It performs only arithmetic and
    comparisons against the hard numeric limits in :class:`config.RiskLimits`.
  * It **never** calls an LLM, the network, or any non-deterministic service.
  * Given the same inputs it always produces the same output, so its behaviour
    is fully covered by unit tests (see ``test_guardrails.py``).
  * When anything is ambiguous it **rejects** — favour blocking over allowing.

Enforcement (thresholds read from ``config.yaml`` via :class:`config.RiskLimits`):
  0. KILL SWITCH        — if set, REJECT EVERYTHING (buys *and* sells).
  1. DRAWDOWN HALT      — if account is down >= max_account_drawdown_halt_pct from
                          its recorded peak, REJECT ALL BUYS and flag a halt
                          (risk-reducing SELLS are still allowed).
  2. daily_max_trades   — REJECT once today's executed + pending count is reached
                          (applies to buys and sells), EXCEPT capital-protecting
                          exits (stop-loss / time-stop / thesis-break), which are
                          exempt so a protective sell can always fire.
  3. malformed qty      — REJECT zero / negative / missing quantities or prices.
  4. no_trade_list      — REJECT buys/adds on listed names (see note below).
  5. max_positions      — REJECT a buy that would OPEN a brand-new name past the cap.
  6. per-trade cap      — RESIZE the order down to the cap (do NOT reject). The
                          cap is min(per_trade_max_usd, per_trade_max_pct * equity),
                          so it scales with the account.
  7. max_position_pct   — REJECT a buy that would push a single name over the cap.
  8. max_sector_pct     — REJECT a buy that would push a sector over the cap.
  9. min_cash_reserve   — REJECT a buy that would spend below the cash floor.
     buying power       — REJECT a buy that exceeds available buying power.

Design note — three distinct stops:
  * KILL SWITCH blocks *every* order so the operator can freeze instantly.
  * DRAWDOWN HALT blocks *new risk* (buys/adds) but deliberately still ALLOWS
    risk-reducing SELLS — being unable to exit while bleeding is itself dangerous.
  * The no_trade_list blocks BUYS/ADDS only; a held name can always be SOLD to
    exit. (Use the kill switch to stop sells too.) Blocking an exit would trap
    risk, which contradicts "favour rejecting" — the safer choice is to permit
    de-risking sells.

Only the per-trade cap resizes; every other breach REJECTS the order so the
upstream Decision Agent is forced to size within the caps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable

# Tolerance for floating-point comparisons (dollars). Anything within a tenth
# of a cent is treated as equal so price math doesn't cause spurious rejects.
EPS = 1e-6


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


# ---------------------------------------------------------------------------
# Domain types (shared across the whole system; imported by agents/executor)
# ---------------------------------------------------------------------------
@dataclass
class OrderIntent:
    """A proposed order, before risk validation.

    For BUYs, ``usd_amount`` is the requested notional to deploy (preferred);
    ``shares`` may be given instead and is converted using ``price``.
    For SELLs, ``shares`` is preferred (how many to sell); ``usd_amount`` is
    derived from ``shares * price`` when omitted.
    """

    ticker: str
    side: Side
    action: str = ""             # semantic label: buy|add|trim|sell (informational)
    usd_amount: float = 0.0      # requested notional in USD
    shares: float | None = None  # requested share count (optional)
    price: float = 0.0           # reference price used for sizing/valuation
    sector: str | None = None    # GICS-style sector for the sector cap
    confidence: float = 0.0      # 0..1 from the decision agent (informational)
    rationale: str = ""
    # Trade plan from the decision agent. Informational here — the guardrails do
    # NOT use these; they are carried through to execution, the trade_plans store,
    # and ultimately the Monitor agent (stop / target / time-stop).
    stop_loss: float | None = None        # suggested stop-loss price level
    take_profit: float | None = None      # suggested take-profit price level
    max_hold_until: str | None = None     # ISO date — swing time-stop
    # Capital-protecting exit (stop-loss / time-stop / thesis-break). When True
    # the order is EXEMPT from the daily-trade cap so a protective sell can always
    # fire. The kill switch still blocks it; take-profit trims are NOT protective.
    protective_exit: bool = False

    def __post_init__(self) -> None:
        self.ticker = self.ticker.upper()
        if not isinstance(self.side, Side):
            self.side = Side(str(self.side).upper())

    def requested_usd(self) -> float:
        """Best estimate of the notional the intent is asking to transact."""
        if self.usd_amount and self.usd_amount > 0:
            return float(self.usd_amount)
        if self.shares and self.price > 0:
            return float(self.shares) * float(self.price)
        return 0.0


@dataclass
class Position:
    """An open position as reported by the broker (Robinhood MCP)."""

    ticker: str
    shares: float
    avg_cost: float = 0.0
    market_value: float = 0.0    # current market value in USD
    sector: str = "Unknown"

    def __post_init__(self) -> None:
        self.ticker = self.ticker.upper()


@dataclass
class AccountState:
    """Snapshot of the agentic account, read from the broker before deciding."""

    equity: float                # total account value (cash + positions market value)
    cash: float                  # settled cash available
    buying_power: float          # spendable buying power (may exceed cash on margin)
    peak_equity: float           # highest equity ever observed (for drawdown calc)
    positions: dict[str, Position] = field(default_factory=dict)

    def open_position_count(self) -> int:
        return sum(1 for p in self.positions.values() if abs(p.shares) > EPS)

    def position_value(self, ticker: str) -> float:
        p = self.positions.get(ticker.upper())
        return p.market_value if p else 0.0

    def sector_value(self, sector: str | None) -> float:
        if not sector:
            return 0.0
        return sum(
            p.market_value
            for p in self.positions.values()
            if (p.sector or "Unknown").lower() == sector.lower()
        )

    def drawdown_pct(self) -> float:
        """Fractional drawdown from peak. 0.0 if no peak history."""
        if self.peak_equity <= EPS:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class CheckOutcome:
    """The result of one individual limit check (for the audit trail)."""

    name: str            # e.g. "max_position_pct"
    passed: bool         # did the order satisfy this check?
    detail: str          # human-readable explanation
    cap_usd: float | None = None  # the USD ceiling a cap implied, when relevant


@dataclass
class GuardrailResult:
    """Outcome of validating a single :class:`OrderIntent`."""

    intent: OrderIntent
    approved: bool
    approved_usd: float
    approved_shares: float
    resized: bool                      # True when per-trade cap reduced the order
    reasons: list[str]                 # why rejected / resized (human readable)
    checks: list[CheckOutcome]
    kill_switch_active: bool = False   # emergency stop blocked this order
    account_halted: bool = False       # drawdown halt is in effect

    @property
    def rejected(self) -> bool:
        return not self.approved

    @property
    def modified(self) -> bool:
        return self.approved and self.resized

    def summary(self) -> str:
        if self.approved and not self.resized:
            return f"APPROVE {self.intent.side.value} {self.intent.ticker} ${self.approved_usd:,.2f}"
        if self.approved and self.resized:
            return (
                f"RESIZE  {self.intent.side.value} {self.intent.ticker} "
                f"${self.intent.requested_usd():,.2f} -> ${self.approved_usd:,.2f} "
                f"({'; '.join(self.reasons)})"
            )
        return f"REJECT  {self.intent.side.value} {self.intent.ticker} ({'; '.join(self.reasons)})"


# ---------------------------------------------------------------------------
# Audit logging (optional; keeps the core pure when no callback is supplied)
# ---------------------------------------------------------------------------
def _result_detail(r: GuardrailResult) -> dict:
    return {
        "ticker": r.intent.ticker,
        "side": getattr(r.intent.side, "value", str(r.intent.side)),
        "action": r.intent.action,
        "requested_usd": round(r.intent.requested_usd(), 2),
        "approved_usd": round(r.approved_usd, 2),
        "approved_shares": round(r.approved_shares, 6),
        "reasons": r.reasons,
    }


def _log_result(audit: Callable[[str, str, dict], None] | None, r: GuardrailResult) -> None:
    """Write one guardrail decision to the audit log via ``audit(level, event, detail)``."""
    if audit is None:
        return
    detail = _result_detail(r)
    if r.rejected and r.kill_switch_active:
        audit("HALT", "guardrail_kill_switch", detail)
    elif r.rejected and r.account_halted:
        audit("HALT", "guardrail_drawdown_halt", detail)
    elif r.rejected:
        audit("WARN", "guardrail_rejected", detail)
    elif r.resized:
        audit("INFO", "guardrail_modified", detail)
    else:
        audit("INFO", "guardrail_approved", detail)


# ---------------------------------------------------------------------------
# Individual limit checks  (each is pure and independently testable)
# ---------------------------------------------------------------------------
def _resolve_sector(intent: OrderIntent, account: AccountState) -> str | None:
    if intent.sector:
        return intent.sector
    held = account.positions.get(intent.ticker)
    if held and held.sector:
        return held.sector
    return None


def check_drawdown_halt(account: AccountState, limits) -> CheckOutcome:
    """Halt-new-risk check. Triggers at or beyond the configured drawdown."""
    dd = account.drawdown_pct()
    breached = dd >= limits.max_account_drawdown_halt_pct - EPS and account.peak_equity > EPS
    return CheckOutcome(
        name="max_account_drawdown_halt_pct",
        passed=not breached,
        detail=(
            f"drawdown {dd:.2%} vs limit {limits.max_account_drawdown_halt_pct:.2%}"
            + (" -> HALT new risk" if breached else "")
        ),
    )


def check_daily_trade_limit(trades_today: int, limits) -> CheckOutcome:
    """``trades_today`` is today's executed + pending order count."""
    ok = trades_today < limits.daily_max_trades
    return CheckOutcome(
        name="daily_max_trades",
        passed=ok,
        detail=f"{trades_today} trades today vs limit {limits.daily_max_trades}",
    )


def check_no_trade_list(intent: OrderIntent, limits) -> CheckOutcome:
    blocked = intent.ticker in limits.no_trade_list
    return CheckOutcome(
        name="no_trade_list",
        passed=not blocked,
        detail=f"{intent.ticker} {'is' if blocked else 'is not'} on the no-trade list",
    )


def check_max_positions(intent: OrderIntent, account: AccountState, limits) -> CheckOutcome:
    """Only blocks orders that would OPEN a brand-new position past the cap."""
    already_held = abs(account.position_value(intent.ticker)) > EPS or (
        intent.ticker in account.positions
        and abs(account.positions[intent.ticker].shares) > EPS
    )
    count = account.open_position_count()
    would_open_new = not already_held
    # ``None`` == no cap: the decision agent decides how many names to hold, so a
    # new position can never be blocked on count alone.
    unlimited = limits.max_positions is None
    ok = unlimited or (not would_open_new) or count < limits.max_positions
    cap_txt = "unlimited" if unlimited else str(limits.max_positions)
    return CheckOutcome(
        name="max_positions",
        passed=ok,
        detail=(
            f"{count} open positions vs limit {cap_txt}; "
            f"{'new position' if would_open_new else 'existing position (add)'}"
        ),
    )


# ---------------------------------------------------------------------------
# Order validation
# ---------------------------------------------------------------------------
def validate_order(
    intent: OrderIntent,
    account: AccountState,
    limits,
    trades_today: int = 0,
    *,
    kill_switch: bool = False,
    audit: Callable[[str, str, dict], None] | None = None,
) -> GuardrailResult:
    """Validate (and if necessary resize/reject) a single order intent.

    Args:
        intent: the proposed order.
        account: current account snapshot from the broker.
        limits: a :class:`config.RiskLimits` (or anything with the same fields).
        trades_today: today's executed + pending order count.
        kill_switch: emergency stop; when True every order is rejected.
        audit: optional ``(level, event, detail)`` sink; each decision is logged.

    Returns:
        A :class:`GuardrailResult` describing approval / resize / rejection and
        the per-limit detail for the audit log.
    """
    checks: list[CheckOutcome] = []
    reasons: list[str] = []

    def finalize(result: GuardrailResult) -> GuardrailResult:
        _log_result(audit, result)
        return result

    def reject(*, kill: bool = False, halted: bool = False) -> GuardrailResult:
        return finalize(GuardrailResult(
            intent=intent, approved=False, approved_usd=0.0, approved_shares=0.0,
            resized=False, reasons=reasons or ["rejected"], checks=checks,
            kill_switch_active=kill, account_halted=halted,
        ))

    def approve(approved_usd: float, approved_shares: float, resized: bool,
                halted: bool = False) -> GuardrailResult:
        return finalize(GuardrailResult(
            intent=intent, approved=True, approved_usd=approved_usd,
            approved_shares=approved_shares, resized=resized,
            reasons=reasons or (["approved (resized)"] if resized else ["approved"]),
            checks=checks, account_halted=halted,
        ))

    # --- 0. KILL SWITCH: blocks everything, buys and sells alike -----------
    if kill_switch:
        checks.append(CheckOutcome("kill_switch", False, "global kill switch is ACTIVE"))
        reasons.append("global kill switch active — all trading halted")
        return reject(kill=True)

    # --- 1. DRAWDOWN HALT (only blocks buys; computed up front for the flag)-
    dd_check = check_drawdown_halt(account, limits)
    checks.append(dd_check)
    account_halted = not dd_check.passed

    # --- 2. DAILY TRADE COUNT (buys and sells; protective exits exempt) -----
    daily_check = check_daily_trade_limit(trades_today, limits)
    checks.append(daily_check)
    if not daily_check.passed:
        if intent.protective_exit:
            checks.append(CheckOutcome(
                "daily_max_trades_exempt", True,
                "protective exit (stop/time/thesis) — exempt from the daily cap",
            ))
        else:
            reasons.append(f"daily trade limit reached ({trades_today}/{limits.daily_max_trades})")
            return reject(halted=account_halted)

    # =======================================================================
    # SELL / TRIM path — risk-reducing, allowed even during a drawdown halt
    # =======================================================================
    if intent.side == Side.SELL:
        held = account.positions.get(intent.ticker)
        held_shares = held.shares if held else 0.0
        if held_shares <= EPS:
            reasons.append(f"no open position in {intent.ticker} to sell")
            checks.append(CheckOutcome("position_exists", False, "no shares held"))
            return reject(halted=account_halted)

        requested_shares = intent.shares if intent.shares is not None else held_shares
        if requested_shares <= EPS:
            reasons.append("malformed sell quantity (zero/negative shares)")
            checks.append(CheckOutcome("sell_quantity", False, f"shares={requested_shares}"))
            return reject(halted=account_halted)

        approved_shares = min(requested_shares, held_shares)
        resized = approved_shares < requested_shares - EPS
        if resized:
            reasons.append(
                f"sell size reduced to held shares ({requested_shares:g} -> {approved_shares:g})"
            )
        checks.append(CheckOutcome(
            "position_exists", True,
            f"holding {held_shares:g} shares; selling {approved_shares:g}",
        ))
        price = intent.price or (held.market_value / held_shares if held_shares else 0.0)
        return approve(approved_shares * price, approved_shares, resized, halted=account_halted)

    # =======================================================================
    # BUY / ADD path — opens or increases risk; fully constrained
    # =======================================================================
    # New risk is blocked while the account is in a drawdown halt.
    if account_halted:
        reasons.append(dd_check.detail)
        return reject(halted=True)

    # No-trade list blocks buys (you may always SELL to exit, but never buy).
    nt = check_no_trade_list(intent, limits)
    checks.append(nt)
    if not nt.passed:
        reasons.append(f"{intent.ticker} is on the no-trade list")
        return reject()

    # Max number of concurrent positions (blocks opening a brand-new name).
    mp = check_max_positions(intent, account, limits)
    checks.append(mp)
    if not mp.passed:
        reasons.append(
            f"max positions reached ({account.open_position_count()}/{limits.max_positions})"
        )
        return reject()

    # --- malformed / zero / negative quantities & prices -------------------
    price = float(intent.price)
    if price <= EPS:
        reasons.append("missing/invalid reference price — cannot size buy")
        checks.append(CheckOutcome("price", False, f"price={price}"))
        return reject()
    if intent.shares is not None and intent.shares < 0:
        reasons.append("malformed buy quantity (negative shares)")
        checks.append(CheckOutcome("buy_quantity", False, f"shares={intent.shares}"))
        return reject()
    if intent.usd_amount and intent.usd_amount < 0:
        reasons.append("malformed buy notional (negative usd_amount)")
        checks.append(CheckOutcome("buy_notional", False, f"usd_amount={intent.usd_amount}"))
        return reject()
    if account.equity <= EPS:
        reasons.append("non-positive account equity — cannot size buy")
        checks.append(CheckOutcome("equity", False, f"equity={account.equity}"))
        return reject()
    requested_usd = intent.requested_usd()
    if requested_usd <= EPS:
        reasons.append("buy has no requested notional/shares")
        checks.append(CheckOutcome("requested_usd", False, "requested USD is zero"))
        return reject()

    # --- per-trade cap: the ONLY cap that resizes (down) --------------------
    # Effective cap is the tighter of the absolute dollar ceiling and the
    # equity-scaled percentage, so a small book stays proportionally sized and
    # the cap grows with the account instead of needing a manual edit.
    per_trade_cap = min(
        float(limits.per_trade_max_usd),
        float(limits.per_trade_max_pct) * account.equity,
    )
    approved_usd = requested_usd
    resized = False
    if approved_usd > per_trade_cap + EPS:
        approved_usd = per_trade_cap
        resized = True
        reasons.append(
            f"resized to per-trade cap ${per_trade_cap:,.2f} "
            f"(requested ${requested_usd:,.2f})"
        )
    checks.append(CheckOutcome(
        "per_trade_max_usd", True,
        f"per-trade cap ${per_trade_cap:,.2f} "
        f"(min of ${limits.per_trade_max_usd:,.2f} and "
        f"{limits.per_trade_max_pct:.0%} of equity); sizing ${approved_usd:,.2f}",
        cap_usd=per_trade_cap,
    ))

    # Convert to shares (honour the fractional-shares setting) BEFORE the
    # cap checks, since flooring can only reduce the notional. Fractional shares
    # are floored to 8 decimals — the broker (Robinhood) rejects a quantity with
    # more than 8 decimal places — and the notional is recomputed to match, so the
    # recorded decision and the cap checks below use the exact tradeable size.
    if limits.allow_fractional_shares:
        approved_shares = math.floor(approved_usd / price * 1e8) / 1e8
        approved_usd = approved_shares * price
    else:
        approved_shares = math.floor(approved_usd / price)
        approved_usd = approved_shares * price
    if approved_shares <= 0:
        reasons.append("size rounds to zero shares")
        checks.append(CheckOutcome("share_quantity", False, "0 shares after rounding"))
        return reject()

    # --- minimum tradeable notional ----------------------------------------
    if approved_usd < limits.min_trade_usd - EPS:
        reasons.append(
            f"size ${approved_usd:,.2f} is below the minimum trade ${limits.min_trade_usd:,.2f}"
        )
        checks.append(CheckOutcome("min_trade_usd", False, f"${approved_usd:,.2f} < min"))
        return reject()

    # --- max_position_pct: REJECT if it would push the name over the cap ----
    max_position_value = limits.max_position_pct * account.equity
    existing_pos = account.position_value(intent.ticker)
    projected_pos = existing_pos + approved_usd
    pos_ok = projected_pos <= max_position_value + EPS
    checks.append(CheckOutcome(
        "max_position_pct", pos_ok,
        f"position would be ${projected_pos:,.2f} vs cap "
        f"{limits.max_position_pct:.0%} = ${max_position_value:,.2f}",
        cap_usd=max(0.0, max_position_value - existing_pos),
    ))
    if not pos_ok:
        reasons.append(
            f"would push {intent.ticker} to ${projected_pos:,.2f} > position cap "
            f"${max_position_value:,.2f} ({limits.max_position_pct:.0%} of equity)"
        )
        return reject()

    # --- max_sector_pct: REJECT if it would push the sector over the cap ----
    sector = _resolve_sector(intent, account)
    max_sector_value = limits.max_sector_pct * account.equity
    existing_sector = account.sector_value(sector)
    projected_sector = existing_sector + approved_usd
    sector_ok = projected_sector <= max_sector_value + EPS
    checks.append(CheckOutcome(
        "max_sector_pct", sector_ok,
        f"sector '{sector or 'Unknown'}' would be ${projected_sector:,.2f} vs cap "
        f"{limits.max_sector_pct:.0%} = ${max_sector_value:,.2f}",
        cap_usd=max(0.0, max_sector_value - existing_sector),
    ))
    if not sector_ok:
        reasons.append(
            f"would push sector '{sector or 'Unknown'}' to ${projected_sector:,.2f} > "
            f"sector cap ${max_sector_value:,.2f} ({limits.max_sector_pct:.0%} of equity)"
        )
        return reject()

    # --- min_cash_reserve_pct: REJECT if it would spend below the floor -----
    cash_floor = limits.min_cash_reserve_pct * account.equity
    cash_after = account.cash - approved_usd
    cash_ok = cash_after >= cash_floor - EPS
    checks.append(CheckOutcome(
        "min_cash_reserve_pct", cash_ok,
        f"cash after ${cash_after:,.2f} vs floor "
        f"{limits.min_cash_reserve_pct:.0%} = ${cash_floor:,.2f}",
        cap_usd=max(0.0, account.cash - cash_floor),
    ))
    if not cash_ok:
        reasons.append(
            f"would drop cash to ${cash_after:,.2f}, below the reserve floor "
            f"${cash_floor:,.2f} ({limits.min_cash_reserve_pct:.0%} of equity)"
        )
        return reject()

    # --- buying power: REJECT if it exceeds spendable buying power ----------
    bp_ok = approved_usd <= account.buying_power + EPS
    checks.append(CheckOutcome(
        "buying_power", bp_ok,
        f"order ${approved_usd:,.2f} vs buying power ${account.buying_power:,.2f}",
        cap_usd=max(0.0, account.buying_power),
    ))
    if not bp_ok:
        reasons.append(
            f"order ${approved_usd:,.2f} exceeds buying power ${account.buying_power:,.2f}"
        )
        return reject()

    return approve(approved_usd, approved_shares, resized)


def validate_batch(
    intents: Iterable[OrderIntent],
    account: AccountState,
    limits,
    trades_today: int = 0,
    *,
    kill_switch: bool = False,
    audit: Callable[[str, str, dict], None] | None = None,
) -> list[GuardrailResult]:
    """Validate a list of intents *sequentially*, accounting for cumulative use.

    Each approved order consumes the daily-trade budget and (for buys) spends
    cash / buying power and adds to position & sector exposure, so that the
    second buy in a batch is validated against the state the first one would
    leave behind. This prevents a batch from collectively breaching a limit that
    each order individually respects.
    """
    results: list[GuardrailResult] = []
    # Work on a shallow mutable copy of the account so we can simulate fills.
    sim = AccountState(
        equity=account.equity,
        cash=account.cash,
        buying_power=account.buying_power,
        peak_equity=account.peak_equity,
        positions={
            t: Position(p.ticker, p.shares, p.avg_cost, p.market_value, p.sector)
            for t, p in account.positions.items()
        },
    )
    executed = trades_today

    for intent in intents:
        res = validate_order(intent, sim, limits, executed,
                             kill_switch=kill_switch, audit=audit)
        results.append(res)
        if not res.approved:
            continue
        # Protective exits are exempt from the daily cap, so they don't consume a
        # slot; every other approved order does.
        if not intent.protective_exit:
            executed += 1

        # Reflect the (assumed) fill in the simulated account state.
        sector = _resolve_sector(intent, sim) or "Unknown"
        pos = sim.positions.get(intent.ticker)
        if intent.side == Side.BUY:
            sim.cash -= res.approved_usd
            sim.buying_power -= res.approved_usd
            if pos:
                pos.shares += res.approved_shares
                pos.market_value += res.approved_usd
            else:
                sim.positions[intent.ticker] = Position(
                    ticker=intent.ticker,
                    shares=res.approved_shares,
                    avg_cost=intent.price,
                    market_value=res.approved_usd,
                    sector=sector,
                )
        else:  # SELL
            sim.cash += res.approved_usd
            sim.buying_power += res.approved_usd
            if pos:
                pos.shares -= res.approved_shares
                pos.market_value = max(0.0, pos.market_value - res.approved_usd)
                if pos.shares <= EPS:
                    pos.shares = 0.0
    return results


def sweep_to_budget(
    results: list[GuardrailResult],
    account: AccountState,
    limits,
    *,
    epsilon: float = 0.01,
    max_rounds: int = 10,
) -> list[GuardrailResult]:
    """Scale approved BUYs UP so they consume the deployable cash.

    The decision agent sizes each name itself, and it has repeatedly anchored
    that size to the position-count cap (``equity / max_positions``) rather than
    to the cash actually available — so a day with fewer qualifying candidates
    than slots left the difference sitting idle. This redistributes the unspent
    remainder across the names the agent DID choose.

    Deliberate properties:
      * **Only scales up.** A rejection stays rejected and a per-trade resize-down
        is never undone; this runs strictly after :func:`validate_batch` and can
        only raise an already-approved buy toward its own caps.
      * **Preserves the agent's relative weighting.** The remainder is split in
        proportion to the approved sizes, so a name it sized small stays smaller
        than one it sized large — this tops the book up, it does not re-rank it.
      * **No approved buys means no sweep.** Proposing nothing is how the agent
        legitimately holds cash, and that must survive untouched.
      * **Ignores same-batch sell proceeds.** The budget comes from the account
        as read at the START of the cycle, so a sell placed in this same batch
        does not fund a buy in it — that sale has not filled yet, and a failed or
        partial exit would leave the buy overcommitted.

    Spendable is ``buying_power``, which is the broker's own real-time figure for
    what this account may deploy right now — NOT ``min(cash, buying_power)``. On
    a cash account buying power already excludes unsettled proceeds, so that
    ``min`` never bound; on a limited-margin account it is exactly the difference
    between deploying the book and leaving most of it stranded in settlement.
    """
    buys = [
        r for r in results
        if r.approved and r.intent.side == Side.BUY
        and not r.intent.protective_exit
        and (r.intent.price or 0.0) > EPS
    ]
    if not buys:
        return results

    reserve = float(limits.min_cash_reserve_pct) * account.equity
    budget = max(0.0, account.buying_power - reserve)
    leftover = budget - sum(r.approved_usd for r in buys)
    if leftover <= epsilon:
        return results

    per_trade_cap = min(
        float(limits.per_trade_max_usd),
        float(limits.per_trade_max_pct) * account.equity,
    )
    max_position_value = float(limits.max_position_pct) * account.equity
    max_sector_value = float(limits.max_sector_pct) * account.equity

    # Sector exposure the book already carries, plus what this batch adds. The
    # sector cap is a portfolio-level ceiling, so it is tracked across orders
    # rather than per order.
    sector_val: dict[str, float] = {}
    for p in account.positions.values():
        s = p.sector or "Unknown"
        sector_val[s] = sector_val.get(s, 0.0) + p.market_value
    for r in buys:
        s = _resolve_sector(r.intent, account) or "Unknown"
        sector_val[s] = sector_val.get(s, 0.0) + r.approved_usd

    def headroom(r: GuardrailResult) -> float:
        """How much more USD this order can absorb before it breaches a cap."""
        sector = _resolve_sector(r.intent, account) or "Unknown"
        held = account.position_value(r.intent.ticker)
        return max(0.0, min(
            per_trade_cap - r.approved_usd,
            max_position_value - held - r.approved_usd,
            max_sector_value - sector_val.get(sector, 0.0),
        ))

    # Water-fill: hand out the remainder in proportion to current size, clamp any
    # order that hits a cap, and re-share its overflow among those still open.
    # Bounded rounds so this always terminates even with pathological caps.
    added: dict[int, float] = {id(r): 0.0 for r in buys}
    for _ in range(max_rounds):
        active = [r for r in buys if headroom(r) - added[id(r)] > epsilon]
        if not active or leftover <= epsilon:
            break
        # Weight by approved size; fall back to an even split when every
        # approved size is zero (nothing to take a proportion of).
        total = sum(r.approved_usd for r in active)
        for r in active:
            share = (leftover * (r.approved_usd / total)) if total > EPS \
                else (leftover / len(active))
            room = headroom(r) - added[id(r)]
            take = min(share, room)
            if take <= 0:
                continue
            added[id(r)] += take
            sector = _resolve_sector(r.intent, account) or "Unknown"
            sector_val[sector] = sector_val.get(sector, 0.0) + take
        spent = sum(added.values())
        leftover = budget - sum(r.approved_usd for r in buys) - spent

    for r in buys:
        extra = added[id(r)]
        if extra <= epsilon:
            continue
        before = r.approved_usd
        target_usd = before + extra
        price = float(r.intent.price)
        # Re-derive shares from the new notional, matching the sizing convention
        # used above: floor to 8 decimals, then recompute the notional so the
        # recorded size is exactly what is tradeable.
        if limits.allow_fractional_shares:
            shares = math.floor(target_usd / price * 1e8) / 1e8
        else:
            shares = math.floor(target_usd / price)
        if shares <= 0:
            continue
        r.approved_shares = shares
        r.approved_usd = shares * price
        r.resized = True
        r.reasons.append(
            f"cash sweep ${before:,.2f} -> ${r.approved_usd:,.2f} "
            f"(deploying idle cash across {len(buys)} approved buy(s))"
        )
        r.checks.append(CheckOutcome(
            "cash_sweep", True,
            f"scaled up from ${before:,.2f} to use the ${budget:,.2f} deployable "
            f"balance", cap_usd=per_trade_cap,
        ))
    return results

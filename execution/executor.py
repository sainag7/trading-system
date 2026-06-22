"""Execution layer.

Two brokers behind one interface:

  * :class:`PaperBroker` — a fully-functional **simulated** broker. It keeps a
    persistent JSON portfolio, marks positions to market via the data provider,
    and fills orders at the reference/limit price. ``paper`` mode uses this and
    needs no network/account, so the whole pipeline runs offline.

  * :class:`RobinhoodMCPBroker` — talks to the official **Robinhood Trading
    MCP** (https://agent.robinhood.com/mcp/trading) through the Claude Agent
    SDK. OAuth is handled by the MCP server; no API key/secret lives in this
    repo. Used to read the account in every mode (when configured) and to place
    real orders in ``live`` mode. Every MCP request and response is written to
    the audit log.

The :class:`Executor` ties a broker + run mode + the audit DB together and is the
ONLY component that ever sends an order. Safety properties:

  * Every order it receives has ALREADY passed the deterministic guardrails.
  * It re-checks the KILL SWITCH before EVERY order and refuses to send if set.
  * **Idempotency** — each order gets a deterministic ``client_order_id`` and a
    trade row is reserved atomically before submission; the same logical order is
    never submitted twice.
  * ``preview`` mode requires explicit confirmation before any live send.
  * On a submission *error* it retries up to ``max_order_retries`` with backoff.
    It NEVER blindly retries a *fill* — an ambiguous/unknown status triggers a
    position **reconciliation** (re-read holdings, compare, warn) and is flagged
    ``needs_review`` for a human, because re-sending could double-fill.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from risk.guardrails import AccountState, Position, Side


@dataclass
class OrderResult:
    ok: bool
    status: str            # filled|simulated|submitted|rejected|error|skipped|needs_review|duplicate
    filled_qty: float = 0.0
    fill_price: float = 0.0
    broker_order_id: str | None = None
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Paper broker (simulated, persistent)
# ---------------------------------------------------------------------------
class PaperBroker:
    """A simulated broker with a JSON-persisted portfolio."""

    def __init__(self, starting_cash: float, state_path: str | Path, provider=None,
                 sectors: dict[str, str] | None = None):
        self.path = Path(state_path)
        self.provider = provider
        self.sectors = {k.upper(): v for k, v in (sectors or {}).items()}
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {"cash": float(starting_cash), "positions": {}}
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2))

    def _price(self, ticker: str, fallback: float = 0.0) -> float:
        if self.provider:
            q = self.provider.get_quote(ticker)
            if q and q.get("price"):
                return float(q["price"])
        pos = self.state["positions"].get(ticker)
        if pos and pos.get("avg_cost"):
            return float(pos["avg_cost"])
        return fallback

    def get_account(self, peak_equity: float = 0.0) -> AccountState:
        positions: dict[str, Position] = {}
        invested = 0.0
        for t, p in self.state["positions"].items():
            price = self._price(t, p.get("avg_cost", 0.0))
            mv = p["shares"] * price
            invested += mv
            positions[t] = Position(
                ticker=t, shares=p["shares"], avg_cost=p.get("avg_cost", 0.0),
                market_value=mv, sector=p.get("sector", self.sectors.get(t, "Unknown")),
            )
        cash = self.state["cash"]
        equity = cash + invested
        return AccountState(
            equity=equity, cash=cash, buying_power=cash,
            peak_equity=max(peak_equity, equity), positions=positions,
        )

    def place_order(self, *, ticker: str, side: Side, qty: float, limit_price: float,
                    sector: str = "Unknown") -> OrderResult:
        """Simulate an immediate fill at the limit/reference price."""
        price = limit_price or self._price(ticker)
        if price <= 0 or qty <= 0:
            return OrderResult(ok=False, status="rejected",
                               detail={"reason": "invalid price/qty in paper fill"})
        positions = self.state["positions"]
        if side == Side.BUY:
            cost = qty * price
            self.state["cash"] -= cost
            pos = positions.get(ticker)
            if pos:
                new_sh = pos["shares"] + qty
                pos["avg_cost"] = (pos["shares"] * pos["avg_cost"] + cost) / new_sh
                pos["shares"] = new_sh
            else:
                positions[ticker] = {"shares": qty, "avg_cost": price,
                                     "sector": sector or self.sectors.get(ticker, "Unknown")}
        else:  # SELL
            pos = positions.get(ticker)
            if not pos or pos["shares"] < qty - 1e-9:
                return OrderResult(ok=False, status="rejected",
                                   detail={"reason": "paper: insufficient shares to sell"})
            pos["shares"] -= qty
            self.state["cash"] += qty * price
            if pos["shares"] <= 1e-9:
                positions.pop(ticker, None)
        self._save()
        return OrderResult(ok=True, status="simulated", filled_qty=qty, fill_price=price,
                           broker_order_id=f"PAPER-{ticker}-{side.value}",
                           detail={"simulated": True})


# ---------------------------------------------------------------------------
# Robinhood Trading MCP broker (real)
# ---------------------------------------------------------------------------
class RobinhoodMCPBroker:
    """Broker backed by the Robinhood Trading MCP via the Claude Agent SDK.

    The MCP is configured as a remote HTTP MCP server. OAuth is handled by the
    MCP/Claude Code; we never store credentials. Tool discovery is automatic;
    set ``allowed_tools`` to the exact Robinhood tool names exposed by the MCP
    (confirm them once against your connected server — they look like
    ``mcp__robinhood__get_positions`` etc.).

    Every MCP request/response is written to the audit log. Order placement goes
    through a narrow, imperative prompt ("place exactly this order") and results
    are parsed as strict JSON. Anything ambiguous is returned as ``needs_review``
    rather than retried.
    """

    def __init__(self, mcp_url: str, model: str, *, token: str | None = None,
                 read_tools: list[str] | None = None, place_tools: list[str] | None = None,
                 audit: Callable | None = None):
        self.mcp_url = mcp_url
        self.model = model
        self.token = token
        self._audit = audit
        self.read_tools = read_tools or [
            "mcp__robinhood__get_account", "mcp__robinhood__get_positions",
            "mcp__robinhood__get_buying_power", "mcp__robinhood__get_holdings",
        ]
        self.place_tools = place_tools or [
            "mcp__robinhood__place_order", "mcp__robinhood__place_equity_order",
            "mcp__robinhood__buy", "mcp__robinhood__sell",
        ]

    def _server_config(self) -> dict:
        cfg: dict[str, Any] = {"robinhood": {"type": "http", "url": self.mcp_url}}
        if self.token:
            cfg["robinhood"]["headers"] = {"Authorization": f"Bearer {self.token}"}
        return cfg

    def _log(self, level: str, event: str, detail: Any) -> None:
        if self._audit:
            try:
                self._audit(level, event, detail)
            except Exception:
                pass

    async def _ask(self, instruction: str, tools: list[str], max_turns: int) -> tuple[Any, str | None]:
        from agents.llm import generate_json
        # Auditable MCP request log (never logs the bearer token).
        self._log("INFO", "mcp_request",
                  {"url": self.mcp_url, "tools": tools, "instruction": instruction[:500]})
        system = (
            "You are an execution bridge to the Robinhood Trading MCP. "
            "Use ONLY the provided Robinhood MCP tools to fulfil the request. "
            "Do not invent data. After acting, reply with STRICT JSON only, no prose."
        )
        parsed, raw = await generate_json(
            system, instruction, self.model,
            mcp_servers=self._server_config(), allowed_tools=tools, max_turns=max_turns,
        )
        self._log("INFO", "mcp_response",
                  {"raw": (raw or "")[:1000], "parsed_ok": isinstance(parsed, (dict, list))})
        return parsed, raw

    async def get_account(self, peak_equity: float = 0.0,
                          sectors: dict[str, str] | None = None) -> AccountState | None:
        instruction = (
            "Read the agentic account from Robinhood. Return STRICT JSON:\n"
            '{"cash": <float>, "buying_power": <float>, "equity": <float>, '
            '"positions": [{"ticker": "AAPL", "shares": <float>, "avg_cost": <float>, '
            '"market_value": <float>}]}'
        )
        try:
            parsed, _ = await self._ask(instruction, self.read_tools, max_turns=6)
        except Exception as e:
            self._log("ERROR", "robinhood_read_failed", {"error": str(e)})
            return None
        if not isinstance(parsed, dict):
            return None
        sectors = {k.upper(): v for k, v in (sectors or {}).items()}
        positions = {}
        for p in parsed.get("positions", []):
            t = str(p.get("ticker", "")).upper()
            if not t:
                continue
            positions[t] = Position(
                ticker=t, shares=float(p.get("shares", 0)),
                avg_cost=float(p.get("avg_cost", 0) or 0),
                market_value=float(p.get("market_value", 0) or 0),
                sector=sectors.get(t, "Unknown"),
            )
        equity = float(parsed.get("equity") or
                       (parsed.get("cash", 0) + sum(p.market_value for p in positions.values())))
        return AccountState(
            equity=equity, cash=float(parsed.get("cash", 0)),
            buying_power=float(parsed.get("buying_power", parsed.get("cash", 0))),
            peak_equity=max(peak_equity, equity), positions=positions,
        )

    async def place_order(self, *, ticker: str, side: Side, qty: float, limit_price: float,
                          order_type: str = "limit") -> OrderResult:
        instruction = (
            f"Place EXACTLY ONE {order_type} {side.value} order for {qty} shares of "
            f"{ticker} at limit price {limit_price:.2f}. Do not place any other order. "
            "If the order is accepted, return STRICT JSON: "
            '{"status": "submitted"|"filled", "order_id": "<id>", '
            '"filled_qty": <float>, "fill_price": <float>}. '
            "If it is rejected or anything is unclear, return "
            '{"status": "rejected"|"unclear", "detail": "<why>"} and do NOT retry.'
        )
        try:
            parsed, raw = await self._ask(instruction, self.place_tools, max_turns=6)
        except Exception as e:
            return OrderResult(ok=False, status="error", detail={"error": str(e)})
        if not isinstance(parsed, dict):
            # Unknown outcome -> never assume; flag for a human. NEVER blind-retry a fill.
            return OrderResult(ok=False, status="needs_review",
                               detail={"raw": raw, "note": "unparseable broker response"})
        status = str(parsed.get("status", "unclear")).lower()
        if status in ("submitted", "filled"):
            return OrderResult(
                ok=True, status=status,
                filled_qty=float(parsed.get("filled_qty", qty) or qty),
                fill_price=float(parsed.get("fill_price", limit_price) or limit_price),
                broker_order_id=str(parsed.get("order_id", "")) or None, detail=parsed,
            )
        if status == "rejected":
            return OrderResult(ok=False, status="rejected", detail=parsed)
        return OrderResult(ok=False, status="needs_review", detail=parsed)


# ---------------------------------------------------------------------------
# Executor (mode-aware orchestration of a single order's lifecycle)
# ---------------------------------------------------------------------------
class Executor:
    def __init__(self, broker, mode: str, db, run_id: str, exec_cfg: dict,
                 *, kill_switch_check: Callable[[], bool],
                 confirm_callback: Callable[[dict], bool] | None = None):
        self.broker = broker
        self.mode = mode
        self.db = db
        self.run_id = run_id
        self.cfg = exec_cfg
        self.kill_switch_check = kill_switch_check
        # Default preview confirmation: interactive y/N prompt.
        self.confirm = confirm_callback or self._default_confirm

    @staticmethod
    def _default_confirm(order: dict) -> bool:
        ans = input(
            f"  >> CONFIRM {order['side']} {order['qty']:g} {order['ticker']} "
            f"@ ~${order['limit_price']:.2f} (${order['notional']:.2f})? [y/N] "
        ).strip().lower()
        return ans in ("y", "yes")

    def _limit_price(self, side: Side, ref_price: float) -> float:
        slip = self.cfg.get("limit_slippage_pct", 0.005)
        if self.cfg.get("order_type", "limit") == "market":
            return ref_price  # informational; broker treats as market
        # Buy a touch above, sell a touch below, to improve fill odds.
        return ref_price * (1 + slip) if side == Side.BUY else ref_price * (1 - slip)

    def _client_oid(self, ticker: str, side: Side, qty: float) -> str:
        """Deterministic idempotency key for one logical order in this run."""
        return f"{self.run_id}:{ticker}:{side.value}:{round(qty, 6)}"

    async def execute_order(self, *, ticker: str, side: Side, qty: float, ref_price: float,
                            notional: float, sector: str = "Unknown",
                            pre_shares: float = 0.0) -> OrderResult:
        """Run one approved order through the appropriate mode path."""
        # Belt-and-braces: refuse to send if the kill switch flipped mid-run.
        if self.kill_switch_check():
            self.db.audit(self.run_id, "HALT", "executor_refused_kill_switch", {"ticker": ticker})
            return OrderResult(ok=False, status="skipped", detail={"reason": "kill switch active"})

        limit_price = self._limit_price(side, ref_price)
        order_type = self.cfg.get("order_type", "limit")
        client_oid = self._client_oid(ticker, side, qty)
        order_row = self.db.log_order(
            self.run_id, self.mode, ticker=ticker, side=side.value, order_type=order_type,
            qty=qty, limit_price=limit_price, notional_usd=notional, status="intended",
        )

        # ----- IDEMPOTENCY: reserve the trade row before doing anything -----
        trade_id, is_new, prior_status = self.db.begin_trade(
            client_order_id=client_oid, run_id=self.run_id, mode=self.mode, ticker=ticker,
            side=side.value, qty=qty, price=limit_price, notional=notional,
        )
        if not is_new and prior_status in ("submitting", "submitted", "filled", "simulated"):
            self.db.audit(self.run_id, "WARN", "duplicate_order_skipped",
                          {"ticker": ticker, "client_order_id": client_oid, "prior": prior_status})
            self.db.update_order_status(order_row, "duplicate", detail={"client_order_id": client_oid})
            return OrderResult(ok=False, status="duplicate",
                               detail={"reason": "already submitted", "client_order_id": client_oid})

        # ----- PAPER: simulate, never touch the real broker ----------------
        if self.mode == "paper":
            res = self.broker.place_order(ticker=ticker, side=side, qty=qty,
                                          limit_price=limit_price, sector=sector)
            self._record_order_fill(order_row, res, ticker=ticker, side=side, simulated=True)
            self.db.finish_trade(trade_id, res.status, broker_order_id=res.broker_order_id,
                                 price=res.fill_price or limit_price, qty=res.filled_qty or qty,
                                 simulated=True, detail=res.detail)
            self.db.audit(self.run_id, "INFO", "order_simulated",
                          {"ticker": ticker, "side": side.value, "qty": qty, "price": res.fill_price})
            return res

        # ----- PREVIEW: confirm, then place live ---------------------------
        if self.mode == "preview":
            approved = self.confirm({"ticker": ticker, "side": side.value, "qty": qty,
                                     "limit_price": limit_price, "notional": notional})
            if not approved:
                self.db.update_order_status(order_row, "skipped", detail={"reason": "user declined"})
                self.db.finish_trade(trade_id, "skipped", price=limit_price, qty=0.0,
                                     detail={"reason": "user declined"})
                self.db.audit(self.run_id, "INFO", "order_skipped",
                              {"ticker": ticker, "reason": "user declined in preview"})
                return OrderResult(ok=False, status="skipped", detail={"reason": "user declined"})
            # fall through to live placement

        # ----- LIVE (or confirmed preview): place via broker w/ retries ----
        res = await self._place_live(order_row, ticker, side, qty, limit_price, order_type)
        self.db.finish_trade(trade_id, res.status, broker_order_id=res.broker_order_id,
                             price=res.fill_price or limit_price, qty=res.filled_qty or qty,
                             simulated=False, detail=res.detail)
        # ----- IDEMPOTENCY/SAFETY: never blind-retry an unclear fill -------
        if res.status == "needs_review":
            await self._reconcile(ticker, side, qty, pre_shares)
        return res

    async def _place_live(self, order_row, ticker, side, qty, limit_price, order_type) -> OrderResult:
        retries = int(self.cfg.get("max_order_retries", 2))
        backoff = float(self.cfg.get("retry_backoff_seconds", 5))
        attempt = 0
        while True:
            res = await self.broker.place_order(
                ticker=ticker, side=side, qty=qty, limit_price=limit_price, order_type=order_type,
            )
            # Retry ONLY transient submission errors. Never retry an ambiguous
            # fill (needs_review) or a rejection — that risks a double fill.
            if res.status == "error" and attempt < retries:
                attempt += 1
                self.db.audit(self.run_id, "WARN", "order_retry",
                              {"ticker": ticker, "attempt": attempt, "detail": res.detail})
                await asyncio.sleep(backoff)
                continue
            self._record_order_fill(order_row, res, ticker=ticker, side=side, simulated=False)
            if res.status == "needs_review":
                self.db.audit(self.run_id, "ERROR", "order_needs_human_review",
                              {"ticker": ticker, "detail": res.detail})
            elif res.ok:
                self.db.audit(self.run_id, "INFO", f"order_{res.status}",
                              {"ticker": ticker, "side": side.value, "qty": res.filled_qty,
                               "price": res.fill_price, "broker_order_id": res.broker_order_id})
            return res

    async def _read_positions(self) -> dict[str, Position]:
        """Re-read current holdings from the broker (sync or async)."""
        try:
            getter = self.broker.get_account
            acct = await getter() if inspect.iscoroutinefunction(getter) else getter()
            return acct.positions if acct else {}
        except Exception as e:
            self.db.audit(self.run_id, "ERROR", "reconcile_read_failed", {"error": str(e)})
            return {}

    async def _reconcile(self, ticker: str, side: Side, qty: float, pre_shares: float) -> None:
        """On an unclear fill, re-read positions and reconcile — do NOT resubmit."""
        positions = await self._read_positions()
        held = positions.get(ticker)
        current = held.shares if held else 0.0
        expected = pre_shares + qty if side == Side.BUY else pre_shares - qty
        likely_filled = abs(current - expected) < 1e-6
        self.db.audit(self.run_id, "WARN", "order_reconciliation", {
            "ticker": ticker, "side": side.value, "qty": qty,
            "pre_shares": pre_shares, "current_shares": current,
            "expected_if_filled": expected, "likely_filled": likely_filled,
            "note": "unclear fill — positions re-read; NOT resubmitting; human review required",
        })

    def _record_order_fill(self, order_row: int, res: OrderResult, *, ticker: str, side: Side,
                           simulated: bool) -> None:
        """Update the orders row and log a fill (the trades row is handled separately)."""
        self.db.update_order_status(order_row, res.status, broker_order_id=res.broker_order_id,
                                    detail=res.detail)
        if res.ok and res.filled_qty > 0:
            self.db.log_fill(order_row, ticker=ticker, side=side.value, qty=res.filled_qty,
                             price=res.fill_price, simulated=simulated)

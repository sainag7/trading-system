"""Execution layer.

One broker:

  * :class:`RobinhoodMCPBroker` — talks to the official **Robinhood Trading
    MCP** (https://agent.robinhood.com/mcp/trading) through the Claude Agent
    SDK. OAuth is handled by the MCP server; no API key/secret lives in this
    repo. Used to read the account (when configured) and to place real orders
    in ``live`` mode. Every MCP request and response is written to the audit
    log.

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
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from risk.guardrails import AccountState, Position, Side

# Fixed namespace so a logical order's ``ref_id`` is a deterministic UUID — the
# same client-order-id always maps to the same UUID, giving the Robinhood MCP an
# idempotency key that survives retries (it never double-places a logical order).
_REF_ID_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

# Robinhood rejects an order whose quantity has more than 8 decimal places
# ("Ensure that there are no more than 8 decimal places."). Fractional sizing
# (approved_usd / price) routinely produces a 17-digit float, so every quantity
# is quantized here before it reaches the broker.
_MAX_QTY_DECIMALS = 8
_QTY_SCALE = 10 ** _MAX_QTY_DECIMALS


def _quantize_shares(qty: float) -> float:
    """Floor a share quantity to Robinhood's 8-decimal limit. Rounding DOWN (not
    nearest) so a buy never spends beyond the approved notional and a sell never
    oversells the held quantity."""
    return math.floor(float(qty) * _QTY_SCALE) / _QTY_SCALE


def _fmt_qty(qty: float) -> str:
    """Render a quantity for the broker order instruction: quantized to <=8
    decimals, no trailing zeros and never scientific notation (e.g.
    0.07439590524937507 -> "0.0743959", 1.0 -> "1")."""
    q = _quantize_shares(qty)
    s = f"{q:.{_MAX_QTY_DECIMALS}f}".rstrip("0").rstrip(".")
    return s or "0"


@dataclass
class OrderResult:
    ok: bool
    status: str            # filled|simulated|submitted|rejected|error|skipped|needs_review|duplicate
    filled_qty: float = 0.0
    fill_price: float = 0.0
    broker_order_id: str | None = None
    detail: dict = field(default_factory=dict)


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

    # The MCP server name as registered with Claude Code (`claude mcp add
    # robinhood-trading ...`). Its tools are namespaced ``mcp__<server>__<tool>``.
    SERVER = "robinhood-trading"

    def __init__(self, mcp_url: str, model: str, *, token: str | None = None,
                 account_number: str | None = None,
                 read_tools: list[str] | None = None, place_tools: list[str] | None = None,
                 audit: Callable | None = None):
        self.mcp_url = mcp_url
        self.model = model
        self.token = token          # optional bearer-token fallback (headless/CI)
        # Which Robinhood account to read/trade. Robinhood exposes several
        # accounts under one login and every per-account MCP tool (get_portfolio,
        # get_equity_positions, place_equity_order, …) REQUIRES this number, so
        # reads and orders are always scoped to exactly one account. When None the
        # broker runs single-account (whatever the MCP treats as default) — the
        # legacy behaviour, kept for backward compatibility.
        self.account_number = str(account_number).strip() if account_number else None
        self._audit = audit
        # Set when the most recent get_account() failed, so callers can print an
        # actionable message (connection/auth lapse vs an unparseable read).
        self.last_read_failure: dict | None = None
        # The Agent SDK grants MCP permission at SERVER granularity, so the
        # allow-list uses the server prefix (individual tool names are silently
        # denied → empty responses). The narrow per-call instruction + the
        # deterministic risk layer are the real controls, not this list.
        # Read tools (get_accounts / get_equity_positions / get_equity_quotes) and
        # place tools (place_equity_order) both live under this one server.
        server = f"mcp__{self.SERVER}"
        self.read_tools = read_tools or [server]
        self.place_tools = place_tools or [server]

    def _server_config(self) -> dict:
        cfg: dict[str, Any] = {self.SERVER: {"type": "http", "url": self.mcp_url}}
        if self.token:
            cfg[self.SERVER]["headers"] = {"Authorization": f"Bearer {self.token}"}
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
                  {"server": self.SERVER, "account": self.account_number,
                   "tools": tools, "instruction": instruction[:500]})
        system = (
            "You are an execution bridge to the Robinhood Trading MCP. "
            "Use ONLY the provided Robinhood MCP tools to fulfil the request. "
            "Do not invent data. After you have called the necessary tools, your "
            "FINAL message MUST be the requested STRICT JSON object and nothing "
            "else — no prose, no markdown, and not a tool call."
        )
        # Prefer the OAuth'd server that Claude Code holds (inherit its config via
        # setting_sources=["local"]); fall back to an explicit bearer-token server
        # only when a token was supplied (headless/CI without an interactive login).
        if self.token:
            parsed, raw = await generate_json(
                system, instruction, self.model,
                mcp_servers=self._server_config(), allowed_tools=tools, max_turns=max_turns)
        else:
            parsed, raw = await generate_json(
                system, instruction, self.model,
                allowed_tools=tools, max_turns=max_turns, setting_sources=["local"])
        self._log("INFO", "mcp_response",
                  {"raw": (raw or "")[:1000], "parsed_ok": isinstance(parsed, (dict, list))})
        return parsed, raw

    def _account_from_json(self, parsed: dict, peak_equity: float, sectors: dict) -> AccountState:
        positions = {}
        for p in parsed.get("positions", []) or []:
            t = str(p.get("ticker", "")).upper()
            if not t:
                continue
            positions[t] = Position(
                ticker=t, shares=float(p.get("shares", 0) or 0),
                avg_cost=float(p.get("avg_cost", 0) or 0),
                market_value=float(p.get("market_value", 0) or 0),
                sector=sectors.get(t, "Unknown"),
            )
        cash = float(parsed.get("cash", 0) or 0)
        # Equity is computed by us (cash + holdings), not trusted from the model.
        equity = cash + sum(p.market_value for p in positions.values())
        return AccountState(
            equity=equity, cash=cash,
            buying_power=float(parsed.get("buying_power", cash) or cash),
            peak_equity=max(peak_equity, equity), positions=positions,
        )

    async def get_account(self, peak_equity: float = 0.0,
                          sectors: dict[str, str] | None = None) -> AccountState | None:
        acct = self.account_number
        if acct:
            # Account-scoped read: get_portfolio + get_equity_positions both take
            # the account number, so cash/holdings are for THIS account only.
            instruction = (
                f'Read Robinhood account number "{acct}", READ-ONLY. You MUST call '
                f'BOTH tools with account_number="{acct}", every time, before '
                "answering:\n"
                f'  1. get_portfolio(account_number="{acct}") → this account\'s cash '
                "and buying power.\n"
                f'  2. get_equity_positions(account_number="{acct}") → EVERY open '
                "equity holding IN THIS ACCOUNT (ticker, quantity, average buy "
                "price, current market value). Do not skip this or summarise — list "
                "every position it returns.\n"
                f'Use ONLY account "{acct}". Call NO buy/sell/place/cancel tools. If '
                "get_equity_positions returns holdings, `positions` MUST be "
                "non-empty. Return STRICT JSON only:\n"
                '{"cash": <float>, "buying_power": <float>, '
                '"positions": [{"ticker": "AAPL", "shares": <float>, "avg_cost": '
                '<float>, "market_value": <float>}]}'
            )
            cash_instruction = (
                f'READ-ONLY. Call get_portfolio(account_number="{acct}") ONLY and '
                "report this account's settled cash and buying power. Call no other "
                'tool. Return STRICT JSON only: {"cash": <float>, "buying_power": '
                "<float>}"
            )
        else:
            instruction = (
                "Read my Robinhood account, READ-ONLY. You MUST call BOTH tools, "
                "every time, before answering:\n"
                "  1. get_accounts  → cash and buying power.\n"
                "  2. get_equity_positions → EVERY open equity holding (ticker, "
                "quantity, average buy price, current market value). Do not skip "
                "this or summarise — list every position it returns.\n"
                "Call NO buy/sell/place/cancel tools. If get_equity_positions "
                "returns holdings, `positions` MUST be non-empty. Return STRICT "
                'JSON only:\n{"cash": <float>, "buying_power": <float>, '
                '"positions": [{"ticker": "AAPL", "shares": <float>, "avg_cost": '
                '<float>, "market_value": <float>}]}'
            )
            cash_instruction = (
                "READ-ONLY. Call get_accounts ONLY and report the account's "
                "settled cash and buying power. Call no other tool. Return STRICT "
                'JSON only: {"cash": <float>, "buying_power": <float>}'
            )
        # The model-mediated read's ONLY failure mode is under-reporting — it
        # sometimes answers from get_accounts alone and skips positions (never the
        # reverse; it can't invent holdings). So take the RICHEST of a few reads
        # and stop as soon as one returns holdings. Reads are idempotent, so
        # retrying is always safe (unlike a fill).
        from agents.llm import last_error
        sec = {k.upper(): v for k, v in (sectors or {}).items()}
        best: AccountState | None = None
        any_text = False            # did any attempt return non-empty model output?
        for attempt in range(3):
            try:
                parsed, raw = await self._ask(instruction, self.read_tools, max_turns=8)
            except Exception as e:
                self._log("WARN", "robinhood_read_retry", {"attempt": attempt + 1, "error": str(e)})
                continue
            if raw:
                any_text = True
            if not isinstance(parsed, dict):
                continue
            cand = self._account_from_json(parsed, peak_equity, sec)
            if best is None or len(cand.positions) > len(best.positions) \
                    or (len(cand.positions) == len(best.positions) and cand.equity > best.equity):
                best = cand
            if best.positions:  # got holdings -> trust it, stop early
                break
        if best is None:
            err = last_error()
            if not any_text:
                # No model output at all on any attempt — the MCP call returned
                # nothing. This is (almost always) a dropped/expired Robinhood MCP
                # connection: the OAuth'd `robinhood-trading` session the subprocess
                # inherits via setting_sources=["local"] has lapsed. NOT a stochastic
                # parse miss — retrying won't help until it's reconnected.
                self.last_read_failure = {
                    "kind": "no_response", "attempts": 3,
                    "account": self.account_number, "error": err,
                    "hint": "reconnect robinhood-trading via /mcp in an interactive "
                            "claude session started from this repo, then re-run",
                }
                self._log("ERROR", "robinhood_mcp_no_response", self.last_read_failure)
            else:
                # Got model text but never valid account JSON — the rarer, genuinely
                # stochastic case; a re-run usually succeeds.
                self.last_read_failure = {
                    "kind": "unparseable", "attempts": 3,
                    "account": self.account_number, "error": err,
                }
                self._log("ERROR", "robinhood_read_unparseable", self.last_read_failure)
            return None
        self.last_read_failure = None

        # The positions-rich read usually reports cash as 0 (the model called the
        # positions tool but not the cash/portfolio one). Top up cash with a
        # focused read so equity/buying-power are complete.
        if best.positions and best.cash <= 0:
            for _ in range(2):
                try:
                    c, _raw = await self._ask(cash_instruction, self.read_tools, max_turns=6)
                except Exception:
                    c = None
                if isinstance(c, dict) and float(c.get("cash", 0) or 0) > 0:
                    best.cash = float(c["cash"])
                    best.buying_power = float(c.get("buying_power", best.cash) or best.cash)
                    best.equity = best.cash + sum(p.market_value for p in best.positions.values())
                    best.peak_equity = max(best.peak_equity, best.equity)
                    break

        self._log("INFO", "robinhood_account_read",
                  {"account": self.account_number, "cash": round(best.cash, 2),
                   "positions": len(best.positions), "equity": round(best.equity, 2)})
        return best

    async def place_order(self, *, ticker: str, side: Side, qty: float, limit_price: float,
                          order_type: str = "limit", ref_id: str | None = None) -> OrderResult:
        acct = self.account_number
        acct_clause = (
            f'in Robinhood account number "{acct}" (pass account_number="{acct}") '
            if acct else ""
        )
        ref_clause = f'Pass ref_id="{ref_id}" for idempotency. ' if ref_id else ""
        instruction = (
            f"Using place_equity_order, place EXACTLY ONE {order_type} {side.value} "
            f"order {acct_clause}for {_fmt_qty(qty)} shares of {ticker} at limit price "
            f"{limit_price:.2f}. {ref_clause}Do not place any other order and do not "
            "use any other account. If the order is accepted, return STRICT JSON: "
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

    @staticmethod
    def _ref_id(client_oid: str) -> str:
        """Deterministic UUID for the broker's ``ref_id`` idempotency key, derived
        from the client-order-id so a retried logical order reuses the same UUID."""
        return str(uuid.uuid5(_REF_ID_NS, client_oid))

    async def execute_order(self, *, ticker: str, side: Side, qty: float, ref_price: float,
                            notional: float, sector: str = "Unknown",
                            pre_shares: float = 0.0) -> OrderResult:
        """Run one approved order through the appropriate mode path."""
        # Belt-and-braces: refuse to send if the kill switch flipped mid-run.
        if self.kill_switch_check():
            self.db.audit(self.run_id, "HALT", "executor_refused_kill_switch", {"ticker": ticker})
            return OrderResult(ok=False, status="skipped", detail={"reason": "kill switch active"})

        # Quantize to Robinhood's 8-decimal quantity limit BEFORE anything else, so
        # the idempotency key, DB rows and broker submission all agree on the exact
        # tradeable quantity (an un-quantized fractional float is rejected HTTP 400).
        qty = _quantize_shares(qty)
        if qty <= 0:
            self.db.audit(self.run_id, "WARN", "order_zero_qty_after_quantize", {"ticker": ticker})
            return OrderResult(ok=False, status="skipped",
                               detail={"reason": "quantity rounds to zero shares"})

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
        res = await self._place_live(order_row, ticker, side, qty, limit_price, order_type,
                                     ref_id=self._ref_id(client_oid))
        self.db.finish_trade(trade_id, res.status, broker_order_id=res.broker_order_id,
                             price=res.fill_price or limit_price, qty=res.filled_qty or qty,
                             simulated=False, detail=res.detail)
        # ----- IDEMPOTENCY/SAFETY: never blind-retry an unclear fill -------
        if res.status == "needs_review":
            await self._reconcile(ticker, side, qty, pre_shares)
        return res

    async def _place_live(self, order_row, ticker, side, qty, limit_price, order_type,
                          *, ref_id: str | None = None) -> OrderResult:
        retries = int(self.cfg.get("max_order_retries", 2))
        backoff = float(self.cfg.get("retry_backoff_seconds", 5))
        attempt = 0
        while True:
            res = await self.broker.place_order(
                ticker=ticker, side=side, qty=qty, limit_price=limit_price,
                order_type=order_type, ref_id=ref_id,
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

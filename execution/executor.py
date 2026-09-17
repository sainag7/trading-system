"""Execution layer.

One broker:

  * :class:`RobinhoodMCPBroker` — talks to the official **Robinhood Trading
    MCP** (https://agent.robinhood.com/mcp/trading) DIRECTLY through
    :class:`data.robinhood_mcp.RobinhoodMCP` — no model in the loop. OAuth is
    the client's own session in ``.cache/`` (or a bearer token); no API
    key/secret lives in this repo. Used to read the account (when configured)
    and to place real orders in ``live`` mode. Every MCP request and response
    is written to the audit log.

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

from risk.guardrails import (
    MAX_SHARE_DECIMALS, AccountState, Position, Side, floor_shares,
)

# Fixed namespace so a logical order's ``ref_id`` is a deterministic UUID — the
# same client-order-id always maps to the same UUID, giving the Robinhood MCP an
# idempotency key that survives retries (it never double-places a logical order).
_REF_ID_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

# Share precision is defined ONCE, in the risk layer — see
# `guardrails.MAX_SHARE_DECIMALS`. Keeping a second copy here is what let this
# drift to 8 while the broker only ever accepted 6.
_MAX_QTY_DECIMALS = MAX_SHARE_DECIMALS
_quantize_shares = floor_shares


def _f_or_none(v) -> float | None:
    """Parse a broker-reported number, or ``None`` when it is absent/unusable.

    Distinguishes "the broker did not tell us" from "the broker said zero", so a
    missing fill price is never silently replaced by a reference price."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _fmt_qty(qty: float) -> str:
    """Render a quantity for the broker order instruction: floored to the
    broker's precision, no trailing zeros and never scientific notation (e.g.
    0.07439590524937507 -> "0.074395", 1.0 -> "1")."""
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
    """Broker backed by the Robinhood Trading MCP, called directly.

    Every tool call and its payload is written to the audit log. Order
    placement sends exactly one ``place_equity_order`` and parses the broker's
    own order object; anything ambiguous is returned as ``needs_review`` rather
    than retried. The deterministic risk layer upstream is the real control —
    this class never sizes, prices or re-tries a fill on its own.
    """

    def __init__(self, client, *, account_number: str | None = None,
                 audit: Callable | None = None):
        self._rh = client
        # Which Robinhood account to read/trade. Robinhood exposes several
        # accounts under one login and every per-account MCP tool (get_portfolio,
        # get_equity_positions, place_equity_order, …) REQUIRES this number, so
        # reads and orders are always scoped to exactly one account.
        self.account_number = str(account_number).strip() if account_number else None
        self._audit = audit
        # Set when the most recent get_account() failed, so callers can print an
        # actionable message (connection/auth lapse vs an unparseable read).
        self.last_read_failure: dict | None = None

    def _log(self, level: str, event: str, detail: Any) -> None:
        if self._audit:
            try:
                self._audit(level, event, detail)
            except Exception:
                pass

    async def _call(self, tool: str, args: dict) -> Any:
        """One direct tool call. Raises ``RobinhoodMCPError`` — callers decide
        whether that is a retryable read or a needs-review order outcome."""
        from data.robinhood_mcp import RobinhoodMCPError  # noqa: F401 (re-raised)
        return await self._rh.call(tool, args)

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
        """Read cash + holdings for this account. ``None`` on failure (with
        ``last_read_failure`` set); reads are idempotent so the caller may retry."""
        from data.robinhood_mcp import RobinhoodMCPError
        acct = self.account_number
        if not acct:
            self.last_read_failure = {"kind": "no_account", "error": "account_number not configured"}
            self._log("ERROR", "robinhood_read_failed", self.last_read_failure)
            return None
        try:
            portfolio = await self._call("get_portfolio", {"account_number": acct})
            positions = await self._read_positions(acct)
            symbols = sorted({p["symbol"] for p in positions if p.get("symbol")})
            quotes = await self._read_quotes(symbols) if symbols else {}
        except RobinhoodMCPError as e:
            self.last_read_failure = {
                "kind": "no_response", "account": acct, "error": str(e)[:300],
                "hint": "Robinhood MCP unreachable or OAuth session lapsed — run "
                        "`python -m data.robinhood_mcp login` from this repo, then re-run",
            }
            self._log("ERROR", "robinhood_mcp_no_response", self.last_read_failure)
            return None
        parsed = account_json(portfolio, positions, quotes)
        if parsed is None:
            self.last_read_failure = {"kind": "unparseable", "account": acct,
                                      "error": "portfolio payload missing cash"}
            self._log("ERROR", "robinhood_read_unparseable", self.last_read_failure)
            return None
        self.last_read_failure = None
        sec = {k.upper(): v for k, v in (sectors or {}).items()}
        best = self._account_from_json(parsed, peak_equity, sec)
        self._log("INFO", "robinhood_account_read",
                  {"account": acct, "cash": round(best.cash, 2),
                   "positions": len(best.positions), "equity": round(best.equity, 2)})
        return best

    async def _read_positions(self, acct: str) -> list[dict]:
        """All open equity positions, following pagination."""
        out: list[dict] = []
        cursor = None
        for _ in range(20):                       # hard stop; a book is never 20 pages
            args = {"account_number": acct}
            if cursor:
                args["cursor"] = cursor
            data = await self._call("get_equity_positions", args)
            rows = (data or {}).get("positions") if isinstance(data, dict) else None
            out.extend(r for r in (rows or []) if isinstance(r, dict))
            cursor = _next_cursor(data)
            if not cursor:
                break
        return out

    async def _read_quotes(self, symbols: list[str]) -> dict[str, float]:
        from data.robinhood_provider import quote_from_result
        prices: dict[str, float] = {}
        for i in range(0, len(symbols), 20):
            data = await self._call("get_equity_quotes", {"symbols": symbols[i:i + 20]})
            for row in ((data or {}).get("results") or []) if isinstance(data, dict) else []:
                q = quote_from_result(row)
                if q:
                    prices[q["ticker"]] = q["price"]
        return prices

    async def place_order(self, *, ticker: str, side: Side, qty: float, limit_price: float,
                          order_type: str = "limit", ref_id: str | None = None,
                          dollar_amount: float | None = None) -> OrderResult:
        from data.robinhood_mcp import RobinhoodMCPError
        if not self.account_number:
            return OrderResult(ok=False, status="error",
                               detail={"error": "account_number not configured"})
        args: dict[str, Any] = {
            "account_number": self.account_number,
            "symbol": ticker.upper(),
            "side": str(side.value).lower(),
            "type": order_type,
            "market_hours": "regular_hours",
            "time_in_force": "gfd",
        }
        # A fractional quantity can only go out as a market order, which has no
        # price protection: between sizing and fill the price can move and a
        # share-based order spends whatever it costs. Sending the approved
        # NOTIONAL instead caps the spend exactly — the broker derives the share
        # count itself. Buys only: a sell needs to move an exact share count
        # (closing a position), and its risk is smaller proceeds, not overspend.
        if dollar_amount is not None:
            args["dollar_amount"] = f"{dollar_amount:.2f}"
        else:
            args["quantity"] = _fmt_qty(qty)
        # A market order takes no limit_price — passing one is an invalid
        # combination at the broker. The reference price is kept only for the
        # audit trail, never sent as an order field.
        if order_type != "market":
            args["limit_price"] = f"{limit_price:.2f}"
        if ref_id:
            args["ref_id"] = ref_id
        self._log("INFO", "order_request", {**args, "reference_price": round(limit_price, 4)})
        try:
            data = await self._call("place_equity_order", args)
        except RobinhoodMCPError as e:
            # The transport failed; the order MAY have been accepted upstream.
            # Never blind-retry a fill: flag it and let reconciliation decide.
            return OrderResult(ok=False, status="needs_review",
                               detail={"error": str(e)[:500], "note": "broker call failed; state unknown"})
        order = order_from_payload(data)
        if order is None:
            return OrderResult(ok=False, status="needs_review",
                               detail={"raw": _clip(data), "note": "unparseable broker response"})
        state = order["state"]
        if state in _REJECTED_STATES:
            return OrderResult(ok=False, status="rejected", detail=order)
        if state not in _ACCEPTED_STATES:
            return OrderResult(ok=False, status="needs_review", detail=order)
        status = "filled" if state == "filled" else "submitted"
        reported_qty = order.get("filled_qty")
        reported_price = order.get("fill_price")
        # NEVER substitute the reference price for a fill the broker did not
        # report — that writes an invented cost basis into the ledger and
        # produces P&L computed against a price nothing traded at. Unknown
        # stays 0.0 and is flagged, so the fills row is skipped and the
        # trades row keeps its "intended" price under a non-filled status.
        detail = dict(order)
        if reported_price is None:
            detail["fill_price_reported"] = False
        # A share count may be assumed only for a share-based order the
        # broker says is FILLED. A "submitted" order has no fill yet, and a
        # dollar-based order's share count is the broker's to determine.
        if reported_qty is None and status == "filled" and dollar_amount is None:
            reported_qty = qty
        elif reported_qty is None:
            detail["filled_qty_reported"] = False
        return OrderResult(
            ok=True, status=status,
            filled_qty=reported_qty or 0.0,
            fill_price=reported_price or 0.0,
            broker_order_id=order.get("order_id"), detail=detail,
        )

    async def get_order_fill(self, order_id: str) -> dict:
        """Read one order's CURRENT state back from the broker (read-only).

        ``place_equity_order`` returns as soon as the order is accepted, so its
        response usually says ``confirmed``/``queued`` with no price — the fill
        lands milliseconds later. Without this read-back the system never
        learns what it actually paid and falls back to the pre-trade reference
        price, which is both a wrong cost basis and a wrongly-anchored stop.

        Returns ``{}`` when the state cannot be read; the caller must treat that
        as "unknown", never as "not filled".
        """
        from data.robinhood_mcp import RobinhoodMCPError
        try:
            data = await self._call("get_equity_orders",
                                    {"account_number": self.account_number, "order_id": order_id})
        except RobinhoodMCPError as e:
            self._log("WARN", "order_readback_failed", {"order_id": order_id, "error": str(e)[:300]})
            return {}
        orders = (data or {}).get("orders") if isinstance(data, dict) else None
        order = order_from_payload(orders[0]) if orders else None
        if order is None:
            self._log("WARN", "order_readback_unparseable", {"order_id": order_id, "raw": _clip(data)})
            return {}
        return {"state": order["state"], "filled_qty": order.get("filled_qty"),
                "fill_price": order.get("fill_price")}


# Broker order states. Anything else (unconfirmed, partially_filled after the
# poll window, unknown strings) is surfaced as needs_review, never guessed.
_ACCEPTED_STATES = {"new", "queued", "confirmed", "unconfirmed", "partially_filled", "filled"}
_REJECTED_STATES = {"rejected", "failed", "cancelled", "voided"}


def _clip(v: Any, n: int = 800) -> str:
    import json
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _next_cursor(data: Any) -> str | None:
    """Pagination cursor from a list payload, if any (``cursor`` or a ``next``
    URL carrying ``?cursor=``)."""
    if not isinstance(data, dict):
        return None
    if data.get("cursor"):
        return str(data["cursor"])
    nxt = data.get("next")
    if isinstance(nxt, str) and "cursor=" in nxt:
        from urllib.parse import parse_qs, urlparse
        return (parse_qs(urlparse(nxt).query).get("cursor") or [None])[0]
    return None


def account_json(portfolio: Any, positions: list[dict], prices: dict[str, float]) -> dict | None:
    """Assemble the ``{cash, buying_power, positions[]}`` shape
    :meth:`RobinhoodMCPBroker._account_from_json` consumes from the raw
    ``get_portfolio`` / ``get_equity_positions`` / quotes payloads."""
    if not isinstance(portfolio, dict) or portfolio.get("cash") is None:
        return None
    cash = float(portfolio.get("cash") or 0)
    bp = portfolio.get("buying_power")
    if isinstance(bp, dict):
        bp = bp.get("buying_power")
    out_positions = []
    for p in positions:
        sym = str(p.get("symbol") or "").upper()
        shares = _f_or_none(p.get("quantity"))
        if not sym or not shares:
            continue
        avg = _f_or_none(p.get("average_buy_price")) or 0.0
        px = prices.get(sym)
        out_positions.append({
            "ticker": sym, "shares": shares, "avg_cost": avg,
            # Market value at the live quote; if a quote is missing fall back
            # to cost so the position is at least counted, never dropped.
            "market_value": shares * (px if px else avg),
        })
    return {"cash": cash, "buying_power": float(bp) if bp is not None else cash,
            "positions": out_positions}


def order_from_payload(data: Any) -> dict | None:
    """Normalise a broker order object (from ``place_equity_order`` or one
    ``get_equity_orders`` entry) to ``{order_id, state, filled_qty, fill_price}``.
    Tolerates an ``{"order": {...}}`` wrapper. ``None`` when it is not an order."""
    if not isinstance(data, dict):
        return None
    o = data.get("order") if isinstance(data.get("order"), dict) else data
    order_id = o.get("id") or o.get("order_id")
    state = o.get("state")
    if not order_id or not state:
        return None
    return {
        "order_id": str(order_id),
        "state": str(state).lower(),
        "filled_qty": _f_or_none(o.get("cumulative_quantity")),
        "fill_price": _f_or_none(o.get("average_price")),
        "symbol": o.get("symbol"), "side": o.get("side"), "type": o.get("type"),
        "quantity": o.get("quantity"), "dollar_based_amount": o.get("dollar_based_amount"),
    }


# ---------------------------------------------------------------------------
# Executor (mode-aware orchestration of a single order's lifecycle)
# ---------------------------------------------------------------------------
class Executor:
    def __init__(self, broker, mode: str, db, run_id: str, exec_cfg: dict,
                 *, kill_switch_check: Callable[[], bool],
                 confirm_callback: Callable[[dict], bool] | None = None,
                 account: str | None = None):
        self.broker = broker
        self.mode = mode
        self.db = db
        self.run_id = run_id
        self.cfg = exec_cfg
        self.kill_switch_check = kill_switch_check
        # Brokerage account the ledger rows belong to (the broker itself is
        # already scoped); lets the review agent read one book's trades.
        self.account = account
        # Default preview confirmation: interactive y/N prompt.
        self.confirm = confirm_callback or self._default_confirm

    @staticmethod
    def _default_confirm(order: dict) -> bool:
        # Say what will actually be sent. "@ ~$X" on a market order reads as a
        # price guarantee the order does not carry; the honest bound on a
        # dollar-capped market buy is the spend, not the price.
        if order.get("order_type") == "market":
            if order.get("dollar_amount"):
                price_txt = (f"as MARKET, spend capped at ${order['dollar_amount']:.2f} "
                             f"(ref ~${order['limit_price']:.2f}, no price protection)")
            else:
                price_txt = (f"as MARKET @ ~${order['limit_price']:.2f} "
                             f"(reference only — NO price protection)")
        else:
            price_txt = f"@ limit ${order['limit_price']:.2f}"
        ans = input(
            f"  >> CONFIRM {order['side']} {order['qty']:g} {order['ticker']} "
            f"{price_txt} (${order['notional']:.2f})? [y/N] "
        ).strip().lower()
        return ans in ("y", "yes")

    def _dollar_amount(self, side: Side, order_type: str,
                       notional: float) -> float | None:
        """The notional to send instead of a share count, or ``None``.

        A market order carries no price protection, so a share-based buy spends
        whatever the shares cost at fill. Sending the approved notional caps the
        spend exactly and lets the broker derive the shares. Buys only — a sell
        must move a specific share count, and its downside is smaller proceeds
        rather than an overspend."""
        if order_type != "market" or side != Side.BUY:
            return None
        return notional if notional and notional > 0 else None

    def _effective_order_type(self, qty: float) -> str:
        """The order type actually sendable for ``qty`` shares.

        Robinhood permits fractional quantities ONLY on market orders during
        regular hours; a fractional LIMIT order is rejected outright (HTTP 400
        "fractional share quantities are not permitted on limit orders"). Because
        orders are sized in dollars, a small book produces a fractional quantity
        almost every time — so a configured ``limit`` would reject every buy.

        Fall back to ``market`` only when the quantity is genuinely fractional;
        whole-share orders keep the configured limit protection, so the account
        regains price protection by itself as it grows into whole-share sizes.
        """
        configured = self.cfg.get("order_type", "limit")
        if configured == "limit" and qty != math.floor(qty):
            return "market"
        return configured

    def _limit_price(self, side: Side, ref_price: float, order_type: str | None = None) -> float:
        if (order_type or self.cfg.get("order_type", "limit")) == "market":
            return ref_price  # informational; broker treats as market
        slip = self.cfg.get("limit_slippage_pct", 0.005)
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
                            pre_shares: float = 0.0, action: str | None = None,
                            trigger: str | None = None) -> OrderResult:
        """Run one approved order through the appropriate mode path.

        ``action`` / ``trigger`` are informational tags (buy|add|trim|exit_full|
        exit_partial and the Monitor rule that fired) recorded in the trade
        ledger so the review agent can attribute each leg to the rule behind it."""
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

        # Resolve the order type AFTER quantizing, since it depends on whether the
        # final tradeable quantity is fractional, then price it accordingly.
        order_type = self._effective_order_type(qty)
        if order_type != self.cfg.get("order_type", "limit"):
            self.db.audit(self.run_id, "INFO", "order_type_fallback_fractional",
                          {"ticker": ticker, "qty": qty,
                           "configured": self.cfg.get("order_type", "limit"),
                           "used": order_type,
                           "reason": "fractional quantity is only tradeable as a market order"})
        limit_price = self._limit_price(side, ref_price, order_type)
        client_oid = self._client_oid(ticker, side, qty)
        order_row = self.db.log_order(
            self.run_id, self.mode, ticker=ticker, side=side.value, order_type=order_type,
            qty=qty, limit_price=limit_price, notional_usd=notional, status="intended",
        )

        # ----- IDEMPOTENCY: reserve the trade row before doing anything -----
        trade_id, is_new, prior_status = self.db.begin_trade(
            client_order_id=client_oid, run_id=self.run_id, mode=self.mode, ticker=ticker,
            side=side.value, qty=qty, price=limit_price, notional=notional,
            account=self.account,
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
                                     "limit_price": limit_price, "notional": notional,
                                     "order_type": order_type,
                                     "dollar_amount": self._dollar_amount(
                                         side, order_type, notional)})
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
                                     ref_id=self._ref_id(client_oid),
                                     dollar_amount=self._dollar_amount(side, order_type, notional))
        # Only a broker-reported fill updates price/qty. Passing None leaves the
        # row's reserved intent values in place (finish_trade COALESCEs) under a
        # status that says the order is not confirmed filled — rather than
        # overwriting them with numbers nothing actually traded at.
        detail = dict(res.detail or {})
        if action:
            detail.setdefault("action", action)
        if trigger:
            detail.setdefault("trigger", trigger)
        self.db.finish_trade(trade_id, res.status, broker_order_id=res.broker_order_id,
                             price=res.fill_price or None, qty=res.filled_qty or None,
                             simulated=False, detail=detail)
        # ----- IDEMPOTENCY/SAFETY: never blind-retry an unclear fill -------
        if res.status == "needs_review":
            await self._reconcile(ticker, side, qty, pre_shares)
        return res

    async def _place_live(self, order_row, ticker, side, qty, limit_price, order_type,
                          *, ref_id: str | None = None,
                          dollar_amount: float | None = None) -> OrderResult:
        retries = int(self.cfg.get("max_order_retries", 2))
        backoff = float(self.cfg.get("retry_backoff_seconds", 5))
        attempt = 0
        while True:
            res = await self.broker.place_order(
                ticker=ticker, side=side, qty=qty, limit_price=limit_price,
                order_type=order_type, ref_id=ref_id, dollar_amount=dollar_amount,
            )
            # Retry ONLY transient submission errors. Never retry an ambiguous
            # fill (needs_review) or a rejection — that risks a double fill.
            if res.status == "error" and attempt < retries:
                attempt += 1
                self.db.audit(self.run_id, "WARN", "order_retry",
                              {"ticker": ticker, "attempt": attempt, "detail": res.detail})
                await asyncio.sleep(backoff)
                continue
            res = await self._confirm_fill(res, ticker=ticker)
            self._record_order_fill(order_row, res, ticker=ticker, side=side, simulated=False)
            if res.status == "needs_review":
                self.db.audit(self.run_id, "ERROR", "order_needs_human_review",
                              {"ticker": ticker, "detail": res.detail})
            elif res.ok:
                self.db.audit(self.run_id, "INFO", f"order_{res.status}",
                              {"ticker": ticker, "side": side.value, "qty": res.filled_qty,
                               "price": res.fill_price, "broker_order_id": res.broker_order_id})
            return res

    # Broker states that mean the order will not change again.
    _TERMINAL_FILLED = ("filled",)
    _TERMINAL_DEAD = ("cancelled", "canceled", "rejected", "failed", "voided")

    async def _confirm_fill(self, res: OrderResult, *, ticker: str) -> OrderResult:
        """Poll the broker until the order settles, so the real fill is recorded.

        ``place_equity_order`` returns on ACCEPTANCE, not on execution, so a
        successful send usually reports ``submitted`` with no price. Left there,
        the caller falls back to the pre-trade reference price — which is how a
        $224.32 NVDA fill came to be booked at $212.03, taking its stop with it.

        Never invents a price: an order still pending when the attempts run out
        keeps its unconfirmed status and is flagged for review.
        """
        attempts = int(self.cfg.get("fill_poll_attempts", 5))
        interval = float(self.cfg.get("fill_poll_interval_seconds", 2))
        getter = getattr(self.broker, "get_order_fill", None)
        # Nothing to confirm: no order id, polling disabled, broker can't read
        # back, or the send already reported a complete fill.
        if (not res.ok or not res.broker_order_id or attempts <= 0
                or getter is None
                or (res.status == "filled" and res.fill_price > 0)):
            return res

        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(interval)
            info = await getter(res.broker_order_id)
            state = (info or {}).get("state", "")
            if not state:
                continue
            qty, price = info.get("filled_qty"), info.get("fill_price")
            if state in self._TERMINAL_DEAD:
                self.db.audit(self.run_id, "WARN", "order_not_filled",
                              {"ticker": ticker, "state": state,
                               "broker_order_id": res.broker_order_id})
                return OrderResult(
                    ok=False, status=state, broker_order_id=res.broker_order_id,
                    detail={**res.detail, "readback_state": state},
                )
            if state in self._TERMINAL_FILLED and qty and price:
                return OrderResult(
                    ok=True, status="filled", filled_qty=qty, fill_price=price,
                    broker_order_id=res.broker_order_id,
                    detail={**res.detail, "readback_state": state,
                            "fill_confirmed": True},
                )
            # A partial fill is real and worth recording, but the order is still
            # working — keep polling in case it completes within the window.
            if state == "partially_filled" and qty and price:
                res = OrderResult(
                    ok=True, status="partially_filled", filled_qty=qty,
                    fill_price=price, broker_order_id=res.broker_order_id,
                    detail={**res.detail, "readback_state": state},
                )

        if res.fill_price <= 0:
            self.db.audit(self.run_id, "WARN", "fill_unconfirmed", {
                "ticker": ticker, "broker_order_id": res.broker_order_id,
                "polled": attempts,
                "note": "order accepted but no fill price confirmed — cost basis "
                        "left unset rather than guessed from the reference price",
            })
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

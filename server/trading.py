"""Trading service — the plan → approve → execute flow for the web app.

Two user flows, both routed entirely through ``Orchestrator``/``Executor`` (every
order still passes the guardrails computed in the plan phase and re-checks the
kill switch at placement):

  * **Preview with per-order approval** — :func:`build` runs the pipeline to the
    guardrails and returns the approved orders WITHOUT placing anything; the
    Orchestrator is kept in a pending registry. :func:`execute` then places only
    the user-selected subset.
  * **One-click live** — :func:`live` builds the plan and immediately places every
    guardrail-approved order (the UI has already collected the typed confirmation).

A pending plan holds a live broker + account snapshot between the two HTTP
requests, so plan and execute share one ``run_id``. Building a new plan discards
any previous pending one (closing its run) so at most one is ever open.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from orchestrator import CyclePlan, Orchestrator
from server import engine
from server import format as F
from server import data

_PLAN_TTL_S = 30 * 60  # a built-but-unexecuted plan expires after 30 minutes


@dataclass
class _Pending:
    plan_id: str
    orch: Orchestrator
    plan: CyclePlan
    mode: str
    account: str | None
    created_ts: float


_pending: dict[str, _Pending] = {}


def _sweep() -> None:
    now = time.time()
    for pid, p in list(_pending.items()):
        if now - p.created_ts > _PLAN_TTL_S:
            _drop(pid)


def _drop(plan_id: str) -> None:
    p = _pending.pop(plan_id, None)
    if p is not None:
        try:
            p.orch.discard_plan()
        except Exception:
            pass


def _order_view(res, idx: int) -> dict:
    it = res.intent
    return {
        "idx": idx,
        "ticker": it.ticker,
        "side": it.side.value,
        "action": it.action,
        "shares": res.approved_shares,
        "shares_display": F.fmt_shares(res.approved_shares),
        "price": it.price,
        "price_display": F.fmt_money(it.price),
        "notional": res.approved_usd,
        "notional_display": F.fmt_money(res.approved_usd),
        "resized": res.resized,
        "sector": it.sector,
        "confidence": it.confidence,
        "rationale": it.rationale,
        "stop_display": F.fmt_money(it.stop_loss),
        "target_display": F.fmt_money(it.take_profit),
        "protective": bool(getattr(it, "protective_exit", False)),
        "reasons": res.reasons or [],
    }


def _rejected_view(res) -> dict:
    it = res.intent
    return {
        "ticker": it.ticker,
        "side": it.side.value,
        "action": it.action,
        "requested_display": F.fmt_money(it.usd_amount or (res.approved_usd or 0)),
        "reasons": res.reasons or [],
    }


def _plan_payload(pending: _Pending) -> dict:
    plan = pending.plan
    approved_idx = 0
    orders = []
    rejected = []
    for res in plan.results:
        if res.approved and res.approved_shares > 0:
            orders.append(_order_view(res, approved_idx))
            approved_idx += 1
        else:
            rejected.append(_rejected_view(res))
    acct = plan.account
    return {
        "plan_id": pending.plan_id,
        "run_id": pending.orch.run_id,
        "mode": pending.mode,
        "account": pending.account,
        "account_summary": {
            "equity_display": F.fmt_money(acct.equity),
            "cash_display": F.fmt_money(acct.cash),
            "positions": acct.open_position_count(),
        },
        "market_view": plan.decision.get("market_view", ""),
        "orders": orders,
        "rejected": rejected,
        "can_execute": bool(orders),
    }


async def build(mode: str = "preview", account: str | None = None) -> dict:
    """Run the pipeline to the guardrails and stash the plan. Returns a payload of
    proposed orders for the approval UI, or a ``halted`` result if a gate stopped
    the run."""
    _sweep()
    # Only one pending plan at a time — discard any earlier one.
    for pid in list(_pending):
        _drop(pid)

    cfg, role, _active = engine.prepare_config(mode=mode, account=account)
    orch = Orchestrator(cfg, assume_yes=True)  # the UI collects any live confirmation
    outcome = await orch.build_plan()
    if outcome.halted:
        return {"halted": True, "reason": outcome.halt_reason,
                "message": outcome.halt_message, "run_id": orch.run_id,
                "mode": mode, "account": role}

    plan_id = uuid.uuid4().hex[:12]
    pending = _Pending(plan_id=plan_id, orch=orch, plan=outcome.plan,
                       mode=mode, account=role, created_ts=time.time())
    _pending[plan_id] = pending
    payload = _plan_payload(pending)
    payload["halted"] = False
    if not payload["orders"]:
        # Nothing cleared the guardrails — close the run now, nothing to execute.
        _drop(plan_id)
        payload["plan_id"] = None
        payload["can_execute"] = False
    return payload


async def execute(plan_id: str, approved_indices: list[int]) -> dict:
    """Place the user-approved SUBSET of a pending plan's orders, then finish."""
    pending = _pending.get(plan_id)
    if pending is None:
        raise ValueError("plan not found or already used/expired — rebuild the plan")
    approved = pending.plan.approved
    chosen = [approved[i] for i in approved_indices if 0 <= i < len(approved)]
    if not chosen:
        _drop(plan_id)
        return {"placed": 0, "summary": None,
                "message": "No orders were approved — nothing placed."}
    try:
        # Preview mode calls the per-order confirm; the subset IS the approval, so
        # auto-confirm. Live mode never calls confirm. Kill switch still re-checked
        # per order inside the Executor.
        await pending.orch.execute_approved(
            pending.plan, chosen, confirm_callback=lambda order: True)
    finally:
        _pending.pop(plan_id, None)
    return _summary(pending.orch.run_id, len(chosen))


async def live(account: str | None = None) -> dict:
    """One-click live: build the plan and place EVERY guardrail-approved order."""
    _sweep()
    for pid in list(_pending):
        _drop(pid)

    cfg, role, _active = engine.prepare_config(mode="live", account=account)
    orch = Orchestrator(cfg, assume_yes=True)  # UI already collected the typed confirm
    outcome = await orch.build_plan()
    if outcome.halted:
        return {"halted": True, "reason": outcome.halt_reason,
                "message": outcome.halt_message, "run_id": orch.run_id}
    plan = outcome.plan
    if not plan.approved:
        orch.discard_plan()
        return {"halted": False, "placed": 0, "summary": None,
                "message": "Nothing cleared the guardrails. No orders placed."}
    await orch.execute_approved(plan, plan.approved)
    result = _summary(orch.run_id, len(plan.approved))
    result["halted"] = False
    return result


def discard(plan_id: str) -> dict:
    _drop(plan_id)
    return {"ok": True}


def pending_summary() -> dict:
    """The current pending plan (for recovering the approval view after a refresh)."""
    _sweep()
    if not _pending:
        return {"pending": False}
    pending = next(iter(_pending.values()))
    return {"pending": True, **_plan_payload(pending)}


def _summary(run_id: str, placed: int) -> dict:
    try:
        s = data.db().get_run_summary(run_id)
    except Exception:
        s = {}
    return {
        "placed": placed,
        "run_id": run_id,
        "summary": {
            "trades_executed": s.get("trades_executed", 0),
            "buys": s.get("buys", 0),
            "sells": s.get("sells", 0),
            "gross_bought_display": F.fmt_money(s.get("gross_bought", 0)),
            "gross_sold_display": F.fmt_money(s.get("gross_sold", 0)),
            "needs_review": s.get("needs_review", 0),
        },
    }

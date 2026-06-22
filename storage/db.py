"""SQLite storage layer — the full audit trail for the trading system.

Every meaningful event is persisted here so the system's behaviour can be
reconstructed after the fact:

  * ``agent_outputs`` — every agent's input + raw output (research, analysis,
    decision, monitor). This is what an LLM produced and what it saw.
  * ``decisions``     — proposed order intents and the guardrail verdict.
  * ``orders``        — every order we tried to place, its mode, and outcome.
  * ``fills``         — confirmed fills (real or simulated in paper mode).
  * ``positions``     — point-in-time snapshots of the broker's positions.
  * ``pnl``           — daily equity / drawdown snapshots.
  * ``audit_log``     — free-form, append-only operational events (halts, kill
                        switch, mode, rate-limit hits, errors).

The module uses only the Python stdlib (``sqlite3``). All writes are committed
immediately — an audit log you might lose on crash is not an audit log.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_outputs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    run_id       TEXT,
    agent        TEXT NOT NULL,       -- research|analysis|decision|monitor
    ticker       TEXT,                -- nullable for portfolio-level outputs
    model        TEXT,
    input_json   TEXT,                -- what the agent was given
    output_json  TEXT,                -- structured output the agent produced
    raw_text     TEXT                 -- raw model text (for debugging)
);

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    run_id       TEXT,
    ticker       TEXT NOT NULL,
    side         TEXT,                -- BUY|SELL
    action       TEXT,                -- buy|add|hold|trim|sell|pass
    requested_usd REAL,
    confidence   REAL,
    approved     INTEGER,             -- 1/0 guardrail verdict
    approved_usd REAL,
    resized      INTEGER,
    reasons      TEXT,                -- guardrail reasons (json list)
    checks_json  TEXT,                -- per-limit detail
    rationale    TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    run_id       TEXT,
    mode         TEXT NOT NULL,       -- paper|preview|live
    ticker       TEXT NOT NULL,
    side         TEXT NOT NULL,
    order_type   TEXT,                -- limit|market
    qty          REAL,
    limit_price  REAL,
    notional_usd REAL,
    status       TEXT,                -- intended|submitted|filled|rejected|error|cancelled
    broker_order_id TEXT,
    detail       TEXT                 -- error text / broker response (json)
);

CREATE TABLE IF NOT EXISTS fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    order_id     INTEGER,             -- FK -> orders.id
    ticker       TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          REAL,
    price        REAL,
    notional_usd REAL,
    simulated    INTEGER,             -- 1 for paper-mode simulated fills
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    run_id        TEXT,
    mode          TEXT,                -- paper|preview|live
    ticker        TEXT NOT NULL,
    side          TEXT,
    qty           REAL,
    price         REAL,
    notional_usd  REAL,
    status        TEXT,                -- submitting|simulated|submitted|filled|rejected|needs_review|skipped|duplicate|error
    broker_order_id  TEXT,
    client_order_id  TEXT,             -- idempotency key (run_id:ticker:side:qty)
    simulated     INTEGER,
    detail        TEXT
);
-- Idempotency: one trade per client_order_id. Lets begin_trade dedupe atomically.
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_client_oid
    ON trades(client_order_id) WHERE client_order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    run_id       TEXT,
    ticker       TEXT NOT NULL,
    shares       REAL,
    avg_cost     REAL,
    market_value REAL,
    sector       TEXT
);

CREATE TABLE IF NOT EXISTS pnl (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    run_id       TEXT,
    equity       REAL,
    cash         REAL,
    buying_power REAL,
    peak_equity  REAL,
    drawdown_pct REAL,
    realized_pnl REAL,
    unrealized_pnl REAL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    run_id       TEXT,
    level        TEXT,                -- INFO|WARN|ERROR|HALT
    event        TEXT NOT NULL,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_ts   TEXT NOT NULL,
    finished_ts  TEXT,
    mode         TEXT,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS trade_plans (
    ticker         TEXT PRIMARY KEY,    -- one active plan per held name
    entry_date     TEXT,                -- date the position was opened (for time-stop)
    entry_price    REAL,
    stop_loss      REAL,                -- price level set by the decision agent
    take_profit    REAL,                -- price level set by the decision agent
    max_hold_until TEXT,                -- ISO date swing time-stop
    thesis         TEXT,                -- original entry thesis (for the Monitor)
    run_id         TEXT,
    updated_ts     TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


class Database:
    """Thin, safe wrapper around the SQLite audit database."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema (no-op if present)."""
        for table, col, decl in [("trade_plans", "thesis", "TEXT")]:
            try:
                with self._cursor() as c:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        finally:
            cur.close()

    # -- runs --------------------------------------------------------------
    def start_run(self, run_id: str, mode: str, notes: str = "") -> None:
        with self._cursor() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs(run_id, started_ts, mode, notes) VALUES (?,?,?,?)",
                (run_id, _now(), mode, notes),
            )

    def finish_run(self, run_id: str) -> None:
        with self._cursor() as c:
            c.execute("UPDATE runs SET finished_ts=? WHERE run_id=?", (_now(), run_id))

    # -- agent outputs -----------------------------------------------------
    def log_agent_output(
        self,
        run_id: str,
        agent: str,
        *,
        ticker: str | None = None,
        model: str | None = None,
        input_obj: Any = None,
        output_obj: Any = None,
        raw_text: str | None = None,
    ) -> int:
        with self._cursor() as c:
            c.execute(
                """INSERT INTO agent_outputs(ts, run_id, agent, ticker, model,
                   input_json, output_json, raw_text) VALUES (?,?,?,?,?,?,?,?)""",
                (_now(), run_id, agent, ticker, model,
                 _json(input_obj), _json(output_obj), raw_text),
            )
            return c.lastrowid

    # -- decisions / guardrails -------------------------------------------
    def log_decision(self, run_id: str, result: Any) -> int:
        """Persist an OrderIntent + its GuardrailResult (duck-typed)."""
        intent = result.intent
        with self._cursor() as c:
            c.execute(
                """INSERT INTO decisions(ts, run_id, ticker, side, action,
                   requested_usd, confidence, approved, approved_usd, resized,
                   reasons, checks_json, rationale)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _now(), run_id, intent.ticker,
                    getattr(intent.side, "value", str(intent.side)),
                    intent.action, intent.requested_usd(), intent.confidence,
                    1 if result.approved else 0, result.approved_usd,
                    1 if result.resized else 0, _json(result.reasons),
                    _json([{"name": ch.name, "passed": ch.passed, "detail": ch.detail,
                            "cap_usd": ch.cap_usd} for ch in result.checks]),
                    intent.rationale,
                ),
            )
            return c.lastrowid

    # -- orders / fills ----------------------------------------------------
    def log_order(
        self, run_id: str, mode: str, *, ticker: str, side: str, order_type: str,
        qty: float, limit_price: float | None, notional_usd: float,
        status: str, broker_order_id: str | None = None, detail: Any = None,
    ) -> int:
        with self._cursor() as c:
            c.execute(
                """INSERT INTO orders(ts, run_id, mode, ticker, side, order_type,
                   qty, limit_price, notional_usd, status, broker_order_id, detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), run_id, mode, ticker, side, order_type, qty, limit_price,
                 notional_usd, status, broker_order_id, _json(detail)),
            )
            return c.lastrowid

    def update_order_status(self, order_id: int, status: str,
                            broker_order_id: str | None = None, detail: Any = None) -> None:
        with self._cursor() as c:
            c.execute(
                "UPDATE orders SET status=?, broker_order_id=COALESCE(?, broker_order_id), detail=? WHERE id=?",
                (status, broker_order_id, _json(detail), order_id),
            )

    def log_fill(self, order_id: int, *, ticker: str, side: str, qty: float,
                 price: float, simulated: bool) -> int:
        with self._cursor() as c:
            c.execute(
                """INSERT INTO fills(ts, order_id, ticker, side, qty, price,
                   notional_usd, simulated) VALUES (?,?,?,?,?,?,?,?)""",
                (_now(), order_id, ticker, side, qty, price, qty * price,
                 1 if simulated else 0),
            )
            return c.lastrowid

    # -- trades ledger (idempotent execution record) ----------------------
    def begin_trade(
        self, *, client_order_id: str, run_id: str, mode: str, ticker: str,
        side: str, qty: float, price: float, notional: float,
    ) -> tuple[int | None, bool, str]:
        """Reserve a trade row for ``client_order_id`` atomically.

        Returns ``(trade_id, is_new, status)``. ``is_new`` is False when a row
        for this client_order_id already exists — the caller must then treat the
        order as a duplicate and NOT submit it again (idempotency guard)."""
        with self._cursor() as c:
            c.execute(
                """INSERT OR IGNORE INTO trades(ts, run_id, mode, ticker, side, qty,
                   price, notional_usd, status, client_order_id, simulated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), run_id, mode, ticker, side, qty, price, notional,
                 "submitting", client_order_id, 0),
            )
            if c.rowcount == 1:
                return c.lastrowid, True, "submitting"
            row = c.execute(
                "SELECT id, status FROM trades WHERE client_order_id=?", (client_order_id,)
            ).fetchone()
            return (row["id"], False, row["status"]) if row else (None, True, "submitting")

    def finish_trade(
        self, trade_id: int | None, status: str, *, broker_order_id: str | None = None,
        price: float | None = None, qty: float | None = None,
        simulated: bool = False, detail: Any = None,
    ) -> None:
        if trade_id is None:
            return
        notional = (qty or 0.0) * (price or 0.0)
        with self._cursor() as c:
            c.execute(
                """UPDATE trades SET status=?, broker_order_id=COALESCE(?, broker_order_id),
                   price=COALESCE(?, price), qty=COALESCE(?, qty),
                   notional_usd=?, simulated=?, detail=? WHERE id=?""",
                (status, broker_order_id, price, qty, notional,
                 1 if simulated else 0, _json(detail), trade_id),
            )

    def get_run_summary(self, run_id: str) -> dict:
        """Aggregate orders / fills / P&L for a run (for the digest)."""
        with self._cursor() as c:
            trades = c.execute(
                "SELECT side, status, notional_usd FROM trades WHERE run_id=?", (run_id,)
            ).fetchall()
            decisions = c.execute(
                "SELECT approved FROM decisions WHERE run_id=?", (run_id,)
            ).fetchall()
            pnl = c.execute(
                "SELECT equity, cash, drawdown_pct, peak_equity FROM pnl WHERE run_id=? "
                "ORDER BY ts DESC LIMIT 1", (run_id,)
            ).fetchone()
        done = {"filled", "simulated", "submitted"}
        filled = [t for t in trades if t["status"] in done]
        buys = [t for t in filled if t["side"] == "BUY"]
        sells = [t for t in filled if t["side"] == "SELL"]
        bought = sum(t["notional_usd"] or 0.0 for t in buys)
        sold = sum(t["notional_usd"] or 0.0 for t in sells)
        return {
            "orders_proposed": len(decisions),
            "orders_approved": sum(1 for d in decisions if d["approved"]),
            "trades_executed": len(filled),
            "buys": len(buys),
            "sells": len(sells),
            "gross_bought": round(bought, 2),
            "gross_sold": round(sold, 2),
            "net_cash_flow": round(sold - bought, 2),
            "needs_review": sum(1 for t in trades if t["status"] == "needs_review"),
            "rejected_or_skipped": sum(
                1 for t in trades if t["status"] in ("rejected", "skipped", "duplicate", "error")
            ),
            "equity": pnl["equity"] if pnl else None,
            "cash": pnl["cash"] if pnl else None,
            "drawdown_pct": pnl["drawdown_pct"] if pnl else None,
        }

    # -- positions / pnl ---------------------------------------------------
    def snapshot_positions(self, run_id: str, positions: dict) -> None:
        with self._cursor() as c:
            for p in positions.values():
                c.execute(
                    """INSERT INTO positions(ts, run_id, ticker, shares, avg_cost,
                       market_value, sector) VALUES (?,?,?,?,?,?,?)""",
                    (_now(), run_id, p.ticker, p.shares, p.avg_cost,
                     p.market_value, p.sector),
                )

    def snapshot_pnl(self, run_id: str, *, equity: float, cash: float,
                     buying_power: float, peak_equity: float, drawdown_pct: float,
                     realized_pnl: float = 0.0, unrealized_pnl: float = 0.0) -> None:
        with self._cursor() as c:
            c.execute(
                """INSERT INTO pnl(ts, run_id, equity, cash, buying_power, peak_equity,
                   drawdown_pct, realized_pnl, unrealized_pnl) VALUES (?,?,?,?,?,?,?,?,?)""",
                (_now(), run_id, equity, cash, buying_power, peak_equity,
                 drawdown_pct, realized_pnl, unrealized_pnl),
            )

    # -- trade plans (decision agent -> monitor agent) --------------------
    def upsert_trade_plan(
        self, *, ticker: str, run_id: str, entry_price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
        max_hold_until: str | None = None, thesis: str | None = None,
    ) -> None:
        """Record/refresh a position's exit plan. ``entry_date`` and ``thesis``
        are set once, on first open, so the Monitor's time-stop measures from the
        real entry and the original thesis is preserved for thesis-break checks."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._cursor() as c:
            exists = c.execute(
                "SELECT ticker FROM trade_plans WHERE ticker=?", (ticker,)
            ).fetchone()
            if exists:
                c.execute(
                    """UPDATE trade_plans SET
                       stop_loss=COALESCE(?, stop_loss),
                       take_profit=COALESCE(?, take_profit),
                       max_hold_until=COALESCE(?, max_hold_until),
                       entry_price=COALESCE(entry_price, ?),
                       thesis=COALESCE(thesis, ?),
                       run_id=?, updated_ts=? WHERE ticker=?""",
                    (stop_loss, take_profit, max_hold_until, entry_price, thesis,
                     run_id, _now(), ticker),
                )
            else:
                c.execute(
                    """INSERT INTO trade_plans(ticker, entry_date, entry_price,
                       stop_loss, take_profit, max_hold_until, thesis, run_id, updated_ts)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (ticker, today, entry_price, stop_loss, take_profit,
                     max_hold_until, thesis, run_id, _now()),
                )

    def get_trade_plans(self) -> dict[str, dict]:
        """All stored exit plans, keyed by ticker."""
        with self._cursor() as c:
            c.execute("SELECT * FROM trade_plans")
            return {row["ticker"]: dict(row) for row in c.fetchall()}

    def delete_trade_plan(self, ticker: str) -> None:
        with self._cursor() as c:
            c.execute("DELETE FROM trade_plans WHERE ticker=?", (ticker,))

    def get_latest_research(self) -> dict[str, dict]:
        """Most recent research object per ticker (for the Monitor's thesis-break
        signals: technicals + news sentiment from the previous cycle)."""
        with self._cursor() as c:
            c.execute(
                """SELECT a.ticker, a.output_json FROM agent_outputs a
                   JOIN (SELECT ticker, MAX(id) AS mid FROM agent_outputs
                         WHERE agent='research' AND ticker IS NOT NULL
                         GROUP BY ticker) m ON a.id = m.mid"""
            )
            rows = c.fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            try:
                out[r["ticker"]] = json.loads(r["output_json"])
            except Exception:
                continue
        return out

    def get_latest_scores(self) -> dict[str, float]:
        """Composite scores from the most recent analysis output, by ticker.

        The Monitor runs before this cycle's analysis, so it uses the previous
        cycle's scores for its thesis-break check (fine at a daily cadence)."""
        with self._cursor() as c:
            c.execute(
                """SELECT output_json FROM agent_outputs WHERE agent='analysis'
                   ORDER BY ts DESC LIMIT 1"""
            )
            row = c.fetchone()
        if not row or not row["output_json"]:
            return {}
        try:
            arr = json.loads(row["output_json"])
            return {
                i["ticker"]: i.get("composite_score", i.get("score"))
                for i in arr
                if isinstance(i, dict) and i.get("ticker") is not None
            }
        except Exception:
            return {}

    def get_peak_equity(self, fallback: float) -> float:
        """Highest equity ever recorded in the pnl table (for drawdown calc)."""
        with self._cursor() as c:
            c.execute("SELECT MAX(MAX(equity, peak_equity)) AS peak FROM pnl")
            row = c.fetchone()
        peak = row["peak"] if row and row["peak"] is not None else None
        return max(peak, fallback) if peak is not None else fallback

    # -- audit / counts ----------------------------------------------------
    def audit(self, run_id: str, level: str, event: str, detail: Any = None) -> None:
        with self._cursor() as c:
            c.execute(
                "INSERT INTO audit_log(ts, run_id, level, event, detail) VALUES (?,?,?,?,?)",
                (_now(), run_id, level, event, _json(detail) if detail is not None else None),
            )

    def trades_today(self) -> int:
        """Count of orders that transacted today (UTC).

        Includes paper-mode ``simulated`` fills so the daily-trade cap is
        faithfully honoured across repeated paper runs within the same day,
        exactly as it would be in live mode.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._cursor() as c:
            c.execute(
                """SELECT COUNT(*) AS n FROM orders
                   WHERE substr(ts, 1, 10) = ? AND status IN ('submitted','filled','simulated')""",
                (today,),
            )
            return c.fetchone()["n"]

    # -- read helpers for the dashboard -----------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._cursor() as c:
            c.execute(sql, params)
            return c.fetchall()

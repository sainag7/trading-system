"""SQLite storage layer — the full audit trail for the trading system.

Every meaningful event is persisted here so the system's behaviour can be
reconstructed after the fact:

  * ``agent_outputs`` — every agent's input + raw output (research, analysis,
    decision, monitor). This is what an LLM produced and what it saw.
  * ``decisions``     — proposed order intents and the guardrail verdict.
  * ``orders``        — every order we tried to place, its mode, and outcome.
  * ``fills``         — confirmed fills from the broker.
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
import threading
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
    mode         TEXT NOT NULL,       -- recommend|preview|live
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
    simulated    INTEGER,             -- 1 for simulated fills (legacy rows)
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    run_id        TEXT,
    mode          TEXT,                -- recommend|preview|live
    ticker        TEXT NOT NULL,
    side          TEXT,
    qty           REAL,
    price         REAL,
    notional_usd  REAL,
    status        TEXT,                -- submitting|simulated|submitted|filled|rejected|needs_review|skipped|duplicate|error
    broker_order_id  TEXT,
    client_order_id  TEXT,             -- idempotency key (run_id:ticker:side:qty)
    simulated     INTEGER,
    detail        TEXT,
    account       TEXT                 -- brokerage account (NULL=legacy; backfilled from pnl)
);
-- Idempotency: one trade per client_order_id. Lets begin_trade dedupe atomically.
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_client_oid
    ON trades(client_order_id) WHERE client_order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    run_id       TEXT,
    account      TEXT,                -- brokerage account number (multi-account); NULL=legacy/single
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
    account      TEXT,                -- brokerage account number (multi-account); NULL=legacy/single
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
    account      TEXT,                -- brokerage account the run acted on (NULL=legacy/single)
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS trade_plans (
    ticker         TEXT NOT NULL,
    account        TEXT NOT NULL DEFAULT '',  -- brokerage account that holds the position ('' = single-account)
    entry_date     TEXT,                -- date the position was opened (for time-stop)
    entry_price    REAL,
    stop_loss      REAL,                -- price level (raised as the trailing stop ratchets up)
    take_profit    REAL,                -- price level set by the decision agent
    max_hold_until TEXT,                -- ISO date swing time-stop
    thesis         TEXT,                -- original entry thesis (for the Monitor)
    peak_price     REAL,                -- highest price seen since entry (chandelier trail)
    tp_taken       INTEGER DEFAULT 0,   -- 1 once the take-profit partial has been banked
    scaled_out     INTEGER DEFAULT 0,   -- 1 once a graded (stop-trail) partial has fired
    run_id         TEXT,
    updated_ts     TEXT,
    PRIMARY KEY (ticker, account)       -- one active plan per held name PER ACCOUNT
);

-- Post-close review agent: one row per review run.
CREATE TABLE IF NOT EXISTS reviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    run_id         TEXT,
    account        TEXT,
    review_date    TEXT,                -- trading date reviewed (YYYY-MM-DD)
    window_days    INTEGER,
    model          TEXT,
    scorecard_json TEXT,                -- deterministic scorecard (input to the model)
    output_json    TEXT,                -- the model's structured review
    markdown       TEXT                 -- rendered brief
);

-- Durable, evidence-backed rules the review agent maintains. Fed back into the
-- decision / monitor prompts while status='active'.
CREATE TABLE IF NOT EXISTS lessons (
    id                 TEXT NOT NULL,
    account            TEXT NOT NULL DEFAULT '',
    text               TEXT NOT NULL,
    evidence           TEXT,
    confidence         INTEGER,         -- 0-100
    applies_to         TEXT,            -- json list: ["decision","monitor"]
    status             TEXT,            -- active|retired
    created_ts         TEXT,
    last_reinforced_ts TEXT,
    times_reinforced   INTEGER DEFAULT 0,
    source_run_id      TEXT,
    PRIMARY KEY (id, account)
);

-- Parameter changes the review agent proposed, and what happened to them.
CREATE TABLE IF NOT EXISTS proposals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    run_id       TEXT,
    account      TEXT,
    key          TEXT NOT NULL,         -- dotted config key, e.g. strategy.stop_atr_mult
    current      REAL,
    proposed     REAL,
    applied      REAL,                  -- the value actually written (may be clamped)
    evidence     TEXT,
    n_trades     INTEGER,
    status       TEXT,                  -- proposed|applied|rejected|dismissed
    reason       TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    run_id        TEXT,
    account       TEXT,
    agent         TEXT,                -- research|analysis|decision|monitor|explain|market_data|execution
    model         TEXT,
    backend       TEXT,                -- anthropic_api|claude_agent_sdk
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cache_write_tokens INTEGER,
    cache_read_tokens  INTEGER,
    cost_usd      REAL,                -- NULL when the model is not priced (never 0 as a guess)
    cost_source   TEXT,                -- sdk_reported|computed|unpriced
    num_turns     INTEGER,
    duration_ms   INTEGER,
    ok            INTEGER,             -- 0 for a failed call (it still burned tokens)
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


class Database:
    """Thin, safe wrapper around the SQLite audit database.

    Thread-safety: research runs provider calls in worker threads
    (``asyncio.to_thread``) whose audit callbacks write here while the main
    thread also logs agent outputs — all on this single connection
    (``check_same_thread=False``). ``self._lock`` serialises every
    execute+commit so one thread's commit can never end another's transaction
    ("cannot commit - no transaction is active")."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema (no-op if present)."""
        for table, col, decl in [("trade_plans", "thesis", "TEXT"),
                                  ("trade_plans", "peak_price", "REAL"),
                                  ("trade_plans", "account", "TEXT"),
                                  ("trade_plans", "scaled_out", "INTEGER"),
                                  ("positions", "account", "TEXT"),
                                  ("pnl", "account", "TEXT"),
                                  ("runs", "account", "TEXT"),
                                  ("trades", "account", "TEXT")]:
            try:
                with self._cursor() as c:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self._migrate_trade_plans_per_account()
        self._backfill_accounts()

    def _migrate_trade_plans_per_account(self) -> None:
        """Rebuild ``trade_plans`` keyed by ``(ticker, account)``.

        The table was keyed by ticker alone, so a run on one brokerage account
        read and RATCHETED the other account's stops: the individual book's
        advice run trailed its own GOOGL (avg cost $368.68) into the row the
        agentic book used for a GOOGL bought at $344.94, leaving that position a
        stop 0.65% below entry. SQLite cannot alter a primary key, so the table
        is rebuilt once; legacy rows get ``account=''`` and are then attributed
        to the account of the run that wrote them (``_backfill_accounts``).
        """
        with self._cursor() as c:
            info = c.execute("PRAGMA table_info(trade_plans)").fetchall()
            cols = {r["name"] for r in info}
            pk = [r["name"] for r in sorted(info, key=lambda r: r["pk"]) if r["pk"]]
            if ("account" in cols and "tp_taken" in cols and "scaled_out" in cols
                    and pk == ["ticker", "account"]):
                return
            c.execute("ALTER TABLE trade_plans RENAME TO trade_plans_legacy")
            c.execute("""CREATE TABLE trade_plans (
                ticker TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
                entry_date TEXT, entry_price REAL, stop_loss REAL, take_profit REAL,
                max_hold_until TEXT, thesis TEXT, peak_price REAL,
                tp_taken INTEGER DEFAULT 0, scaled_out INTEGER DEFAULT 0,
                run_id TEXT, updated_ts TEXT,
                PRIMARY KEY (ticker, account))""")
            acct_expr = "COALESCE(account, '')" if "account" in cols else "''"
            tp_expr = "COALESCE(tp_taken, 0)" if "tp_taken" in cols else "0"
            so_expr = "COALESCE(scaled_out, 0)" if "scaled_out" in cols else "0"
            c.execute(f"""INSERT OR IGNORE INTO trade_plans(ticker, account, entry_date,
                entry_price, stop_loss, take_profit, max_hold_until, thesis, peak_price,
                tp_taken, scaled_out, run_id, updated_ts)
                SELECT ticker, {acct_expr}, entry_date, entry_price, stop_loss, take_profit,
                       max_hold_until, thesis, peak_price, {tp_expr}, {so_expr},
                       run_id, updated_ts
                FROM trade_plans_legacy""")
            c.execute("DROP TABLE trade_plans_legacy")

    def _backfill_accounts(self) -> None:
        """Attribute legacy rows to an account via the run that wrote them.

        ``pnl`` has carried ``account`` since multi-account support landed and is
        snapshotted at the start of every run, so ``run_id -> pnl.account`` is a
        reliable map for ``runs``, ``trades`` and the rebuilt ``trade_plans``.
        Rows whose run has no pnl snapshot stay unattributed. Idempotent."""
        with self._cursor() as c:
            c.execute("""UPDATE runs SET account = (
                            SELECT account FROM pnl WHERE pnl.run_id = runs.run_id
                              AND pnl.account IS NOT NULL ORDER BY pnl.id LIMIT 1)
                         WHERE account IS NULL""")
            c.execute("""UPDATE trades SET account = (
                            SELECT account FROM pnl WHERE pnl.run_id = trades.run_id
                              AND pnl.account IS NOT NULL ORDER BY pnl.id LIMIT 1)
                         WHERE account IS NULL""")
            c.execute("""UPDATE OR IGNORE trade_plans SET account = COALESCE((
                            SELECT account FROM pnl WHERE pnl.run_id = trade_plans.run_id
                              AND pnl.account IS NOT NULL ORDER BY pnl.id LIMIT 1), '')
                         WHERE account = ''""")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    # -- runs --------------------------------------------------------------
    def start_run(self, run_id: str, mode: str, notes: str = "",
                  account: str | None = None) -> None:
        with self._cursor() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs(run_id, started_ts, mode, account, notes) "
                "VALUES (?,?,?,?,?)",
                (run_id, _now(), mode, account, notes),
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
        account: str | None = None,
    ) -> tuple[int | None, bool, str]:
        """Reserve a trade row for ``client_order_id`` atomically.

        Returns ``(trade_id, is_new, status)``. ``is_new`` is False when a row
        for this client_order_id already exists — the caller must then treat the
        order as a duplicate and NOT submit it again (idempotency guard)."""
        with self._cursor() as c:
            c.execute(
                """INSERT OR IGNORE INTO trades(ts, run_id, mode, ticker, side, qty,
                   price, notional_usd, status, client_order_id, simulated, account)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), run_id, mode, ticker, side, qty, price, notional,
                 "submitting", client_order_id, 0, account),
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
    def snapshot_positions(self, run_id: str, positions: dict,
                           account: str | None = None) -> None:
        with self._cursor() as c:
            for p in positions.values():
                c.execute(
                    """INSERT INTO positions(ts, run_id, account, ticker, shares,
                       avg_cost, market_value, sector) VALUES (?,?,?,?,?,?,?,?)""",
                    (_now(), run_id, account, p.ticker, p.shares, p.avg_cost,
                     p.market_value, p.sector),
                )

    def snapshot_pnl(self, run_id: str, *, equity: float, cash: float,
                     buying_power: float, peak_equity: float, drawdown_pct: float,
                     realized_pnl: float = 0.0, unrealized_pnl: float = 0.0,
                     account: str | None = None) -> None:
        with self._cursor() as c:
            c.execute(
                """INSERT INTO pnl(ts, run_id, account, equity, cash, buying_power,
                   peak_equity, drawdown_pct, realized_pnl, unrealized_pnl)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (_now(), run_id, account, equity, cash, buying_power, peak_equity,
                 drawdown_pct, realized_pnl, unrealized_pnl),
            )

    # -- trade plans (decision agent -> monitor agent) --------------------
    @staticmethod
    def _acct(account: str | None) -> str:
        """Plans are keyed by ``(ticker, account)``; single-account mode uses ''."""
        return str(account) if account else ""

    def upsert_trade_plan(
        self, *, ticker: str, run_id: str, entry_price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
        max_hold_until: str | None = None, thesis: str | None = None,
        peak_price: float | None = None, account: str | None = None,
    ) -> None:
        """Record/refresh a position's exit plan for ``account``. ``entry_date``,
        ``thesis`` and ``peak_price`` are set once, on first open, so the
        Monitor's time-stop measures from the real entry, the original thesis is
        preserved, and the chandelier trail starts from the entry price. The plan
        is keyed by the account that actually holds the shares."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        acct = self._acct(account)
        with self._cursor() as c:
            exists = c.execute(
                "SELECT ticker FROM trade_plans WHERE ticker=? AND account=?",
                (ticker, acct),
            ).fetchone()
            if exists:
                c.execute(
                    """UPDATE trade_plans SET
                       stop_loss=COALESCE(?, stop_loss),
                       take_profit=COALESCE(?, take_profit),
                       max_hold_until=COALESCE(?, max_hold_until),
                       entry_price=COALESCE(entry_price, ?),
                       thesis=COALESCE(thesis, ?),
                       peak_price=COALESCE(peak_price, ?),
                       run_id=?, updated_ts=? WHERE ticker=? AND account=?""",
                    (stop_loss, take_profit, max_hold_until, entry_price, thesis,
                     peak_price, run_id, _now(), ticker, acct),
                )
            else:
                c.execute(
                    """INSERT INTO trade_plans(ticker, account, entry_date, entry_price,
                       stop_loss, take_profit, max_hold_until, thesis, peak_price,
                       tp_taken, scaled_out, run_id, updated_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?)""",
                    (ticker, acct, today, entry_price, stop_loss, take_profit,
                     max_hold_until, thesis, peak_price, run_id, _now()),
                )

    def mark_scaled_out(self, ticker: str, account: str | None = None) -> None:
        """Flag that a graded (stop-trail) partial exit has fired on this
        position. The Monitor reads it so the NEXT trailing-stop hit closes the
        remainder in full instead of halving it again — a position halved on
        every hit never actually exits, it just pays spread on ever-smaller stubs."""
        with self._cursor() as c:
            c.execute(
                "UPDATE trade_plans SET scaled_out=1, updated_ts=? WHERE ticker=? AND account=?",
                (_now(), ticker, self._acct(account)),
            )

    def mark_take_profit_taken(self, ticker: str, account: str | None = None) -> None:
        """The take-profit partial has been banked: the Monitor must not fire it
        again — the remainder rides the trailing stop only. Without this flag
        HPE was halved on four consecutive touches of the same target."""
        with self._cursor() as c:
            c.execute(
                "UPDATE trade_plans SET tp_taken=1, updated_ts=? WHERE ticker=? AND account=?",
                (_now(), ticker, self._acct(account)),
            )

    def prune_trade_plans(self, held: Iterable[str], account: str | None) -> list[str]:
        """Delete this account's plans for tickers no longer held; return them.

        Catches a sell made outside the system, or an exit whose fill landed
        after the poll window. Scoped to ``account`` on purpose: a plan is only
        ever pruned against a read of the account that owns it, so a run on the
        individual book can never discard the agentic book's plans. With
        ``account`` None nothing is pruned."""
        if account is None:
            return []
        keep = {str(t).upper() for t in held}
        acct = self._acct(account)
        with self._cursor() as c:
            rows = c.execute(
                "SELECT ticker FROM trade_plans WHERE account=?", (acct,)
            ).fetchall()
            stale = sorted(r["ticker"] for r in rows if r["ticker"] not in keep)
            for t in stale:
                c.execute("DELETE FROM trade_plans WHERE ticker=? AND account=?",
                          (t, acct))
        return stale

    def update_trail(self, ticker: str, *, stop_loss: float | None = None,
                     peak_price: float | None = None,
                     account: str | None = None) -> None:
        """Ratchet a position's chandelier trail: raise ``stop_loss`` and
        ``peak_price`` but NEVER lower them (a trailing stop only moves up). Called
        each monitor cycle. ``MAX(existing, new)`` keeps the higher; when either
        side is NULL the COALESCE chain falls back so nothing is clobbered.
        Scoped to ``account`` — a run never touches another book's plan."""
        with self._cursor() as c:
            c.execute(
                """UPDATE trade_plans SET
                   stop_loss  = COALESCE(MAX(stop_loss, ?), ?, stop_loss),
                   peak_price = COALESCE(MAX(peak_price, ?), ?, peak_price),
                   updated_ts = ? WHERE ticker=? AND account=?""",
                (stop_loss, stop_loss, peak_price, peak_price, _now(), ticker,
                 self._acct(account)),
            )

    def get_trade_plans(self, account: str | None = None) -> dict[str, dict]:
        """This account's stored exit plans, keyed by ticker."""
        with self._cursor() as c:
            c.execute("SELECT * FROM trade_plans WHERE account=?", (self._acct(account),))
            return {row["ticker"]: dict(row) for row in c.fetchall()}

    def delete_trade_plan(self, ticker: str, account: str | None = None) -> None:
        with self._cursor() as c:
            c.execute("DELETE FROM trade_plans WHERE ticker=? AND account=?",
                      (ticker, self._acct(account)))

    def recent_exits(self, account: str | None, days: int,
                     *, exclude_triggers: tuple[str, ...] = ()) -> dict[str, dict]:
        """Tickers this account SOLD (live, filled) in the last ``days`` calendar
        days, keyed by ticker with the latest sell's ``ts`` and Monitor trigger.
        Feeds the rebuy cooldown: NVDA was sold and rebought the next day three
        times in two weeks, paying market-order spread for no net change."""
        if days <= 0:
            return {}
        with self._cursor() as c:
            c.execute(
                """SELECT ticker, ts, detail FROM trades
                   WHERE side='SELL' AND mode='live'
                     AND status IN ('filled','submitted')
                     AND (? IS NULL OR account = ?)
                     AND substr(ts, 1, 10) >= date('now', ?)
                   ORDER BY id""",
                (account, account, f"-{int(days)} days"),
            )
            rows = c.fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            trig = None
            try:
                trig = (json.loads(r["detail"] or "{}") or {}).get("trigger")
            except Exception:
                pass
            if trig and trig in exclude_triggers:
                continue
            out[r["ticker"]] = {"ts": r["ts"], "trigger": trig}
        return out

    # -- review agent: reviews / lessons / proposals ----------------------
    def save_review(self, *, run_id: str, account: str | None, review_date: str,
                    window_days: int, model: str | None, scorecard: Any,
                    output: Any, markdown: str | None) -> int:
        with self._cursor() as c:
            c.execute(
                """INSERT INTO reviews(ts, run_id, account, review_date, window_days,
                   model, scorecard_json, output_json, markdown)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (_now(), run_id, account, review_date, int(window_days), model,
                 _json(scorecard), _json(output), markdown),
            )
            return c.lastrowid

    def _review_row(self, r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in ("scorecard_json", "output_json"):
            try:
                d[k[:-5]] = json.loads(d.pop(k) or "null")
            except Exception:
                d[k[:-5]] = None
        return d

    def latest_review(self, account: str | None = None) -> dict | None:
        with self._cursor() as c:
            if account is not None:
                c.execute("SELECT * FROM reviews WHERE account=? ORDER BY id DESC LIMIT 1",
                          (account,))
            else:
                c.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT 1")
            return self._review_row(c.fetchone())

    def review_history(self, account: str | None = None, limit: int = 30) -> list[dict]:
        """Recent reviews, newest first, WITHOUT the heavy json blobs."""
        with self._cursor() as c:
            if account is not None:
                c.execute("""SELECT id, ts, run_id, account, review_date, window_days, model
                             FROM reviews WHERE account=? ORDER BY id DESC LIMIT ?""",
                          (account, int(limit)))
            else:
                c.execute("""SELECT id, ts, run_id, account, review_date, window_days, model
                             FROM reviews ORDER BY id DESC LIMIT ?""", (int(limit),))
            return [dict(r) for r in c.fetchall()]

    def latest_playbook(self, account: str | None = None) -> str | None:
        """The playbook text from the most recent review (rewritten in full each
        review, so the latest one is the whole playbook)."""
        rev = self.latest_review(account)
        out = (rev or {}).get("output") or {}
        pb = out.get("playbook") if isinstance(out, dict) else None
        return str(pb).strip() if pb else None

    def upsert_lessons(self, account: str | None, lessons: list[dict],
                       source_run_id: str | None = None) -> dict:
        """Merge the review agent's lessons into the store.

        ``status: new`` inserts (or reinforces an existing id); ``reinforced``
        bumps the counter and refreshes text/evidence/confidence; ``retired``
        marks the lesson inactive. Returns counts. Ids are slugs the model
        chooses; the ``(id, account)`` key is what makes a lesson reinforceable
        across days instead of duplicated."""
        acct = self._acct(account)
        counts = {"new": 0, "reinforced": 0, "retired": 0, "skipped": 0}
        now = _now()
        with self._cursor() as c:
            for l in lessons or []:
                lid = str(l.get("id") or "").strip().lower().replace(" ", "-")[:80]
                text = str(l.get("text") or "").strip()
                if not lid or not text:
                    continue
                status = str(l.get("status") or "new").lower()
                conf = int(max(0, min(100, float(l.get("confidence") or 0))))
                applies = _json(l.get("applies_to") or ["decision"])
                evidence = str(l.get("evidence") or "")[:1000]
                row = c.execute("SELECT * FROM lessons WHERE id=? AND account=?",
                                (lid, acct)).fetchone()
                if status == "retired":
                    if row and row["status"] != "retired":
                        c.execute("UPDATE lessons SET status='retired', last_reinforced_ts=? "
                                  "WHERE id=? AND account=?", (now, lid, acct))
                        counts["retired"] += 1
                    continue
                if row and row["status"] == "retired":
                    # Retired stays retired. A person (or an earlier review) took
                    # this lesson out of force; the model re-emitting the same id
                    # must not quietly put it back. It can write a NEW id if the
                    # evidence really has changed.
                    counts["skipped"] += 1
                    continue
                if row:
                    c.execute(
                        """UPDATE lessons SET text=?, evidence=?, confidence=?, applies_to=?,
                           status='active', last_reinforced_ts=?,
                           times_reinforced=times_reinforced+1, source_run_id=?
                           WHERE id=? AND account=?""",
                        (text[:600], evidence, conf, applies, now, source_run_id, lid, acct))
                    counts["reinforced"] += 1
                else:
                    c.execute(
                        """INSERT INTO lessons(id, account, text, evidence, confidence,
                           applies_to, status, created_ts, last_reinforced_ts,
                           times_reinforced, source_run_id)
                           VALUES (?,?,?,?,?,?,'active',?,?,0,?)""",
                        (lid, acct, text[:600], evidence, conf, applies, now, now,
                         source_run_id))
                    counts["new"] += 1
        return counts

    def active_lessons(self, account: str | None = None, limit: int = 8) -> list[dict]:
        """Active lessons, most confident / most reinforced first."""
        with self._cursor() as c:
            c.execute(
                """SELECT * FROM lessons WHERE account=? AND status='active'
                   ORDER BY confidence DESC, times_reinforced DESC, last_reinforced_ts DESC
                   LIMIT ?""", (self._acct(account), int(limit)))
            rows = [dict(r) for r in c.fetchall()]
        for r in rows:
            try:
                r["applies_to"] = json.loads(r.get("applies_to") or "[]")
            except Exception:
                r["applies_to"] = []
        return rows

    def all_lessons(self, account: str | None = None) -> list[dict]:
        with self._cursor() as c:
            c.execute("""SELECT * FROM lessons WHERE account=?
                         ORDER BY status ASC, confidence DESC, last_reinforced_ts DESC""",
                      (self._acct(account),))
            rows = [dict(r) for r in c.fetchall()]
        for r in rows:
            try:
                r["applies_to"] = json.loads(r.get("applies_to") or "[]")
            except Exception:
                r["applies_to"] = []
        return rows

    def retire_lesson(self, lesson_id: str, account: str | None = None) -> bool:
        with self._cursor() as c:
            c.execute("UPDATE lessons SET status='retired', last_reinforced_ts=? "
                      "WHERE id=? AND account=?", (_now(), lesson_id, self._acct(account)))
            return c.rowcount > 0

    def save_proposal(self, *, run_id: str | None, account: str | None, key: str,
                      current: float | None, proposed: float | None,
                      applied: float | None, evidence: str | None, n_trades: int | None,
                      status: str, reason: str | None = None) -> int:
        with self._cursor() as c:
            c.execute(
                """INSERT INTO proposals(ts, run_id, account, key, current, proposed,
                   applied, evidence, n_trades, status, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), run_id, account, key, current, proposed, applied,
                 (evidence or "")[:1000], n_trades, status, reason),
            )
            return c.lastrowid

    def proposals(self, account: str | None = None, limit: int = 50) -> list[dict]:
        with self._cursor() as c:
            if account is not None:
                c.execute("SELECT * FROM proposals WHERE account=? ORDER BY id DESC LIMIT ?",
                          (account, int(limit)))
            else:
                c.execute("SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (int(limit),))
            return [dict(r) for r in c.fetchall()]

    def get_proposal(self, proposal_id: int) -> dict | None:
        with self._cursor() as c:
            c.execute("SELECT * FROM proposals WHERE id=?", (int(proposal_id),))
            r = c.fetchone()
            return dict(r) if r else None

    def mark_proposal(self, proposal_id: int, status: str, *,
                      applied: float | None = None, reason: str | None = None) -> None:
        with self._cursor() as c:
            c.execute(
                "UPDATE proposals SET status=?, applied=COALESCE(?, applied), "
                "reason=COALESCE(?, reason) WHERE id=?",
                (status, applied, reason, int(proposal_id)))

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

    # -- explain-mode report history ---------------------------------------
    def get_explain_reports(self, ticker: str, limit: int = 10) -> list[dict]:
        """Saved deep-research reports for a ticker, newest first."""
        with self._cursor() as c:
            c.execute(
                """SELECT ts, run_id, output_json FROM agent_outputs
                   WHERE agent='explain' AND ticker=? ORDER BY ts DESC LIMIT ?""",
                (ticker.upper(), int(limit)),
            )
            rows = c.fetchall()
        out = []
        for r in rows:
            try:
                report = json.loads(r["output_json"])
            except Exception:
                continue
            out.append({"ts": r["ts"], "run_id": r["run_id"], "report": report})
        return out

    def get_explain_tickers(self) -> list[str]:
        """Tickers that have at least one saved deep-research report."""
        with self._cursor() as c:
            c.execute(
                """SELECT ticker, MAX(ts) AS latest FROM agent_outputs
                   WHERE agent='explain' AND ticker IS NOT NULL
                   GROUP BY ticker ORDER BY latest DESC"""
            )
            return [r["ticker"] for r in c.fetchall()]

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

    def prior_pnl_for_account(self, account: str | None, exclude_run_id: str) -> dict | None:
        """The most recent pnl snapshot for ``account`` from a run OTHER than
        ``exclude_run_id`` (used by the read-sanity gate to compare the current
        read against the account's last known state before this run's snapshot)."""
        with self._cursor() as c:
            if account is not None:
                c.execute("SELECT equity, cash FROM pnl WHERE account = ? AND run_id != ? "
                          "ORDER BY id DESC LIMIT 1", (account, exclude_run_id))
            else:
                c.execute("SELECT equity, cash FROM pnl WHERE run_id != ? "
                          "ORDER BY id DESC LIMIT 1", (exclude_run_id,))
            row = c.fetchone()
        return dict(row) if row else None

    def get_peak_equity(self, fallback: float, account: str | None = None) -> float:
        """Highest equity ever recorded in the pnl table (for drawdown calc).

        When ``account`` is given the high-water mark is scoped to THAT account,
        so each brokerage account has its own peak — the agentic account's
        drawdown halt is never computed against the (much larger) individual
        account's equity history, and vice versa."""
        with self._cursor() as c:
            if account is not None:
                c.execute("SELECT MAX(MAX(equity, peak_equity)) AS peak FROM pnl "
                          "WHERE account = ?", (account,))
            else:
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
        """Count of orders that transacted today (UTC) for the daily-trade cap.

        The legacy ``simulated`` status is still counted so historical rows keep
        working; new orders are only ``submitted``/``filled``.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._cursor() as c:
            c.execute(
                """SELECT COUNT(*) AS n FROM orders
                   WHERE substr(ts, 1, 10) = ? AND status IN ('submitted','filled','simulated')""",
                (today,),
            )
            return c.fetchone()["n"]

    # -- LLM token / cost accounting ---------------------------------------
    def log_llm_usage(self, **f: Any) -> int:
        """Record one LLM call. Tolerant of missing keys — the caller is an
        observability sink, not a schema-checked writer."""
        with self._cursor() as c:
            c.execute(
                """INSERT INTO llm_usage(ts, run_id, account, agent, model, backend,
                   input_tokens, output_tokens, cache_write_tokens, cache_read_tokens,
                   cost_usd, cost_source, num_turns, duration_ms, ok, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), f.get("run_id"), f.get("account"), f.get("agent"),
                 f.get("model"), f.get("backend"),
                 f.get("input_tokens", 0), f.get("output_tokens", 0),
                 f.get("cache_write_tokens", 0), f.get("cache_read_tokens", 0),
                 f.get("cost_usd"), f.get("cost_source"),
                 f.get("num_turns"), f.get("duration_ms"),
                 f.get("ok", 1), f.get("error")),
            )
            return c.lastrowid

    # Shared projection so every usage view reports the same columns. `cost_usd`
    # is SUMmed separately from `unpriced` calls: a NULL cost means "we don't
    # know", and folding those into the total as zero would understate spend.
    _USAGE_COLS = """
        COUNT(*) AS calls,
        COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failed,
        COALESCE(SUM(input_tokens), 0)       AS input_tokens,
        COALESCE(SUM(output_tokens), 0)      AS output_tokens,
        COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
        COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens,
        COALESCE(SUM(cost_usd), 0.0)         AS cost_usd,
        COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END), 0) AS unpriced_calls
    """

    # `ts` is an ISO string with a 'T' separator while SQLite's datetime() uses a
    # space, so the two never compare cleanly. Every window filter therefore
    # compares DATE PREFIXES (YYYY-MM-DD), which is unambiguous — the same
    # approach `trades_today` already uses.
    _SINCE = "substr(ts, 1, 10) >= date('now', ?)"

    def usage_daily(self, days: int = 30) -> list[dict]:
        """Per-day totals, oldest first, for the dashboard chart."""
        return [dict(r) for r in self.query(
            f"""SELECT substr(ts, 1, 10) AS day, {self._USAGE_COLS}
                FROM llm_usage WHERE {self._SINCE}
                GROUP BY day ORDER BY day""",
            (f"-{int(days)} days",),
        )]

    def usage_grouped(self, by: str, days: int = 30) -> list[dict]:
        """Totals grouped by ``agent``, ``model`` or ``backend``, biggest spend first."""
        if by not in ("agent", "model", "backend"):
            raise ValueError(f"cannot group usage by {by!r}")
        return [dict(r) for r in self.query(
            f"""SELECT COALESCE({by}, 'unknown') AS {by}, {self._USAGE_COLS}
                FROM llm_usage WHERE {self._SINCE}
                GROUP BY {by} ORDER BY cost_usd DESC""",
            (f"-{int(days)} days",),
        )]

    def usage_totals(self, days: int | None = None) -> dict:
        """Grand totals over ``days`` (all time when ``None``)."""
        if days is None:
            rows = self.query(f"SELECT {self._USAGE_COLS} FROM llm_usage")
        else:
            rows = self.query(
                f"SELECT {self._USAGE_COLS} FROM llm_usage WHERE {self._SINCE}",
                (f"-{int(days)} days",),
            )
        return dict(rows[0]) if rows else {}

    def usage_for_run(self, run_id: str) -> dict:
        rows = self.query(
            f"SELECT {self._USAGE_COLS} FROM llm_usage WHERE run_id = ?", (run_id,))
        return dict(rows[0]) if rows else {}

    # -- read helpers for the dashboard -----------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._cursor() as c:
            c.execute(sql, params)
            return c.fetchall()

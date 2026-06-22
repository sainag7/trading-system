# Multi-Agent Swing-Trading System

A daily-cadence, multi-agent swing-trading system for US equities, built on the
**Claude Agent SDK**. It researches a watchlist, scores and ranks names, decides
buys/adds/holds/trims/sells, **forces every order through a deterministic risk
layer**, and (optionally) executes through the official **Robinhood Trading
MCP** — with a full SQLite audit trail and a global kill switch.

> Strategy: swing trades held **weeks to months**, a concentrated portfolio of
> **5–15 US equities**, evaluated **once per day** (not intraday / not HFT).

---

## ⚠️ RISK WARNING — READ THIS FIRST

**This software can place real orders with real money. Trading equities can lose
you money, potentially all of it. Nothing here is financial advice.**

- The system **defaults to `recommend` mode** (advice only — it trades nothing).
  It was **developed and tested without placing real orders**. Do not run `live`
  until you have read the code, run `recommend`/`paper` for a long time, and
  understand exactly what it will do.
- LLM agents are **fallible**. They can be confidently wrong. That is *why* a
  separate, deterministic, LLM-free **risk layer** ([risk/guardrails.py](risk/guardrails.py))
  sits between every decision and every order and can resize or reject it.
- The hard limits in [config.yaml](config.yaml) are *your* safety budget. They
  are enforced numerically and cannot be talked around by a model.
- A **global kill switch** halts all trading instantly (`python orchestrator.py
  --kill`, or set `kill_switch: true`, or create a `KILL_SWITCH` file).
- You are solely responsible for any orders placed. Markets, your broker's
  terms, and tax/regulatory obligations are your responsibility.
- Past performance, backtests, or paper results do **not** predict live results.

---

## Architecture

```
                 ┌─────────────────────────── orchestrator.py ───────────────────────────┐
                 │   --mode paper | preview | live      (checks KILL SWITCH first)        │
                 └────────────────────────────────────────────────────────────────────────┘
                                                │
   Research ──▶ Analysis ──▶ Decision ──▶  RISK (guardrails) ──▶ Execution ──▶ Monitor
   (per-ticker   (0–100      (buy/add/hold/   DETERMINISTIC,        Robinhood     (exits:
    JSON)         scores &    trim/sell +     NO LLM. Resizes or     MCP / paper   stop/target/
                  ranking)    size + conf.)   rejects every order.   simulator)    time/thesis)
        │            │             │                │                    │             │
        └────────────┴─────────────┴──── all inputs & outputs ──────────┴─────────────┘
                                    written to SQLite audit trail (storage/db.py)
```

The four agents (`agents/`) each have a dedicated system prompt in
[prompts/](prompts/). The risk layer is **plain Python** and never calls an LLM —
it is the one component you can fully unit-test and trust.

### Repo layout

| Path | What it does |
|------|--------------|
| [config.yaml](config.yaml) | Strategy params, **hard risk limits**, watchlist, models |
| [orchestrator.py](orchestrator.py) | Main loop; `--mode paper\|preview\|live`; kill switch |
| [agents/](agents/) | research / analysis / decision / monitor (+ shared `llm.py`) |
| [risk/guardrails.py](risk/guardrails.py) | **Deterministic** order validation (no LLM) |
| [risk/test_guardrails.py](risk/test_guardrails.py) | Thorough unit tests for every limit |
| [execution/executor.py](execution/executor.py) | Robinhood MCP broker + paper broker + retries |
| [data/providers.py](data/providers.py) | Alpha Vantage / yfinance / FRED with caching + rate limits |
| [storage/db.py](storage/db.py) | SQLite: trades, agent_outputs, positions, pnl, audit_log |
| [dashboard/app.py](dashboard/app.py) | Streamlit view of scores, positions, P&L, decision log |
| [smoke_test.py](smoke_test.py) | Offline end-to-end paper pipeline check (no keys needed) |

---

## Setup

```bash
cd trading-system
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # installs yfinance, anthropic, the Agent SDK, etc.

cp .env.example .env        # then fill in your keys
```

> **Run everything from the SAME environment you install into.** If you create a
> `.venv`, `pip install` into it AND run `python orchestrator.py` / `streamlit run`
> from that same activated venv. A common gotcha: installing in a venv but running
> from a different (e.g. anaconda base) Python — the deps won't be found and you'll
> get a degraded run (see below).

Edit `.env`:

- `ANTHROPIC_API_KEY` — **required for real recommendations** (the LLM agents).
- `ALPHAVANTAGE_API_KEY` — optional; market data (free tier = **25 requests/day**).
  Not needed if `yfinance` is installed (it's free and needs no key).
- `FRED_API_KEY` — optional macro data.
- Robinhood uses **OAuth handled by the MCP server** — there is **no API key or
  password in this repo**.

### Why is everything "pass" / score 50?

The agents need **market data** and (for real analysis) an **LLM**. If neither is
available, research produces no numbers, so every analysis score defaults to a
neutral **50** — which is below the buy threshold (65) — and **every stock shows
`pass`** with empty stop/target. The dashboard shows a ⚠️ banner when this happens.
To fix it:

1. `pip install -r requirements.txt` **in the env you run from** (gets `yfinance`,
   so you get prices with no key).
2. Set `ANTHROPIC_API_KEY` in `.env` (so the LLM agents actually analyze).
3. Re-run `python orchestrator.py --mode recommend` and refresh the dashboard.

With those, scores differentiate and you get real buy/hold/trim recommendations
with stops and targets. Without `ANTHROPIC_API_KEY` the system still runs on
deterministic heuristics (useful for testing), but the analysis is much thinner.

---

## The run modes

Mode is set by `--mode` (preferred), then `TRADING_MODE` env, then `config.yaml`.
**Default is `recommend`** (advice only — the safest setting while you evaluate the
agents). Switch to `paper` to track simulated P&L, and only to `preview`/`live`
once you trust it.

| Mode | What happens | Real orders? |
|------|--------------|:---:|
| `recommend` | Runs the full pipeline + guardrails and **prints/saves recommendations only** — places nothing, simulates nothing, writes no fills/trades/plans. Reads your real account read-only **if connected**, else a hypothetical book. Use it to judge the agents' decisions before risking money. | **No** |
| `paper` | Simulates fills against a persistent paper account and **tracks P&L/drawdown over time**. Best for judging *performance*. Reads no real account. | **No** |
| `preview` | Reads your real account, runs the full pipeline, prints each planned order and **asks for explicit confirmation** before sending it live. | Only after you confirm |
| `live` | Reads your real account and **places real orders** via the Robinhood Trading MCP, within the guardrails. Requires typing `I UNDERSTAND`. | **Yes** |

```bash
# Advice only — see what the agents would do (nothing is traded)
python orchestrator.py --mode recommend

# Simulate everything and track simulated P&L over time
python orchestrator.py --mode paper

# Plan against the real account, confirm each order by hand
python orchestrator.py --mode preview

# Place real orders (you will be warned and must confirm)
python orchestrator.py --mode live
python orchestrator.py --mode live --yes     # skip the typed confirmation
```

> **`recommend` vs `paper` for evaluation:** `recommend` answers *"what would you do
> today?"* (a daily advice list). `paper` actually simulates those trades and charts
> the resulting equity/drawdown in the dashboard, so it answers *"how would these
> decisions have performed?"* Use both. View either in the dashboard:
> `streamlit run dashboard/app.py` (top section shows the latest recommendations).

### Kill switch (emergency stop)

Any one of these halts **all** trading before any order, in every mode:

```bash
python orchestrator.py --kill          # creates the KILL_SWITCH file
# or set  kill_switch: true  in config.yaml
# or:     touch KILL_SWITCH
```

The orchestrator re-reads the switch before the cycle **and before every single
order**, so flipping it mid-run stops the next order. Remove the `KILL_SWITCH`
file (and/or set `kill_switch: false`) to resume.

---

## Hard risk limits (`config.yaml → risk:`)

Every one of these is enforced deterministically by `risk/guardrails.py`. An
order that violates a limit is **resized down** to the largest compliant size,
or **rejected**, and the reason is logged.

| Limit | Default | Meaning |
|-------|:---:|---------|
| `max_positions` | 15 | Max concurrent open positions |
| `max_position_pct` | 0.15 | No single name > 15% of account equity |
| `max_sector_pct` | 0.40 | No single sector > 40% of equity |
| `per_trade_max_usd` | 500 | Max $ notional per individual order |
| `daily_max_trades` | 5 | Max executed orders per day (paper fills count too) |
| `min_cash_reserve_pct` | 0.10 | Always keep ≥ 10% of equity in cash |
| `max_account_drawdown_halt_pct` | 0.15 | If down ≥ 15% from peak, halt new risk |
| `no_trade_list` | `[]` | Tickers never to buy (you may still sell to exit) |

**Two distinct stops, by design:**
- **Kill switch** = emergency stop → blocks *every* order (buys *and* sells).
- **Drawdown halt** = blocks *new risk* (buys/adds) but still **allows
  risk-reducing sells**, so you can never get trapped unable to exit while the
  account is bleeding.

Sizing picks the **smallest** binding cap among per-trade $, position %, sector
%, cash reserve, and buying power, then converts to shares (fractional if
enabled). Sells can never exceed shares held (no shorting / no overselling).

---

## Testing

The risk layer is the safety contract, so it is covered thoroughly:

```bash
pytest risk/test_guardrails.py -v      # 40+ tests: approve / resize / reject per limit
python smoke_test.py                   # offline end-to-end paper run (no keys)
```

`smoke_test.py` injects a deterministic stub data provider and asserts the full
pipeline runs, orders fill, the audit DB is populated, and **no guardrail is
breached** (no position > 15%, daily cap respected).

---

## Dashboard

```bash
streamlit run dashboard/app.py
```

A read-only view of equity/drawdown, current positions, latest scores, the
decision/guardrail log, orders, fills, and the audit log. It never trades.

---

## Data & rate limits

The Alpha Vantage free tier is **25 requests/day**. `data/providers.py` defends
the quota three ways:

1. **On-disk cache** (default 24h TTL — we trade daily, so intraday refetches
   are pointless). Cached reads never spend quota.
2. **A persistent daily request budget**; once spent, AV calls stop for the day
   and the system falls back to `yfinance`.
3. **Self-throttling** between live AV calls (≈5/min on the free tier).

Keep your `universe` focused (the default is 12 names) so a run never blows the
quota.

---

## Dynamic discovery (trending stocks)

Beyond the fixed `universe`, the system can **scan for trending / most-active
names** each run and fold them into that run's research — no permanent config
change. It uses **free sources only**: yfinance screeners (`most_actives` /
`day_gainers`), Yahoo Finance's trending endpoint, and Reddit
r/wallstreetbets + r/stocks "hot" mentions. Candidates are de-duplicated,
excluded against the universe + `no_trade_list`, then passed through a
**yfinance-only pre-filter** (price band + average volume) so it costs **zero
Alpha Vantage quota**. The top few by recent momentum are appended for the run.

It's **additive and silent-fail**: if a source is down or nothing passes the
filter, the run quietly proceeds on the fixed universe. Toggle it in
`config.yaml → discovery:`:

```yaml
discovery:
  dynamic_discovery: true     # master on/off
  max_discovered: 5           # cap appended per run
  min_avg_volume: 500000      # pre-filter thresholds (yfinance only, no AV cost)
  min_price: 2
  max_price: 500
  use_yfinance_screeners: true
  use_yahoo_trending: true
  use_reddit: true
```

Each run logs what it found and why each candidate was kept/dropped. Discovered
names go through the **same** research → analysis → decision → risk pipeline as
the fixed universe (≈1 extra Alpha Vantage call each, covered by the yfinance
fallback when the quota is spent). Requires `yfinance` installed + network; the
offline tests disable discovery so they stay deterministic.

---

## Performance / speed

A run's cost is **LLM calls + data fetching**, not the guardrails (those are pure
arithmetic — removing them buys nothing and only adds risk). The system is tuned
for speed by default:

- **Fast model preset** (`config.yaml → models:`) — Haiku for research/analysis/
  monitor, Sonnet for the final decision. Bump `decision_agent` to
  `claude-opus-4-8` for max depth on the one decision call (slower).
- **Deterministic research** (`research.llm_enrichment: false`) — research makes
  **no per-ticker LLM call**; the technicals/fundamentals/news-sentiment are
  computed in plain Python. This is the single biggest speedup. News still drives
  recommendations via the deterministic sentiment score + the analysis step.
- **Parallel research** (`research.concurrency: 5`) — tickers are fetched
  concurrently instead of one-by-one.
- **Fast LLM path** — the agents use the Anthropic Messages API directly (much
  faster than spinning up the Agent SDK runtime per call). **This requires
  `ANTHROPIC_API_KEY` in `.env`** — without it the agents fall back to the slower
  SDK path (or the offline heuristic).

So for the fastest, real (non-degraded) runs: **set `ANTHROPIC_API_KEY`** and keep
the defaults. To go even faster, shrink the `universe` / `discovery.max_discovered`
(fewer tickers = fewer calls) or raise `research.concurrency`. Only the execution
layer (Robinhood, live/preview) uses the Agent SDK + MCP.

---

## Scheduling a daily run

This is a daily-cadence system; run it once per trading day (e.g. shortly after
the open or near the close). Example cron (paper mode, weekdays 9:45 ET):

```cron
45 9 * * 1-5  cd /path/to/trading-system && .venv/bin/python orchestrator.py --mode paper >> run.log 2>&1
```

Promote to `--mode preview` only once you trust the paper results, and to
`--mode live` only with eyes open and the kill switch within reach.

---

## How execution talks to Robinhood

`execution/executor.py` configures the Robinhood Trading MCP
(`https://agent.robinhood.com/mcp/trading`) as a remote HTTP MCP server via the
Claude Agent SDK. OAuth is handled by the MCP/Claude Code; **no credentials live
in this repo.** Order placement uses narrow, imperative prompts ("place exactly
this order") and parses a strict-JSON confirmation. Submission errors retry with
backoff; an **ambiguous fill is never blindly retried** — it is flagged
`needs_review` for a human, because re-sending risks a double fill.

> Confirm the exact Robinhood MCP tool names against your connected server and
> set them in `RobinhoodMCPBroker` if they differ from the defaults.
```

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
  until you have read the code, run `recommend` for a long time, and understand
  exactly what it will do.
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
                 │   --mode recommend | preview | live   (checks KILL SWITCH first)       │
                 └────────────────────────────────────────────────────────────────────────┘
                                                │
   Research ──▶ Analysis ──▶ Decision ──▶  RISK (guardrails) ──▶ Execution ──▶ Monitor
   (per-ticker   (0–100      (buy/add/hold/   DETERMINISTIC,        Robinhood     (exits:
    JSON)         scores &    trim/sell +     NO LLM. Resizes or     Trading       stop/target/
                  ranking)    size + conf.)   rejects every order.   MCP)          time/thesis)
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
| [orchestrator.py](orchestrator.py) | Main loop; `--mode recommend\|preview\|live`; kill switch |
| [agents/](agents/) | research / analysis / decision / monitor (+ shared `llm.py`) |
| [risk/guardrails.py](risk/guardrails.py) | **Deterministic** order validation (no LLM) |
| [risk/test_guardrails.py](risk/test_guardrails.py) | Thorough unit tests for every limit |
| [execution/executor.py](execution/executor.py) | Robinhood MCP broker + idempotent order lifecycle + retries |
| [data/providers.py](data/providers.py) | Alpha Vantage / yfinance / FRED with caching + rate limits |
| [storage/db.py](storage/db.py) | SQLite: trades, agent_outputs, positions, pnl, audit_log |
| [server/](server/) | FastAPI backend for the web dashboard (over the same Python backend) |
| [web/](web/) | React dashboard frontend (built to `web/dist`, served by the server) |
| [start.command](start.command) | One-click launcher: build + serve the dashboard on localhost |
| [recommend_check.py](recommend_check.py) | Offline end-to-end pipeline check (no keys needed) |

---

## Setup

```bash
cd trading-system
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # installs yfinance, anthropic, the Agent SDK, etc.

cp .env.example .env        # then fill in your keys
```

> **Run everything from the SAME environment you install into.** If you create a
> `.venv`, `pip install` into it AND run `python orchestrator.py` / the dashboard
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
agents). Move to `preview`/`live` only once you trust it.

| Mode | What happens | Real orders? |
|------|--------------|:---:|
| `recommend` | Runs the full pipeline + guardrails and **prints/saves recommendations only** — places nothing, writes no fills/trades/plans. Reads your real account read-only **if connected**, else sizes advice against a hypothetical book (`recommend.hypothetical_cash`). | **No** |
| `preview` | Reads your real account, runs the full pipeline, prints each planned order and **asks for explicit confirmation** before sending it live. | Only after you confirm |
| `live` | Reads your real account and **places real orders** via the Robinhood Trading MCP, within the guardrails. Requires typing `I UNDERSTAND`. | **Yes** |

```bash
# Advice only — see what the agents would do (nothing is traded)
python orchestrator.py --mode recommend

# Plan against the real account, confirm each order by hand
python orchestrator.py --mode preview

# Place real orders (you will be warned and must confirm)
python orchestrator.py --mode live
python orchestrator.py --mode live --yes     # skip the typed confirmation
```

View recommendations in the dashboard: double-click `start.command` (or run
`uvicorn server.app:app` — see [Dashboard](#dashboard-web-app)). You can also run
scans, deep research, and preview/live trades right from the dashboard.

### Two accounts (`--account`, advice vs. autonomous)

Robinhood exposes multiple accounts under one login; every per-account MCP tool
takes an account number. Map friendly roles in `config.yaml → accounts:` (put the
real numbers in the gitignored `config.local.yaml`):

| Role | Typical use | Default mode |
|------|-------------|--------------|
| `individual` | Your main book — **advice only** | `recommend` / `explain` |
| `agentic` | A small book the agents **trade autonomously** | `preview` / `live` |

`--account` selects the account for a run. Advice modes default to `individual`,
trading modes to `agentic`, so **autonomous orders never touch your main book**:

```bash
python orchestrator.py --mode recommend --account individual   # advice on the big book
python orchestrator.py --mode live --yes --account agentic   # trade the small book
```

The `agentic` role also carries a **per-account risk overlay** (`accounts.agentic.risk`)
so a small book is actually tradable (e.g. `min_trade_usd: 1`) — deep-merged over
the base `risk:` for agentic runs only; the individual account keeps the strict
base limits. Three independent backstops prevent trading the wrong account:

1. **Robinhood** only permits agent orders on an account flagged
   `agentic_allowed=true` (rejects others server-side).
2. `place_equity_order` **requires** an explicit account number (no silent default).
3. **`accounts.agentic.max_equity_guard`** — this system refuses to place any
   order when the target account reads richer than the ceiling (default $500), so
   a mis-route to a large account can't trade locally either.

Each account keeps its **own** equity history / drawdown high-water mark, and a
**read-sanity gate** skips a live cycle if the (model-mediated) account read looks
like it under-reported holdings — so a bad read never trades on stale positions.

---

## Deep research on one stock (`--mode explain`)

```bash
python orchestrator.py --mode explain --ticker NVDA     # lowercase works too
```

A read-only, single-ticker briefing — the ticker does **not** need to be in your
watchlist. Every report ends with a **🎯 verdict** (buy / watch / avoid — or
add / hold / trim / sell when you already hold the name), derived
deterministically from the analysis composite score and *your* configured
strategy thresholds, with a confidence score whose dampeners (earnings event
risk, high
volatility, thin news, missing data) are listed explicitly. A buy verdict
includes informational stop/target levels — nothing is planned or ordered.
It also covers: what the company does (sourced from provider data),
returns over 1d/5d/1m/3m/YTD and where price sits vs SMA20/50/200/RSI/MACD/ATR,
**why it moved** (attributed ONLY to actually-fetched headlines — if the news is
thin it says *"no clear catalyst found in available news"* rather than inventing
a reason), the next earnings date with an event-risk flag, fundamentals,
**bull/base/bear scenarios with concrete levels** derived from support/
resistance + ATR (conditional levels, never a forecast), key risks, what to
watch, and — if your Robinhood account is connected — whether you already hold
the name and any stored trade plan.

Reports are persisted to SQLite (`agent_outputs`, agent='explain') and
re-readable in the dashboard's **🔎 Deep dive** tab, newest first — where you can
also **run a new deep dive directly** (type a ticker → Research) without touching
the CLI.

**Limits:** daily data (not intraday); move attribution is best-effort from the
available headlines only; scenario levels are mechanical (levels ± ATR), not
price targets; nothing here is financial advice.

---

## The strategy — momentum + quality, factor-ranked, risk-managed

**No system predicts stock prices.** The durable edge is *factor exposure* +
*risk management* + *consistency* — probabilistic, and it can underperform for
long stretches. This system is built around the best-evidenced version of that,
not a crystal ball. Set `strategy.methodology` back to `legacy` to restore the
old technical/fundamental/sentiment blend + fixed-% stops.

**Scoring is a cross-sectional factor model** (`agents/analysis_agent.py`). Each
name gets five smooth 0–100 factor scores, then they're **z-scored across the
run's universe** so selection is *relative* (own the strongest names), tilted on
top of an absolute score so a broken name is never bought just for being the
"least bad". Momentum + quality carry the weight (`analysis.factor_weights`):

| Factor | Default weight | Signals |
|---|---|---|
| Momentum | **0.30** | 12-1 return, trend vs 50/200-SMA, proximity to highs |
| Quality | **0.25** | margins, free cash flow, low leverage |
| Value | 0.15 | forward P/E, P/S, upside to the analyst target |
| Growth | 0.15 | revenue / EPS growth |
| Sentiment | 0.15 | news + analyst recommendation |

**Risk is volatility-based, not a flat percentage** (`strategy:`):
- **Stops** = `price − stop_atr_mult·ATR`, but **never risking more than
  `max_loss_pct` (10%)** — the hard cap that prevents a −42%-style hold.
- **Targets** sit at a fixed `target_r_multiple` (2.5×) reward:risk.
- **Trailing (chandelier):** each day the stop is raised to
  `peak − trail_atr_mult·ATR` and **never lowered**, so winners' stops ratchet up
  and losers are cut early. Persisted in `trade_plans.peak_price`.
- **Graded exits:** a broken downtrend (below the 200-SMA / large loss) is a full
  exit; a quality name merely *pulling back* to its trailing stop scales out to
  half. This is the difference between correctly dumping a −42% name and knifing a
  healthy pullback.
- **Sizing** shrinks high-ATR names (`vol_target_sizing`) so dollar risk is
  roughly constant across the book.

Deep-research briefings (`--mode explain`) are now **forward-informed**: a
directional **lean** (bullish/neutral/bearish), a rough **expected-return
estimate**, **analyst upside**, forward P/E, a probability-weighted **scenario
expected value**, and the trade's **reward:risk (R-multiple)** — all clearly
labeled estimates with wide error bars, not predictions. Numbers stay
deterministic; the model only narrates.

**Limitations — read this:** the system runs on **daily** data, once per day.
This is *idea generation*, **not** intraday day-trading. Prices gap overnight —
a stop does not guarantee its level as a worst case. The forward numbers are
estimates, not promises. Evaluate any change in `recommend` mode first.

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
| `daily_max_trades` | 5 | Max executed orders per day |
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
pytest risk/test_guardrails.py -v      # 59 tests: approve / resize / reject per limit
python recommend_check.py              # offline end-to-end pipeline run (no keys)
```

`recommend_check.py` injects a deterministic stub data provider and asserts the
full pipeline (research → analysis → decision → guardrails) runs and produces
recommendations with **zero trading side effects** — no fills, trades, plans, or
P&L rows are ever written by recommend mode.

---

## Dashboard (web app)

The dashboard is a local web app — a FastAPI server (`server/`) over the existing
Python backend, serving a React frontend (`web/`). **Zero terminal needed:**

```
double-click  start.command      # macOS: builds if needed, starts the server, opens your browser
```

Or run it directly:

```bash
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000   (stop.command, or Ctrl+C, stops it)
```

It binds to **localhost only** — it can place real orders, so it is never exposed
off-host. The `start.command` builds the frontend on first run (needs Node 18+).

Eight sections, everything runnable from the UI:

- **Overview** — status, last run, quick actions, top ideas.
- **Ideas** — ranked recommendations with score bars + a per-stock "why" drawer
  (technicals / fundamentals / news / scoring / guardrail).
- **Deep dive** — research any ticker (read-only briefing); researched tickers can
  be **removed** with the ✕ (reversible — restore from "Removed tickers").
- **Portfolio** — equity curve, positions, sector allocation, per-account.
- **Trade** — **plan → approve each order → place**, or **one-click live** after a
  typed confirmation. Every order still passes the guardrails and re-checks the
  kill switch; trading modes route to the `agentic` account by default.
- **Automation** — install/remove the daily launchd schedule and run diagnostics.
- **Activity** — decisions/orders/fills/audit, with friendly timestamps.
- **Settings** — the full config editor (strategy, weights, universe + sectors,
  discovery, models, data, execution, accounts, notifications, and the hard risk
  limits behind a confirmation). Edits write **`config.local.yaml`** (gitignored,
  merged over `config.yaml` at load); "Reset all overrides" deletes the overlay.
  API keys stay in `.env`, never in the dashboard.

A **kill switch** toggle (top bar) engages/releases the `KILL_SWITCH` file for an
instant halt. Long actions stream a **live log** in a docked panel.

The old Streamlit app (`dashboard/`) has been superseded by this web app.

---

## Data & rate limits

### Robinhood market data (default) — no Alpha Vantage quota for prices/fundamentals

With `data.use_robinhood_data: true` (the default), the system pulls **real-time
quotes and fundamentals** from your connected **Robinhood Trading MCP** —
quota-free. [data/robinhood_provider.py](data/robinhood_provider.py) wraps the
classic provider and **batch-fetches the whole universe once per run** (a couple
of compact multi-symbol MCP calls), so per-ticker reads hit an in-memory store.
Fundamentals Robinhood doesn't expose (operating margin, debt/equity, free cash
flow, beta, next-earnings date, company name) are still filled from `yfinance`;
`revenue_ttm / growth / margins / P-S / EPS` are computed from Robinhood's
quarterly `get_financials`. On **any** Robinhood miss it falls back to `yfinance`.

Each source does what it's best at:
- **Quotes + fundamentals** → Robinhood (compact, real-time, quota-free).
- **Daily OHLCV series** → **yfinance**. Robinhood *has* the data, but its MCP is
  LLM-mediated and a model can't reliably re-emit hundreds of bars per symbol as
  JSON, so series is served free by yfinance instead. Alpha Vantage is **not**
  used for series — its free tier made `TIME_SERIES_DAILY outputsize=full`
  premium-only (`data.use_alphavantage_series` defaults `false`; set `true` only
  with a premium AV key).
- **News sentiment** → **Alpha Vantage** — now the *only* AV endpoint used, so its
  25/day budget is spent on news alone.
- **Macro** (rates/CPI/unemployment) → **FRED**.

Set `use_robinhood_data: false` to use the pure Alpha-Vantage/yfinance path below
— the switch is fully reversible.

### Alpha Vantage / yfinance fallback

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

This is a daily-cadence system; run it **once per trading day** (e.g. ~30–60 min
after the open, so the opening auction settles). Runs are **single-shot and
idempotent** — there is no daemon. A **market-day gate** (`market.py`) makes any
weekend/NYSE-holiday firing a clean no-op for trading modes, so it is safe to
schedule on every weekday.

### The daily brief

Every scheduled run renders a markdown briefing to **`reports/YYYY-MM-DD.md`**
and fires a macOS notification when it is ready, so the output is pushed to you
rather than waiting in SQLite to be remembered. It contains:

- **Your positions** — *every* holding, with its action (add / hold / trim /
  sell), composite score **and the change since the previous run** (a decaying
  score is the trim signal), P&L, and the rationale. A holding that receives no
  verdict is listed as `NO VERDICT` and called out — a position silently missing
  from the report is the one failure this is built to prevent.
- **New ideas** — actionable names you don't already hold, with size/stop/target.
- **Needs your attention** — orders stuck in `needs_review`, guardrail resizes,
  kill-switch and drawdown state.
- A **⚠️ degraded run** banner when the decision agent fell back to its
  deterministic policy, or when every score is exactly 50 (no market data) — so
  a briefing built on missing inputs never reads like a confident one.

Render one by hand at any time (read-only, renders the latest run):

```bash
python -m reporting.brief                  # writes reports/<date>.md, prints the path
python -m reporting.brief --summary        # one-line summary, as used in the notification
python -m reporting.brief --run-id <RUN>   # a specific run
```

`reports/` and `logs/` are gitignored — they contain real holdings and equity.

### macOS launchd (recommended)

Schedule times are declared in **market time (ET)** and converted to this Mac's
local timezone at install; the installer prints both. Re-run it if the machine's
timezone changes.

```bash
./scheduling/install.sh                 # individual advice job (read-only), 10:00 ET weekdays
./scheduling/install.sh --enable-live   # ALSO schedule autonomous $100 trading — commission first!
./scheduling/uninstall.sh               # stop all scheduled runs
```

See [scheduling/README.md](scheduling/README.md) for the timezone caveat, logs,
and a cron alternative. Before enabling the live job, **commission it once**:

```bash
# Supervised: proposes an order on the $100 book and asks y/N before placing.
python orchestrator.py --mode preview --account agentic
```

Confirm a single small order lands in the Agentic account, then
`install.sh --enable-live`.

Repeated runs are safe by construction: `recommend` writes only advice (zero
trading side effects); in preview/live the `daily_max_trades` budget bounds the
day, held names are never re-bought as fresh positions (the decision agent sees
your positions; position/sector caps bound adds), and the executor's idempotency
key (plus the MCP `ref_id`) prevents double-submitting an order. Each position's
exit plan is stored at entry, so retuning the config later never rewrites the
exits on positions you already hold.

Promote to `--mode preview` only once you trust the recommendations, and to
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

### Troubleshooting: "Could not read the account from the broker"

The account read reaches Robinhood through the Agent SDK inheriting Claude Code's
OAuth'd `robinhood-trading` connection (`setting_sources=["local"]`). That session
can lapse (it survives a day or so, then the background/subprocess path stops
returning data even while interactive Claude Code still works). Symptoms: the read
returns no data and preview/live abort (they never fall back to a hypothetical
book — that would be unsafe). To fix:

```bash
python orchestrator.py --check-broker              # tests the connection, prints the real error
# if it reports "needs re-authentication":
#   start an interactive `claude` session from this repo, run  /mcp,
#   reconnect robinhood-trading, then re-run.
```

The failure is logged as `robinhood_mcp_no_response` (with the underlying error)
vs `robinhood_read_unparseable` (rare, stochastic — just re-run). For **scheduled
autonomous** runs, set `notifications.enabled: true` so a lapse is reported in the
daily brief instead of silently skipping the run.

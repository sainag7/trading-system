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
| [dashboard/app.py](dashboard/app.py) | Streamlit view of scores, positions, P&L, decision log |
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

View recommendations in the dashboard: `streamlit run dashboard/app.py` (the top
section shows the latest advice; equity/positions appear once your real account
is connected).

### Strategy profiles (`--profile`, orthogonal to `--mode`)

Independent of the mode, a **strategy profile** selects the parameter set the
agents run with (horizon, exit rules, scoring weights, screener appetite):

```bash
python orchestrator.py --mode recommend                      # swing (default)
python orchestrator.py --profile momentum --mode recommend   # quick-profit ideas
```

Profiles live in `config.yaml → strategy_profiles:` and only overlay soft
strategy/analysis/discovery parameters — **the hard `risk:` limits are never
touched by a profile**. See "Momentum profile" below.

---

## Deep research on one stock (`--mode explain`)

```bash
python orchestrator.py --mode explain --ticker NVDA     # lowercase works too
```

A read-only, single-ticker briefing — the ticker does **not** need to be in your
watchlist. Every report ends with a **🎯 verdict** (buy / watch / avoid — or
add / hold / trim / sell when you already hold the name), derived
deterministically from the analysis composite score and *your* configured
strategy thresholds (profile-aware: `--profile momentum` applies its stricter
bar), with a confidence score whose dampeners (earnings event risk, high
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

## Momentum profile (quick-profit ideas)

`--profile momentum` re-tunes the same pipeline for **short-horizon momentum
trades: a few days to ~2 weeks**, aiming to surface names likely to move soon
and get in and out fast:

- **Leans on the discovery screener** (`max_discovered: 10`) — momentum,
  breakouts, volume spikes, gainers — and **weights technicals 65%** of the
  composite (fundamentals drop to 10%; news sentiment 25%).
- **Fast, tight exits**, all config-driven under `strategy_profiles.momentum`:
  | Param | Momentum | Swing (base) |
  |---|---|---|
  | `default_stop_loss_pct` | **5%** | 8% |
  | `default_take_profit_pct` | **8%** | 20% |
  | `max_holding_days` (time-stop) | **10** | 120 |
  | `min_score_to_buy` | **70** | 65 |
  | `target_portfolio_size` | **5** | 10 |
- Exits are stored **per position at entry** (`trade_plans`), so a momentum
  position keeps its tight stop/target/time-stop even if your next run uses the
  swing profile — and vice versa.

**Same pipeline, same safety.** Research → Analysis → Decision → **Guardrails**
→ Execution is unchanged; there is no path to an order that skips the risk
layer. Profiles may only *tighten* per-position exits; every hard limit in
`risk:` (position/sector caps, per-trade $, daily trade cap, cash floor,
drawdown halt, kill switch) applies identically.

**Limitations — read this:** the system runs on **daily** data, once per day.
This is days-to-2-weeks *idea generation*, **not** intraday day-trading. Prices
gap overnight — a 5% stop does not guarantee a −5% worst case. Short-horizon
momentum trading has higher turnover and is riskier than swing holding; evaluate
it in `recommend` mode first, like everything else.

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

## Dashboard

```bash
streamlit run dashboard/app.py
```

An interactive control center with four tabs — **Ideas** (recommendations with
score bars + click-through "why this stock" drill-down), **Portfolio**
(equity/positions once your real account is connected), **Settings**, and
**Activity** (decision/guardrail log, orders, fills, audit log).

- **Run scans from the sidebar** — pick a profile (swing/momentum) and hit Run.
  A live log streams while the scan runs and results refresh automatically.
- **Run deep research from the Deep dive tab** — type any ticker and hit
  🔎 Research to generate a full briefing without the CLI.
- Both launchers execute **read-only modes only** (`recommend` / `explain`,
  hardcoded): the dashboard can never place a trade. Preview/live remain CLI-only.
- **Kill switch** — engage from the sidebar (creates the `KILL_SWITCH` file,
  halting all trading instantly); release requires a confirm.
- **Settings** — edits everything including the hard risk limits (those require
  an explicit confirmation). Changes are written to **`config.local.yaml`**
  (gitignored, machine-managed) and merged over `config.yaml` at load time, so
  your commented base config is never rewritten. "Reset all overrides" deletes
  the overlay. API keys stay in `.env`, never in the dashboard.

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
the open or near the close). Runs are **single-shot and idempotent** — there is
no daemon; schedule them with cron:

```cron
# Swing ideas every weekday morning (9:45 ET)
45 9 * * 1-5   cd /path/to/trading-system && .venv/bin/python orchestrator.py --mode recommend >> run.log 2>&1

# Momentum quick-profit scan near the close (15:30 ET) — or use `* * 1`/`* * 1,3,5`
# for weekly / Mon-Wed-Fri cadence
30 15 * * 1-5  cd /path/to/trading-system && .venv/bin/python orchestrator.py --profile momentum --mode recommend >> momentum.log 2>&1
```

Repeated runs are safe by construction: `recommend` writes only advice (zero
trading side effects); in preview/live the `daily_max_trades` budget is shared
across profiles, held names are never re-bought as fresh positions (the decision
agent sees your positions; position/sector caps bound adds), and the executor's
idempotency key prevents double-submitting an order. Each position's exit plan
is stored at entry, so mixing profiles across runs never mixes up exits.

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

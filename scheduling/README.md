# Scheduling — run the agents once per trading day

This is a **daily-cadence** system (free-tier daily bars, ~25 quotes/day). It is
designed to run **once per trading day**, not continuously intraday. Two jobs:

| Job | Time (local) | Command | What it does |
|-----|--------------|---------|--------------|
| Individual advice | 10:00, weekdays | `--mode recommend --account individual` | Reads your ~$3k account **read-only** and produces advice. Never trades. |
| Agentic autonomous | 10:05, weekdays | `--mode live --yes --profile momentum --account agentic` | Trades the **$100** agentic account hands-off, within guardrails. |

Running ~30–60 min after the 9:30 ET open lets the opening auction settle. The
orchestrator's **market-day gate** (`market.py`) makes any weekend/NYSE-holiday
firing a clean no-op, so it is safe to schedule on every weekday.

## Install (macOS launchd)

```bash
# Advice job only (read-only). Safe to run anytime.
./scheduling/install.sh

# ALSO schedule autonomous live trading on the $100 account.
# Do this ONLY after the supervised commissioning order (see below).
./scheduling/install.sh --enable-live      # asks you to type "ENABLE LIVE"
```

Inspect / logs / remove:

```bash
launchctl list | grep trading-system
tail -f logs/scheduled.log
./scheduling/uninstall.sh                  # stop all scheduled runs
python orchestrator.py --kill              # instant emergency stop (kill switch)
```

## Before enabling the live job — commission it (once)

The order-placement path is real but should be proven once with you watching:

```bash
# Supervised: proposes an order on the $100 account and asks y/N before placing.
python orchestrator.py --mode preview --profile momentum --account agentic
```

Confirm a single small order, check it landed in the **Agentic** account in the
Robinhood app, then enable the autonomous job with `install.sh --enable-live`.

## Notes / caveats

- **Timezone:** launchd fires at the Mac's *local* time. The 10:00/10:05 defaults
  assume US/Eastern (market time). If this Mac is not on Eastern time, edit
  `HOUR_ADVICE` / `HOUR_LIVE` in `install.sh`.
- **cron alternative** (if you prefer): the wrapper is cron-friendly —
  ```cron
  0  10 * * 1-5  /ABS/PATH/trading-system/scheduling/run.sh --mode recommend --account individual
  5  10 * * 1-5  /ABS/PATH/trading-system/scheduling/run.sh --mode live --yes --profile momentum --account agentic
  ```
- **Holidays:** `market.py` carries the NYSE holiday list; extend `NYSE_HOLIDAYS`
  a year ahead periodically.
- **Reliability:** the account read is model-mediated (~85% reliable). The
  read-sanity gate skips a live cycle if the read looks like it under-reported
  holdings, so a bad read never trades on stale position data — it just waits for
  the next day (re-run manually to retry sooner).

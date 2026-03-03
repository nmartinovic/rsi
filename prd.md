# Product Requirements Document: LEAP RSI Alert Monitor

## Overview

Build a lightweight, automated system that:

1. **Scans the S&P 1500 daily** for stocks with weekly RSI below 30, using rolling week-over-week candles anchored to the current day
2. **Calculates two RSI values:** a Confirmed RSI (completed weeks only) and a Current RSI (includes the in-progress week)
3. **Checks oversold stocks weekly** for LEAP options availability (>360 DTE)
4. **Sends a Pushover notification** when an oversold stock (Confirmed RSI < 30) has LEAPs available
5. **Suppresses duplicate alerts** until the Confirmed RSI recovers above 30
6. **Serves a status dashboard** via GitHub Pages showing system health, qualifying stocks, and early warning signals from Current RSI

The system should run at $0/month with minimal infrastructure.

---

## Architecture

### Stack

| Component | Choice | Rationale |
|---|---|---|
| Runtime | Python 3.11+ | Best financial data library support |
| Scheduler | GitHub Actions (cron) | Free for public repos; 2,000 min/month private |
| Market Data | `yfinance` library | Free, no API key, bulk downloads + options chains |
| Notifications | Pushover API (HTTP POST) | Already in use |
| State Storage | JSON files committed to repo | Zero-cost persistence |
| Dashboard | GitHub Pages (static HTML) | Free hosting, auto-deploys, no server |

### Key Design Decisions

**RSI-first, LEAPs-second:** Instead of discovering all LEAP-eligible stocks first (8,000+ individual `.options` calls), we scan RSI on the full S&P 1500 daily using efficient bulk downloads, then only check `.options` on the small number of oversold stocks. This reduces weekly options chain lookups from ~8,000 to ~100-300.

**Rolling weekly candles from daily data:** We do NOT use `yf.download(interval="1wk")` because Yahoo's weekly candles use fixed calendar weeks (Mon-Fri), not rolling windows. Instead, we pull daily OHLCV data and build custom 5-trading-day candles anchored to the current day, giving us true week-over-week RSI regardless of what day we run.

**Dual RSI signals:** Confirmed RSI (stable, completed weeks) drives alerts. Current RSI (includes in-progress week) provides early warning on the dashboard.

### Two Workflows

#### Workflow 1: Daily RSI Scan (Monday-Friday after market close)

- Load S&P 1500 constituent list
- Bulk-download daily OHLCV via `yf.download()` in batches of 50
- Build rolling weekly candles and calculate Confirmed RSI + Current RSI
- Save results and update dashboard

#### Workflow 2: Weekly LEAP Check + Alert (Sunday evening)

- Load oversold stocks (Confirmed RSI < 30)
- Check `yfinance.Ticker(symbol).options` for expirations >360 days out
- Send Pushover alerts for qualifying stocks (if not suppressed)
- Update suppression state and dashboard

---

## Detailed Requirements

### 1. S&P 1500 Universe Management

**Source options (in order of preference):**
- Wikipedia S&P 500 / S&P 400 / S&P 600 pages (scrapeable, updated regularly)
- A static CSV in the repo, refreshed manually or via a quarterly workflow

**Store as:** `data/sp1500_universe.json`
```json
{
  "updated_at": "2026-03-01T00:00:00Z",
  "source": "wikipedia",
  "count": 1498,
  "tickers": ["AAPL", "MSFT", "GOOG", "..."]
}
```

The universe does not need to be perfectly current. A quarterly refresh is fine. Add a `--refresh-universe` flag to manually update it.

### 2. Rolling Weekly RSI Calculation (CRITICAL SECTION)

This is the core logic. The goal is to compute a true week-over-week RSI anchored to whatever day we run, not Yahoo's fixed calendar weeks.

#### Step 1: Pull Daily Data

Use `yf.download(tickers, period="6mo", interval="1d")` to get daily OHLCV. 6 months of daily data gives ~126 trading days, which is ~25 rolling weeks — more than enough for 14-period RSI with warm-up.

#### Step 2: Build Rolling Weekly Candles

Group daily bars into 5-trading-day blocks, working backwards from the most recent trading day.

**Example: Running on Tuesday March 3, 2026.**

The most recent trading day is Tuesday March 3. Count backwards in blocks of 5 trading days:

```
Current (in-progress) week:  Wed Feb 26, Thu Feb 27, Fri Feb 28, Mon Mar 2, Tue Mar 3
                             (5 complete trading days — this is a full rolling week)

If running on Wednesday March 4 instead, current week would be:
                             Thu Feb 27, Fri Feb 28, Mon Mar 2, Tue Mar 3, Wed Mar 4
                             (also 5 trading days)

If running on Monday March 2, current week would be:
                             Thu Feb 20 (last trading day before this block starts counting back)
                             ...actually: Wed Feb 26, Thu Feb 27, Fri Feb 28, Mon Mar 2
                             (only 4 trading days so far — this is the in-progress week)
```

More precisely, the algorithm is:

1. Get the sorted list of trading days from the daily data (dates where we have price data)
2. Starting from the most recent trading day, take the last N trading days where N = number of trading days since the start of the most recent 5-day block
3. Working backwards from there, group into blocks of exactly 5 trading days each

**For each weekly candle, compute:**
- Open: first day's open in the block
- High: max of all highs in the block
- Low: min of all lows in the block
- Close: last day's close in the block
- Volume: sum of all volumes in the block

**Separate into two series:**
- **Confirmed weekly candles:** All completed 5-trading-day blocks (excluding the current in-progress block)
- **Current weekly candle:** The most recent block, which may have fewer than 5 trading days if we are mid-week

If the current block happens to have exactly 5 trading days, it is still considered the "current" (in-progress) week for the purposes of Current vs Confirmed RSI. The distinction is about the most recent block vs all prior blocks, not about whether it has 5 days.

#### Step 3: Calculate RSI (Wilder Smoothing, 14-Period)

Apply this calculation twice — once for Confirmed candles only, once including the Current candle:

1. **Price changes:** For each weekly candle, `change = close - previous_close`
2. **Separate gains and losses:**
   - If change > 0: gain = change, loss = 0
   - If change < 0: gain = 0, loss = abs(change)
3. **First averages (period 14):** Simple mean of the first 14 gains and first 14 losses
4. **Subsequent averages (Wilder smoothing):**
   - `avg_gain = (prev_avg_gain * 13 + current_gain) / 14`
   - `avg_loss = (prev_avg_loss * 13 + current_loss) / 14`
5. **RSI formula:**
   - `RS = avg_gain / avg_loss`
   - `RSI = 100 - (100 / (1 + RS))`
   - Edge case: if `avg_loss == 0`, RSI = 100

**Confirmed RSI:** Use only the completed weekly candles. This is the stable signal that drives alerts.

**Current RSI:** Use all completed weekly candles PLUS the current in-progress weekly candle appended as the most recent period. This is the early warning signal shown on the dashboard.

#### Important Notes

- The rolling week anchoring means RSI values will differ slightly from what you see on charting platforms that use fixed Mon-Fri weeks. This is intentional and correct for our use case.
- With 6 months of daily data (~25 rolling weeks), we have 14 weeks for RSI warm-up and ~11 weeks of valid RSI output. This is sufficient.
- If a ticker has missing daily data (e.g. trading halted), skip those dates naturally. A weekly candle may have fewer than 5 days in rare cases. This is fine.

### 3. Daily RSI Scan (`scripts/check_rsi.py`)

**Input:** `data/sp1500_universe.json`

**Process:**
1. Load S&P 1500 ticker list
2. Bulk-download daily price data using `yf.download(ticker_list, period="6mo", interval="1d")` in batches of 50 tickers per call
3. For each ticker, build rolling weekly candles as described above
4. Calculate Confirmed RSI and Current RSI
5. Save all values to `data/rsi_values.json`
6. Save tickers with Confirmed RSI < 30 to `data/oversold.json`
7. Generate dashboard data

**Rate Limiting:**
- Use `yf.download()` bulk API exclusively (NOT individual `Ticker.history()` calls)
- Batch size: 50 tickers per call
- Delay between batches: 5 seconds
- On 429 error: backoff 10s / 30s / 60s, retry batch up to 3 times
- ~1,500 tickers / 50 per batch = 30 calls with 5s gaps = ~3 minutes total

**Output files:**

`data/rsi_values.json`:
```json
{
  "updated_at": "2026-03-03T22:15:00Z",
  "run_date": "2026-03-03",
  "values": {
    "AAPL": {
      "confirmed_rsi": 55.2,
      "current_rsi": 53.8,
      "price": 187.43
    },
    "INTC": {
      "confirmed_rsi": 24.3,
      "current_rsi": 22.1,
      "price": 22.15
    }
  }
}
```

`data/oversold.json`:
```json
{
  "updated_at": "2026-03-03T22:15:00Z",
  "confirmed_oversold": [
    {"ticker": "INTC", "confirmed_rsi": 24.3, "current_rsi": 22.1, "price": 22.15},
    {"ticker": "WBA", "confirmed_rsi": 18.7, "current_rsi": 19.2, "price": 9.42}
  ],
  "current_only_oversold": [
    {"ticker": "XYZ", "confirmed_rsi": 33.1, "current_rsi": 28.5, "price": 45.60}
  ]
}
```

The `current_only_oversold` list captures stocks where Confirmed RSI is still above 30 but Current RSI has dipped below — these are early warning candidates shown on the dashboard but not used for alerts.

### 4. Weekly LEAP Check and Alerts (`scripts/check_leaps.py`)

**Input:** `data/oversold.json`, `data/alert_state.json`

**Process:**
1. Load the `confirmed_oversold` stocks list (NOT current_only_oversold — those are dashboard-only)
2. For each ticker, call `yfinance.Ticker(symbol).options` to get available expiration dates
3. Check if `max(expiration_dates) - today > 360 days`
4. If LEAP exists AND ticker is NOT in suppression list:
   - Send Pushover alert
   - Add to suppression list
5. If ticker is in suppression list and Confirmed RSI has recovered above 30 (check `data/rsi_values.json`), remove from suppression list
6. Update dashboard data

**Rate Limiting:**
- Individual `.options` calls with 2.5 second delay between each
- Exponential backoff on 429: 30s / 60s / 120s, max 5 retries
- With ~100-300 oversold stocks in a normal market, this takes 4-12 minutes
- Even in a severe crash with 500-1000 oversold stocks, 20-40 minutes. Well within timeout.

### 5. Alert and Suppression Logic (`scripts/alert.py`)

**Pushover request (replicate in Python with `requests.post()`):**
```bash
curl -s \
  --form-string "token=${PUSHOVER_APP_TOKEN}" \
  --form-string "user=${PUSHOVER_USER_KEY}" \
  --form-string "message=Weekly RSI for ${TICKER} is ${RSI_VALUE} (below 30)" \
  --form-string "title=LEAP RSI Alert" \
  --form-string "priority=1" \
  https://api.pushover.net/1/messages.json
```

**Include in the message body:**
- Ticker symbol
- Confirmed RSI value (rounded to 1 decimal)
- Current RSI value (rounded to 1 decimal)
- Current stock price
- Furthest LEAP expiration date available

Example message:
```
LEAP RSI Alert: INTC
Confirmed Weekly RSI: 24.3 | Current Weekly RSI: 22.1
Price: $22.15 | Furthest LEAP: Jan 2028
```

**Suppression state file:** `data/alert_state.json`
```json
{
  "suppressed": {
    "INTC": {
      "first_alert_date": "2026-03-02",
      "confirmed_rsi_at_alert": 24.3,
      "furthest_leap": "2028-01-21"
    }
  }
}
```

**Suppression rules:**
- Confirmed RSI < 30 + has LEAPs + NOT suppressed: send alert, add to suppressed
- Confirmed RSI < 30 + has LEAPs + IS suppressed: skip (no alert)
- Confirmed RSI >= 30 + IS suppressed: remove from suppressed (reset for next dip)
- No LEAPs available: do not alert, do not suppress (check again next week)

### 6. Status Dashboard

**Data generation** (`scripts/generate_dashboard.py`, called at end of both workflows):

Writes `docs/data/dashboard.json`:
```json
{
  "generated_at": "2026-03-03T22:15:00Z",
  "system_status": {
    "universe": {
      "name": "S&P 1500",
      "ticker_count": 1498,
      "last_updated": "2026-03-01T00:00:00Z",
      "status": "ok"
    },
    "rsi_scan": {
      "last_run": "2026-03-03T22:12:00Z",
      "tickers_scanned": 1498,
      "tickers_failed": 3,
      "rate_limits_hit": 0,
      "confirmed_oversold_count": 7,
      "current_only_oversold_count": 4,
      "status": "ok"
    },
    "leap_check": {
      "last_run": "2026-03-02T03:15:00Z",
      "oversold_checked": 12,
      "leaps_found": 5,
      "rate_limits_hit": 0,
      "status": "ok"
    },
    "alerts": {
      "total_sent_this_week": 2,
      "total_suppressed": 5,
      "pushover_errors": 0
    }
  },
  "rate_limit_log": [
    {
      "timestamp": "2026-02-28T22:08:00Z",
      "phase": "rsi_scan",
      "count": 1,
      "resolution": "recovered_after_backoff"
    }
  ],
  "qualifying_stocks": [
    {
      "ticker": "INTC",
      "confirmed_rsi": 24.3,
      "current_rsi": 22.1,
      "price": 22.15,
      "has_leaps": true,
      "furthest_leap_expiry": "2028-01-21",
      "first_triggered": "2026-03-02",
      "alert_status": "new"
    },
    {
      "ticker": "WBA",
      "confirmed_rsi": 18.7,
      "current_rsi": 19.2,
      "price": 9.42,
      "has_leaps": true,
      "furthest_leap_expiry": "2028-01-21",
      "first_triggered": "2026-02-25",
      "alert_status": "suppressed"
    },
    {
      "ticker": "ABC",
      "confirmed_rsi": 27.1,
      "current_rsi": 26.5,
      "price": 15.30,
      "has_leaps": false,
      "furthest_leap_expiry": null,
      "first_triggered": "2026-03-03",
      "alert_status": "awaiting_leap_check"
    }
  ],
  "early_warnings": [
    {
      "ticker": "XYZ",
      "confirmed_rsi": 33.1,
      "current_rsi": 28.5,
      "price": 45.60,
      "note": "Current RSI below 30 but Confirmed RSI still above"
    }
  ],
  "recently_recovered": [
    {
      "ticker": "NKE",
      "confirmed_rsi": 34.2,
      "current_rsi": 35.1,
      "recovered_date": "2026-03-01",
      "was_suppressed_since": "2026-02-10"
    }
  ],
  "all_rsi_values": {
    "AAPL": {"confirmed_rsi": 55.2, "current_rsi": 53.8},
    "MSFT": {"confirmed_rsi": 61.8, "current_rsi": 60.4},
    "INTC": {"confirmed_rsi": 24.3, "current_rsi": 22.1}
  }
}
```

**Dashboard UI** (`docs/index.html`):

A single self-contained HTML file (HTML + CSS + JS, no build step) that fetches `data/dashboard.json` and renders the following:

**Header:**
- Title: "LEAP RSI Monitor"
- Last updated timestamp (from `generated_at`)
- Overall system health indicator (green/yellow/red badge)

**Section 1: System Status**
- Card layout showing each subsystem:
  - **Universe:** S&P 1500 ticker count, last refresh date
  - **RSI Scan:** last run, tickers scanned, failures, oversold counts, status badge
  - **LEAP Check:** last run, tickers checked, LEAPs found, status badge
  - **Alerts:** sent this week, currently suppressed, any errors
- Yellow warning banner if any rate limits hit in last 7 days

**Section 2: Qualifying Stocks (Confirmed RSI < 30 with LEAPs)**
- Sortable table:
  - Ticker (link to Yahoo Finance page)
  - Confirmed RSI (color-coded: deeper red = lower RSI)
  - Current RSI (color-coded independently)
  - Price
  - Furthest LEAP Expiry
  - Date First Triggered
  - Alert Status (New / Suppressed)
- Sort by Confirmed RSI ascending by default (most oversold first)
- Count in header: "5 stocks with Confirmed RSI < 30 and LEAPs available"

**Section 3: Early Warnings (Current RSI < 30 only)**
- Stocks where Confirmed RSI is still above 30 but Current RSI has dipped below
- These may become confirmed oversold by end of week
- Columns: Ticker, Confirmed RSI, Current RSI, Price, Delta (how far Confirmed RSI is from 30)
- Highlighted in amber/yellow to distinguish from confirmed signals
- Header: "3 stocks approaching RSI 30 this week"

**Section 4: Oversold Without LEAPs / Awaiting Check**
- Stocks with Confirmed RSI < 30 but either no LEAPs available or not yet checked this week
- Columns: Ticker, Confirmed RSI, Current RSI, Price, LEAP Status (No LEAPs / Pending check)

**Section 5: Recently Recovered**
- Tickers that were below 30 but have since recovered
- Columns: Ticker, Confirmed RSI, Current RSI, Date Recovered, Duration Below 30
- Keep last 30 days of recovery history

**Section 6: RSI Distribution**
- Histogram of Confirmed RSI values across all S&P 1500 stocks using Chart.js from CDN
- Highlight the <30 zone in red
- Optional: overlay Current RSI distribution as a second semi-transparent series

**Design requirements:**
- Clean, minimal, dark-mode friendly
- Responsive (works on mobile for quick phone checks)
- Auto-refreshes data every 5 minutes (re-fetches JSON)
- Timestamps show relative time ("Updated 3 hours ago") with absolute on hover
- Vanilla HTML/CSS/JS only, Chart.js from CDN for charts

### 7. GitHub Actions Workflows

#### `.github/workflows/daily_rsi.yml`
```yaml
name: Daily RSI Scan
on:
  schedule:
    - cron: '0 22 * * 1-5'  # Mon-Fri 22:00 UTC (5pm ET, after close)
  workflow_dispatch: {}

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python scripts/check_rsi.py
      - name: Commit updated data and dashboard
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/ docs/data/
          git diff --staged --quiet || git commit -m "Daily RSI scan [bot]"
          git push
```

#### `.github/workflows/weekly_leaps.yml`
```yaml
name: Weekly LEAP Check
on:
  schedule:
    - cron: '0 3 * * 0'  # Sunday 03:00 UTC (Saturday 10pm ET)
  workflow_dispatch: {}

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    env:
      PUSHOVER_APP_TOKEN: ${{ secrets.PUSHOVER_APP_TOKEN }}
      PUSHOVER_USER_KEY: ${{ secrets.PUSHOVER_USER_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python scripts/check_leaps.py
      - name: Commit updated state and dashboard
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/ docs/data/
          git diff --staged --quiet || git commit -m "Weekly LEAP check [bot]"
          git push
```

### 8. Configuration (`config.py`)

```python
import os

# Pushover (from environment / GitHub Secrets)
PUSHOVER_APP_TOKEN = os.environ.get("PUSHOVER_APP_TOKEN", "")
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY", "")

# RSI parameters
RSI_PERIOD = 14             # Standard 14-period RSI
RSI_THRESHOLD = 30          # Alert when Confirmed RSI below this
RSI_LOOKBACK = "6mo"        # Daily data lookback (gives ~25 rolling weeks)
ROLLING_WEEK_DAYS = 5       # Trading days per rolling weekly candle

# LEAP filter
MIN_DTE_DAYS = 360          # Minimum days to expiration for LEAP qualification

# Rate limiting
RSI_BATCH_SIZE = 50                 # Tickers per yf.download() bulk call
RSI_BATCH_DELAY = 5                 # Seconds between bulk download batches
LEAP_REQUEST_DELAY = 2.5            # Seconds between .options calls
BACKOFF_INITIAL = 30                # First backoff wait on 429 (seconds)
BACKOFF_MULTIPLIER = 2              # Double wait each consecutive 429
BACKOFF_MAX_RETRIES = 5             # Max consecutive 429s before exiting

# Paths
SP1500_UNIVERSE_PATH = "data/sp1500_universe.json"
RSI_VALUES_PATH = "data/rsi_values.json"
OVERSOLD_PATH = "data/oversold.json"
ALERT_STATE_PATH = "data/alert_state.json"
SYSTEM_LOG_PATH = "data/system_log.json"
DASHBOARD_DATA_PATH = "docs/data/dashboard.json"
```

---

## Project Structure

```
leap-rsi-monitor/
  .github/
    workflows/
      daily_rsi.yml
      weekly_leaps.yml
  scripts/
    check_rsi.py
    check_leaps.py
    alert.py
    generate_dashboard.py
    refresh_universe.py
  data/
    sp1500_universe.json
    rsi_values.json
    oversold.json
    alert_state.json
    system_log.json
  docs/
    index.html
    data/
      dashboard.json
  config.py
  requirements.txt
  README.md
  .gitignore
```

**`requirements.txt`:**
```
yfinance>=0.2.30
requests>=2.31.0
pandas>=2.0.0
beautifulsoup4>=4.12.0
lxml>=5.0.0
```

---

## System Logging (`data/system_log.json`)

All scripts append entries tracking operational health. Dashboard reads this for status and rate-limit history. Retain last 90 days, prune older entries on each write.

```json
{
  "entries": [
    {
      "timestamp": "2026-03-03T22:12:00Z",
      "phase": "rsi_scan",
      "tickers_scanned": 1498,
      "tickers_failed": 3,
      "confirmed_oversold": 7,
      "current_only_oversold": 4,
      "rate_limits_hit": 0,
      "duration_seconds": 195,
      "status": "ok"
    },
    {
      "timestamp": "2026-03-02T03:15:00Z",
      "phase": "leap_check",
      "tickers_checked": 12,
      "leaps_found": 5,
      "alerts_sent": 2,
      "rate_limits_hit": 0,
      "duration_seconds": 35,
      "status": "ok"
    }
  ]
}
```

---

## Rate Limiting Strategy

| Phase | Method | Delay | Backoff | Worst Case |
|---|---|---|---|---|
| Daily RSI scan | Bulk `yf.download()` batches of 50 | 5s between batches | 10s/30s/60s, 3 retries | 30 calls = ~3 min |
| Weekly LEAP check | Individual `.options` calls | 2.5s per request | 30s/60s/120s, 5 retries | ~300 stocks = 12 min normal |
| Alerts | Individual Pushover POST | None | Retry once after 5s | Negligible |

**If Yahoo Finance becomes unreliable**, fallback data sources to document in README:
- Polygon.io free tier (5 calls/min)
- Alpha Vantage free tier (25 requests/day)
- Self-hosted runner on free Oracle Cloud VM for dedicated IP

---

## Error Handling and Resilience

- Individual ticker failures should not crash the run. Wrap each ticker in try/except, log, continue.
- Log a summary at the end of each run: tickers scanned, alerts sent, errors.
- If `oversold.json` is empty or missing when the weekly LEAP check runs, log a warning and exit cleanly.
- If Pushover API fails, retry once after 5 seconds. If it fails again, log the error and continue. Do NOT suppress the alert so it retries next week.
- Dashboard data must always be written, even if a run partially fails. Show whatever was collected with appropriate warning indicators.
- If a ticker has insufficient daily data to build 14 rolling weekly candles, skip it and log a warning.

---

## Testing

- `python scripts/check_rsi.py --dry-run` prints RSI values and oversold list to console, does not write files
- `python scripts/check_leaps.py --dry-run` checks LEAPs and prints alerts to console, does not send Pushover
- `python scripts/check_rsi.py --ticker INTC` single-ticker RSI check with detailed candle output for debugging
- `python scripts/check_leaps.py --ticker INTC` single-ticker LEAP check for debugging
- `python scripts/generate_dashboard.py --sample` generates mock dashboard JSON for UI development
- `python scripts/refresh_universe.py` manually refreshes S&P 1500 list

---

## Setup Instructions

1. Create a new GitHub repo (public for free Actions minutes, or private if under 2,000 min/month)
2. Add GitHub Secrets:
   - `PUSHOVER_APP_TOKEN` your Pushover application token
   - `PUSHOVER_USER_KEY` your Pushover user key
3. Run `python scripts/refresh_universe.py` to populate the S&P 1500 list and commit
4. Enable GitHub Pages: Settings > Pages > Source: Deploy from branch > `main` / `docs`
5. Trigger the daily RSI scan manually to populate initial data
6. Trigger the weekly LEAP check manually to send first alerts
7. Both workflows will then run on schedule automatically
8. Dashboard live at `https://<username>.github.io/leap-rsi-monitor/`

---

## Cost Estimate

| Component | Cost |
|---|---|
| GitHub Actions (public repo) | Free |
| GitHub Actions (private repo) | ~5 min/day x 22 + ~15 min/week x 4 = ~170 min/month (well within 2,000 free) |
| GitHub Pages | Free |
| yfinance data | Free |
| Pushover | Free tier: 10,000 messages/month |
| **Total** | **$0/month** |

---

## Future Enhancements (Out of Scope for V1)

- Add RSI upper bound alerts (>70) for exit signals
- Include additional indicators (MACD, Bollinger Bands)
- Filter by minimum options volume or open interest
- Track and report alert performance (did the stock recover after alert?)
- Slack or email as alternative notification channels
- Support for put vs call LEAP filtering
- Historical RSI chart per ticker on dashboard
- Email digest: weekly summary of all activity
- Expand universe beyond S&P 1500 if rate limits allow
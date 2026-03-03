# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

This repository contains only `prd.md` — a full Product Requirements Document. **No implementation files exist yet.** All scripts, data files, and workflows need to be created from scratch per the spec.

## What to Build

**LEAP RSI Alert Monitor** — a zero-cost, GitHub Actions-based system that:
1. Scans S&P 1500 stocks daily for weekly RSI < 30
2. Checks oversold stocks weekly for LEAP options (>360 DTE)
3. Sends Pushover alerts for qualifying stocks
4. Serves a status dashboard via GitHub Pages

## Development Commands

Once implemented, these are the canonical ways to test locally:

```bash
# Dry-run (no file writes, no alerts sent)
python scripts/check_rsi.py --dry-run
python scripts/check_leaps.py --dry-run

# Single-ticker debug (prints detailed candle output)
python scripts/check_rsi.py --ticker INTC
python scripts/check_leaps.py --ticker INTC

# Generate mock dashboard data for UI dev
python scripts/generate_dashboard.py --sample

# Refresh S&P 1500 universe from Wikipedia
python scripts/refresh_universe.py
```

## Architecture

### Two GitHub Actions Workflows

| Workflow | Schedule | Script | Purpose |
|---|---|---|---|
| `daily_rsi.yml` | Mon-Fri 22:00 UTC | `check_rsi.py` | RSI scan + dashboard update |
| `weekly_leaps.yml` | Sunday 03:00 UTC | `check_leaps.py` | LEAP check + Pushover alerts |

### Critical: Rolling Weekly RSI (not Yahoo's fixed weeks)

The RSI calculation uses **custom 5-trading-day candles anchored to today**, NOT `yf.download(interval="1wk")`. This is intentional — Yahoo's weekly candles use fixed Mon-Fri boundaries, giving inconsistent RSI depending on run day.

**Algorithm:** Pull 6 months of daily OHLCV → group backwards into 5-trading-day blocks from the most recent trading day → calculate two RSI series:
- **Confirmed RSI:** Only completed blocks (drives alerts — stable)
- **Current RSI:** All blocks including in-progress block (dashboard early warnings only)

Wilder smoothing (14-period): first avg = simple mean of first 14 periods; subsequent = `(prev_avg * 13 + current) / 14`.

### RSI-First Strategy (efficiency)

Instead of querying options for all ~8,000 optionable stocks, the system:
1. Bulk-downloads RSI for all 1,500 S&P 1500 stocks efficiently
2. Only calls `.options` on stocks where Confirmed RSI < 30 (~100-300 stocks)

This reduces options API calls by ~95%.

### Data Flow

```
sp1500_universe.json → check_rsi.py → rsi_values.json + oversold.json
oversold.json + alert_state.json → check_leaps.py → alert_state.json (updated)
Both scripts call generate_dashboard.py → docs/data/dashboard.json
docs/index.html fetches dashboard.json (static, no build step)
```

### Suppression Logic

- Alert sent → add ticker to `alert_state.json` suppressed list
- Ticker suppressed + Confirmed RSI < 30 → skip (no duplicate alert)
- Ticker suppressed + Confirmed RSI ≥ 30 → remove from suppressed (reset for next dip)
- No LEAPs found → neither alert nor suppress (recheck next week)

## Key Configuration (`config.py`)

```python
RSI_PERIOD = 14              # Wilder smoothing periods
RSI_THRESHOLD = 30           # Oversold cutoff
RSI_LOOKBACK = "6mo"         # Daily data pull (~25 rolling weeks)
ROLLING_WEEK_DAYS = 5        # Trading days per candle
MIN_DTE_DAYS = 360           # LEAP minimum days to expiry
RSI_BATCH_SIZE = 50          # Tickers per yf.download() call
RSI_BATCH_DELAY = 5          # Seconds between batches
LEAP_REQUEST_DELAY = 2.5     # Seconds between .options calls
BACKOFF_INITIAL = 30         # First 429 backoff (seconds)
BACKOFF_MULTIPLIER = 2
BACKOFF_MAX_RETRIES = 5
```

Pushover credentials come from env vars `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY` (GitHub Secrets in CI).

## Rate Limiting

| Phase | Method | Delay | Backoff |
|---|---|---|---|
| RSI scan | `yf.download()` batches of 50 | 5s between batches | 10s/30s/60s, 3 retries |
| LEAP check | Individual `.options` calls | 2.5s per call | 30s/60s/120s, 5 retries |
| Alerts | Pushover POST | None | Retry once after 5s |

**Never use individual `Ticker.history()` for RSI scanning** — always use bulk `yf.download()`.

## Dashboard

`docs/index.html` is vanilla HTML/CSS/JS with Chart.js from CDN. No build step. It fetches `docs/data/dashboard.json` at runtime and auto-refreshes every 5 minutes. The workflow commits updated JSON; GitHub Pages serves it automatically.

## Setup (first-time)

1. Add GitHub Secrets: `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`
2. Run `python scripts/refresh_universe.py` and commit `data/sp1500_universe.json`
3. Enable GitHub Pages: Settings → Pages → Branch: `main`, Folder: `/docs`
4. Trigger workflows manually via `workflow_dispatch` to populate initial data

"""
Daily RSI scan for S&P 1500 stocks.

Usage:
    python scripts/check_rsi.py              # Full run
    python scripts/check_rsi.py --dry-run   # Print results, no file writes
    python scripts/check_rsi.py --ticker AAPL  # Debug single ticker
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# Backoff delays (seconds) for RSI batch 429 errors
RSI_BACKOFF_SEQUENCE = [10, 30, 60]


def build_rolling_candles(daily_closes: pd.Series, daily_opens: pd.Series,
                           daily_highs: pd.Series, daily_lows: pd.Series,
                           daily_volumes: pd.Series) -> tuple[list[dict], dict | None]:
    """
    Group daily OHLCV into rolling 5-trading-day blocks anchored to the most
    recent trading day.

    Returns:
        confirmed_candles: list of completed 5-day blocks (oldest first)
        current_candle: the most recent (possibly incomplete) block, or None
    """
    trading_days = list(daily_closes.index)
    if len(trading_days) < 2:
        return [], None

    # Split trading days into 5-day blocks working backwards
    blocks = []
    remaining = trading_days[:]
    while len(remaining) >= 5:
        block = remaining[-5:]
        remaining = remaining[:-5]
        blocks.append(block)

    # If there are leftover days at the start, they form the oldest partial block
    # (we discard them — they're just the RSI warm-up tail)
    # The most recent block is the "current" week; the rest are "confirmed"
    if not blocks:
        return [], None

    # blocks[0] is most recent (current), blocks[1:] are confirmed (also most-recent-first)
    current_block = blocks[0]
    confirmed_blocks = blocks[1:][::-1]  # reverse so oldest is first

    def block_to_candle(days: list) -> dict:
        return {
            "date": str(days[-1].date()),
            "open": float(daily_opens.loc[days[0]]),
            "high": float(daily_highs.loc[days].max()),
            "low": float(daily_lows.loc[days].min()),
            "close": float(daily_closes.loc[days[-1]]),
            "volume": float(daily_volumes.loc[days].sum()),
        }

    confirmed_candles = [block_to_candle(b) for b in confirmed_blocks]
    current_candle = block_to_candle(current_block)

    return confirmed_candles, current_candle


def wilder_rsi(candles: list[dict], period: int = 14) -> float | None:
    """
    Calculate Wilder-smoothed RSI for a list of candles (oldest first).
    Returns the final RSI value, or None if insufficient data.
    """
    if len(candles) < period + 1:
        return None

    closes = [c["close"] for c in candles]
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    gains = [max(ch, 0.0) for ch in changes]
    losses = [abs(min(ch, 0.0)) for ch in changes]

    # Seed with simple mean of first `period` values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def wilder_rsi_verbose(candles: list[dict], period: int = 14) -> float | None:
    """
    Same as wilder_rsi but prints each step for --ticker debugging.
    """
    if len(candles) < period + 1:
        print(f"  Insufficient candles: {len(candles)} (need {period + 1})")
        return None

    closes = [c["close"] for c in candles]
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    gains = [max(ch, 0.0) for ch in changes]
    losses = [abs(min(ch, 0.0)) for ch in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    print(f"  {'Period':<8} {'Date':<12} {'Close':>8} {'Change':>8} {'Gain':>8} {'Loss':>8} {'AvgGain':>9} {'AvgLoss':>9} {'RSI':>7}")
    print("  " + "-" * 80)

    # Print seed period
    for i in range(period):
        date = candles[i + 1]["date"] if i + 1 < len(candles) else "?"
        ch = changes[i]
        print(f"  {i+1:<8} {date:<12} {closes[i+1]:>8.2f} {ch:>+8.2f} {gains[i]:>8.2f} {losses[i]:>8.2f} {'(seed)':>9} {'(seed)':>9} {'':>7}")

    rsi_val = None
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_val = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_val = round(100 - (100 / (1 + rs)), 2)
        date = candles[i + 1]["date"] if i + 1 < len(candles) else "?"
        ch = changes[i]
        print(f"  {i+1:<8} {date:<12} {closes[i+1]:>8.2f} {ch:>+8.2f} {gains[i]:>8.2f} {losses[i]:>8.2f} {avg_gain:>9.4f} {avg_loss:>9.4f} {rsi_val:>7.2f}")

    return rsi_val


def compute_rsi_for_ticker(ticker: str, df_ticker: pd.DataFrame,
                            verbose: bool = False) -> dict | None:
    """
    Given a single-ticker OHLCV DataFrame, compute confirmed_rsi, current_rsi, and price.
    Returns None if insufficient data.
    """
    try:
        closes = df_ticker["Close"].dropna()
        opens = df_ticker["Open"].dropna()
        highs = df_ticker["High"].dropna()
        lows = df_ticker["Low"].dropna()
        volumes = df_ticker["Volume"].fillna(0)

        if len(closes) < config.RSI_PERIOD * config.ROLLING_WEEK_DAYS + config.ROLLING_WEEK_DAYS:
            if verbose:
                print(f"  Insufficient daily data: {len(closes)} days")
            return None

        # Align all series to common index
        common_idx = closes.index
        opens = opens.reindex(common_idx)
        highs = highs.reindex(common_idx)
        lows = lows.reindex(common_idx)
        volumes = volumes.reindex(common_idx).fillna(0)

        confirmed_candles, current_candle = build_rolling_candles(
            closes, opens, highs, lows, volumes
        )

        if len(confirmed_candles) < config.RSI_PERIOD + 1:
            if verbose:
                print(f"  Insufficient confirmed candles: {len(confirmed_candles)} (need {config.RSI_PERIOD + 1})")
            return None

        if verbose:
            print(f"\n  === Confirmed Weekly Candles ({len(confirmed_candles)}) ===")
            for i, c in enumerate(confirmed_candles):
                print(f"  [{i:>2}] date={c['date']} O={c['open']:.2f} H={c['high']:.2f} L={c['low']:.2f} C={c['close']:.2f} V={c['volume']:.0f}")
            if current_candle:
                print(f"\n  === Current (in-progress) Candle ===")
                print(f"  [CUR] date={current_candle['date']} O={current_candle['open']:.2f} H={current_candle['high']:.2f} L={current_candle['low']:.2f} C={current_candle['close']:.2f} V={current_candle['volume']:.0f}")

            print(f"\n  === RSI Calculation (Confirmed only) ===")
            confirmed_rsi = wilder_rsi_verbose(confirmed_candles)

            current_rsi = None
            if current_candle:
                print(f"\n  === RSI Calculation (Confirmed + Current) ===")
                current_rsi = wilder_rsi_verbose(confirmed_candles + [current_candle])
        else:
            confirmed_rsi = wilder_rsi(confirmed_candles)
            current_rsi = wilder_rsi(confirmed_candles + [current_candle]) if current_candle else confirmed_rsi

        if confirmed_rsi is None:
            return None

        price = round(float(closes.iloc[-1]), 2)
        return {
            "confirmed_rsi": confirmed_rsi,
            "current_rsi": current_rsi if current_rsi is not None else confirmed_rsi,
            "price": price,
        }

    except Exception as e:
        if verbose:
            print(f"  ERROR computing RSI: {e}")
        return None


def download_batch_with_retry(tickers: list[str]) -> pd.DataFrame | None:
    """Download a batch of tickers with exponential backoff on rate limit errors."""
    for attempt, backoff in enumerate([0] + RSI_BACKOFF_SEQUENCE):
        if backoff > 0:
            print(f"  Rate limit hit, backing off {backoff}s (attempt {attempt + 1})...")
            time.sleep(backoff)
        try:
            df = yf.download(
                tickers,
                period=config.RSI_LOOKBACK,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            return df
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "too many requests" in err:
                if attempt >= len(RSI_BACKOFF_SEQUENCE):
                    print(f"  Max retries exceeded for batch. Skipping.")
                    return None
                continue
            print(f"  Download error: {e}")
            return None
    return None


def extract_ticker_df(df: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """
    Extract a flat (non-MultiIndex) OHLCV DataFrame for one ticker from a
    yf.download() result. Works whether df has MultiIndex or flat columns.
    """
    if isinstance(df.columns, pd.MultiIndex):
        level1_vals = df.columns.get_level_values(1)
        if ticker not in level1_vals:
            return None
        return df.xs(ticker, axis=1, level=1).dropna(how="all")
    # Flat columns (shouldn't happen with modern yfinance but handle anyway)
    return df.dropna(how="all")


def run_single_ticker(ticker: str):
    """Debug mode: download and print full RSI detail for one ticker."""
    print(f"\n=== Single-ticker debug: {ticker} ===\n")
    df = yf.download(
        ticker,
        period=config.RSI_LOOKBACK,
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if df.empty:
        print("No data returned.")
        return

    df_ticker = extract_ticker_df(df, ticker)
    if df_ticker is None or df_ticker.empty:
        print("Could not extract ticker data.")
        return

    result = compute_rsi_for_ticker(ticker, df_ticker, verbose=True)
    if result:
        print(f"\n  CONFIRMED RSI: {result['confirmed_rsi']}")
        print(f"  CURRENT RSI:   {result['current_rsi']}")
        print(f"  PRICE:         ${result['price']}")
    else:
        print("\n  Could not compute RSI.")


def run_full_scan(dry_run: bool = False):
    """Full S&P 1500 RSI scan."""
    start_time = time.time()
    now = datetime.now(timezone.utc)

    if not os.path.exists(config.SP1500_UNIVERSE_PATH):
        print(f"ERROR: {config.SP1500_UNIVERSE_PATH} not found. Run refresh_universe.py first.")
        sys.exit(1)

    with open(config.SP1500_UNIVERSE_PATH) as f:
        universe = json.load(f)
    tickers = universe["tickers"]
    print(f"Loaded {len(tickers)} tickers from universe.")

    rsi_values: dict[str, dict] = {}
    failed: list[str] = []
    rate_limits_hit = 0

    batches = [tickers[i:i + config.RSI_BATCH_SIZE] for i in range(0, len(tickers), config.RSI_BATCH_SIZE)]
    print(f"Processing {len(batches)} batches of up to {config.RSI_BATCH_SIZE} tickers...")

    for batch_num, batch in enumerate(batches, 1):
        print(f"  Batch {batch_num}/{len(batches)}: {len(batch)} tickers", end="", flush=True)

        df = download_batch_with_retry(batch)

        if df is None or df.empty:
            print(f" -> FAILED (no data)")
            failed.extend(batch)
            if batch_num < len(batches):
                time.sleep(config.RSI_BATCH_DELAY)
            continue

        success_count = 0
        for ticker in batch:
            try:
                df_ticker = extract_ticker_df(df, ticker)
                if df_ticker is None or df_ticker.empty:
                    failed.append(ticker)
                    continue

                result = compute_rsi_for_ticker(ticker, df_ticker)
                if result:
                    rsi_values[ticker] = result
                    success_count += 1
                else:
                    failed.append(ticker)
            except Exception as e:
                failed.append(ticker)

        print(f" -> {success_count}/{len(batch)} succeeded")

        if batch_num < len(batches):
            time.sleep(config.RSI_BATCH_DELAY)

    # Categorize oversold
    confirmed_oversold = []
    current_only_oversold = []
    for ticker, vals in rsi_values.items():
        if vals["confirmed_rsi"] < config.RSI_THRESHOLD:
            confirmed_oversold.append({"ticker": ticker, **vals})
        elif vals["current_rsi"] < config.RSI_THRESHOLD:
            current_only_oversold.append({"ticker": ticker, **vals})

    confirmed_oversold.sort(key=lambda x: x["confirmed_rsi"])
    current_only_oversold.sort(key=lambda x: x["current_rsi"])

    duration = int(time.time() - start_time)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    run_date = now.strftime("%Y-%m-%d")

    print(f"\nResults: {len(rsi_values)} computed, {len(failed)} failed")
    print(f"Confirmed oversold (<30): {len(confirmed_oversold)}")
    print(f"Current-only oversold:    {len(current_only_oversold)}")
    print(f"Duration: {duration}s")

    if dry_run:
        print("\n[DRY RUN] No files written.")
        if confirmed_oversold:
            print("\nConfirmed oversold:")
            for s in confirmed_oversold:
                print(f"  {s['ticker']}: confirmed={s['confirmed_rsi']} current={s['current_rsi']} price=${s['price']}")
        return

    # Write output files
    os.makedirs("data", exist_ok=True)

    rsi_out = {
        "updated_at": ts,
        "run_date": run_date,
        "values": rsi_values,
    }
    with open(config.RSI_VALUES_PATH, "w") as f:
        json.dump(rsi_out, f, indent=2)

    oversold_out = {
        "updated_at": ts,
        "confirmed_oversold": confirmed_oversold,
        "current_only_oversold": current_only_oversold,
    }
    with open(config.OVERSOLD_PATH, "w") as f:
        json.dump(oversold_out, f, indent=2)

    # Append to system log
    log_entry = {
        "timestamp": ts,
        "phase": "rsi_scan",
        "tickers_scanned": len(rsi_values),
        "tickers_failed": len(failed),
        "confirmed_oversold": len(confirmed_oversold),
        "current_only_oversold": len(current_only_oversold),
        "rate_limits_hit": rate_limits_hit,
        "duration_seconds": duration,
        "status": "ok",
    }
    _append_log(log_entry)

    # Generate dashboard
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import generate_dashboard
    generate_dashboard.generate()

    print(f"\nWrote {config.RSI_VALUES_PATH}, {config.OVERSOLD_PATH}")


def _append_log(entry: dict):
    """Append a log entry, pruning entries older than 90 days."""
    log = {"entries": []}
    if os.path.exists(config.SYSTEM_LOG_PATH):
        try:
            with open(config.SYSTEM_LOG_PATH) as f:
                log = json.load(f)
        except Exception:
            pass

    log["entries"].append(entry)

    # Prune entries older than 90 days
    cutoff = datetime.now(timezone.utc).timestamp() - 90 * 86400
    log["entries"] = [
        e for e in log["entries"]
        if datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp() >= cutoff
    ]

    os.makedirs(os.path.dirname(config.SYSTEM_LOG_PATH), exist_ok=True)
    with open(config.SYSTEM_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Daily RSI scan for S&P 1500")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing files")
    parser.add_argument("--ticker", type=str, help="Debug a single ticker")
    args = parser.parse_args()

    if args.ticker:
        run_single_ticker(args.ticker.upper())
    else:
        run_full_scan(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

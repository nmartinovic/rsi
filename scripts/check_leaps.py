"""
Weekly LEAP check and alert script.

Usage:
    python scripts/check_leaps.py              # Full run with Pushover alerts
    python scripts/check_leaps.py --dry-run   # Print results, no alerts sent
    python scripts/check_leaps.py --ticker AAPL  # Debug single ticker
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, date, timezone, timedelta

import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# Backoff delays for LEAP check 429 errors
LEAP_BACKOFF_SEQUENCE = [30, 60, 120, 240, 480]


def get_options_with_retry(ticker: str) -> tuple | None:
    """
    Fetch available option expiration dates for a ticker with backoff on rate limits.
    Returns a tuple of date strings, or None on failure.
    """
    for attempt, backoff in enumerate([0] + LEAP_BACKOFF_SEQUENCE):
        if backoff > 0:
            print(f"    Rate limit, backing off {backoff}s (attempt {attempt + 1})...")
            time.sleep(backoff)
        try:
            t = yf.Ticker(ticker)
            expirations = t.options
            return expirations
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "too many requests" in err:
                if attempt >= config.BACKOFF_MAX_RETRIES:
                    print(f"    Max retries exceeded for {ticker}")
                    return None
                continue
            print(f"    Error fetching options for {ticker}: {e}")
            return None
    return None


def has_leap(expirations: tuple, min_dte: int = config.MIN_DTE_DAYS) -> tuple[bool, str | None]:
    """
    Check if any expiration is more than min_dte days away.
    Returns (has_leap, furthest_expiry_str).
    """
    if not expirations:
        return False, None

    today = date.today()
    furthest = None
    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            if furthest is None or exp_date > furthest:
                furthest = exp_date
        except ValueError:
            continue

    if furthest and (furthest - today).days > min_dte:
        return True, furthest.strftime("%Y-%m-%d")
    return False, None


def _load_alert_state() -> dict:
    state = {"suppressed": {}, "recovered": {}}
    if os.path.exists(config.ALERT_STATE_PATH):
        try:
            with open(config.ALERT_STATE_PATH) as f:
                loaded = json.load(f)
                state["suppressed"] = loaded.get("suppressed", {})
                state["recovered"] = loaded.get("recovered", {})
        except Exception:
            pass
    return state


def _load_rsi_values() -> dict:
    if not os.path.exists(config.RSI_VALUES_PATH):
        return {}
    try:
        with open(config.RSI_VALUES_PATH) as f:
            return json.load(f).get("values", {})
    except Exception:
        return {}


def _append_log(entry: dict):
    log = {"entries": []}
    if os.path.exists(config.SYSTEM_LOG_PATH):
        try:
            with open(config.SYSTEM_LOG_PATH) as f:
                log = json.load(f)
        except Exception:
            pass
    log["entries"].append(entry)
    cutoff = datetime.now(timezone.utc).timestamp() - 90 * 86400
    log["entries"] = [
        e for e in log["entries"]
        if datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp() >= cutoff
    ]
    os.makedirs(os.path.dirname(config.SYSTEM_LOG_PATH), exist_ok=True)
    with open(config.SYSTEM_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def run_single_ticker(ticker: str):
    """Debug mode: check LEAP availability for a single ticker."""
    print(f"\n=== Single-ticker LEAP check: {ticker} ===\n")
    expirations = get_options_with_retry(ticker)
    if expirations is None:
        print("  Failed to fetch options.")
        return
    if not expirations:
        print("  No option expirations available.")
        return

    print(f"  Available expirations ({len(expirations)}):")
    for exp in expirations:
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days
        print(f"    {exp}  ({dte} DTE)")

    qualifies, furthest = has_leap(expirations)
    print(f"\n  Has LEAP (>{config.MIN_DTE_DAYS} DTE): {qualifies}")
    if furthest:
        print(f"  Furthest LEAP: {furthest}")


def run_full_check(dry_run: bool = False):
    """Full weekly LEAP check."""
    start_time = time.time()
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_str = now.strftime("%Y-%m-%d")

    # Load oversold list
    if not os.path.exists(config.OVERSOLD_PATH):
        print("WARNING: oversold.json not found. Run check_rsi.py first.")
        return

    with open(config.OVERSOLD_PATH) as f:
        oversold_data = json.load(f)

    confirmed_oversold = oversold_data.get("confirmed_oversold", [])
    if not confirmed_oversold:
        print("No confirmed oversold stocks. Nothing to check.")
        return

    print(f"Checking {len(confirmed_oversold)} confirmed oversold stocks for LEAPs...")

    alert_state = _load_alert_state()
    suppressed = alert_state["suppressed"]
    recovered = alert_state["recovered"]
    rsi_values = _load_rsi_values()

    tickers_checked = 0
    leaps_found = 0
    alerts_sent = 0
    rate_limits_hit = 0
    pushover_errors = 0

    # Import alert module (in scripts/ directory)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import alert as alert_mod

    for stock in confirmed_oversold:
        ticker = stock["ticker"]
        confirmed_rsi = stock["confirmed_rsi"]
        current_rsi = stock["current_rsi"]
        price = stock["price"]

        print(f"  {ticker}: confirmed_rsi={confirmed_rsi}", end="", flush=True)

        expirations = get_options_with_retry(ticker)
        tickers_checked += 1

        if expirations is None:
            print(" -> fetch failed")
            time.sleep(config.LEAP_REQUEST_DELAY)
            continue

        qualifies, furthest = has_leap(expirations)

        if not qualifies:
            print(f" -> no LEAP")
            time.sleep(config.LEAP_REQUEST_DELAY)
            continue

        leaps_found += 1
        print(f" -> LEAP: {furthest}", end="")

        if ticker in suppressed:
            print(f" -> SUPPRESSED")
        else:
            print(f" -> ALERTING")
            if not dry_run:
                success = alert_mod.send_alert(ticker, confirmed_rsi, current_rsi, price, furthest)
                if success:
                    alerts_sent += 1
                    suppressed[ticker] = {
                        "first_alert_date": today_str,
                        "confirmed_rsi_at_alert": confirmed_rsi,
                        "furthest_leap": furthest,
                    }
                else:
                    pushover_errors += 1
                    print(f"    Pushover failed for {ticker}")
            else:
                print(f"    [DRY RUN] Would send alert for {ticker}")
                alerts_sent += 1

        time.sleep(config.LEAP_REQUEST_DELAY)

    # Check suppressed tickers for RSI recovery
    recovered_tickers = []
    for ticker, sup_info in list(suppressed.items()):
        rsi_info = rsi_values.get(ticker)
        if rsi_info and rsi_info.get("confirmed_rsi", 0) >= config.RSI_THRESHOLD:
            print(f"  {ticker}: RSI recovered to {rsi_info['confirmed_rsi']:.1f}, removing from suppressed")
            recovered_tickers.append(ticker)
            recovered[ticker] = {
                "recovered_date": today_str,
                "was_suppressed_since": sup_info.get("first_alert_date", today_str),
                "confirmed_rsi_at_alert": sup_info.get("confirmed_rsi_at_alert"),
                "furthest_leap": sup_info.get("furthest_leap"),
            }

    for ticker in recovered_tickers:
        del suppressed[ticker]

    # Prune recovered entries older than 30 days
    cutoff_30d = now - timedelta(days=30)
    recovered = {
        ticker: info for ticker, info in recovered.items()
        if datetime.fromisoformat(info["recovered_date"]).replace(tzinfo=timezone.utc) >= cutoff_30d
    }

    duration = int(time.time() - start_time)

    print(f"\nResults: {tickers_checked} checked, {leaps_found} with LEAPs, {alerts_sent} alerts sent")
    if dry_run:
        print("[DRY RUN] No files written, no alerts sent.")
        return

    # Write alert state
    alert_state["suppressed"] = suppressed
    alert_state["recovered"] = recovered
    os.makedirs(os.path.dirname(config.ALERT_STATE_PATH), exist_ok=True)
    with open(config.ALERT_STATE_PATH, "w") as f:
        json.dump(alert_state, f, indent=2)

    # Append log
    log_entry = {
        "timestamp": ts,
        "phase": "leap_check",
        "tickers_checked": tickers_checked,
        "leaps_found": leaps_found,
        "alerts_sent": alerts_sent,
        "rate_limits_hit": rate_limits_hit,
        "pushover_errors": pushover_errors,
        "duration_seconds": duration,
        "status": "ok",
    }
    _append_log(log_entry)

    # Generate dashboard
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import generate_dashboard
    generate_dashboard.generate()


def main():
    parser = argparse.ArgumentParser(description="Weekly LEAP check and alert")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing files or sending alerts")
    parser.add_argument("--ticker", type=str, help="Debug a single ticker")
    args = parser.parse_args()

    if args.ticker:
        run_single_ticker(args.ticker.upper())
    else:
        run_full_check(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

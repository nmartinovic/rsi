"""
Generates docs/data/dashboard.json from current data files.
Called programmatically by check_rsi.py and check_leaps.py, or directly:

    python scripts/generate_dashboard.py           # real data
    python scripts/generate_dashboard.py --sample  # mock data for UI dev
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _latest_log_entry(entries: list[dict], phase: str) -> dict | None:
    phase_entries = [e for e in entries if e.get("phase") == phase]
    return phase_entries[-1] if phase_entries else None


def generate():
    """Build and write dashboard.json from current data files."""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)

    universe = _load_json(config.SP1500_UNIVERSE_PATH, {})
    rsi_data = _load_json(config.RSI_VALUES_PATH, {"values": {}})
    oversold = _load_json(config.OVERSOLD_PATH, {"confirmed_oversold": [], "current_only_oversold": []})
    alert_state = _load_json(config.ALERT_STATE_PATH, {"suppressed": {}, "recovered": {}})
    sys_log = _load_json(config.SYSTEM_LOG_PATH, {"entries": []})

    entries = sys_log.get("entries", [])
    rsi_entry = _latest_log_entry(entries, "rsi_scan")
    leap_entry = _latest_log_entry(entries, "leap_check")

    # System status
    universe_status = {
        "name": "S&P 1500",
        "ticker_count": universe.get("count", 0),
        "last_updated": universe.get("updated_at", ""),
        "status": "ok" if universe.get("count", 0) > 0 else "unknown",
    }

    if rsi_entry:
        rsi_status = {
            "last_run": rsi_entry["timestamp"],
            "tickers_scanned": rsi_entry.get("tickers_scanned", 0),
            "tickers_failed": rsi_entry.get("tickers_failed", 0),
            "rate_limits_hit": rsi_entry.get("rate_limits_hit", 0),
            "confirmed_oversold_count": rsi_entry.get("confirmed_oversold", 0),
            "current_only_oversold_count": rsi_entry.get("current_only_oversold", 0),
            "status": rsi_entry.get("status", "unknown"),
        }
    else:
        rsi_status = {"last_run": None, "status": "unknown"}

    if leap_entry:
        leap_status = {
            "last_run": leap_entry["timestamp"],
            "oversold_checked": leap_entry.get("tickers_checked", 0),
            "leaps_found": leap_entry.get("leaps_found", 0),
            "rate_limits_hit": leap_entry.get("rate_limits_hit", 0),
            "status": leap_entry.get("status", "unknown"),
        }
    else:
        leap_status = {"last_run": None, "status": "unknown"}

    suppressed = alert_state.get("suppressed", {})
    alerts_this_week = sum(
        1 for e in entries
        if e.get("phase") == "leap_check"
        and datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) >= cutoff_7d
        and e.get("alerts_sent", 0) > 0
    )
    # Sum alerts sent over the week
    total_sent_this_week = sum(
        e.get("alerts_sent", 0) for e in entries
        if e.get("phase") == "leap_check"
        and datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) >= cutoff_7d
    )
    pushover_errors = sum(
        e.get("pushover_errors", 0) for e in entries
        if e.get("phase") == "leap_check"
        and datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) >= cutoff_7d
    )

    alert_status_block = {
        "total_sent_this_week": total_sent_this_week,
        "total_suppressed": len(suppressed),
        "pushover_errors": pushover_errors,
    }

    # Rate limit log (last 7 days)
    rate_limit_log = [
        {
            "timestamp": e["timestamp"],
            "phase": e["phase"],
            "count": e.get("rate_limits_hit", 0),
            "resolution": "recovered_after_backoff",
        }
        for e in entries
        if e.get("rate_limits_hit", 0) > 0
        and datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) >= cutoff_7d
    ]

    # Build per-ticker LEAP info from alert_state (suppressed contains furthest_leap and first_alert_date)
    rsi_values = rsi_data.get("values", {})

    # Qualifying stocks: confirmed_oversold, annotated with LEAP info from alert_state
    qualifying_stocks = []
    no_leap_stocks = []
    for stock in oversold.get("confirmed_oversold", []):
        ticker = stock["ticker"]
        sup_info = suppressed.get(ticker)
        if sup_info and sup_info.get("furthest_leap"):
            qualifying_stocks.append({
                "ticker": ticker,
                "confirmed_rsi": stock["confirmed_rsi"],
                "current_rsi": stock["current_rsi"],
                "price": stock["price"],
                "has_leaps": True,
                "furthest_leap_expiry": sup_info["furthest_leap"],
                "first_triggered": sup_info.get("first_alert_date", ts[:10]),
                "alert_status": "suppressed",
            })
        else:
            # Not yet in alert_state (LEAP check hasn't run yet or no LEAP found)
            has_leaps = False
            furthest = None
            alert_stat = "awaiting_leap_check"
            qualifying_stocks.append({
                "ticker": ticker,
                "confirmed_rsi": stock["confirmed_rsi"],
                "current_rsi": stock["current_rsi"],
                "price": stock["price"],
                "has_leaps": has_leaps,
                "furthest_leap_expiry": furthest,
                "first_triggered": ts[:10],
                "alert_status": alert_stat,
            })

    # Early warnings: current_only_oversold
    early_warnings = [
        {
            "ticker": s["ticker"],
            "confirmed_rsi": s["confirmed_rsi"],
            "current_rsi": s["current_rsi"],
            "price": s["price"],
            "note": "Current RSI below 30 but Confirmed RSI still above",
        }
        for s in oversold.get("current_only_oversold", [])
    ]

    # Recently recovered: from alert_state.recovered, filtered to 30 days
    recovered = alert_state.get("recovered", {})
    recently_recovered = []
    for ticker, rec_info in recovered.items():
        try:
            rec_date = datetime.fromisoformat(rec_info["recovered_date"])
            if rec_date.tzinfo is None:
                rec_date = rec_date.replace(tzinfo=timezone.utc)
            if rec_date >= cutoff_30d:
                current_rsi_val = rsi_values.get(ticker, {}).get("current_rsi")
                confirmed_rsi_val = rsi_values.get(ticker, {}).get("confirmed_rsi")
                recently_recovered.append({
                    "ticker": ticker,
                    "confirmed_rsi": confirmed_rsi_val,
                    "current_rsi": current_rsi_val,
                    "recovered_date": rec_info["recovered_date"],
                    "was_suppressed_since": rec_info.get("was_suppressed_since", ""),
                })
        except Exception:
            pass

    # All RSI values for histogram
    all_rsi_values = {
        ticker: {
            "confirmed_rsi": vals.get("confirmed_rsi"),
            "current_rsi": vals.get("current_rsi"),
        }
        for ticker, vals in rsi_values.items()
    }

    dashboard = {
        "generated_at": ts,
        "system_status": {
            "universe": universe_status,
            "rsi_scan": rsi_status,
            "leap_check": leap_status,
            "alerts": alert_status_block,
        },
        "rate_limit_log": rate_limit_log,
        "qualifying_stocks": qualifying_stocks,
        "early_warnings": early_warnings,
        "recently_recovered": recently_recovered,
        "all_rsi_values": all_rsi_values,
    }

    os.makedirs(os.path.dirname(config.DASHBOARD_DATA_PATH), exist_ok=True)
    with open(config.DASHBOARD_DATA_PATH, "w") as f:
        json.dump(dashboard, f, indent=2)

    print(f"Dashboard written to {config.DASHBOARD_DATA_PATH}")


def generate_sample():
    """Write mock dashboard.json for UI development."""
    sample = {
        "generated_at": "2026-03-03T22:15:00Z",
        "system_status": {
            "universe": {"name": "S&P 1500", "ticker_count": 1498, "last_updated": "2026-03-01T00:00:00Z", "status": "ok"},
            "rsi_scan": {"last_run": "2026-03-03T22:12:00Z", "tickers_scanned": 1498, "tickers_failed": 3, "rate_limits_hit": 0, "confirmed_oversold_count": 7, "current_only_oversold_count": 4, "status": "ok"},
            "leap_check": {"last_run": "2026-03-02T03:15:00Z", "oversold_checked": 12, "leaps_found": 5, "rate_limits_hit": 0, "status": "ok"},
            "alerts": {"total_sent_this_week": 2, "total_suppressed": 5, "pushover_errors": 0},
        },
        "rate_limit_log": [
            {"timestamp": "2026-02-28T22:08:00Z", "phase": "rsi_scan", "count": 1, "resolution": "recovered_after_backoff"}
        ],
        "qualifying_stocks": [
            {"ticker": "INTC", "confirmed_rsi": 24.3, "current_rsi": 22.1, "price": 22.15, "has_leaps": True, "furthest_leap_expiry": "2028-01-21", "first_triggered": "2026-03-02", "alert_status": "new"},
            {"ticker": "WBA", "confirmed_rsi": 18.7, "current_rsi": 19.2, "price": 9.42, "has_leaps": True, "furthest_leap_expiry": "2028-01-21", "first_triggered": "2026-02-25", "alert_status": "suppressed"},
            {"ticker": "ABC", "confirmed_rsi": 27.1, "current_rsi": 26.5, "price": 15.30, "has_leaps": False, "furthest_leap_expiry": None, "first_triggered": "2026-03-03", "alert_status": "awaiting_leap_check"},
        ],
        "early_warnings": [
            {"ticker": "XYZ", "confirmed_rsi": 33.1, "current_rsi": 28.5, "price": 45.60, "note": "Current RSI below 30 but Confirmed RSI still above"},
            {"ticker": "FOO", "confirmed_rsi": 31.8, "current_rsi": 27.2, "price": 88.10, "note": "Current RSI below 30 but Confirmed RSI still above"},
        ],
        "recently_recovered": [
            {"ticker": "NKE", "confirmed_rsi": 34.2, "current_rsi": 35.1, "recovered_date": "2026-03-01", "was_suppressed_since": "2026-02-10"},
        ],
        "all_rsi_values": {
            "AAPL": {"confirmed_rsi": 55.2, "current_rsi": 53.8},
            "MSFT": {"confirmed_rsi": 61.8, "current_rsi": 60.4},
            "INTC": {"confirmed_rsi": 24.3, "current_rsi": 22.1},
            "WBA": {"confirmed_rsi": 18.7, "current_rsi": 19.2},
            "NKE": {"confirmed_rsi": 34.2, "current_rsi": 35.1},
            "GOOG": {"confirmed_rsi": 48.9, "current_rsi": 47.3},
            "META": {"confirmed_rsi": 72.1, "current_rsi": 70.8},
            "AMZN": {"confirmed_rsi": 65.3, "current_rsi": 63.7},
            "XYZ": {"confirmed_rsi": 33.1, "current_rsi": 28.5},
            "FOO": {"confirmed_rsi": 31.8, "current_rsi": 27.2},
        },
    }

    os.makedirs(os.path.dirname(config.DASHBOARD_DATA_PATH), exist_ok=True)
    with open(config.DASHBOARD_DATA_PATH, "w") as f:
        json.dump(sample, f, indent=2)
    print(f"Sample dashboard written to {config.DASHBOARD_DATA_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Generate dashboard JSON")
    parser.add_argument("--sample", action="store_true", help="Generate mock data for UI development")
    args = parser.parse_args()

    if args.sample:
        generate_sample()
    else:
        generate()


if __name__ == "__main__":
    main()

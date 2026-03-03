"""
Pushover notification handler for LEAP RSI alerts.
"""

import time
import requests
import config

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def send_alert(ticker: str, confirmed_rsi: float, current_rsi: float,
               price: float, furthest_leap_expiry: str) -> bool:
    """
    Send a Pushover alert for a qualifying LEAP RSI opportunity.

    Args:
        ticker: Stock ticker symbol
        confirmed_rsi: Confirmed weekly RSI value
        current_rsi: Current weekly RSI value
        price: Current stock price
        furthest_leap_expiry: ISO date string of furthest LEAP expiry (e.g. "2028-01-21")

    Returns:
        True on success, False on failure.
    """
    from datetime import datetime
    try:
        expiry_date = datetime.strptime(furthest_leap_expiry, "%Y-%m-%d")
        expiry_display = expiry_date.strftime("%b %Y")
    except (ValueError, TypeError):
        expiry_display = str(furthest_leap_expiry)

    message = (
        f"LEAP RSI Alert: {ticker}\n"
        f"Confirmed Weekly RSI: {confirmed_rsi:.1f} | Current Weekly RSI: {current_rsi:.1f}\n"
        f"Price: ${price:.2f} | Furthest LEAP: {expiry_display}"
    )

    payload = {
        "token": config.PUSHOVER_APP_TOKEN,
        "user": config.PUSHOVER_USER_KEY,
        "title": "LEAP RSI Alert",
        "message": message,
        "priority": 1,
    }

    for attempt in range(2):
        try:
            resp = requests.post(PUSHOVER_URL, data=payload, timeout=15)
            if resp.status_code == 200:
                return True
            print(f"  Pushover error ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"  Pushover request failed: {e}")

        if attempt == 0:
            print(f"  Retrying Pushover in 5s...")
            time.sleep(5)

    return False

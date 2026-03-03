"""
Scrapes S&P 500, S&P 400, and S&P 600 constituent lists from Wikipedia
and writes the combined S&P 1500 universe to data/sp1500_universe.json.
"""

import json
import sys
import os
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

WIKIPEDIA_URLS = {
    "S&P 500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "S&P 400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "S&P 600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; leap-rsi-monitor/1.0)"
}


def scrape_tickers(url: str, index_name: str) -> list[str]:
    print(f"  Fetching {index_name}...")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Wikipedia constituent tables use id="constituents" or are the first
    # wikitable on the page. Try id first, then fall back to first wikitable.
    table = soup.find("table", {"id": "constituents"})
    if table is None:
        table = soup.find("table", {"class": "wikitable"})
    if table is None:
        raise ValueError(f"Could not find constituent table on {url}")

    tickers = []
    rows = table.find_all("tr")[1:]  # skip header row
    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        raw = cells[0].get_text(strip=True)
        # Clean up ticker: remove footnotes, replace dots (BRK.B -> BRK-B for yfinance)
        ticker = raw.split("[")[0].strip().replace(".", "-")
        if ticker:
            tickers.append(ticker)

    print(f"    Found {len(tickers)} tickers")
    return tickers


def main():
    print("Refreshing S&P 1500 universe from Wikipedia...")

    all_tickers: set[str] = set()
    for index_name, url in WIKIPEDIA_URLS.items():
        try:
            tickers = scrape_tickers(url, index_name)
            all_tickers.update(tickers)
        except Exception as e:
            print(f"  ERROR fetching {index_name}: {e}")

    if not all_tickers:
        print("ERROR: No tickers retrieved. Aborting.")
        sys.exit(1)

    sorted_tickers = sorted(all_tickers)
    universe = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "wikipedia",
        "count": len(sorted_tickers),
        "tickers": sorted_tickers,
    }

    os.makedirs(os.path.dirname(config.SP1500_UNIVERSE_PATH), exist_ok=True)
    with open(config.SP1500_UNIVERSE_PATH, "w") as f:
        json.dump(universe, f, indent=2)

    print(f"\nWrote {len(sorted_tickers)} tickers to {config.SP1500_UNIVERSE_PATH}")


if __name__ == "__main__":
    main()

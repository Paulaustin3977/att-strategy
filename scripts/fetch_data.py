#!/usr/bin/env python3
"""CLI: fetch all asset-universe symbols via yfinance.

Writes one CSV per symbol to ``data/``.  4-year daily bars.

Usage:
    python scripts/fetch_data.py                # fetch all
    python scripts/fetch_data.py GC=F ES=F      # fetch only listed symbols
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Ensure src is importable when running this script directly
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data import fetch_all, fetch_symbol  # noqa: E402
from src.universe import all_symbols  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch OHLCV via yfinance.")
    parser.add_argument(
        "symbols",
        nargs="*",
        help="Optional explicit list. Defaults to full asset universe.",
    )
    parser.add_argument(
        "--data-dir",
        default=str(ROOT / "data"),
        help="Output directory (default: data/).",
    )
    parser.add_argument(
        "--period",
        default="4y",
        help="yfinance period (default: 4y).",
    )
    parser.add_argument(
        "--min-bars",
        type=int,
        default=750,
        help="Minimum number of bars required (default: 750).",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing CSV files in the data dir before fetching.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("att.fetch")

    if args.clean:
        from src.data import nuke
        log.info("Cleaning data directory %s", args.data_dir)
        nuke(args.data_dir)

    symbols = args.symbols if args.symbols else all_symbols()
    log.info("Fetching %d symbols", len(symbols))

    results = fetch_all(symbols, args.data_dir, period=args.period,
                        min_bars=args.min_bars)

    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    log.info("OK:   %d", len(ok))
    log.info("FAIL: %d", len(bad))
    if bad:
        log.info("Failed tickers:")
        for r in bad:
            log.info("  %-12s -> %s", r.symbol, r.error)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

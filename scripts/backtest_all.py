#!/usr/bin/env python3
"""Run the ATT Regime Switch backtest on every symbol in data/.

Writes:
  results/per_symbol.csv
  results/aggregate.json
  results/run_summary.txt
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data import load_symbol, sanitise  # noqa: E402
from src.engine import simulate  # noqa: E402
from src.reporting import (  # noqa: E402
    aggregate,
    metrics_for,
    summary_row,
    top_bottom,
    write_reports,
)
from src.strategy import ATTStrategy  # noqa: E402
from src.universe import all_symbols  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ATT backtest on every CSV in data/.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--results-dir", default=str(ROOT / "results"))
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--trail-mult", type=float, default=2.0)
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument(
        "--symbols",
        nargs="*",
        help="Optional explicit list (yfinance tickers). Defaults to CSVs found in --data-dir.",
    )
    args = parser.parse_args()

    log = logging.getLogger("att.backtest")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    data_dir = Path(args.data_dir)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    # Build the symbol list
    if args.symbols:
        symbols = args.symbols
    else:
        # Discover CSVs
        symbols = []
        for csv in sorted(data_dir.glob("*.csv")):
            symbols.append(csv.stem)

    if not symbols:
        log.error("No symbols found in %s. Run fetch_data.py first.", data_dir)
        return 1

    params = ATTStrategy(
        initial_capital=args.initial_capital,
        risk_pct=args.risk_pct,
        trail_mult=args.trail_mult,
    )

    rows = []
    failures = []

    log.info("Running backtest for %d symbols with risk_pct=%.2f trail_mult=%.2f",
             len(symbols), params.risk_pct, params.trail_mult)

    for i, sym in enumerate(symbols, 1):
        # Resolve to CSV; `sym` may be either 'GC_F' (sanitised) or 'GC=F' (raw)
        csv_path = data_dir / f"{sanitise(sym)}.csv"
        if not csv_path.exists():
            log.warning("[%d/%d] %s: csv not found, skipping", i, len(symbols), sym)
            continue
        try:
            df = load_symbol(csv_path)
            if len(df) < 60:
                log.warning("[%d/%d] %s: only %d bars (<60), skipping",
                            i, len(symbols), sym, len(df))
                continue
            res = simulate(df, params, symbol=sym)
            m = metrics_for(res)
            m["symbol"] = sym
            rows.append(m)
            log.info("[%d/%d] %-12s -> ret=%7.2f%% sharpe=%5.2f maxDD=%7.2f%% trades=%d",
                     i, len(symbols), sym,
                     m["total_return"] * 100,
                     m["sharpe"],
                     m["max_drawdown_pct"],
                     m["n_trades"])
        except Exception as e:
            log.error("[%d/%d] %s: FAILED %s", i, len(symbols), sym, e)
            failures.append({"symbol": sym, "error": str(e)})

    if not rows:
        log.error("No successful backtests.")
        return 2

    # Persist reports
    per_symbol_path = results_dir / "per_symbol.csv"
    aggregate_path = results_dir / "aggregate.json"
    write_reports(rows, per_symbol_path=str(per_symbol_path),
                  aggregate_path=str(aggregate_path))
    log.info("Wrote %s and %s", per_symbol_path, aggregate_path)

    # Headline summary
    top, bot = top_bottom(rows, n=5, by="total_return")
    agg = aggregate(rows)
    headline = agg.get("_headline", {})

    print()
    print("=" * 78)
    print("ATT Regime Switch — universe backtest summary")
    print("=" * 78)
    print(f"Symbols run        : {len(rows)}/{len(symbols)}")
    print(f"% symbols profitable: {headline.get('pct_symbols_profitable', 0)*100:.1f}%")
    print(f"Mean Sharpe        : {agg.get('sharpe', {}).get('mean', 0):.3f}")
    print(f"Median Sharpe      : {agg.get('sharpe', {}).get('median', 0):.3f}")
    print(f"Mean MaxDD         : {agg.get('max_drawdown_pct', {}).get('mean', 0):.2f}%")
    print(f"Mean total return  : {agg.get('total_return', {}).get('mean', 0)*100:.2f}%")
    print(f"Median total return: {agg.get('total_return', {}).get('median', 0)*100:.2f}%")
    print()
    print("Top 5 by total return:")
    for m in top:
        print("  " + summary_row(m))
    print("Bottom 5 by total return:")
    for m in bot:
        print("  " + summary_row(m))

    if failures:
        print()
        print("Failures:")
        for f in failures:
            print(f"  {f['symbol']}: {f['error']}")

    # Save a human-readable summary
    summary_path = results_dir / "run_summary.txt"
    with open(summary_path, "w", encoding="utf-8") as fp:
        fp.write(f"Run timestamp: {datetime.now().isoformat()}\n")
        fp.write(f"params: risk_pct={params.risk_pct}, trail_mult={params.trail_mult}, "
                 f"initial_capital={params.initial_capital}\n\n")
        fp.write(f"Symbols successful: {len(rows)}\n")
        fp.write(f"Symbols requested : {len(symbols)}\n")
        fp.write(f"% profitable      : {headline.get('pct_symbols_profitable', 0)*100:.2f}\n\n")
        fp.write("Top 5 by total_return:\n")
        for m in top:
            fp.write("  " + summary_row(m) + "\n")
        fp.write("Bottom 5 by total_return:\n")
        for m in bot:
            fp.write("  " + summary_row(m) + "\n")
        if failures:
            fp.write("\nFailures:\n")
            for f in failures:
                fp.write(f"  {f['symbol']}: {f['error']}\n")
    log.info("Wrote %s", summary_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

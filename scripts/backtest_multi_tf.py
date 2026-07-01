#!/usr/bin/env python3
"""Multi-timeframe default-params backtest sweep.

For each timeframe (15m, 30m, 1h, 4h, weekly), loads every
``data/<symbol>_<tf>.csv`` file, runs ``simulate()`` with the verified
default ``ATTStrategy()`` parameters, and writes:

* ``results/multi_tf/<tf>_per_symbol.csv``
* ``results/multi_tf/<tf>_aggregate.json``

A consolidated summary table lands in
``results/multi_tf/sweep_summary.csv`` for the SUMMARY.md writer.

Usage:
    python scripts/backtest_multi_tf.py
    python scripts/backtest_multi_tf.py --tfs 1h weekly
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.engine import simulate  # noqa: E402
from src.reporting import (  # noqa: E402
    aggregate,
    metrics_for,
    top_bottom,
    write_reports,
)
from src.strategy import ATTStrategy  # noqa: E402

log = logging.getLogger("att.backtest_mtf")

# Per-TF sanity thresholds for "is this backtest meaningful?" We re-use
# the fetch thresholds: if the file made it past the fetch stage it's at
# least this big.
TF_LABELS = ["15m", "30m", "1h", "4h", "weekly"]

# Heuristic "trading days per year" used by Sharpe / Sortino for each TF.
# (Trade-bar returns, not calendar-bar returns.)  We pick numbers that
# match the real-world cadence of each timeframe so the annualised Sharpe
# stays comparable across TFs.
BAR_FREQ = {
    "15m":    252 * 26,    # ~6,552 bars/year (RTH) — we just use the same
    "30m":    252 * 13,    # ~3,276 bars/year
    "1h":     252 * 8,     # ~2,016 (liquid-hours approximation)
    "4h":     252 * 2,     # ~504 bars/year
    "weekly": 52,          # 52 bars/year
}


@dataclass
class TFRunSummary:
    tf: str
    n_symbols: int
    n_traded: int
    n_with_trades: int
    pct_profitable: float
    mean_sharpe: float
    median_sharpe: float
    mean_total_return: float
    mean_max_dd: float
    best: list
    worst: list


def _list_tf_files(data_dir: Path, tf: str) -> list[Path]:
    return sorted(data_dir.glob(f"*_{tf}.csv"))


def _annualisation_factor(tf: str) -> int:
    return BAR_FREQ.get(tf, 252)


def run_tf_backtest(tf: str, data_dir: Path, out_dir: Path,
                    *, min_bars: int = 60, risk_pct: float = 1.0,
                    trail_mult: float = 2.0,
                    initial_capital: float = 10_000.0) -> TFRunSummary:
    """Run the default-params backtest on every CSV in ``*_<tf>.csv``."""
    files = _list_tf_files(data_dir, tf)
    if not files:
        log.warning("[%s] no CSVs found in %s", tf, data_dir)
        return TFRunSummary(
            tf=tf, n_symbols=0, n_traded=0, n_with_trades=0,
            pct_profitable=0.0, mean_sharpe=0.0, median_sharpe=0.0,
            mean_total_return=0.0, mean_max_dd=0.0, best=[], worst=[],
        )

    log.info("[%s] %d CSVs found", tf, len(files))
    periods_per_year = _annualisation_factor(tf)

    # The strategy/engine uses default periods_per_year=252 inside
    # reporting._sharpe. To get a fair annualised Sharpe for each TF we
    # monkey-patch the function for the duration of the loop.
    import src.reporting as reporting
    original_sharpe = reporting._sharpe
    reporting._sharpe = lambda dr: original_sharpe(dr, periods_per_year=periods_per_year)
    original_sortino = reporting._sortino
    reporting._sortino = lambda dr: original_sortino(dr, periods_per_year=periods_per_year)

    rows: list[dict] = []
    failures: list[dict] = []
    try:
        for i, csv in enumerate(files, 1):
            sym = csv.stem.replace(f"_{tf}", "")
            try:
                import pandas as pd
                df = pd.read_csv(csv, parse_dates=["date"]).set_index("date").sort_index()
                if len(df) < min_bars:
                    log.warning("[%s] %s: only %d bars, skipping",
                                tf, sym, len(df))
                    failures.append({"symbol": sym, "error": f"only {len(df)} bars"})
                    continue
                res = simulate(df, ATTStrategy(
                    initial_capital=initial_capital,
                    risk_pct=risk_pct,
                    trail_mult=trail_mult,
                ), symbol=sym)
                m = metrics_for(res)
                m["symbol"] = sym
                m["tf"] = tf
                rows.append(m)
                log.info("[%s %d/%d] %-12s -> ret=%7.2f%% sharpe=%5.2f maxDD=%7.2f%% trades=%d",
                         tf, i, len(files), sym,
                         m["total_return"] * 100, m["sharpe"],
                         m["max_drawdown_pct"], m["n_trades"])
            except Exception as e:
                log.error("[%s %d/%d] %s: FAILED %s", tf, i, len(files), sym, e)
                failures.append({"symbol": sym, "error": str(e)})
    finally:
        # Restore the original _sharpe / _sortino so we don't leak state
        reporting._sharpe = original_sharpe
        reporting._sortino = original_sortino

    if not rows:
        log.warning("[%s] no successful backtests", tf)
        return TFRunSummary(
            tf=tf, n_symbols=len(files), n_traded=0, n_with_trades=0,
            pct_profitable=0.0, mean_sharpe=0.0, median_sharpe=0.0,
            mean_total_return=0.0, mean_max_dd=0.0, best=[], worst=[],
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    per_symbol_path = out_dir / f"{tf}_per_symbol.csv"
    agg_path = out_dir / f"{tf}_aggregate.json"

    # write_reports expects the same schema as backtest_all; the
    # ``tf`` column will appear in the CSV but the aggregate ignores it.
    rows_for_write = [{k: v for k, v in r.items() if k != "tf"} for r in rows]
    write_reports(rows_for_write, per_symbol_path=str(per_symbol_path),
                  aggregate_path=str(agg_path))

    # Re-load the per-symbol CSV to keep schema clean (write_reports
    # doesn't know about our extra tf column).  We just write our own
    # full version over the top.
    import pandas as pd
    df_out = pd.DataFrame(rows)
    df_out.to_csv(per_symbol_path, index=False)

    top, bot = top_bottom(rows, n=5, by="total_return")
    agg = aggregate(rows_for_write)
    headline = agg.get("_headline", {})
    sharpe_stats = agg.get("sharpe", {})
    ret_stats = agg.get("total_return", {})
    dd_stats = agg.get("max_drawdown_pct", {})

    summary = TFRunSummary(
        tf=tf,
        n_symbols=len(files),
        n_traded=len(rows),
        n_with_trades=sum(1 for r in rows if r["n_trades"] > 0),
        pct_profitable=float(headline.get("pct_symbols_profitable", 0.0)),
        mean_sharpe=float(sharpe_stats.get("mean", 0.0)),
        median_sharpe=float(sharpe_stats.get("median", 0.0)),
        mean_total_return=float(ret_stats.get("mean", 0.0)),
        mean_max_dd=float(dd_stats.get("mean", 0.0)),
        best=top,
        worst=bot,
    )

    print()
    print(f"=== {tf} default-params sweep ===")
    print(f"  files found       : {summary.n_symbols}")
    print(f"  successful backts : {summary.n_traded}")
    print(f"  with >=1 trade    : {summary.n_with_trades}")
    print(f"  % profitable      : {summary.pct_profitable*100:.1f}%")
    print(f"  mean Sharpe       : {summary.mean_sharpe:.3f}")
    print(f"  median Sharpe     : {summary.median_sharpe:.3f}")
    print(f"  mean total return : {summary.mean_total_return*100:.2f}%")
    print(f"  mean MaxDD        : {summary.mean_max_dd:.2f}%")
    print(f"  top 5:")
    for m in top:
        print(f"    {m['symbol']:<14s} ret={m['total_return']*100:>7.2f}% sharpe={m['sharpe']:>5.2f} maxDD={m['max_drawdown_pct']:>7.2f}% trades={m['n_trades']}")
    print(f"  bottom 5:")
    for m in bot:
        print(f"    {m['symbol']:<14s} ret={m['total_return']*100:>7.2f}% sharpe={m['sharpe']:>5.2f} maxDD={m['max_drawdown_pct']:>7.2f}% trades={m['n_trades']}")

    if failures:
        print(f"  failures: {len(failures)}")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-TF default-params sweep.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--out-dir", default=str(ROOT / "results" / "multi_tf"))
    parser.add_argument("--tfs", nargs="*", default=TF_LABELS,
                        choices=TF_LABELS)
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--trail-mult", type=float, default=2.0)
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument("--min-bars", type=int, default=60)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    overall_t0 = time.time()
    summaries: list[TFRunSummary] = []
    for tf in args.tfs:
        s = run_tf_backtest(tf, Path(args.data_dir), out_dir,
                            min_bars=args.min_bars,
                            risk_pct=args.risk_pct,
                            trail_mult=args.trail_mult,
                            initial_capital=args.initial_capital)
        summaries.append(s)

    # Write a small cross-TF CSV for the SUMMARY writer.
    import pandas as pd
    rows = []
    for s in summaries:
        rows.append({
            "tf": s.tf,
            "n_files": s.n_symbols,
            "n_traded": s.n_traded,
            "n_with_trades": s.n_with_trades,
            "pct_profitable": round(s.pct_profitable, 4),
            "mean_sharpe": round(s.mean_sharpe, 4),
            "median_sharpe": round(s.median_sharpe, 4),
            "mean_total_return": round(s.mean_total_return, 4),
            "mean_max_dd": round(s.mean_max_dd, 2),
        })
    pd.DataFrame(rows).to_csv(out_dir / "sweep_summary.csv", index=False)
    with open(out_dir / "sweep_summary.json", "w", encoding="utf-8") as fp:
        json.dump(rows, fp, indent=2)

    log.info("All done in %.1fs. Wrote %d TF reports to %s",
             time.time() - overall_t0, len(summaries), out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
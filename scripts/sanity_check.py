#!/usr/bin/env python3
"""Sanity-check script — run ATT Regime Switch on GC=F.

Prints the first 5 entries/exits plus headline metrics.  Paul's eyeball
verification hook — numbers should match TradingView to a reasonable
tolerance.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data import load_symbol, sanitise  # noqa: E402
from src.engine import simulate  # noqa: E402
from src.reporting import metrics_for, summary_row  # noqa: E402
from src.strategy import ATTStrategy, BacktestResult  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="ATT Regime Switch sanity check on GC=F")
    parser.add_argument(
        "--symbol", default="GC=F",
        help="Ticker to test (default GC=F). Uses data/<sanitised>.csv.",
    )
    parser.add_argument(
        "--data-dir", default=str(ROOT / "data"),
        help="Directory containing the CSV files.",
    )
    parser.add_argument(
        "--risk-pct", type=float, default=1.0,
        help="Per-trade risk as percent of equity (default 1.0).",
    )
    parser.add_argument(
        "--trail-mult", type=float, default=2.0,
        help="ATR multiplier for trailing stop (default 2.0).",
    )
    parser.add_argument(
        "--initial-capital", type=float, default=10_000.0,
        help="Initial capital (default 10000).",
    )
    args = parser.parse_args()

    csv_path = Path(args.data_dir) / f"{sanitise(args.symbol)}.csv"
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found. Run fetch_data.py first.", file=sys.stderr)
        return 1

    df = load_symbol(csv_path)
    print(f"Loaded {args.symbol}: {len(df)} bars, "
          f"{df.index.min().date() if len(df) else '?'} -> {df.index.max().date() if len(df) else '?'}")

    params = ATTStrategy(
        initial_capital=args.initial_capital,
        risk_pct=args.risk_pct,
        trail_mult=args.trail_mult,
    )
    print("Strategy params:")
    for k, v in params.__dict__.items():
        print(f"  {k} = {v}")

    res: BacktestResult = simulate(df, params, symbol=args.symbol)

    print()
    print("=" * 78)
    print(f"{args.symbol} headline metrics")
    print("=" * 78)
    m = metrics_for(res)
    print(summary_row(m))
    print()
    # Headline values for easy reference
    print(f"total_return       : {m['total_return']*100:8.2f}%")
    print(f"final_equity       : {m['final_equity']:10.2f}")
    print(f"initial_capital    : {m['initial_capital']:10.2f}")
    print(f"n_trades           : {m['n_trades']:5d}")
    print(f"win_rate           : {m['win_rate']*100:8.2f}%")
    print(f"sharpe             : {m['sharpe']:8.3f}")
    print(f"sortino            : {m['sortino']:8.3f}")
    print(f"max_drawdown_pct   : {m['max_drawdown_pct']:8.2f}%")
    print(f"max_dd_dur (bars)  : {m['max_dd_duration_bars']:5d}")
    print(f"profit_factor      : {m['profit_factor']:8.3f}")
    print(f"avg_trade          : {m['avg_trade']:10.2f}")
    print(f"avg_bars_in_trade  : {m['avg_bars_in_trade']:8.2f}")
    print(f"%time_in_market    : {m['pct_time_in_market']*100:8.2f}%")

    eq = res.equity_curve
    if eq is not None and len(eq):
        print()
        print(f"Equity curve final: {eq.iloc[-1]:.2f}")
        print(f"Equity curve min : {eq.min():.2f}")
        print(f"Equity curve max : {eq.max():.2f}")

    print()
    print("=" * 78)
    print(f"First 5 trades ({min(5, len(res.trades))} of {len(res.trades)})")
    print("=" * 78)
    for i, t in enumerate(res.trades[:5]):
        entry_time = t.entry_time.date() if t.entry_time is not None else "?"
        exit_time = t.exit_time.date() if t.exit_time is not None else "?"
        print(
            f"#{i+1:>2} {t.side:<5} bars[{t.entry_bar:>4d}..{t.exit_bar:>4d}] "
            f"{entry_time} -> {exit_time}  "
            f"entry={t.entry_price:.4f} exit={t.exit_price:.4f} qty={t.qty:.2f} "
            f"pnl={t.pnl:>8.2f} ({t.pnl_pct:>6.2f}%) reason={t.exit_reason}"
        )

    print()
    print("=" * 78)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

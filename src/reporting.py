"""Performance and risk metrics for the ATT backtests.

All numbers are produced from a `BacktestResult`.  Where possible we
mirror the Pine `strategy.*` fields, but most of those (e.g. `netprofit`)
are just scaled variants of what we have here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from .engine import BacktestResult
from .strategy import ATTStrategy


def _max_dd_info(equity: pd.Series):
    """Return ``(max_drawdown_pct, max_dd_duration_bars)``.

    `max_drawdown_pct` is negative, expressed as a percentage of peak.
    """
    if equity is None or len(equity) == 0:
        return 0.0, 0

    # Mark to peak
    peak = equity.cummax()
    dd = (equity - peak) / peak  # negative or zero
    max_dd = float(dd.min()) if len(dd) else 0.0
    max_dd_pct = max_dd * 100.0

    # Duration — count of consecutive bars where dd < 0, max run
    underwater = (dd < 0).astype(int)
    if len(underwater) == 0:
        return max_dd_pct, 0
    # Use run-length
    run = 0
    max_run = 0
    for v in underwater.values:
        if v == 1:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return max_dd_pct, max_run


def _cagr(initial: float, final: float, n_bars: int, periods_per_year: int = 252) -> float:
    if initial <= 0 or n_bars <= 0 or final <= 0:
        return 0.0
    years = n_bars / periods_per_year
    if years <= 0:
        return 0.0
    try:
        return (final / initial) ** (1.0 / years) - 1.0
    except Exception:
        return 0.0


def _sharpe(daily_returns: pd.Series, periods_per_year: int = 252) -> float:
    if daily_returns is None or len(daily_returns) < 2:
        return 0.0
    s = daily_returns.std(ddof=1)
    if s is None or s == 0 or math.isnan(float(s)):
        return 0.0
    return float(daily_returns.mean() / s * math.sqrt(periods_per_year))


def _sortino(daily_returns: pd.Series, periods_per_year: int = 252) -> float:
    if daily_returns is None or len(daily_returns) < 2:
        return 0.0
    downside = daily_returns.clip(upper=0.0)
    s = downside.std(ddof=1)
    if s is None or s == 0 or math.isnan(float(s)):
        return 0.0
    return float(daily_returns.mean() / s * math.sqrt(periods_per_year))


def _profit_factor(trades: list) -> float:
    wins = sum(t.pnl for t in trades if t.pnl > 0)
    losses = -sum(t.pnl for t in trades if t.pnl < 0)
    if losses <= 0:
        return float("inf") if wins > 0 else 0.0
    return float(wins / losses)


def _expectancy(trades: list) -> float:
    if not trades:
        return 0.0
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    p_win = len(wins) / len(trades) if trades else 0.0
    p_loss = 1.0 - p_win
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return float(p_win * avg_win + p_loss * avg_loss)


def metrics_for(result: BacktestResult) -> dict:
    """Compute per-backtest summary metrics.

    Returns a dictionary of scalar metrics, JSON-friendly (no NaN — uses 0
    or None where required).
    """
    trades = result.trades
    eq = result.equity_curve
    dr = result.daily_returns
    init = result.initial_capital
    final = result.final_equity

    n_trades = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]

    win_rate = len(wins) / n_trades if n_trades else 0.0
    avg_trade = (sum(t.pnl for t in trades) / n_trades) if n_trades else 0.0
    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0

    # Time in market — count of bars where equity != previous equity
    # approximation: bars between entry and exit inclusive
    n_bars = len(eq) if eq is not None else 0
    bars_in_market = sum((t.exit_bar - t.entry_bar + 1) for t in trades)
    pct_time_in_market = bars_in_market / n_bars if n_bars else 0.0

    avg_bars_in_trade = (sum((t.exit_bar - t.entry_bar + 1) for t in trades) / n_trades) if n_trades else 0.0

    total_return = (final - init) / init if init else 0.0
    max_dd_pct, max_dd_dur = _max_dd_info(eq)

    return {
        "symbol": result.symbol,
        "n_bars": int(n_bars),
        "n_trades": int(n_trades),
        "initial_capital": float(init),
        "final_equity": float(final),
        "total_return": float(total_return),
        "cagr": float(_cagr(init, final, n_bars)),
        "sharpe": float(_sharpe(dr)),
        "sortino": float(_sortino(dr)),
        "max_drawdown_pct": float(max_dd_pct),
        "max_dd_duration_bars": int(max_dd_dur),
        "win_rate": float(win_rate),
        "profit_factor": float(_profit_factor(trades)),
        "avg_trade": float(avg_trade),
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "best_trade": float(max((t.pnl for t in trades), default=0.0)),
        "worst_trade": float(min((t.pnl for t in trades), default=0.0)),
        "expectancy": float(_expectancy(trades)),
        "avg_bars_in_trade": float(avg_bars_in_trade),
        "pct_time_in_market": float(pct_time_in_market),
    }


def aggregate(per_symbol: Iterable[dict]) -> dict:
    """Aggregate per-symbol metrics: mean/median/std/min/max per column."""
    per_symbol = list(per_symbol)
    if not per_symbol:
        return {}
    df = pd.DataFrame(per_symbol)
    out = {}
    for col in df.columns:
        if col == "symbol":
            continue
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if s.empty:
            out[col] = {"mean": None, "median": None, "std": None, "min": None, "max": None, "n": 0}
            continue
        out[col] = {
            "mean": float(s.mean()),
            "median": float(s.median()),
            "std": float(s.std(ddof=1)) if len(s) > 1 else 0.0,
            "min": float(s.min()),
            "max": float(s.max()),
            "n": int(len(s)),
        }

    # Headline counts
    if "total_return" in df.columns:
        returns = pd.to_numeric(df["total_return"], errors="coerce").fillna(0.0)
        out["_headline"] = {
            "pct_symbols_profitable": float((returns > 0).mean()) if len(returns) else 0.0,
            "n_symbols": int(len(df)),
        }
    return out


def write_reports(per_symbol_results: list[dict], *,
                  per_symbol_path: str,
                  aggregate_path: str) -> None:
    """Persist per-symbol CSV + aggregate JSON to disk."""
    import json
    from pathlib import Path

    p = Path(per_symbol_path)
    a = Path(aggregate_path)
    a.parent.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(per_symbol_results)
    df.to_csv(p, index=False)

    agg = aggregate(per_symbol_results)
    with open(a, "w", encoding="utf-8") as f:
        json.dump(agg, f, indent=2, default=str)


def top_bottom(per_symbol: list[dict], *, n: int = 5,
               by: str = "total_return") -> tuple[list[dict], list[dict]]:
    """Return the top-N and bottom-N by a metric (default ``total_return``)."""
    sorted_list = sorted(per_symbol, key=lambda r: r.get(by, 0.0), reverse=True)
    return sorted_list[:n], list(reversed(sorted_list[-n:]))


def summary_row(metrics: dict) -> str:
    """Compact single-line summary."""
    return (
        f"{metrics.get('symbol','?'):<12s} "
        f"trades={metrics.get('n_trades',0):>4d} "
        f"ret={metrics.get('total_return',0)*100:>7.2f}% "
        f"sharpe={metrics.get('sharpe',0):>5.2f} "
        f"maxDD={metrics.get('max_drawdown_pct',0):>7.2f}% "
        f"win={metrics.get('win_rate',0)*100:>5.2f}%"
    )

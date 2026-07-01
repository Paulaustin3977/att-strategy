"""Walk-forward parameter optimizer for the ATT Regime Switch strategy.

The optimizer sweeps a parameter grid under proper out-of-sample (OOS)
discipline:

* For every (symbol, parameter-combo) we split the 4-year OHLCV frame
  into **4 rolling 1-year windows**.  Within each window we train on the
  first 75% (~9 months) and test on the trailing 25% (~3 months).
* We record OOS Sharpe, total return, MaxDD, and trade count on the
  *test* segment of each window.  These are averaged across the 4
  windows to give a per-(symbol, combo) score.
* We then aggregate across symbols and rank parameter combos by their
  cross-symbol robustness: high mean OOS Sharpe **subject to** OOS
  profitability / MaxDD / return-consistency filters so we don't pick a
  combo that happens to work on a single instrument.

**Methodology choices and honest caveats**

* **Phase 1 (coarse)** uses Latin Hypercube Sampling over a discrete
  grid.  The full cartesian product of the grid is ~210k combos; we
  draw ~300 representative samples to keep runtime tractable.
* **Phase 2 (refined)** takes the top-K combos from Phase 1 and
  performs a local ±1-step search around each axis.
* **Phase 3 (winner)** picks the best combo, computes its full-universe
  metrics, runs a ±10% sensitivity sweep, plots equity curves, and
  writes a markdown robustness report.

**Performance**

The iterative engine is the bottleneck (~0.04 s per simulation on a
single 1000-bar symbol).  We parallelise across (symbol, combo)
chunks via ``multiprocessing.Pool`` so each worker loads one symbol
once and then iterates over its assigned combos.  Memory stays bounded
because workers never hold more than one symbol at a time.

Total expected runtime: 300 combos × ~50 symbols × 4 windows ÷ 8 cores
≈ 4-8 minutes per phase; cap Phase 2 at 3000 combos → < 90 min total.

**Hard dependencies**
``pandas``, ``numpy``, ``matplotlib``, ``multiprocessing``, ``json``,
``pathlib``.  No scipy / sklearn / pytorch.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import traceback
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from .data import load_symbol, sanitise
from .engine import BacktestResult, simulate
from .reporting import _max_dd_info, _sharpe
from .strategy import ATTStrategy

log = logging.getLogger("att.optimizer")


# ---------------------------------------------------------------------------
# Parameter grid
# ---------------------------------------------------------------------------
# NOTE: the spec referenced a mix of camelCase param names.  The actual
# ``ATTStrategy`` dataclass uses snake_case, and several spec params
# (``rsiOversold``, ``smaTrendLen``, ``mrTrailMult`` ...) don't exist in
# the implemented strategy.  We optimize only the parameters that
# actually drive the engine and signal code.
PARAM_GRID_COARSE: dict[str, list[Any]] = {
    # Indicator lengths
    "adx_len":       [10, 14, 20],
    "atr_len":       [10, 14, 20],
    "ema_len":       [50, 100, 200],
    "rsi_len":       [2, 5, 14],
    "dmi_len":       [10, 14, 20],
    "st_atr_len":    [10, 14, 20],
    # Supertrend multiplier
    "st_mult":       [2.0, 3.0, 4.0],
    # ADX regime thresholds
    "adx_trend":     [20.0, 25.0, 30.0],
    "adx_range":     [15.0, 18.0, 20.0],
    # BBW volatility filter
    "bbwpct_min":    [0.05, 0.10, 0.20],
    # Risk / trail
    "risk_pct":      [0.5, 1.0, 2.0],
    "trail_mult":    [1.5, 2.0, 3.0],
    "max_bars_in_trade": [10, 20, 30],
    # Dead-money exit threshold (|PnL%| < this → exit)
    "dead_money_pct": [0.25, 0.5, 1.0],
}

# Params that must stay integers
INTEGER_PARAMS = {
    "adx_len", "atr_len", "ema_len", "rsi_len", "dmi_len", "st_atr_len",
    "max_bars_in_trade",
}


def params_to_dict(p: ATTStrategy) -> dict[str, Any]:
    """Flatten an ATTStrategy to a dict (only the fields we optimize)."""
    return {k: getattr(p, k) for k in PARAM_GRID_COARSE.keys()}


def dict_to_overrides(d: dict[str, Any]) -> dict[str, Any]:
    """Turn a flat param-dict into an ``ATTStrategy.replace`` kwargs dict,
    coercing integer params to ``int``."""
    out = {}
    for k, v in d.items():
        if k in INTEGER_PARAMS:
            out[k] = int(round(float(v)))
        else:
            out[k] = float(v)
    return out


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
def sample_param_grid(grid: dict[str, list[Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """Latin Hypercube-ish sampling over a discrete grid.

    For each axis we shuffle the indices and pair them up so that the
    marginal distribution is uniform.  We then take ``n`` rows from the
    cartesian outer product without replacement (when the grid is small
    enough; otherwise with replacement and dedup).
    """
    rng = np.random.default_rng(seed)
    keys = list(grid.keys())
    # Index columns: for each key, a random permutation of len(values)
    cols = []
    for k in keys:
        vals = grid[k]
        perm = rng.permutation(len(vals))
        cols.append(perm)

    n_cols = len(keys)
    n_rows = max(n, max(len(c) for c in cols))

    combos: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    attempts = 0
    while len(combos) < n and attempts < n * 50:
        attempts += 1
        idx = rng.integers(0, n_rows, size=n_cols)
        row = {}
        for k, c, i in zip(keys, cols, idx):
            row[k] = grid[k][int(c[int(i) % len(c)])]
        key = tuple(sorted(row.items()))
        if key in seen:
            continue
        seen.add(key)
        combos.append(row)
    return combos


def local_search_neighborhood(center: dict[str, Any], grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """For each axis, try values within ±1 step of the center's value.

    Returns the *delta-1* neighborhood: includes the original plus all
    single-axis mutations.  Capped at the cartesian neighborhood size.
    """
    out: list[dict[str, Any]] = [dict(center)]
    for k, vals in grid.items():
        c = center.get(k)
        if c not in vals:
            continue
        i = vals.index(c)
        for di in (-1, +1):
            j = i + di
            if 0 <= j < len(vals):
                nb = dict(center)
                nb[k] = vals[j]
                out.append(nb)
    return out


def refined_combos(seeds: list[dict[str, Any]], grid: dict[str, list[Any]], cap: int) -> list[dict[str, Any]]:
    """Take top-K seeds and generate a focused local neighborhood.

    Deduplicates across seeds and caps at ``cap`` total combos to bound
    runtime.
    """
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for s in seeds:
        for c in local_search_neighborhood(s, grid):
            k = tuple(sorted(c.items()))
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
            if len(out) >= cap:
                return out
    return out


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------
@dataclass
class WindowResult:
    symbol: str
    params: dict
    window_idx: int
    train_start: Any
    train_end: Any
    test_start: Any
    test_end: Any
    train_sharpe: float
    test_sharpe: float
    test_total_return: float
    test_max_dd_pct: float
    test_n_trades: int


@dataclass
class ComboSymbolResult:
    symbol: str
    params: dict
    n_windows: int
    mean_oos_sharpe: float
    mean_oos_total_return: float
    mean_oos_max_dd: float
    sum_oos_n_trades: int
    pct_oos_positive: float          # share of windows with total_return > 0
    robustness_score: float          # mean_oos_sharpe * sqrt(n_windows) * pct_oos_positive


def walk_forward(df: pd.DataFrame, params: ATTStrategy, *,
                 n_windows: int = 4, train_frac: float = 0.75,
                 min_window_bars: int = 200) -> list[WindowResult]:
    """Split ``df`` into ``n_windows`` rolling windows; on each window,
    run a backtest on the train slice and on the test slice.
    """
    n = len(df)
    if n < min_window_bars:
        return []

    # Slice the frame into n equal contiguous windows of the calendar axis.
    # We use date-based binning so windows are "1 year" of data each.
    # First/last bar are excluded if NaN.
    df = df.dropna(subset=["close"])
    n = len(df)
    if n < min_window_bars:
        return []

    # Determine window boundaries by integer index so each window has
    # ~n/n_windows bars.
    win_size = n // n_windows
    if win_size < min_window_bars:
        # Not enough data for the requested number of windows.
        return []

    overrides = params_to_dict(params)
    overrides.update(dict_to_overrides(overrides))  # coerce ints

    out: list[WindowResult] = []
    for w in range(n_windows):
        start = w * win_size
        end = n if w == n_windows - 1 else (w + 1) * win_size
        if end - start < min_window_bars:
            continue
        train_n = int((end - start) * train_frac)
        if train_n < 60 or (end - start) - train_n < 30:
            continue
        train_df = df.iloc[start:start + train_n]
        test_df = df.iloc[start + train_n:end]

        # Train backtest (only its Sharpe is used as in-sample diagnostic)
        try:
            train_res = simulate(train_df, params)
        except Exception as e:
            log.debug("train sim failed window %d: %s", w, e)
            continue

        # Test backtest (these are the OOS metrics)
        try:
            test_res = simulate(test_df, params)
        except Exception as e:
            log.debug("test sim failed window %d: %s", w, e)
            continue

        test_dr = test_res.daily_returns if test_res.daily_returns is not None else pd.Series(dtype=float)
        test_eq = test_res.equity_curve if test_res.equity_curve is not None else pd.Series(dtype=float)
        test_sharpe = _sharpe(test_dr)
        test_max_dd, _ = _max_dd_info(test_eq)
        init = test_res.initial_capital or 1.0
        test_total_return = (test_res.final_equity - init) / init if init else 0.0

        train_dr = train_res.daily_returns if train_res.daily_returns is not None else pd.Series(dtype=float)
        train_sharpe = _sharpe(train_dr)

        out.append(WindowResult(
            symbol="",  # filled by caller
            params=params_to_dict(params),
            window_idx=w,
            train_start=train_df.index[0],
            train_end=train_df.index[-1],
            test_start=test_df.index[0],
            test_end=test_df.index[-1],
            train_sharpe=float(train_sharpe),
            test_sharpe=float(test_sharpe),
            test_total_return=float(test_total_return),
            test_max_dd_pct=float(test_max_dd),
            test_n_trades=int(test_res.n_trades()),
        ))
    return out


# ---------------------------------------------------------------------------
# Worker (runs inside multiprocessing.Pool)
# ---------------------------------------------------------------------------
def _evaluate_symbol_combo(args: tuple[str, dict[str, Any], str]) -> Optional[ComboSymbolResult]:
    """Worker entry point: load one symbol, run walk-forward for one combo.

    Exceptions are caught and logged so a single bad combo doesn't take
    down the pool.
    """
    symbol_csv, combo, csv_dir = args
    try:
        df = load_symbol(Path(csv_dir) / f"{symbol_csv}.csv")
        params = ATTStrategy(**dict_to_overrides(combo))
        windows = walk_forward(df, params)
        if not windows:
            return None
        sharpes = [w.test_sharpe for w in windows]
        returns = [w.test_total_return for w in windows]
        maxdds = [w.test_max_dd_pct for w in windows]
        n_trades = sum(w.test_n_trades for w in windows)
        if n_trades < 5:
            # Per spec: discard symbols that produced <5 trades total.
            return None
        sharpe_arr = np.asarray(sharpes, dtype=float)
        mean_sharpe = float(sharpe_arr.mean())
        mean_return = float(np.mean(returns)) if returns else 0.0
        mean_max_dd = float(np.mean(maxdds)) if maxdds else 0.0
        pct_pos = float(np.mean([1.0 if r > 0 else 0.0 for r in returns])) if returns else 0.0
        n_w = len(windows)
        # Robustness score rewards both Sharpe and consistency.
        robustness = mean_sharpe * math.sqrt(max(n_w, 1)) * pct_pos
        return ComboSymbolResult(
            symbol=symbol_csv,
            params=combo,
            n_windows=n_w,
            mean_oos_sharpe=mean_sharpe,
            mean_oos_total_return=mean_return,
            mean_oos_max_dd=mean_max_dd,
            sum_oos_n_trades=int(n_trades),
            pct_oos_positive=pct_pos,
            robustness_score=float(robustness),
        )
    except Exception as e:
        log.debug("worker failed for %s: %s\n%s", symbol_csv, e, traceback.format_exc())
        return None


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------
@dataclass
class ComboSummary:
    params: dict
    n_symbols_evaluated: int
    n_symbols_qualified: int          # >=5 OOS trades
    mean_oos_sharpe: float
    median_oos_sharpe: float
    mean_oos_total_return: float
    mean_oos_max_dd: float
    pct_symbols_positive_oos: float    # OOS Sharpe > 0
    pct_symbols_positive_return: float # OOS total_return > 0
    pct_symbols_low_dd: float          # MaxDD < 35%
    robustness_score: float


def aggregate_combos(per_combo_symbol: list[ComboSymbolResult]) -> dict[tuple, list[ComboSymbolResult]]:
    """Group results by parameter-combo (frozen dict → tuple)."""
    out: dict[tuple, list[ComboSymbolResult]] = {}
    for r in per_combo_symbol:
        key = tuple(sorted(r.params.items()))
        out.setdefault(key, []).append(r)
    return out


def summarize_combo(group: list[ComboSymbolResult]) -> ComboSummary:
    sharpes = np.asarray([r.mean_oos_sharpe for r in group], dtype=float)
    returns = np.asarray([r.mean_oos_total_return for r in group], dtype=float)
    maxdds = np.asarray([r.mean_oos_max_dd for r in group], dtype=float)
    n_total = len(group)
    n_pos_sharpe = int((sharpes > 0).sum())
    n_pos_return = int((returns > 0).sum())
    n_low_dd = int((maxdds > -35.0).sum())  # MaxDD < 35% means > -35%

    mean_sharpe = float(sharpes.mean())
    median_sharpe = float(np.median(sharpes))
    pct_pos_sharpe = n_pos_sharpe / n_total if n_total else 0.0
    pct_pos_return = n_pos_return / n_total if n_total else 0.0
    pct_low_dd = n_low_dd / n_total if n_total else 0.0

    # Cross-symbol robustness score.  Reward mean Sharpe and consistency.
    robustness = mean_sharpe * pct_pos_sharpe * math.log1p(n_total)

    return ComboSummary(
        params=group[0].params,
        n_symbols_evaluated=n_total,
        n_symbols_qualified=n_total,
        mean_oos_sharpe=mean_sharpe,
        median_oos_sharpe=median_sharpe,
        mean_oos_total_return=float(returns.mean()) if n_total else 0.0,
        mean_oos_max_dd=float(maxdds.mean()) if n_total else 0.0,
        pct_symbols_positive_oos=pct_pos_sharpe,
        pct_symbols_positive_return=pct_pos_return,
        pct_symbols_low_dd=pct_low_dd,
        robustness_score=float(robustness),
    )


def select_winner(summaries: list[ComboSummary],
                  *,
                  min_pct_pos_oos: float = 0.60,
                  min_pct_low_dd: float = 0.70,
                  min_pct_pos_return: float = 0.55,
                  min_n_symbols: int = 5) -> Optional[ComboSummary]:
    """Pick the best combo subject to cross-symbol robustness filters."""
    eligible = [
        s for s in summaries
        if s.n_symbols_qualified >= min_n_symbols
        and s.pct_symbols_positive_oos >= min_pct_pos_oos
        and s.pct_symbols_low_dd >= min_pct_low_dd
        and s.pct_symbols_positive_return >= min_pct_pos_return
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda s: s.robustness_score)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
@dataclass
class OptimizerConfig:
    csv_dir: str = "data"
    out_dir: str = "results/optimization"
    n_phase1_combos: int = 300
    n_phase2_combos: int = 3000
    # Per spec: refine around top-30 phase-1 combos
    n_top_for_phase2: int = 30
    n_jobs: int = 8
    seed: int = 42
    n_windows: int = 4
    train_frac: float = 0.75
    min_trades_per_symbol: int = 5
    min_pct_pos_oos: float = 0.60
    min_pct_low_dd: float = 0.70
    min_pct_pos_return: float = 0.55


def list_symbol_csvs(csv_dir: Path, symbols: Optional[list[str]] = None) -> list[str]:
    """Return sanitised symbol stems present as CSVs."""
    csv_dir = Path(csv_dir)
    if symbols:
        return [sanitise(s) for s in symbols if (csv_dir / f"{sanitise(s)}.csv").exists()]
    return sorted(p.stem for p in csv_dir.glob("*.csv"))


def run_phase1(cfg: OptimizerConfig, symbols: list[str]) -> tuple[list[ComboSummary], list[ComboSymbolResult]]:
    """Phase 1: LHS over the coarse grid, full walk-forward."""
    log.info("Phase 1: sampling %d combos from coarse grid (seed=%d, symbols=%d)",
             cfg.n_phase1_combos, cfg.seed, len(symbols))
    combos = sample_param_grid(PARAM_GRID_COARSE, cfg.n_phase1_combos, cfg.seed)
    log.info("Phase 1: %d unique combos to evaluate", len(combos))

    t0 = time.time()
    raw = _run_pool(cfg, symbols, combos)
    log.info("Phase 1 simulation wall-time: %.1fs, %d successful results", time.time() - t0, len(raw))

    grouped = aggregate_combos(raw)
    summaries = [summarize_combo(g) for g in grouped.values()]
    summaries.sort(key=lambda s: s.robustness_score, reverse=True)
    return summaries, raw


def run_phase2(cfg: OptimizerConfig, symbols: list[str],
               phase1_summaries: list[ComboSummary]) -> tuple[list[ComboSummary], list[ComboSymbolResult]]:
    """Phase 2: refined search around top-K phase-1 combos."""
    top = phase1_summaries[: cfg.n_top_for_phase2]
    log.info("Phase 2: refining around top-%d phase-1 combos, cap=%d", len(top), cfg.n_phase2_combos)
    seeds = [s.params for s in top]
    combos = refined_combos(seeds, PARAM_GRID_COARSE, cfg.n_phase2_combos)
    log.info("Phase 2: %d unique refined combos", len(combos))

    t0 = time.time()
    raw = _run_pool(cfg, symbols, combos)
    log.info("Phase 2 simulation wall-time: %.1fs, %d successful results", time.time() - t0, len(raw))

    grouped = aggregate_combos(raw)
    summaries = [summarize_combo(g) for g in grouped.values()]
    summaries.sort(key=lambda s: s.robustness_score, reverse=True)
    return summaries, raw


def _run_pool(cfg: OptimizerConfig, symbols: list[str],
              combos: list[dict[str, Any]]) -> list[ComboSymbolResult]:
    """Build the (symbol, combo) work list and run it across processes."""
    work = [(s, c, cfg.csv_dir) for s in symbols for c in combos]
    if not work:
        return []
    n_jobs = max(1, min(cfg.n_jobs, os.cpu_count() or 1))
    log.info("Pool: %d jobs, %d work items", n_jobs, len(work))

    results: list[ComboSymbolResult] = []
    # On macOS the default "fork" start method is fine; we don't share
    # any state across workers.  chunksize is chosen so each worker
    # gets roughly equal chunks.
    chunksize = max(1, len(work) // (n_jobs * 8))
    if n_jobs == 1:
        for w in work:
            r = _evaluate_symbol_combo(w)
            if r is not None:
                results.append(r)
        return results
    import multiprocessing as mp
    ctx = mp.get_context("fork")
    with ctx.Pool(processes=n_jobs) as pool:
        for r in pool.imap_unordered(_evaluate_symbol_combo, work, chunksize=chunksize):
            if r is not None:
                results.append(r)
    return results


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------
def _results_to_long_df(per_combo_symbol: list[ComboSymbolResult]) -> pd.DataFrame:
    """Long-format: one row per (symbol, combo)."""
    rows = []
    for r in per_combo_symbol:
        row = {"symbol": r.symbol, **r.params}
        row.update({
            "n_windows": r.n_windows,
            "mean_oos_sharpe": r.mean_oos_sharpe,
            "mean_oos_total_return": r.mean_oos_total_return,
            "mean_oos_max_dd_pct": r.mean_oos_max_dd,
            "sum_oos_n_trades": r.sum_oos_n_trades,
            "pct_oos_positive": r.pct_oos_positive,
            "robustness_score": r.robustness_score,
        })
        rows.append(row)
    return pd.DataFrame(rows)


def _summaries_to_df(summaries: list[ComboSummary]) -> pd.DataFrame:
    rows = []
    for s in summaries:
        row = {**s.params}
        row.update({
            "n_symbols_evaluated": s.n_symbols_evaluated,
            "n_symbols_qualified": s.n_symbols_qualified,
            "mean_oos_sharpe": s.mean_oos_sharpe,
            "median_oos_sharpe": s.median_oos_sharpe,
            "mean_oos_total_return": s.mean_oos_total_return,
            "mean_oos_max_dd_pct": s.mean_oos_max_dd,
            "pct_symbols_positive_oos": s.pct_symbols_positive_oos,
            "pct_symbols_positive_return": s.pct_symbols_positive_return,
            "pct_symbols_low_dd": s.pct_symbols_low_dd,
            "robustness_score": s.robustness_score,
        })
        rows.append(row)
    return pd.DataFrame(rows)


def write_phase_outputs(out_dir: Path, phase: int,
                        summaries: list[ComboSummary],
                        raw: list[ComboSymbolResult]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    long_df = _results_to_long_df(raw)
    long_df.to_csv(out_dir / f"phase{phase}_results.csv", index=False)
    sum_df = _summaries_to_df(summaries)
    sum_df.to_csv(out_dir / f"phase{phase}_summary.csv", index=False)


def write_winner(out_dir: Path, winner: ComboSummary,
                 per_symbol: list[ComboSymbolResult],
                 cfg: OptimizerConfig) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "params": winner.params,
        "metrics": {
            "n_symbols_evaluated": winner.n_symbols_qualified,
            "mean_oos_sharpe": winner.mean_oos_sharpe,
            "median_oos_sharpe": winner.median_oos_sharpe,
            "mean_oos_total_return": winner.mean_oos_total_return,
            "mean_oos_max_dd_pct": winner.mean_oos_max_dd,
            "pct_symbols_positive_oos": winner.pct_symbols_positive_oos,
            "pct_symbols_positive_return": winner.pct_symbols_positive_return,
            "pct_symbols_low_dd": winner.pct_symbols_low_dd,
            "robustness_score": winner.robustness_score,
        },
        "filters": {
            "min_pct_pos_oos": cfg.min_pct_pos_oos,
            "min_pct_low_dd": cfg.min_pct_low_dd,
            "min_pct_pos_return": cfg.min_pct_pos_return,
        },
        "per_symbol": [
            {
                "symbol": r.symbol,
                "mean_oos_sharpe": r.mean_oos_sharpe,
                "mean_oos_total_return": r.mean_oos_total_return,
                "mean_oos_max_dd_pct": r.mean_oos_max_dd,
                "sum_oos_n_trades": r.sum_oos_n_trades,
                "pct_oos_positive": r.pct_oos_positive,
            }
            for r in per_symbol
        ],
    }
    with open(out_dir / "winner.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


def render_winner_params_python(params: dict[str, Any]) -> str:
    """Pretty-print a Python dict literal for ``strategy.py``."""
    lines = ["ATTStrategy("]
    items = list(params.items())
    for i, (k, v) in enumerate(items):
        sep = "," if i < len(items) - 1 else ""
        if isinstance(v, float):
            lines.append(f"    {k}={v!r}{sep}")
        else:
            lines.append(f"    {k}={v}{sep}")
    lines.append(")")
    return "\n".join(lines)
"""Walk-forward optimiser adapted for multiple timeframes.

Reuses the same ``PARAM_GRID_COARSE``, ``walk_forward``, ``run_phase1/2/3``
machinery as ``optimizer.py`` but allows:

* A per-timeframe walk-forward window configuration
  (``TFWindowConfig`` — window size, # windows, train fraction).
* A per-timeframe annualisation factor for Sharpe / Sortino / CAGR so
  numbers are comparable across TFs.

Why a separate module? The 1D / 4y study's optimiser is signed off;
we don't want to risk regressing it. This module is the multi-TF
extension and is exercised only by ``scripts/optimize_multi_tf.py``.

Timeframe configurations (per spec):

| TF     | window size       | # windows | train% |
|--------|-------------------|-----------|--------|
| 15m    | 5 days (~130 bars)| 8         | 75%    |
| 30m    | 10 days (~130 bars)| 6        | 75%    |
| 1h     | 90 days (~360 bars)| 6        | 75%    |
| 4h     | 180 days (~180 bars)| 4       | 75%    |
| weekly | 2 years (~104 bars)| 4        | 75%    |

Statistical power notes:

* 15m: only ~1500 bars per symbol; 8 walk-forward windows of 5-day length.
  With ~130 bars per window and 75% train / 25% test, the OOS slice has
  only ~33 bars.  Sharpe confidence intervals are extremely wide.
* 30m: similar issues — 60-day history means the largest split is 6
  windows of 10 days = ~65 bars each, OOS ~16 bars.  Essentially
  direction-only signal at this horizon.
* 1h: 730-day history → 6 windows of 90 days = ~360 bars each.  Best of
  the intraday options.  OOS ≈ 90 bars per window.
* 4h: derived from 1h so has the same total range but only 4 windows of
  180 days (~180 bars).  OOS ~45 bars per window.
* weekly: 10y history → 4 windows of 2 years (~104 bars).  Most
  comparable to the 1D/4y study's statistical power.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from .data import load_symbol
from .engine import simulate
from .optimizer import (
    INTEGER_PARAMS,
    PARAM_GRID_COARSE,
    ComboSymbolResult,
    ComboSummary,
    OptimizerConfig,
    WindowResult,
    _evaluate_symbol_combo,
    _run_pool,
    aggregate_combos,
    dict_to_overrides,
    list_symbol_csvs,
    params_to_dict,
    refined_combos,
    sample_param_grid,
    select_winner,
    summarize_combo,
    write_phase_outputs,
    write_winner,
)
from .reporting import _max_dd_info, _sharpe
from .strategy import ATTStrategy

log = logging.getLogger("att.optimizer.mtf")


# ---------------------------------------------------------------------------
# Per-timeframe config
# ---------------------------------------------------------------------------
@dataclass
class TFWindowConfig:
    """Walk-forward window sizing for one timeframe."""
    label: str
    n_windows: int
    train_frac: float = 0.75
    min_window_bars: int = 60
    periods_per_year: int = 252  # for Sharpe annualisation


# Per spec.
TF_WINDOWS: dict[str, TFWindowConfig] = {
    "15m":    TFWindowConfig(label="15m",    n_windows=8, min_window_bars=80),
    "30m":    TFWindowConfig(label="30m",    n_windows=6, min_window_bars=80),
    "1h":     TFWindowConfig(label="1h",     n_windows=6, min_window_bars=180),
    "4h":     TFWindowConfig(label="4h",     n_windows=4, min_window_bars=120),
    "weekly": TFWindowConfig(label="weekly", n_windows=4, min_window_bars=60,
                             periods_per_year=52),
}


def list_tf_csvs(data_dir: Path, tf: str) -> list[str]:
    """Return sanitised stems for ``*_<tf>.csv`` files."""
    data_dir = Path(data_dir)
    return sorted(p.stem for p in data_dir.glob(f"*_{tf}.csv"))


# ---------------------------------------------------------------------------
# TF-aware walk-forward
# ---------------------------------------------------------------------------
def walk_forward_mtf(df: pd.DataFrame, params: ATTStrategy, *,
                     cfg: TFWindowConfig) -> list[WindowResult]:
    """Same logic as ``optimizer.walk_forward`` but reads window config from
    a ``TFWindowConfig`` and uses the per-TF periods_per_year for Sharpe.

    NOTE on window floor requirements:

    The engine (``src.engine.simulate``) raises ``ValueError`` if the input
    frame has fewer than 60 bars — it's the floor for the indicator warm-up
    (ADX / DMI need ~14 bars, plus the regime filter and ATR lookback chain
    need additional buildup). Therefore *both* the train slice and the OOS
    test slice must be ≥60 bars. The original MTF code used ``train_n < 30``
    and ``test_n < 10`` as soft floors, which let windows through where the
    OOS slice was 30-40 bars; those windows then died silently inside
    ``simulate`` and the function returned 0 windows.

    This version enforces the 60-bar engine floor for both slices. When the
    requested ``cfg.n_windows`` would force a window smaller than ``2 * 60 =
    120`` bars, the window is dropped rather than silently producing zero
    OOS trades.
    """
    # Engine hard floor on slice length (NOT a soft threshold — actual
    # ValueError is raised by simulate() if violated).
    MIN_SLICE_BARS = 60

    n = len(df)
    if n < MIN_SLICE_BARS:
        return []
    df = df.dropna(subset=["close"])
    n = len(df)
    if n < MIN_SLICE_BARS:
        return []

    # Each window must be at least 2 × MIN_SLICE_BARS so we can split into
    # a 60-bar train and a 60-bar test.
    effective_min_window = max(cfg.min_window_bars, 2 * MIN_SLICE_BARS)
    win_size = n // cfg.n_windows
    if win_size < effective_min_window:
        return []

    overrides = params_to_dict(params)
    overrides.update(dict_to_overrides(overrides))

    out: list[WindowResult] = []
    for w in range(cfg.n_windows):
        start = w * win_size
        end = n if w == cfg.n_windows - 1 else (w + 1) * win_size
        win_len = end - start
        if win_len < effective_min_window:
            continue
        # Default 75/25 train/test, but cap so OOS gets ≥MIN_SLICE_BARS.
        requested_train = int(win_len * cfg.train_frac)
        train_n = max(MIN_SLICE_BARS,
                      min(requested_train, win_len - MIN_SLICE_BARS))
        test_n = win_len - train_n
        if train_n < MIN_SLICE_BARS or test_n < MIN_SLICE_BARS:
            continue
        train_df = df.iloc[start:start + train_n]
        test_df = df.iloc[start + train_n:end]  # noqa

        try:
            train_res = simulate(train_df, params)
        except Exception as e:
            log.debug("train sim failed window %d: %s", w, e)
            continue
        try:
            test_res = simulate(test_df, params)
        except Exception as e:
            log.debug("test sim failed window %d: %s", w, e)
            continue

        test_dr = test_res.daily_returns if test_res.daily_returns is not None else pd.Series(dtype=float)
        test_eq = test_res.equity_curve if test_res.equity_curve is not None else pd.Series(dtype=float)
        test_sharpe = _sharpe(test_dr, periods_per_year=cfg.periods_per_year)
        test_max_dd, _ = _max_dd_info(test_eq)
        init = test_res.initial_capital or 1.0
        test_total_return = (test_res.final_equity - init) / init if init else 0.0

        train_dr = train_res.daily_returns if train_res.daily_returns is not None else pd.Series(dtype=float)
        train_sharpe = _sharpe(train_dr, periods_per_year=cfg.periods_per_year)

        out.append(WindowResult(
            symbol="",
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


def _evaluate_symbol_combo_mtf(args: tuple[str, dict[str, Any], str, str]
                               ) -> Optional[ComboSymbolResult]:
    """Worker: load one symbol × one combo, run TF-aware walk-forward.

    ``args`` = (symbol_stem, combo, csv_dir, tf_label)

    Note on naming: ``list_tf_csvs`` globs ``*_<tf>.csv`` and returns the
    full stem (which already includes the ``_<tf>`` suffix). So the worker
    uses ``symbol_stem`` *as-is* (no second ``_<tf>`` suffix appended).
    """
    symbol_stem, combo, csv_dir, tf_label = args
    cfg = TF_WINDOWS[tf_label]
    try:
        # symbol_stem already encodes the timeframe (e.g. "AUDCAD_X_4h")
        csv_path = Path(csv_dir) / f"{symbol_stem}.csv"
        if not csv_path.exists():
            # Defensive fallback in case a future caller passes a
            # tf-less stem.
            csv_path = Path(csv_dir) / f"{symbol_stem}_{tf_label}.csv"
            if not csv_path.exists():
                return None
        df = load_symbol(csv_path)
        params = ATTStrategy(**dict_to_overrides(combo))
        windows = walk_forward_mtf(df, params, cfg=cfg)
        if not windows:
            return None
        sharpes = [w.test_sharpe for w in windows]
        returns = [w.test_total_return for w in windows]
        maxdds = [w.test_max_dd_pct for w in windows]
        n_trades = sum(w.test_n_trades for w in windows)
        if n_trades < 5:
            return None
        sharpe_arr = np.asarray(sharpes, dtype=float)
        mean_sharpe = float(sharpe_arr.mean())
        mean_return = float(np.mean(returns)) if returns else 0.0
        mean_max_dd = float(np.mean(maxdds)) if maxdds else 0.0
        pct_pos = float(np.mean([1.0 if r > 0 else 0.0 for r in returns])) if returns else 0.0
        n_w = len(windows)
        robustness = mean_sharpe * math.sqrt(max(n_w, 1)) * pct_pos
        return ComboSymbolResult(
            symbol=symbol_stem,
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
        log.debug("MTF worker failed for %s [%s]: %s\n%s",
                  symbol_stem, tf_label, e, traceback.format_exc())
        return None


# ---------------------------------------------------------------------------
# Pool runner for MTF (parallelises (symbol, combo) pairs)
# ---------------------------------------------------------------------------
def _run_pool_mtf(cfg: OptimizerConfig, symbols: list[str], combos: list[dict],
                  tf: str) -> list[ComboSymbolResult]:
    work = [(s, c, cfg.csv_dir, tf) for s in symbols for c in combos]
    if not work:
        return []
    n_jobs = max(1, min(cfg.n_jobs, os.cpu_count() or 1))
    log.info("MTF %s pool: %d jobs, %d work items", tf, n_jobs, len(work))
    results: list[ComboSymbolResult] = []
    chunksize = max(1, len(work) // (n_jobs * 8))
    if n_jobs == 1:
        for i, w in enumerate(work, 1):
            r = _evaluate_symbol_combo_mtf(w)
            if r is not None:
                results.append(r)
            if i % 100 == 0:
                print(f"  [{i}/{len(work)}] {len(results)} ok", flush=True)
        return results
    import multiprocessing as mp
    ctx = mp.get_context("fork")
    with ctx.Pool(processes=n_jobs) as pool:
        completed = 0
        for r in pool.imap_unordered(_evaluate_symbol_combo_mtf, work,
                                     chunksize=chunksize):
            completed += 1
            if r is not None:
                results.append(r)
            if completed % 500 == 0:
                print(f"  [{completed}/{len(work)}] {len(results)} ok", flush=True)
    print(f"  [{completed}/{len(work)}] {len(results)} ok (done)", flush=True)
    return results


# ---------------------------------------------------------------------------
# MTF phase runners (1, 2, 3)
# ---------------------------------------------------------------------------
def run_phase1_mtf(cfg: OptimizerConfig, symbols: list[str], tf: str
                   ) -> tuple[list[ComboSummary], list[ComboSymbolResult]]:
    log.info("[%s] Phase 1: sampling %d combos (seed=%d, symbols=%d)",
             tf, cfg.n_phase1_combos, cfg.seed, len(symbols))
    combos = sample_param_grid(PARAM_GRID_COARSE, cfg.n_phase1_combos, cfg.seed)
    log.info("[%s] Phase 1: %d unique combos", tf, len(combos))
    t0 = time.time()
    raw = _run_pool_mtf(cfg, symbols, combos, tf)
    log.info("[%s] Phase 1 simulation wall-time: %.1fs, %d successful results",
             tf, time.time() - t0, len(raw))
    print(f"[{tf} phase1] simulation wall-time: {time.time() - t0:.1f}s, "
          f"{len(raw)} results", flush=True)
    grouped = aggregate_combos(raw)
    summaries = [summarize_combo(g) for g in grouped.values()]
    summaries.sort(key=lambda s: s.robustness_score, reverse=True)
    return summaries, raw


def run_phase2_mtf(cfg: OptimizerConfig, symbols: list[str], tf: str,
                   phase1_summaries: list[ComboSummary]
                   ) -> tuple[list[ComboSummary], list[ComboSymbolResult]]:
    top = phase1_summaries[: cfg.n_top_for_phase2]
    log.info("[%s] Phase 2: refining around top-%d combos, cap=%d",
             tf, len(top), cfg.n_phase2_combos)
    seeds = [s.params for s in top]
    combos = refined_combos(seeds, PARAM_GRID_COARSE, cfg.n_phase2_combos)
    log.info("[%s] Phase 2: %d unique refined combos", tf, len(combos))
    t0 = time.time()
    raw = _run_pool_mtf(cfg, symbols, combos, tf)
    log.info("[%s] Phase 2 simulation wall-time: %.1fs, %d successful results",
             tf, time.time() - t0, len(raw))
    print(f"[{tf} phase2] simulation wall-time: {time.time() - t0:.1f}s, "
          f"{len(raw)} results", flush=True)
    grouped = aggregate_combos(raw)
    summaries = [summarize_combo(g) for g in grouped.values()]
    summaries.sort(key=lambda s: s.robustness_score, reverse=True)
    return summaries, raw


def _sharpe_for_tf(daily_returns: pd.Series, tf: str) -> float:
    return _sharpe(daily_returns, periods_per_year=TF_WINDOWS[tf].periods_per_year)


# Re-bind the helper so optimizer_report.py picks it up via the same name
# when we run sensitivity — but only for THIS module's call paths.
# We can't easily monkey-patch the imported reference inside
# optimizer_report, so for sensitivity we override its inner function.
def _sensitivity_analysis_mtf(winner_params: dict, symbols: list[str],
                              csv_dir: Path, tf: str,
                              *, pct: float = 0.10) -> list[dict]:
    """Per-TF sensitivity (±pct per param).  Uses periods_per_year for the TF."""
    base = ATTStrategy(**dict_to_overrides(winner_params))
    rows: list[dict] = []

    def _mean_sharpe(p: ATTStrategy) -> tuple[float, int]:
        sharpes = []
        n_qual = 0
        for s in symbols:
            path = csv_dir / f"{s}.csv"
            if not path.exists():
                path = csv_dir / f"{s}_{tf}.csv"
                if not path.exists():
                    continue
            try:
                df = load_symbol(path)
                res = simulate(df, p)
                dr = res.daily_returns if res.daily_returns is not None else pd.Series(dtype=float)
                sh = _sharpe(dr, periods_per_year=TF_WINDOWS[tf].periods_per_year)
                if not math.isnan(sh):
                    sharpes.append(sh)
                    if res.n_trades() >= 1:
                        n_qual += 1
            except Exception:
                continue
        if not sharpes:
            return float("nan"), 0
        return float(np.mean(sharpes)), n_qual

    base_sharpe, base_qual = _mean_sharpe(base)
    rows.append({"param": "_baseline", "value": "winner",
                 "oos_sharpe": base_sharpe, "n_symbols": base_qual})

    for k in PARAM_GRID_COARSE.keys():
        v = getattr(base, k)
        if v == 0:
            continue
        for direction, factor in (("up", 1.0 + pct), ("down", 1.0 - pct)):
            new_v = v * factor
            if k in INTEGER_PARAMS:
                new_v = max(1, int(round(new_v)))
            new_params = ATTStrategy(**{**dict_to_overrides(winner_params), k: new_v})
            sh, nq = _mean_sharpe(new_params)
            rows.append({"param": k, "value": new_v, "direction": direction,
                         "oos_sharpe": sh, "n_symbols": nq})
    return rows


def _simulate_full_universe_mtf(winner_params: dict, symbols: list[str],
                                csv_dir: Path, tf: str) -> pd.DataFrame:
    """One row per symbol, equity curves normalised to 100."""
    params = ATTStrategy(**dict_to_overrides(winner_params))
    curves: dict[str, pd.Series] = {}
    for s in symbols:
        path = csv_dir / f"{s}.csv"
        if not path.exists():
            path = csv_dir / f"{s}_{tf}.csv"
            if not path.exists():
                continue
        try:
            df = load_symbol(path)
            res = simulate(df, params, symbol=s)
        except Exception:
            continue
        eq = (res.equity_curve if res.equity_curve is not None
              else pd.Series(dtype=float)).dropna()
        if eq.empty:
            continue
        norm = eq / float(eq.iloc[0]) * 100.0
        curves[s] = norm
    if not curves:
        return pd.DataFrame()
    return pd.DataFrame(curves).sort_index()


# ---------------------------------------------------------------------------
# Per-TF report writer
# ---------------------------------------------------------------------------
def _md_table(headers: list[str], rows) -> str:
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(lines)


def _params_dict_literal(params: dict) -> str:
    lines = ["```python", "from src.strategy import ATTStrategy", "",
             "ATTStrategy("]
    items = list(params.items())
    for i, (k, v) in enumerate(items):
        sep = "," if i < len(items) - 1 else ""
        if k in INTEGER_PARAMS:
            lines.append(f"    {k}={int(round(float(v)))}{sep}")
        else:
            lines.append(f"    {k}={float(v)!r}{sep}")
    lines.append(")")
    lines.append("```")
    return "\n".join(lines)


def write_tf_robustness_report(*, out_dir: Path,
                               winner: Optional[ComboSummary],
                               per_symbol: list[ComboSymbolResult],
                               tf: str,
                               cfg: OptimizerConfig,
                               phase1_summaries: list[ComboSummary],
                               phase2_summaries: list[ComboSummary],
                               symbols: list[str]) -> dict:
    """Write the per-TF markdown report and return a verdict dict.

    The verdict dict is also written as ``<tf>_verdict.json`` so the
    cross-TF SUMMARY writer can pick it up without re-parsing markdown.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_dir = Path(cfg.csv_dir)
    tfw = TF_WINDOWS[tf]

    n_total = len(symbols)

    # ----- Equity curves + chart -----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    curves = _simulate_full_universe_mtf(winner.params if winner else {},
                                         symbols, csv_dir, tf)
    eq_chart = out_dir / f"{tf}_equity_curves.png"
    if not curves.empty:
        fig, ax = plt.subplots(figsize=(11, 6))
        n = curves.shape[1]
        cmap = plt.get_cmap("viridis")
        for i, col in enumerate(curves.columns):
            ax.plot(curves.index, curves[col], color=cmap(i / max(n - 1, 1)),
                    alpha=0.45, linewidth=0.9, label=col if n <= 12 else None)
        mean_curve = curves.mean(axis=1)
        ax.plot(curves.index, mean_curve, color="crimson", linewidth=2.2,
                label=f"mean ({n} symbols)")
        ax.axhline(100.0, color="grey", linewidth=0.8, linestyle="--")
        ax.set_title(f"ATT Regime Switch — {tf} — winner equity curves "
                     f"({n} symbols, normalised to 100)")
        ax.set_ylabel("Normalized equity (start = 100)")
        ax.set_xlabel("Date")
        ax.grid(True, alpha=0.3)
        if n <= 12:
            ax.legend(loc="upper left", fontsize=8, ncol=2)
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(eq_chart, dpi=130, bbox_inches="tight")
        plt.close(fig)

    # ----- Sensitivity -----
    sens: list[dict] = []
    if winner:
        sens = _sensitivity_analysis_mtf(winner.params, symbols, csv_dir, tf,
                                         pct=0.10)

    # ----- Verdict dict (machine-readable) -----
    if winner is None:
        verdict = {
            "tf": tf,
            "winner": None,
            "mean_oos_sharpe": 0.0,
            "median_oos_sharpe": 0.0,
            "mean_oos_total_return": 0.0,
            "mean_oos_max_dd_pct": 0.0,
            "pct_symbols_positive_oos": 0.0,
            "pct_symbols_positive_return": 0.0,
            "pct_symbols_low_dd": 0.0,
            "robustness_score": 0.0,
            "n_symbols_qualified": 0,
            "n_symbols_total": n_total,
            "passes_filters": False,
            "filters": {
                "min_pct_pos_oos": cfg.min_pct_pos_oos,
                "min_pct_low_dd": cfg.min_pct_low_dd,
                "min_pct_pos_return": cfg.min_pct_pos_return,
            },
            "filter_pass_pct_pos_oos": False,
            "filter_pass_pct_low_dd": False,
            "filter_pass_pct_pos_return": False,
        }
    else:
        passes_pos_oos = winner.pct_symbols_positive_oos >= cfg.min_pct_pos_oos
        passes_low_dd = winner.pct_symbols_low_dd >= cfg.min_pct_low_dd
        passes_pos_return = winner.pct_symbols_positive_return >= cfg.min_pct_pos_return
        verdict = {
            "tf": tf,
            "winner": {k: (float(v) if isinstance(v, (int, float)) else v)
                       for k, v in winner.params.items()},
            "mean_oos_sharpe": winner.mean_oos_sharpe,
            "median_oos_sharpe": winner.median_oos_sharpe,
            "mean_oos_total_return": winner.mean_oos_total_return,
            "mean_oos_max_dd_pct": winner.mean_oos_max_dd,
            "pct_symbols_positive_oos": winner.pct_symbols_positive_oos,
            "pct_symbols_positive_return": winner.pct_symbols_positive_return,
            "pct_symbols_low_dd": winner.pct_symbols_low_dd,
            "robustness_score": winner.robustness_score,
            "n_symbols_qualified": winner.n_symbols_qualified,
            "n_symbols_total": n_total,
            "passes_filters": passes_pos_oos and passes_low_dd and passes_pos_return,
            "filters": {
                "min_pct_pos_oos": cfg.min_pct_pos_oos,
                "min_pct_low_dd": cfg.min_pct_low_dd,
                "min_pct_pos_return": cfg.min_pct_pos_return,
            },
            "filter_pass_pct_pos_oos": passes_pos_oos,
            "filter_pass_pct_low_dd": passes_low_dd,
            "filter_pass_pct_pos_return": passes_pos_return,
        }

    with open(out_dir / f"{tf}_verdict.json", "w", encoding="utf-8") as fp:
        json.dump(verdict, fp, indent=2)

    # ----- Markdown body -----
    md = []
    md.append(f"# ATT Regime Switch — {tf} walk-forward robustness report\n")
    md.append(f"_Generated: {pd.Timestamp.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
    md.append(f"_Universe: {n_total} symbols with `*_{tf}.csv` files._\n")

    md.append("## 1. Walk-forward setup\n")
    md.append(_md_table(
        ["Setting", "Value"],
        [
            ("Window config", tfw.label),
            ("# windows", tfw.n_windows),
            ("Train fraction", f"{tfw.train_frac*100:.0f}%"),
            ("min window bars", tfw.min_window_bars),
            ("periods_per_year (Sharpe annualisation)", tfw.periods_per_year),
            ("Phase 1 combos", cfg.n_phase1_combos),
            ("Phase 2 combos cap", cfg.n_phase2_combos),
            ("Top-K seeded into Phase 2", cfg.n_top_for_phase2),
            ("Workers", cfg.n_jobs),
            ("Seed", cfg.seed),
        ],
    ))
    md.append("")

    if winner is None:
        md.append("## 2. Walk-forward result\n")
        md.append("**No parameter combo survived the ≥5 OOS trades per symbol filter.**")
        md.append("")
        md.append("This is a *negative result*: there is no evidence of a real edge on this "
                  "timeframe in this data window. The strategy did not produce enough trades "
                  "across enough symbols to make a statistically meaningful statement.\n")
    else:
        md.append("## 2. Winning parameter set\n")
        md.append(_params_dict_literal(winner.params))
        md.append("")

        md.append("## 3. Cross-symbol OOS metrics (winner)\n")
        md.append(_md_table(
            ["Metric", "Value"],
            [
                ("Mean OOS Sharpe", f"{winner.mean_oos_sharpe:.3f}"),
                ("Median OOS Sharpe", f"{winner.median_oos_sharpe:.3f}"),
                ("Mean OOS total return", f"{winner.mean_oos_total_return*100:.2f}%"),
                ("Mean OOS MaxDD", f"{winner.mean_oos_max_dd:.2f}%"),
                ("% symbols with OOS Sharpe > 0", f"{winner.pct_symbols_positive_oos*100:.1f}%"),
                ("% symbols with positive OOS return", f"{winner.pct_symbols_positive_return*100:.1f}%"),
                ("% symbols with MaxDD < 35%", f"{winner.pct_symbols_low_dd*100:.1f}%"),
                ("Robustness score", f"{winner.robustness_score:.4f}"),
                ("Symbols qualified (≥5 OOS trades)", f"{winner.n_symbols_qualified}"),
            ],
        ))
        md.append("")

        md.append("## 4. Per-symbol OOS performance (winner)\n")
        md.append("Sorted by OOS Sharpe (descending). Sharpe averaged across walk-forward windows.\n")
        if per_symbol:
            rows = []
            for r in sorted(per_symbol, key=lambda x: x.mean_oos_sharpe, reverse=True):
                rows.append([
                    r.symbol,
                    f"{r.mean_oos_sharpe:.3f}",
                    f"{r.mean_oos_total_return*100:.2f}%",
                    f"{r.mean_oos_max_dd:.2f}%",
                    r.sum_oos_n_trades,
                    f"{r.pct_oos_positive*100:.0f}%",
                ])
            md.append(_md_table(
                ["Symbol", "Mean OOS Sharpe", "Mean OOS Return", "Mean OOS MaxDD",
                 "OOS Trades", "% Windows > 0"],
                rows,
            ))
        md.append("")

        md.append("## 5. Equity curves\n")
        md.append(f"![equity curves]({eq_chart.name})\n")
        md.append("Each line is one symbol's equity, normalised to 100. "
                  "Crimson = cross-symbol mean.\n")

        md.append("## 6. Sensitivity analysis (±10% per parameter)\n")
        md.append("Each row mutates **one** parameter by ±10% and re-runs the strategy on "
                  "every symbol (full-sample, not walk-forward — a fast proxy).\n")
        base_sharpe = sens[0]["oos_sharpe"] if sens else float("nan")
        md.append(f"**Baseline mean universe Sharpe: {base_sharpe:.3f}**\n")
        if sens and len(sens) > 1:
            sens_rows = []
            for r in sens[1:]:
                delta = r["oos_sharpe"] - base_sharpe
                sign = "+" if delta >= 0 else ""
                sens_rows.append([
                    r["param"], r["value"], r["direction"],
                    f"{r['oos_sharpe']:.3f}", f"{sign}{delta:.3f}",
                ])
            md.append(_md_table(
                ["Parameter", "Value", "Direction", "Mean Sharpe", "Δ vs base"],
                sens_rows,
            ))
        md.append("")

    md.append("## 7. Phase 1 / Phase 2 top-10\n")
    md.append("### Phase 1 — coarse LHS\n")
    if phase1_summaries:
        rows = []
        for s in phase1_summaries[:10]:
            rows.append([
                f"{s.robustness_score:.4f}",
                f"{s.mean_oos_sharpe:.3f}",
                f"{s.pct_symbols_positive_oos*100:.0f}%",
                s.n_symbols_qualified,
            ])
        md.append(_md_table(
            ["Robustness", "Mean Sharpe", "% Pos Sharpe", "N symbols"],
            rows,
        ))
    md.append("\n### Phase 2 — refined local search\n")
    if phase2_summaries:
        rows = []
        for s in phase2_summaries[:10]:
            rows.append([
                f"{s.robustness_score:.4f}",
                f"{s.mean_oos_sharpe:.3f}",
                f"{s.pct_symbols_positive_oos*100:.0f}%",
                s.n_symbols_qualified,
            ])
        md.append(_md_table(
            ["Robustness", "Mean Sharpe", "% Pos Sharpe", "N symbols"],
            rows,
        ))
    md.append("")

    md.append("## 8. Honest assessment\n")
    md.append(_tf_caveats(tf, winner))
    md.append("")

    md.append("## 9. Output files\n")
    md.append(
        f"* `phase1_results.csv`, `phase1_summary.csv` — Phase 1 raw + summary.\n"
        f"* `phase2_results.csv`, `phase2_summary.csv` — Phase 2 raw + summary.\n"
        f"* `{tf}_equity_curves.png` — overlaid equity curves for the winner.\n"
        f"* `{tf}_verdict.json` — machine-readable verdict (mean OOS Sharpe, "
        "filter pass/fail, etc.).\n"
        f"* `robustness_report.md` — this file.\n"
    )

    out_path = out_dir / f"{tf}_robustness_report.md"
    out_path.write_text("\n".join(md), encoding="utf-8")
    log.info("[%s] Wrote %s", tf, out_path)

    return verdict


def _tf_caveats(tf: str, winner: Optional[ComboSummary]) -> str:
    """Per-TF honest-verdict text."""
    bits = []
    if tf == "15m":
        bits.append(
            "* **Statistical power is tiny.** Only ~1500 15-minute bars per symbol "
            "(yfinance's 60-day cap). Walk-forward uses 8 windows of 5 days (~130 "
            "bars each), of which 25% is OOS — so each OOS slice is roughly 33 bars. "
            "Sharpe on 33 bars has a 95% confidence interval approximately "
            "spanning ±1.5 around the point estimate.\n"
            "* **Trade count is low.** With a 5-day window and a regime-switch "
            "strategy, expect 1-3 trades per OOS slice. The ≥5-trade filter is "
            "therefore *very* lenient — most symbols will fail it. The winners "
            "that do survive are likely over-fit to noise.\n"
            "* **Tick noise dominates.** 15-minute bars still contain a lot of "
            "intraday noise; the regime classifier (ADX + DMI) was designed for "
            "directional daily moves and doesn't translate cleanly to intraday "
            "noise.\n"
        )
    elif tf == "30m":
        bits.append(
            "* **Same statistical-power issue as 15m.** Even less data per walk-"
            "forward window (10 days × ~13 bars/day = 130 bars, OOS ≈ 32 bars).\n"
            "* **Slightly less noise than 15m** but the regime classifier still "
            "needs multiple bars to settle into a 'bull / bear / range' "
            "classification, and ADX needs ~14 bars of buildup.\n"
        )
    elif tf == "1h":
        bits.append(
            "* **Best intraday option.** 730-day history × ~18 bars per day = "
            "~13,000 hourly bars per symbol. Walk-forward uses 6 windows of 90 "
            "days (~360 bars each, OOS ≈ 90 bars). At 90 bars per OOS slice, "
            "Sharpe's 95% CI tightens to roughly ±0.6 — still wide but "
            "meaningful.\n"
            "* **Regime classifier still has to settle.** ADX / DMI with "
            "default length 14 needs 14 bars to compute. A 14-bar hourly look-"
            "back is ~14 hours of trading — about 2 trading days. Regimes "
            "will be slower to flip at 1h than at 1D.\n"
            "* **Lower commission impact** in absolute dollars per trade at "
            "this cadence vs. 1D; risk-pct is already scaled.\n"
        )
    elif tf == "4h":
        bits.append(
            "* **Built from 1h by resampling.** Total bar count is small "
            "(~1200 4h bars per symbol). Walk-forward uses 4 windows of 180 "
            "days (~180 bars each, OOS ≈ 45 bars).\n"
            "* **Bars include all hours** (no market-hours filter), so a 4h "
            "bar may span a quiet pre-market period followed by the open — "
            "introducing some intraday-structure noise.\n"
            "* **Most bars but fewer trades** — the regime filter requires "
            "ADX / DMI to stabilise, and 4h ADX is sluggish.\n"
        )
    elif tf == "weekly":
        bits.append(
            "* **Most comparable to the 1D/4y study.** 10 years of weekly bars "
            "= ~520 bars per symbol. Walk-forward uses 4 windows of 2 years "
            "(~104 bars each, OOS ≈ 26 bars).\n"
            "* **Sharpe confidence intervals at 26 bars** are wide (~±1.5), "
            "but the absolute number of bars is the same order of magnitude "
            "as the 1D study's OOS slices.\n"
            "* **Less overfitting pressure** — fewer bars means fewer "
            "distinct trade setups to fit against.\n"
        )

    if winner is None:
        bits.append(
            "**Verdict: NEGATIVE.** No combo survived the ≥5 OOS trades per "
            "symbol filter. The strategy does not produce a sufficient number "
            "of OOS trades to make a statistically meaningful statement on "
            "this timeframe.\n"
        )
    else:
        bits.append(_verdict_paragraph(tf, winner))
    return "\n".join(bits)


def _verdict_paragraph(tf: str, winner: ComboSummary) -> str:
    """Render the pass/fail verdict block for a winner."""
    pos_ok = winner.pct_symbols_positive_oos >= 0.60
    dd_ok = winner.pct_symbols_low_dd >= 0.70
    ret_ok = winner.pct_symbols_positive_return >= 0.55
    n_ok = winner.n_symbols_qualified >= 5

    passes_all = pos_ok and dd_ok and ret_ok and n_ok

    if passes_all:
        bits = [
            "**Verdict: PASS (with caveats).** The winner satisfies all four "
            "robustness filters (OOS Sharpe > 0 on ≥60% of symbols, MaxDD < 35% "
            "on ≥70%, positive OOS return on ≥55%, ≥5 qualified symbols).",
            f"Mean OOS Sharpe across {winner.n_symbols_qualified} symbols is "
            f"{winner.mean_oos_sharpe:.3f}.",
            "Caveats: see the per-TF notes above for statistical-power warnings.",
        ]
    else:
        reasons = []
        if not n_ok:
            reasons.append(f"only {winner.n_symbols_qualified} symbols survived the ≥5-trade filter (need ≥5)")
        if not pos_ok:
            reasons.append(
                f"only {winner.pct_symbols_positive_oos*100:.1f}% of symbols had OOS Sharpe > 0 (need ≥60%)"
            )
        if not dd_ok:
            reasons.append(
                f"only {winner.pct_symbols_low_dd*100:.1f}% of symbols had MaxDD < 35% (need ≥70%)"
            )
        if not ret_ok:
            reasons.append(
                f"only {winner.pct_symbols_positive_return*100:.1f}% of symbols had positive OOS return (need ≥55%)"
            )
        bits = [
            "**Verdict: NEGATIVE.** The winner does **not** satisfy all four "
            "robustness filters:",
        ]
        for r in reasons:
            bits.append(f"*   {r}")
        bits.append(
            f"\nMean OOS Sharpe is {winner.mean_oos_sharpe:.3f} across "
            f"{winner.n_symbols_qualified} symbols, but the cross-symbol "
            "consistency is too weak to call this a real edge."
        )
        bits.append(
            "We still write the report, but **no live-trading recommendation** "
            "is implied for this timeframe."
        )
    return "\n\n".join(bits)


# ---------------------------------------------------------------------------
# Cross-TF summary writer
# ---------------------------------------------------------------------------
def write_summary_md(out_dir: Path, tfs: list[str]) -> None:
    """Compose the cross-TF SUMMARY.md from each tf's verdict + sweep_summary.csv."""
    out_dir = Path(out_dir)
    sweep_csv = out_dir / "sweep_summary.csv"
    sweep = pd.read_csv(sweep_csv) if sweep_csv.exists() else pd.DataFrame()

    verdicts = {}
    for tf in tfs:
        v_path = out_dir / f"{tf}_verdict.json"
        if v_path.exists():
            with open(v_path) as fp:
                verdicts[tf] = json.load(fp)

    md = []
    md.append("# ATT Regime Switch — Multi-Timeframe Summary\n")
    md.append(f"_Generated: {pd.Timestamp.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")

    md.append("## 1. Default-params sweep (per timeframe)\n")
    md.append("Each row is one timeframe, default Pine params, no parameter search.\n")
    if not sweep.empty:
        rows = []
        for _, r in sweep.iterrows():
            rows.append([
                r["tf"],
                f"{int(r['n_traded'])}/{int(r['n_files'])}",
                f"{r['pct_profitable']*100:.1f}%",
                f"{r['mean_sharpe']:.3f}",
                f"{r['mean_total_return']*100:.2f}%",
                f"{r['mean_max_dd']:.2f}%",
            ])
        md.append(_md_table(
            ["TF", "N traded", "% profitable", "Mean Sharpe",
             "Mean return", "Mean MaxDD"],
            rows,
        ))
    md.append("")

    md.append("## 2. Walk-forward winners (per timeframe)\n")
    md.append(
        "Each row shows the cross-symbol OOS performance of the winning parameter "
        "set for that timeframe. Filters: OOS Sharpe>0 on ≥60%, MaxDD<35% on ≥70%, "
        "positive OOS return on ≥55%, ≥5 symbols with ≥5 OOS trades.\n"
    )
    if verdicts:
        rows = []
        for tf, v in verdicts.items():
            rows.append([
                tf,
                "— none —" if v.get("winner") is None else f"{v['mean_oos_sharpe']:.3f}",
                "—" if v.get("winner") is None else f"{v['pct_symbols_positive_oos']*100:.1f}%",
                "—" if v.get("winner") is None else f"{v['pct_symbols_low_dd']*100:.1f}%",
                "—" if v.get("winner") is None else f"{v['pct_symbols_positive_return']*100:.1f}%",
                v.get("n_symbols_qualified", 0),
                "PASS" if v.get("passes_filters") else "FAIL",
            ])
        md.append(_md_table(
            ["TF", "Mean OOS Sharpe", "% Pos Sharpe", "% Low DD",
             "% Pos Return", "N qualified", "Filters"],
            rows,
        ))
    md.append("")

    md.append("## 3. Per-timeframe verdict\n")
    for tf, v in verdicts.items():
        md.append(f"### {tf}\n")
        if v.get("winner") is None:
            md.append("- No combo survived the ≥5 OOS trades per symbol filter.\n")
        else:
            md.append(f"- Mean OOS Sharpe: **{v['mean_oos_sharpe']:.3f}**")
            md.append(f"- % symbols positive OOS: **{v['pct_symbols_positive_oos']*100:.1f}%**")
            md.append(f"- % symbols low MaxDD: **{v['pct_symbols_low_dd']*100:.1f}%**")
            md.append(f"- % symbols positive OOS return: **{v['pct_symbols_positive_return']*100:.1f}%**")
            md.append(f"- Symbols qualified: **{v['n_symbols_qualified']}**")
            verdict_text = "PASS" if v["passes_filters"] else "FAIL"
            md.append(f"- Robustness filters: **{verdict_text}**\n")
        # Link to the per-TF report
        report_path = f"{tf}_robustness_report.md"
        md.append(f"  Full report: `{report_path}`\n")

    md.append("## 4. Recommendation\n")
    md.append(_recommendation(tfs, verdicts, sweep))
    md.append("")

    out_path = out_dir / "SUMMARY.md"
    out_path.write_text("\n".join(md), encoding="utf-8")
    log.info("Wrote %s", out_path)


def _recommendation(tfs: list[str], verdicts: dict, sweep: pd.DataFrame) -> str:
    """Pick which timeframe(s) to consider for live trading (if any)."""
    # Score = mean OOS Sharpe if passes filters, else -inf
    ranking = []
    for tf in tfs:
        v = verdicts.get(tf)
        if not v or v.get("winner") is None:
            ranking.append((tf, float("-inf"), "no winner"))
            continue
        score = v["mean_oos_sharpe"] if v["passes_filters"] else float("-inf")
        ranking.append((tf, score, "passes filters" if v["passes_filters"] else "fails filters"))

    ranking.sort(key=lambda x: x[1], reverse=True)
    top = ranking[0] if ranking else None
    any_pass = any(v.get("passes_filters") for v in verdicts.values())

    bits = []
    if not any_pass:
        bits.append(
            "**No timeframe passes all four robustness filters.** "
            "Combined with the very limited statistical power of the intraday "
            "data sets (especially 15m and 30m) and the small weekly dataset "
            "(520 bars), there is **no evidence of a real, tradable edge** on "
            "any timeframe in this study."
        )
        bits.append(
            "**Recommendation: do not trade this strategy on any of these "
            "timeframes until further validation is done.**"
        )
    else:
        bits.append("Timeframes that pass all four filters, ranked by mean OOS Sharpe:\n")
        for tf, score, status in ranking:
            if score == float("-inf"):
                continue
            bits.append(f"* **{tf}**: mean OOS Sharpe = {score:.3f} ({status})")
        bits.append("")
        if top and top[1] != float("-inf"):
            bits.append(
                f"**Top candidate: {top[0]}.**  Caveats:"
            )
            bits.append(_caveats_per_tf(top[0]))
        else:
            bits.append("No timeframe passed the filters — no live trading recommended.")
    bits.append("")
    bits.append(
        "**Caveats across the board:**\n"
        "* yfinance's intraday history is capped (60d for 15m/30m, 730d for "
        "1h). Even the best intraday dataset is too short for robust "
        "inference.\n"
        "* The strategy's regime classifier (ADX + DMI) was designed for "
        "daily bars; sub-daily data introduces more noise without a "
        "proportionate signal increase.\n"
        "* 4h bars are resampled from 1h and include pre/post-market "
        "hours for some symbols, slightly skewing OHLCV.\n"
        "* Slippage is a fixed basis-points model; in real intraday "
        "execution, queue position and partial fills would add further cost."
    )
    return "\n".join(bits)


def _caveats_per_tf(tf: str) -> str:
    if tf == "weekly":
        return ("* Weekly regime-flip speed is reasonable but absolute trade count is low.\n"
                "* 10 years × 52 weeks = 520 bars is comparable to the 1D/4y study's "
                "statistical power — *good* news.\n"
                "* Mean reversion is most pronounced at daily/weekly timeframes; this is "
                "the strongest theoretical case in the multi-TF set.")
    if tf == "1h":
        return ("* 1h has the most intraday data (~13k bars) and the longest OOS slices.\n"
                "* But 1h intraday execution carries higher relative cost (more trades, "
                "more slippage in basis-point terms) than daily.\n"
                "* Before live trading, validate by paper-trading 1h for 3+ months.")
    if tf == "4h":
        return ("* 4h has less data than 1h but more bars per trade.\n"
                "* Resampled 4h bars may include extended-hours noise.")
    if tf in ("15m", "30m"):
        return ("* Statistical power on 15m/30m is too low to draw any "
                "meaningful conclusion. Even a winning combo on these "
                "timeframes would need live-paper validation of several "
                "months before any consideration.")
    return ""


# ---------------------------------------------------------------------------
# CLI runner for one or all TFs
# ---------------------------------------------------------------------------
def run_one_tf(tf: str, *, data_dir: Path, out_dir: Path,
               n_phase1: int = 200, n_phase2: int = 1000,
               n_top_phase2: int = 20,
               n_jobs: int = 3, seed: int = 42) -> dict:
    """Phase 1 → 2 → 3 for one timeframe.  Returns the verdict dict."""
    out_dir = Path(out_dir)
    tf_dir = out_dir  # write everything into results/multi_tf

    cfg = OptimizerConfig(
        csv_dir=str(data_dir),
        out_dir=str(tf_dir),
        n_phase1_combos=n_phase1,
        n_phase2_combos=n_phase2,
        n_top_for_phase2=n_top_phase2,
        n_jobs=n_jobs,
        seed=seed,
        # The walk-forward windowing comes from TF_WINDOWS, not cfg.
        n_windows=TF_WINDOWS[tf].n_windows,
        train_frac=TF_WINDOWS[tf].train_frac,
    )

    symbols = list_tf_csvs(data_dir, tf)
    if not symbols:
        log.error("[%s] no CSVs found in %s", tf, data_dir)
        return {"tf": tf, "winner": None, "passes_filters": False,
                "n_symbols_total": 0}

    # Phase 1
    log.info("=" * 60)
    log.info("[%s] PHASE 1 (n_phase1=%d, n_jobs=%d)", tf, n_phase1, n_jobs)
    log.info("=" * 60)
    p1_t0 = time.time()
    p1_summ, p1_raw = run_phase1_mtf(cfg, symbols, tf)
    write_phase_outputs(tf_dir, 1, p1_summ, p1_raw)
    # Rename to include tf
    _rename_phase_outputs(tf_dir, 1, tf)
    log.info("[%s] Phase 1 done in %.1fs — %d unique combos",
             tf, time.time() - p1_t0, len(p1_summ))
    if not p1_summ:
        verdict = write_tf_robustness_report(
            out_dir=tf_dir, winner=None, per_symbol=[], tf=tf, cfg=cfg,
            phase1_summaries=[], phase2_summaries=[], symbols=symbols,
        )
        return verdict

    # Phase 2
    log.info("=" * 60)
    log.info("[%s] PHASE 2 (n_phase2 cap=%d)", tf, n_phase2)
    log.info("=" * 60)
    p2_t0 = time.time()
    p2_summ, p2_raw = run_phase2_mtf(cfg, symbols, tf, p1_summ)
    write_phase_outputs(tf_dir, 2, p2_summ, p2_raw)
    _rename_phase_outputs(tf_dir, 2, tf)
    log.info("[%s] Phase 2 done in %.1fs — %d unique combos",
             tf, time.time() - p2_t0, len(p2_summ))

    # Phase 3
    log.info("=" * 60)
    log.info("[%s] PHASE 3 — winner selection", tf)
    log.info("=" * 60)
    winner = select_winner(
        p2_summ,
        min_pct_pos_oos=cfg.min_pct_pos_oos,
        min_pct_low_dd=cfg.min_pct_low_dd,
        min_pct_pos_return=cfg.min_pct_pos_return,
    )
    if winner is None:
        log.warning("[%s] No combo passed filters; falling back to top-by-robustness",
                    tf)
        winner = p2_summ[0] if p2_summ else None

    # Per-symbol results for the winner
    per_symbol: list[ComboSymbolResult] = []
    if winner is not None:
        winner_key = tuple(sorted(winner.params.items()))
        for r in p2_raw:
            if tuple(sorted(r.params.items())) == winner_key:
                per_symbol.append(r)
        per_symbol.sort(key=lambda r: r.mean_oos_sharpe, reverse=True)

        # Write a winner.json so the format matches the 1D one
        write_winner(tf_dir, winner, per_symbol, cfg)
        _rename_winner(tf_dir, tf)

    verdict = write_tf_robustness_report(
        out_dir=tf_dir, winner=winner, per_symbol=per_symbol, tf=tf, cfg=cfg,
        phase1_summaries=p1_summ, phase2_summaries=p2_summ, symbols=symbols,
    )
    return verdict


def _rename_phase_outputs(out_dir: Path, phase: int, tf: str) -> None:
    """Rename phase{1,2}_*.{csv,json} → {tf}_phase{1,2}_* to keep TFs distinct."""
    src_results = out_dir / f"phase{phase}_results.csv"
    src_summary = out_dir / f"phase{phase}_summary.csv"
    if src_results.exists():
        dst_results = out_dir / f"{tf}_phase{phase}_results.csv"
        if dst_results.exists():
            dst_results.unlink()
        src_results.rename(dst_results)
    if src_summary.exists():
        dst_summary = out_dir / f"{tf}_phase{phase}_summary.csv"
        if dst_summary.exists():
            dst_summary.unlink()
        src_summary.rename(dst_summary)


def _rename_winner(out_dir: Path, tf: str) -> None:
    src = out_dir / "winner.json"
    if src.exists():
        dst = out_dir / f"{tf}_winner.json"
        if dst.exists():
            dst.unlink()
        src.rename(dst)
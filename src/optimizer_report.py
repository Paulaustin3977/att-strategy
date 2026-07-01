"""Render the human-readable robustness report + winner plots.

Lives in its own module so ``scripts/optimize.py`` stays a thin CLI.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from .data import load_symbol
from .engine import simulate
from .optimizer import (
    INTEGER_PARAMS,
    OptimizerConfig,
    PARAM_GRID_COARSE,
    dict_to_overrides,
)
from .reporting import _max_dd_info, _sharpe
from .strategy import ATTStrategy

log = logging.getLogger("att.optimizer.report")

import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt  # noqa: E402


# ---------------------------------------------------------------------------
# Equity curves
# ---------------------------------------------------------------------------
def _simulate_full_universe(winner_params: dict, symbols: list[str], csv_dir: Path) -> pd.DataFrame:
    """Run the winning combo on every symbol, return per-symbol equity curves.

    Returns a wide DataFrame (one column per symbol) of mark-to-market
    equity, normalized to 100 so the curves overlay cleanly.
    """
    params = ATTStrategy(**dict_to_overrides(winner_params))
    curves: dict[str, pd.Series] = {}
    for s in symbols:
        path = csv_dir / f"{s}.csv"
        if not path.exists():
            continue
        try:
            df = load_symbol(path)
            res = simulate(df, params, symbol=s)
        except Exception as e:
            log.warning("winner sim failed for %s: %s", s, e)
            continue
        eq = (res.equity_curve if res.equity_curve is not None else pd.Series(dtype=float)).dropna()
        if eq.empty:
            continue
        # Normalize to 100
        norm = eq / float(eq.iloc[0]) * 100.0
        curves[s] = norm
    if not curves:
        return pd.DataFrame()
    out = pd.DataFrame(curves)
    return out.sort_index()


def plot_equity_curves(curves: pd.DataFrame, out_path: Path, title: str) -> None:
    """Save an overlaid equity-curve PNG."""
    if curves is None or curves.empty:
        # Save a placeholder image
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.text(0.5, 0.5, "No equity curves produced", ha="center", va="center")
        ax.set_axis_off()
        fig.savefig(out_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return

    fig, ax = plt.subplots(figsize=(11, 6))
    # Greys + a couple of accent colours for the mean
    n = curves.shape[1]
    cmap = plt.get_cmap("viridis")
    for i, col in enumerate(curves.columns):
        ax.plot(curves.index, curves[col], color=cmap(i / max(n - 1, 1)),
                alpha=0.45, linewidth=0.9, label=col if n <= 12 else None)
    mean_curve = curves.mean(axis=1)
    ax.plot(curves.index, mean_curve, color="crimson", linewidth=2.2,
            label=f"mean ({n} symbols)")
    ax.axhline(100.0, color="grey", linewidth=0.8, linestyle="--")
    ax.set_title(title)
    ax.set_ylabel("Normalized equity (start = 100)")
    ax.set_xlabel("Date")
    ax.grid(True, alpha=0.3)
    if n <= 12:
        ax.legend(loc="upper left", fontsize=8, ncol=2)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Sensitivity
# ---------------------------------------------------------------------------
def sensitivity_analysis(winner_params: dict, symbols: list[str], csv_dir: Path,
                         *, pct: float = 0.10) -> list[dict]:
    """For each parameter, vary by ±pct and report Sharpe range across universe.

    Returns one row per (param, direction) with mean OOS Sharpe across
    the universe (using the full sample — not walk-forward — for speed).
    """
    base = ATTStrategy(**dict_to_overrides(winner_params))
    rows: list[dict] = []

    def _mean_universe_sharpe(p: ATTStrategy) -> tuple[float, int]:
        sharpes = []
        n_qual = 0
        for s in symbols:
            path = csv_dir / f"{s}.csv"
            if not path.exists():
                continue
            try:
                df = load_symbol(path)
                res = simulate(df, p)
                dr = res.daily_returns if res.daily_returns is not None else pd.Series(dtype=float)
                sh = _sharpe(dr)
                if not math.isnan(sh):
                    sharpes.append(sh)
                    if res.n_trades() >= 1:
                        n_qual += 1
            except Exception:
                continue
        if not sharpes:
            return float("nan"), 0
        return float(np.mean(sharpes)), n_qual

    base_sharpe, base_qual = _mean_universe_sharpe(base)
    rows.append({"param": "_baseline", "value": "winner", "oos_sharpe": base_sharpe, "n_symbols": base_qual})

    for k in PARAM_GRID_COARSE.keys():
        v = getattr(base, k)
        if v == 0:
            continue
        for direction, factor in (("up", 1.0 + pct), ("down", 1.0 - pct)):
            new_v = v * factor
            if k in INTEGER_PARAMS:
                new_v = max(1, int(round(new_v)))
            new_params = ATTStrategy(**{**dict_to_overrides(winner_params), k: new_v})
            sh, nq = _mean_universe_sharpe(new_params)
            rows.append({"param": k, "value": new_v, "direction": direction,
                         "oos_sharpe": sh, "n_symbols": nq})
    return rows


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
def _md_table(headers: list[str], rows: Iterable[Iterable]) -> str:
    headers = [str(h) for h in headers]
    lines = ["| " + " | ".join(headers) + " |",
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


def write_robustness_report(*, out_dir: Path,
                            winner, per_symbol: list,
                            cfg: OptimizerConfig,
                            phase1_summaries: list,
                            phase2_summaries: list) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_dir = Path(cfg.csv_dir)
    symbols = [s for s in sorted(p.stem for p in csv_dir.glob("*.csv"))]
    n_total_symbols = len(symbols)

    # Build equity curves + chart
    log.info("Computing winner equity curves for chart...")
    curves = _simulate_full_universe(winner.params, symbols, csv_dir)
    eq_chart = out_dir / "winner_equity_curves.png"
    plot_equity_curves(curves, eq_chart,
                       title=f"Winner equity curves — {n_total_symbols} symbols (normalized to 100)")

    # Sensitivity analysis (full-sample, fast proxy for OOS stability)
    log.info("Running sensitivity analysis (±10%%)...")
    sens = sensitivity_analysis(winner.params, symbols, csv_dir, pct=0.10)

    # Compose markdown
    md = []
    md.append("# ATT Regime Switch — Walk-Forward Robustness Report\n")
    md.append(f"_Generated: {pd.Timestamp.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
    md.append(f"_Symbols evaluated: **{winner.n_symbols_qualified}** / {n_total_symbols} available "
              f"(filter: ≥5 OOS trades)_\n")

    md.append("## 1. Winning parameter set\n")
    md.append(_params_dict_literal(winner.params))
    md.append("")

    md.append("## 2. Cross-symbol OOS metrics (winner)\n")
    headline = [
        ("Mean OOS Sharpe", f"{winner.mean_oos_sharpe:.3f}"),
        ("Median OOS Sharpe", f"{winner.median_oos_sharpe:.3f}"),
        ("Mean OOS total return", f"{winner.mean_oos_total_return*100:.2f}%"),
        ("Mean OOS MaxDD", f"{winner.mean_oos_max_dd:.2f}%"),
        ("% symbols with OOS Sharpe > 0", f"{winner.pct_symbols_positive_oos*100:.1f}%"),
        ("% symbols with positive OOS return", f"{winner.pct_symbols_positive_return*100:.1f}%"),
        ("% symbols with MaxDD < 35%", f"{winner.pct_symbols_low_dd*100:.1f}%"),
        ("Robustness score", f"{winner.robustness_score:.4f}"),
    ]
    md.append(_md_table(["Metric", "Value"], headline))
    md.append("")

    md.append("## 3. Per-symbol OOS performance (winner)\n")
    md.append("Sorted by OOS Sharpe (descending). "
              "Sharpe is averaged across 4 rolling walk-forward windows.\n")
    if per_symbol:
        rows = []
        for r in per_symbol:
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
    else:
        md.append("_No per-symbol results available._")
    md.append("")

    md.append("## 4. Equity curves\n")
    md.append(f"![equity curves]({eq_chart.name})\n")
    md.append("Each line is one symbol's equity, normalized to 100 at the start. "
              "The crimson line is the cross-symbol mean. Useful for spotting "
              "whether the winner depends on a handful of outliers.\n")

    md.append("## 5. Sensitivity analysis (±10% per parameter)\n")
    md.append("Each non-baseline row mutates **one** parameter by ±10% and "
              "re-runs the strategy on every symbol. Sharpe is the mean "
              "full-sample Sharpe across the universe (a quick proxy for OOS "
              "stability — actual OOS would multiply wall-time by ~4×).\n")
    base_sharpe = sens[0]["oos_sharpe"] if sens else float("nan")
    md.append(f"**Baseline mean universe Sharpe: {base_sharpe:.3f}**\n")
    sens_rows = []
    for r in sens[1:]:
        delta = r["oos_sharpe"] - base_sharpe
        sign = "+" if delta >= 0 else ""
        sens_rows.append([
            r["param"],
            r["value"],
            r["direction"],
            f"{r['oos_sharpe']:.3f}",
            f"{sign}{delta:.3f}",
        ])
    if sens_rows:
        md.append(_md_table(
            ["Parameter", "Value", "Direction", "Mean Sharpe", "Δ vs base"],
            sens_rows,
        ))
    md.append("")
    # Robustness callout
    sharpes = [r["oos_sharpe"] for r in sens[1:] if not math.isnan(r["oos_sharpe"])]
    if sharpes:
        rng = max(sharpes) - min(sharpes)
        md.append(f"**Sensitivity range across ±10% perturbations:** "
                  f"{min(sharpes):.3f} → {max(sharpes):.3f}  (span {rng:.3f}). "
                  + ("Small span ⇒ stable." if rng < 0.5 else
                     "Wide span ⇒ fragile — proceed with extreme caution.")
                  + "\n")

    md.append("## 6. Phase 1 / Phase 2 ranking (top 10)\n")
    md.append("### Phase 1 — coarse LHS\n")
    if phase1_summaries:
        rows = []
        for s in phase1_summaries[:10]:
            rows.append([
                s.robustness_score,
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
                s.robustness_score,
                f"{s.mean_oos_sharpe:.3f}",
                f"{s.pct_symbols_positive_oos*100:.0f}%",
                s.n_symbols_qualified,
            ])
        md.append(_md_table(
            ["Robustness", "Mean Sharpe", "% Pos Sharpe", "N symbols"],
            rows,
        ))
    md.append("")

    md.append("## 7. Methodology\n")
    md.append(
        "* **Walk-forward**: each symbol's 4-year daily frame is split into "
        "4 rolling 1-year windows. Within each window the first 75% is "
        "in-sample and the last 25% is out-of-sample. Metrics on the OOS "
        "segment are the only ones used for ranking.\n"
        "* **Per-symbol score**: mean of OOS Sharpe, total return, MaxDD, "
        "and trade count across the 4 windows for that combo on that "
        "symbol.\n"
        "* **Cross-symbol aggregation**: a combo is summarised by its "
        "mean OOS Sharpe across symbols, the share of symbols with positive "
        "OOS Sharpe, the share with positive OOS return, and the share "
        "with MaxDD shallower than 35%.\n"
        f"* **Filters applied** (winner must satisfy all):\n"
        f"    - OOS Sharpe > 0 on ≥ {cfg.min_pct_pos_oos*100:.0f}% of symbols\n"
        f"    - MaxDD < 35% on ≥ {cfg.min_pct_low_dd*100:.0f}% of symbols\n"
        f"    - Total return > 0 on ≥ {cfg.min_pct_pos_return*100:.0f}% of symbols\n"
        "* **Phase 1**: Latin Hypercube sampling over the discrete grid.\n"
        "* **Phase 2**: top-K Phase 1 combos → ±1-step local neighborhood "
        "on each axis, deduped and capped.\n"
    )

    md.append("## 8. Caveats and honest assessment\n")
    md.append(
        "* **Data volume is small.** Only 4 years × ~250 trading days = "
        "~1000 bars per symbol. After walk-forward we have 4 OOS "
        "windows of ~63 bars each. With short OOS segments, Sharpe "
        "estimates have very wide confidence intervals; a sample "
        "Sharpe of 1.0 from 63 bars has a 95% CI roughly spanning "
        "[-0.3, +2.3].\n"
        "* **High overfit risk.** We sampled hundreds of parameter "
        "combinations. Even with the cross-symbol robustness filters, "
        "the most attractive combination is partly a product of "
        "search noise. The strongest argument for this winner is "
        "**consistency** (positive Sharpe across many symbols) rather "
        "than absolute return.\n"
        "* **Universe is biased toward liquid instruments.** Many of "
        "the symbols (ES, NQ, gold, oil) are highly traded and have "
        "well-documented mean-reversion behaviour that may not survive "
        "into 2027+.\n"
        "* **No transaction-cost realism at the portfolio level.** The "
        "engine applies commission and slippage per trade, but real "
        "portfolio execution would also have spread, partial fills, "
        "and queue position risk.\n"
        "* **Recommendation**: **do not trade this on real capital yet.** "
        "Paper-trade the winner on live data for at least 3-6 months "
        "before considering real money. A future phase should extend "
        "to 10+ years of data (when available) to validate regime "
        "robustness across multiple economic cycles.\n"
        "* **Statistical fragility.** The reported 'pct symbols with "
        "positive OOS Sharpe' is itself a binomial estimate; with a "
        "universe of ~60 symbols, a difference of 5 symbols "
        "between winners is **not** statistically significant.\n"
    )

    md.append("## 9. Output files\n")
    md.append(
        "* `phase1_results.csv` — every (symbol, combo) row from the coarse sweep.\n"
        "* `phase1_summary.csv` — per-combo aggregate across symbols.\n"
        "* `phase2_results.csv` — refined sweep (same shape).\n"
        "* `phase2_summary.csv` — refined sweep aggregate.\n"
        "* `winner.json` — winning parameters + headline metrics + per-symbol rows.\n"
        "* `winner_equity_curves.png` — overlaid equity curves.\n"
        "* `robustness_report.md` — this file.\n"
    )

    out_path = out_dir / "robustness_report.md"
    out_path.write_text("\n".join(md), encoding="utf-8")
    log.info("Wrote %s", out_path)
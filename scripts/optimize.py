#!/usr/bin/env python3
"""CLI: walk-forward parameter optimizer for ATT Regime Switch.

Run modes:
    python scripts/optimize.py --phase 1        # coarse LHS sweep
    python scripts/optimize.py --phase 2        # refined search (needs phase 1)
    python scripts/optimize.py --phase 3        # winner pick + report (needs phase 2)
    python scripts/optimize.py --phase all      # run 1 → 2 → 3 in sequence

Outputs land in ``results/optimization/``.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.optimizer import (  # noqa: E402
    OptimizerConfig,
    PARAM_GRID_COARSE,
    list_symbol_csvs,
    run_phase1,
    run_phase2,
    select_winner,
    summarize_combo,
    aggregate_combos,
    write_phase_outputs,
    write_winner,
    render_winner_params_python,
)
from src.data import sanitise  # noqa: E402


def _parse_symbols(s: str | None) -> list[str] | None:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward ATT optimizer")
    parser.add_argument("--phase", default="all",
                        choices=["1", "2", "3", "all"],
                        help="Which phase(s) to run (default: all)")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--out-dir", default=str(ROOT / "results" / "optimization"))
    parser.add_argument("--n-jobs", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n-phase1", type=int, default=300,
                        help="Number of Phase 1 LHS samples (default 300).")
    parser.add_argument("--n-phase2", type=int, default=3000,
                        help="Cap on Phase 2 refined combos (default 3000).")
    parser.add_argument("--n-top-phase2", type=int, default=30,
                        help="Top-K Phase 1 combos to seed Phase 2 (default 30).")
    parser.add_argument("--n-windows", type=int, default=4)
    parser.add_argument("--train-frac", type=float, default=0.75)
    parser.add_argument("--symbols", default="",
                        help="Optional comma-separated yfinance tickers")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("att.optimize")

    cfg = OptimizerConfig(
        csv_dir=args.data_dir,
        out_dir=args.out_dir,
        n_phase1_combos=args.n_phase1,
        n_phase2_combos=args.n_phase2,
        n_top_for_phase2=args.n_top_phase2,
        n_jobs=args.n_jobs,
        seed=args.seed,
        n_windows=args.n_windows,
        train_frac=args.train_frac,
    )

    symbols = list_symbol_csvs(Path(cfg.csv_dir), _parse_symbols(args.symbols))
    log.info("Universe: %d symbols with CSVs in %s", len(symbols), cfg.csv_dir)
    if not symbols:
        log.error("No CSV files in %s; run scripts/fetch_data.py first.", cfg.csv_dir)
        return 2

    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    overall_t0 = time.time()
    phase1_summaries: list = []
    phase1_raw: list = []
    phase2_summaries: list = []
    phase2_raw: list = []

    if args.phase in ("1", "all"):
        log.info("=" * 60)
        log.info("PHASE 1: coarse LHS over %d-dim grid", len(PARAM_GRID_COARSE))
        log.info("=" * 60)
        t0 = time.time()
        phase1_summaries, phase1_raw = run_phase1(cfg, symbols)
        write_phase_outputs(out_dir, 1, phase1_summaries, phase1_raw)
        log.info("Phase 1 done in %.1fs — %d combos evaluated, %d unique combos survived ≥5-trade filter.",
                 time.time() - t0, len(phase1_summaries), len(phase1_summaries))
        if not phase1_summaries:
            log.error("Phase 1 produced no surviving combos.")
            return 1

    if args.phase in ("2", "all"):
        if not phase1_summaries:
            # Reload from disk if phase 1 wasn't run this session
            p1 = out_dir / "phase1_summary.csv"
            if p1.exists():
                log.info("Reloading Phase 1 summaries from %s", p1)
                phase1_summaries = _reload_phase1_summaries(p1)
            else:
                log.error("Phase 1 summaries not found; run with --phase 1 first.")
                return 1
        log.info("=" * 60)
        log.info("PHASE 2: refined ±1-step search around top-%d Phase 1 combos",
                 cfg.n_top_for_phase2)
        log.info("=" * 60)
        t0 = time.time()
        phase2_summaries, phase2_raw = run_phase2(cfg, symbols, phase1_summaries)
        write_phase_outputs(out_dir, 2, phase2_summaries, phase2_raw)
        log.info("Phase 2 done in %.1fs — %d combos survived ≥5-trade filter.",
                 time.time() - t0, len(phase2_summaries))

    if args.phase in ("3", "all"):
        if not phase2_summaries:
            p2 = out_dir / "phase2_summary.csv"
            if p2.exists():
                log.info("Reloading Phase 2 summaries from %s", p2)
                phase2_summaries = _reload_phase1_summaries(p2)  # same schema
            else:
                log.error("Phase 2 summaries not found; run with --phase 2 first.")
                return 1
        if not phase2_raw:
            p2r = out_dir / "phase2_results.csv"
            if p2r.exists():
                phase2_raw = _reload_phase2_results(p2r)
        log.info("=" * 60)
        log.info("PHASE 3: pick winner and write robustness report")
        log.info("=" * 60)
        winner = select_winner(
            phase2_summaries,
            min_pct_pos_oos=cfg.min_pct_pos_oos,
            min_pct_low_dd=cfg.min_pct_low_dd,
            min_pct_pos_return=cfg.min_pct_pos_return,
        )
        if winner is None:
            log.warning("No combo passed robustness filters.  Falling back to top-by-robustness winner.")
            if not phase2_summaries:
                log.error("No Phase 2 summaries at all.")
                return 1
            winner = phase2_summaries[0]

        # Per-symbol results for the winner
        winner_key = tuple(sorted(winner.params.items()))
        per_symbol = []
        for r in phase2_raw:
            if tuple(sorted(r.params.items())) == winner_key:
                per_symbol.append(r)
        per_symbol.sort(key=lambda r: r.mean_oos_sharpe, reverse=True)

        write_winner(out_dir, winner, per_symbol, cfg)

        # Build the human-readable report (separate module for clarity)
        from src.optimizer_report import write_robustness_report  # local import
        write_robustness_report(
            out_dir=out_dir,
            winner=winner,
            per_symbol=per_symbol,
            cfg=cfg,
            phase1_summaries=phase1_summaries,
            phase2_summaries=phase2_summaries,
        )

        log.info("Winner: %s", render_winner_params_python(winner.params))
        log.info("Mean OOS Sharpe across universe: %.3f", winner.mean_oos_sharpe)
        log.info("%% symbols with positive OOS Sharpe: %.1f%%",
                 winner.pct_symbols_positive_oos * 100)
        log.info("%% symbols with MaxDD < 35%%: %.1f%%",
                 winner.pct_symbols_low_dd * 100)
        log.info("%% symbols with positive OOS return: %.1f%%",
                 winner.pct_symbols_positive_return * 100)
        log.info("Symbols evaluated: %d", winner.n_symbols_qualified)

    log.info("Total wall-time: %.1fs", time.time() - overall_t0)
    return 0


def _reload_phase1_summaries(csv_path: Path) -> list:
    """Load Phase 1/2 summaries CSV back into ComboSummary objects."""
    from src.optimizer import ComboSummary
    import pandas as pd
    df = pd.read_csv(csv_path)
    out = []
    for _, row in df.iterrows():
        params = {k: row[k] for k in PARAM_GRID_COARSE.keys() if k in row}
        out.append(ComboSummary(
            params=params,
            n_symbols_evaluated=int(row.get("n_symbols_evaluated", 0)),
            n_symbols_qualified=int(row.get("n_symbols_qualified", 0)),
            mean_oos_sharpe=float(row.get("mean_oos_sharpe", 0.0)),
            median_oos_sharpe=float(row.get("median_oos_sharpe", 0.0)),
            mean_oos_total_return=float(row.get("mean_oos_total_return", 0.0)),
            mean_oos_max_dd=float(row.get("mean_oos_max_dd_pct", 0.0)),
            pct_symbols_positive_oos=float(row.get("pct_symbols_positive_oos", 0.0)),
            pct_symbols_positive_return=float(row.get("pct_symbols_positive_return", 0.0)),
            pct_symbols_low_dd=float(row.get("pct_symbols_low_dd", 0.0)),
            robustness_score=float(row.get("robustness_score", 0.0)),
        ))
    return out


def _reload_phase2_results(csv_path: Path) -> list:
    """Load Phase 2 raw results CSV back into ComboSymbolResult objects."""
    from src.optimizer import ComboSymbolResult
    import pandas as pd
    df = pd.read_csv(csv_path)
    out = []
    for _, row in df.iterrows():
        params = {k: row[k] for k in PARAM_GRID_COARSE.keys() if k in row}
        out.append(ComboSymbolResult(
            symbol=str(row.get("symbol", "")),
            params=params,
            n_windows=int(row.get("n_windows", 0)),
            mean_oos_sharpe=float(row.get("mean_oos_sharpe", 0.0)),
            mean_oos_total_return=float(row.get("mean_oos_total_return", 0.0)),
            mean_oos_max_dd=float(row.get("mean_oos_max_dd_pct", 0.0)),
            sum_oos_n_trades=int(row.get("sum_oos_n_trades", 0)),
            pct_oos_positive=float(row.get("pct_oos_positive", 0.0)),
            robustness_score=float(row.get("robustness_score", 0.0)),
        ))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
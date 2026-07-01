#!/usr/bin/env python3
"""CLI wrapper around ``src.optimizer_mtf``.

Usage:

::

    # Process one timeframe
    python scripts/optimize_multi_tf.py --tf 4h \\
        --phase1 200 --phase2 1000 --n-top 20 --n-jobs 3

    # Process every timeframe (default order: 15m, 30m, 1h, 4h, weekly)
    python scripts/optimize_multi_tf.py --all --n-jobs 3

    # Phase 1+2+3 pipeline is per-TF.  At the end this also writes
    # results/multi_tf/SUMMARY.md (cross-timeframe comparison).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Allow running as a script (project-relative imports).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.optimizer_mtf import run_one_tf, write_summary_md  # noqa: E402


DEFAULT_TFS = ["15m", "30m", "1h", "4h", "weekly"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--tf", choices=DEFAULT_TFS,
                   help="Single timeframe to process.")
    g.add_argument("--all", action="store_true",
                   help="Process every timeframe in order.")
    parser.add_argument("--data-dir", default="data",
                        help="Directory containing the <stem>_<tf>.csv files.")
    parser.add_argument("--out-dir", default="results/multi_tf",
                        help="Output directory (defaults to results/multi_tf).")
    parser.add_argument("--phase1", type=int, default=200,
                        help="Number of Phase-1 (LHS) combos.")
    parser.add_argument("--phase2", type=int, default=1000,
                        help="Phase-2 combo cap.")
    parser.add_argument("--n-top", type=int, default=20,
                        help="Top-K from Phase 1 to seed into Phase 2.")
    parser.add_argument("--n-jobs", type=int, default=3,
                        help="Number of parallel workers.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--skip-summary", action="store_true",
                        help="Don't regenerate the cross-TF SUMMARY.md at end.")
    parser.add_argument("--quiet", action="store_true",
                        help="Set log level to WARNING (default INFO).")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger("att.optimize_mtf_cli")

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tfs = DEFAULT_TFS if args.all else [args.tf]

    verdicts = []
    for tf in tfs:
        log.info("=" * 70)
        log.info("Starting TF: %s", tf)
        log.info("=" * 70)
        t0 = time.time()
        verdict = run_one_tf(
            tf,
            data_dir=data_dir,
            out_dir=out_dir,
            n_phase1=args.phase1,
            n_phase2=args.phase2,
            n_top_phase2=args.n_top,
            n_jobs=args.n_jobs,
            seed=args.seed,
        )
        dt = time.time() - t0
        log.info("[%s] DONE in %.1fs — winner %s, "
                 "mean_oos_sharpe=%s, passes_filters=%s",
                 tf, dt,
                 "OK" if verdict.get("winner") else "NONE",
                 verdict.get("mean_oos_sharpe", "—"),
                 verdict.get("passes_filters", False))
        verdicts.append((tf, verdict))

    if not args.skip_summary and len(tfs) > 1:
        write_summary_md(out_dir, tfs)
        log.info("Wrote %s/SUMMARY.md", out_dir)

    # Print a tiny summary table to stdout (CI-friendly)
    print()
    print(f"{'TF':<8} {'Win?':<6} {'OOS Sharpe':<12} {'%PosOOS':<8} {'%LowDD':<8} {'Filters':<8}")
    for tf, v in verdicts:
        if v.get("winner") is None:
            print(f"{tf:<8} {'—':<6} {'—':<12} {'—':<8} {'—':<8} {'FAIL':<8}")
        else:
            print(f"{tf:<8} {'OK':<6} {v['mean_oos_sharpe']:<12.3f} "
                  f"{v['pct_symbols_positive_oos']*100:<8.1f} "
                  f"{v['pct_symbols_low_dd']*100:<8.1f} "
                  f"{'PASS' if v['passes_filters'] else 'FAIL':<8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

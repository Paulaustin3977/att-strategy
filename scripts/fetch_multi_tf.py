#!/usr/bin/env python3
"""Multi-timeframe yfinance fetcher.

Downloads maximum-lookback OHLCV for every symbol in the universe across
five timeframes:

* ``15m``    — yfinance caps at 60 days  → ~1500 bars
* ``30m``    — yfinance caps at 60 days  → ~750 bars
* ``1h``     — 730 days                  → ~4800 bars
* ``4h``     — derived from 1h by resampling (~1200 bars)
* ``weekly`` — 10y                       → ~520 bars

Outputs land in ``data/<sanitised>_<tf>.csv`` (e.g. ``GC_F_15m.csv``).
The existing 1D files (``data/<sanitised>.csv``) are NOT touched.

Sanity-check thresholds:

* 15m:    >= 200 bars
* 30m:    >= 100 bars
* 1h:     >= 200 bars
* 4h:     >= 100 bars
* weekly: >= 100 bars

Below threshold → warn and skip (do not raise).

Usage:
    python scripts/fetch_multi_tf.py                  # all symbols, all TFs
    python scripts/fetch_multi_tf.py --tfs 1h weekly  # only some TFs
    python scripts/fetch_multi_tf.py GC=F ES=F        # only some symbols
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data import sanitise  # noqa: E402
from src.universe import all_symbols  # noqa: E402

log = logging.getLogger("att.fetch_mtf")


# ---------------------------------------------------------------------------
# Per-timeframe config
# ---------------------------------------------------------------------------
@dataclass
class TFConfig:
    label: str           # suffix for the CSV file
    yf_interval: str     # yfinance interval argument
    yf_period: str       # yfinance period argument
    min_bars: int        # sanity threshold
    aggregate_first: bool = False  # if True, build from a different interval


TF_CONFIGS: dict[str, TFConfig] = {
    "15m":    TFConfig(label="15m",    yf_interval="15m",  yf_period="60d",  min_bars=200),
    "30m":    TFConfig(label="30m",    yf_interval="30m",  yf_period="60d",  min_bars=100),
    "1h":     TFConfig(label="1h",     yf_interval="1h",   yf_period="730d", min_bars=200),
    # 4h is built from 1h, not directly from yfinance.
    "4h":     TFConfig(label="4h",     yf_interval="1h",   yf_period="730d", min_bars=100),
    "weekly": TFConfig(label="weekly", yf_interval="1wk",  yf_period="10y",  min_bars=100),
}


# ---------------------------------------------------------------------------
# Fetch + normalise
# ---------------------------------------------------------------------------
def _normalize_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """Flatten yfinance's multi-index output and return a tidy OHLCV frame."""
    if df is None or len(df) == 0:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        level0 = df.columns.get_level_values(0)
        level1 = df.columns.get_level_values(1)
        if symbol in level0:
            df = df.xs(symbol, axis=1, level=0)
        elif symbol in level1:
            df = df.xs(symbol, axis=1, level=1)
        else:
            try:
                df.columns = level0
            except Exception:
                return None

    wanted = {"Open", "High", "Low", "Close", "Volume"}
    present = {str(c) for c in df.columns}
    if not wanted.issubset(present):
        return None

    out = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    out.columns = ["open", "high", "low", "close", "volume"]

    if not isinstance(out.index, pd.DatetimeIndex):
        try:
            out.index = pd.to_datetime(out.index)
        except Exception:
            return None

    # Drop tz if present (keep naive, matching the 1D pipeline)
    try:
        if getattr(out.index, "tz", None) is not None:
            out.index = out.index.tz_localize(None)
    except Exception:
        pass

    out = out.reset_index()
    out = out.rename(columns={out.columns[0]: "date"})
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    return out


def _fetch_raw(symbol: str, *, interval: str, period: str,
               retries: int = 2) -> pd.DataFrame | None:
    """One-shot yfinance download with linear backoff. Returns None on failure."""
    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            df = yf.download(
                symbol,
                period=period,
                interval=interval,
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            return _normalize_ohlcv(df, symbol)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            sleep_for = 2.0 * (attempt + 1)
            log.warning("%s %s attempt %d/%d failed (%s); retrying in %.1fs",
                        symbol, interval, attempt + 1, retries + 1, last_err, sleep_for)
            time.sleep(sleep_for)
    log.error("%s %s failed: %s", symbol, interval, last_err)
    return None


def _resample_to_4h(hourly_df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1h OHLCV into 4H bars.

    Aggregation: open=first, high=max, low=min, close=last, volume=sum.
    Requires a sorted, indexed-by-date frame.
    """
    df = hourly_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    out = df.resample("4H").agg(agg)
    out = out.dropna(subset=["close"]).reset_index()
    return out


# ---------------------------------------------------------------------------
# Per-symbol × per-timeframe fetch
# ---------------------------------------------------------------------------
@dataclass
class FetchSummary:
    symbol: str
    tf: str
    csv_path: str
    n_bars: int
    ok: bool
    error: str | None = None


def fetch_symbol_tf(symbol: str, tf: str, data_dir: Path,
                    retries: int = 2) -> FetchSummary:
    """Download one (symbol, timeframe) pair and persist to CSV.

    Filename: ``<sanitised>_<tf>.csv``.
    """
    cfg = TF_CONFIGS[tf]
    sanitised = sanitise(symbol)
    csv_path = data_dir / f"{sanitised}_{cfg.label}.csv"

    # 1h serves double duty: it is the source for 4h.
    if tf == "4h":
        hourly = _fetch_raw(symbol, interval=cfg.yf_interval,
                            period=cfg.yf_period, retries=retries)
        if hourly is None or len(hourly) == 0:
            return FetchSummary(symbol, tf, str(csv_path), 0, False,
                                error="1h source empty")
        try:
            bars = _resample_to_4h(hourly)
        except Exception as e:
            return FetchSummary(symbol, tf, str(csv_path), 0, False,
                                error=f"resample fail: {e}")
    else:
        bars = _fetch_raw(symbol, interval=cfg.yf_interval,
                          period=cfg.yf_period, retries=retries)
        if bars is None or len(bars) == 0:
            return FetchSummary(symbol, tf, str(csv_path), 0, False,
                                error="empty/normalized to empty")

    n = len(bars)
    if n < cfg.min_bars:
        return FetchSummary(symbol, tf, str(csv_path), n, False,
                            error=f"insufficient bars ({n} < {cfg.min_bars})")

    bars = bars.copy()
    bars["tf"] = cfg.label
    bars.to_csv(csv_path, index=False)
    return FetchSummary(symbol, tf, str(csv_path), n, True)


def fetch_all_mtf(symbols: Iterable[str], data_dir: Path,
                  tfs: list[str] | None = None,
                  *, retries: int = 2) -> list[FetchSummary]:
    """Sequentially fetch every (symbol, tf).  Sequential to avoid hammering yfinance."""
    symbols = list(symbols)
    tfs = tfs or list(TF_CONFIGS.keys())
    out: list[FetchSummary] = []

    total = len(symbols) * len(tfs)
    n_done = 0
    for sym in symbols:
        for tf in tfs:
            n_done += 1
            log.info("[%d/%d] %-12s %-6s ...", n_done, total, sym, tf)
            r = fetch_symbol_tf(sym, tf, data_dir, retries=retries)
            if r.ok:
                log.info("    ok (%d bars) -> %s", r.n_bars, Path(r.csv_path).name)
            else:
                log.warning("    SKIP: %s", r.error)
            out.append(r)
            # Tiny breather between yfinance calls
            time.sleep(0.2)

    ok = sum(r.ok for r in out)
    log.info("Done: %d/%d (symbol,tf) pairs fetched.", ok, len(out))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch multi-timeframe OHLCV.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--symbols", nargs="*",
                        help="Optional explicit yfinance ticker list.")
    parser.add_argument("--tfs", nargs="*", default=list(TF_CONFIGS.keys()),
                        choices=list(TF_CONFIGS.keys()),
                        help="Timeframes to fetch (default: all).")
    parser.add_argument("--retries", type=int, default=2,
                        help="Retries per (symbol,tf) (default 2 → 3 attempts).")
    parser.add_argument("--report", default=None,
                        help="Optional path to write a per-pair status report (JSON).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    symbols = args.symbols if args.symbols else all_symbols()
    log.info("Fetching %d symbols × %d timeframes = %d pairs",
             len(symbols), len(args.tfs), len(symbols) * len(args.tfs))

    results = fetch_all_mtf(symbols, data_dir, args.tfs, retries=args.retries)

    # Headline counts
    by_tf: dict[str, list[FetchSummary]] = {tf: [] for tf in args.tfs}
    for r in results:
        if r.tf in by_tf:
            by_tf[r.tf].append(r)

    print()
    print("=" * 60)
    print("Multi-timeframe fetch summary")
    print("=" * 60)
    for tf in args.tfs:
        rows = by_tf[tf]
        ok = sum(r.ok for r in rows)
        print(f"  {tf:<7s} ok={ok:>3d}/{len(rows)}  "
              f"min_bars={TF_CONFIGS[tf].min_bars}")

    bad = [r for r in results if not r.ok]
    if bad:
        print()
        print("Failures (logged):")
        for r in bad[:50]:
            print(f"  {r.symbol:<12s} {r.tf:<7s} -> {r.error}")
        if len(bad) > 50:
            print(f"  ... and {len(bad) - 50} more")

    if args.report:
        import json
        with open(args.report, "w", encoding="utf-8") as fp:
            json.dump([r.__dict__ for r in results], fp, indent=2)
        log.info("Wrote fetch report to %s", args.report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
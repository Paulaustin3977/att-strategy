"""Data pipeline — fetch OHLCV via yfinance and persist to CSV.

Rules:
* Period ``4y``, interval ``1d``, ``auto_adjust=True``.
* Sanitised filenames (`^` -> `_`, `=` -> `_`, `/` -> `_`).
* Linear backoff retries (2 attempts total).
* Minimum 750 bars per symbol; below that -> skip + log warning.
* Provides ``load_all`` to read everything back.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import yfinance as yf

log = logging.getLogger("att.data")


def sanitise(symbol: str) -> str:
    """Sanitise a yfinance ticker for use as a filename.

    Examples:
        ``^GSPC`` -> ``_GSPC``
        ``EURUSD=X`` -> ``EURUSD_X``
        ``BRENT/USD`` -> ``BRENT_USD``
    """
    s = symbol.strip()
    s = s.replace("^", "_")
    s = s.replace("=", "_")
    s = s.replace("/", "_")
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s)
    return s


def desanitise(sanitised: str) -> str:
    """Inverse of ``sanitise`` — known patterns only.  Best-effort.

    Used by ``load_all`` to recover the original ticker from the filename.
    Because the reverse mapping is ambiguous (an underscore could come from
    ``=``, ``/``, ``^``, or a non-alphanum), we keep a JSON-style sidecar
    mapping in practice. For our purposes, callers pass the *original*
    ticker through ``load_all`` themselves.
    """
    # Try common patterns — yfinance tickers are well defined.
    return sanitised  # callers carry the original symbol anyway


@dataclass
class FetchResult:
    symbol: str
    csv_path: str
    n_bars: int
    ok: bool
    error: str | None = None


def _normalize_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """Normalise yfinance's output to a tidy OHLCV frame.

    yfinance frequently returns multi-level columns when the request
    includes a single symbol (auto-back to single if multi-index).  We
    flatten them, keep `Close`, `High`, `Low`, `Open`, `Volume`, and write a
    `date` column.

    Returns ``None`` if the frame has zero rows.
    """
    if df is None or len(df) == 0:
        return None

    # If MultiIndex columns, take the symbol's slice
    if isinstance(df.columns, pd.MultiIndex):
        # Reduce to rows for this symbol, but only if the top-level Field
        # names appear in the second level.  yfinance puts (Field, Symbol)
        # into the columns; sometimes Symbol on level 0 and Field on level 1.
        level0 = df.columns.get_level_values(0)
        level1 = df.columns.get_level_values(1)
        if symbol in level0:
            df = df.xs(symbol, axis=1, level=0)
        elif symbol in level1:
            df = df.xs(symbol, axis=1, level=1)
        else:
            # Generic — fall back to flat first level
            try:
                df.columns = level0
            except Exception:
                return None

    # Now columns should be strings like 'Open', 'High', ...
    wanted = {"Open", "High", "Low", "Close", "Volume"}
    present = {str(c) for c in df.columns}
    if not wanted.issubset(present):
        return None

    out = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    out.columns = ["open", "high", "low", "close", "volume"]

    # Index may be a tz-aware DatetimeIndex; convert to date-only
    if not isinstance(out.index, pd.DatetimeIndex):
        try:
            out.index = pd.to_datetime(out.index)
        except Exception:
            return None
    out = out.reset_index()
    out = out.rename(columns={out.columns[0]: "date"})
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)

    # Drop rows where close is NaN
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    return out


def fetch_symbol(symbol: str, data_dir: str | Path, *, retries: int = 2,
                 period: str = "4y", min_bars: int = 750) -> FetchResult:
    """Fetch one symbol via yfinance and persist to CSV.

    Args:
        symbol: yfinance ticker (e.g. ``GC=F``).
        data_dir: directory in which to place ``<sanitised>.csv``.
        retries: number of attempts after the first failure (so total attempts
            = ``retries + 1``).
        period: yfinance period string (default ``"4y"``).
        min_bars: minimum number of rows to accept; below this the symbol is
            skipped.

    Returns:
        :class:`FetchResult`.  ``ok`` is True iff the CSV was written and
        the row count met the threshold.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / f"{sanitise(symbol)}.csv"

    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            df = yf.download(
                symbol,
                period=period,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            tidy = _normalize_ohlcv(df, symbol)
            if tidy is None or len(tidy) == 0:
                last_err = "empty/normalized to empty"
            elif len(tidy) < min_bars:
                last_err = f"insufficient bars ({len(tidy)} < {min_bars})"
            else:
                tidy.to_csv(csv_path, index=False)
                return FetchResult(
                    symbol=symbol,
                    csv_path=str(csv_path),
                    n_bars=len(tidy),
                    ok=True,
                )
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        # Linear backoff
        if attempt < retries:
            sleep_for = 2.0 * (attempt + 1)
            log.warning(
                "fetch %s attempt %d/%d failed (%s); retrying in %.1fs",
                symbol, attempt + 1, retries + 1, last_err, sleep_for,
            )
            time.sleep(sleep_for)

    # Failed: do not write anything; report error
    return FetchResult(
        symbol=symbol,
        csv_path="",
        n_bars=0,
        ok=False,
        error=last_err or "unknown",
    )


def fetch_all(symbols: Iterable[str], data_dir: str | Path,
              *, log_failures: bool = True, **kwargs) -> list[FetchResult]:
    """Fetch every symbol in ``symbols``.  Best-effort.

    Logs warnings on individual failures but does not raise.
    """
    data_dir = Path(data_dir)
    out: list[FetchResult] = []
    for i, sym in enumerate(symbols, 1):
        log.info("[%d] fetch %s", i, sym)
        r = fetch_symbol(sym, data_dir, **kwargs)
        out.append(r)
        if r.ok:
            log.info("    ok (%d bars) -> %s", r.n_bars, r.csv_path)
        else:
            log.warning("    FAIL: %s", r.error)
    ok = sum(r.ok for r in out)
    log.info("Done: %d/%d symbols fetched.", ok, len(out))
    return out


def load_symbol(csv_path: str | Path) -> pd.DataFrame:
    """Read a single CSV back as a tidy OHLCV DataFrame indexed by date."""
    csv_path = Path(csv_path)
    df = pd.read_csv(csv_path, parse_dates=["date"])
    df = df.set_index("date").sort_index()
    return df


def load_all(data_dir: str | Path) -> dict[str, pd.DataFrame]:
    """Load every CSV in ``data_dir`` keyed by sanitised filename stem.

    Returns dict mapping sanitised-stem -> DataFrame (close-to-engine ready).
    Callers that need the *original* ticker should keep their own mapping
    externally; by convention we name files by ``sanitise(symbol)``.
    """
    data_dir = Path(data_dir)
    out: dict[str, pd.DataFrame] = {}
    if not data_dir.exists():
        return out
    for csv in sorted(data_dir.glob("*.csv")):
        key = csv.stem
        try:
            out[key] = load_symbol(csv)
        except Exception as e:
            log.warning("skip %s: %s", csv, e)
    return out


def nuke(data_dir: str | Path) -> None:
    """Remove every CSV in ``data_dir``.  Used to do a clean re-fetch."""
    data_dir = Path(data_dir)
    if data_dir.exists():
        for csv in data_dir.glob("*.csv"):
            csv.unlink()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Smoke test: fetch a single symbol
    r = fetch_symbol("GC=F", "data")
    print(r)

"""Technical indicators implemented as pure vectorised pandas/numpy.

All implementations match TradingView / Pine Script semantics exactly:

* `atr`            -> `ta.atr(length)`  (Wilder smoothing, RMA via ewm alpha=1/length)
* `dmi`            -> `ta.dmi(length)`  (DI+, DI-, ADX)
* `supertrend`     -> `ta.supertrend(factor, atrPeriod)`
* `bbw_pct`        -> `ta.percentrank(..., lookback)`
* `rsi`            -> `ta.rsi(length)`  (Wilder)
* `ema`            -> `ta.ema(length)`
* `sma`            -> `ta.sma(length)`
* `slope`          -> percent change over lookback bars

Pine-to-Python convention notes (READ ME):
* Pine `[i]` indexing is equivalent to `series.shift(i)` in pandas.
  I.e. `adx[1]` in Pine = `adx.shift(1)` in pandas.
* `ta.valuewhen` / `var` persistent state is intentionally NOT implemented in the
  indicators layer — those are bar-by-bar pieces of mutable state that belong
  in the engine (`src/engine.py`).
* `nz(x)` in Pine replaces NaN with the last non-NaN value when used inside
  `ta.valuewhen`, but `nz(x)` alone in Pine is "treat NaN as 0". The engine
  uses Python's `math.nan` semantics explicitly and handles `nz` there.

Wilder smoothing is implemented as `ewm(alpha=1/length, adjust=False).mean()`
which is the canonical representation of RMA ("Wilder's smoothing").
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# ATR — Wilder / RMA
# ---------------------------------------------------------------------------
def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Wilder's ATR. Expects df with columns ``high``, ``low``, ``close``.

    Returns a Series aligned to ``df.index``.  Matches Pine `ta.atr(length)`.
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder smoothing == RMA == pandas ewm(alpha=1/length, adjust=False)
    out = tr.ewm(alpha=1.0 / length, adjust=False).mean()
    out.name = f"atr_{length}"
    return out


# ---------------------------------------------------------------------------
# DMI — DI+, DI-, ADX (Wilder smoothing)
# ---------------------------------------------------------------------------
def _rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder-style smoothing (RMA)."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def dmi(df: pd.DataFrame, length: int = 14):
    """ADX / DI+/DI- matching TradingView `ta.dmi(length)`.

    Returns three Series aligned to ``df.index``: ``(di_plus, di_minus, adx)``.

    Uses Wilder smoothing for both DI lines and the ADX line.
    Implementation matches the TradingView reference:
      1. up_move  = high - high[1]
         down_move= low[1] - low
      2. plusDM   = up_move if (up_move > down_move and up_move > 0) else 0
         minusDM  = down_move if (down_move > up_move and down_move > 0) else 0
      3. tr       = max(high-low, |high-close[1]|, |low-close[1]|)
      4. plusDI   = 100 * rma(plusDM, length) / rma(tr, length)
         minusDI  = 100 * rma(minusDM, length) / rma(tr, length)
      5. dx       = 100 * |plusDI - minusDI| / (plusDI + minusDI)
         adx      = rma(dx, length)
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    plus_dm_s = pd.Series(plus_dm, index=df.index)
    minus_dm_s = pd.Series(minus_dm, index=df.index)

    atr_p = _rma(tr, length)
    plus_di = 100.0 * _rma(plus_dm_s, length) / atr_p
    minus_di = 100.0 * _rma(minus_dm_s, length) / atr_p

    di_sum = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan)
    adx = _rma(dx, length)

    plus_di.name = f"plus_di_{length}"
    minus_di.name = f"minus_di_{length}"
    adx.name = f"adx_{length}"
    return plus_di, minus_di, adx


# ---------------------------------------------------------------------------
# Supertrend — `ta.supertrend(factor, atrPeriod)`
# ---------------------------------------------------------------------------
def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    multiplier: float = 3.0,
    atr_period: int = 10,
):
    """Supertrend matching TradingView `ta.supertrend(factor, atrPeriod)`.

    Returns ``(supertrend_line, direction)``.

    Direction convention (matches Paul's original Pine Script):
        ``direction == -1``  -> uptrend   (Pine ``stDir < 0``)
        ``direction ==  1``  -> downtrend (Pine ``stDir > 0``)

    The canonical TradingView reference uses the opposite sign (1 = up). We
    deliberately match the Pine source Paul ported, where:

        isUptrend  = stDir < 0
        isDowntrend= stDir > 0

    The iterative loop below computes the canonical TradingView
    convention (1 = up, -1 = down) and then negates the resulting series
    to convert to Paul's convention.  After the flip, the returned
    ``direction`` series uses plain ``int`` (not pandas ``Int64``) so
    downstream ``==`` comparisons are clean.

    Pine reference:
        atr      = ta.atr(atrPeriod)
        up       = hl2 - multiplier * atr      # lower band ("upperBand")
        dn       = hl2 + multiplier * atr      # upper band ("lowerBand")
        # ratchet final bands toward price when in trend
        trendUp  = close[1] > trendUp[1]   ? max(up, trendUp[1]) : up
        trendDown= close[1] < trendDown[1] ? min(dn, trendDown[1]) : dn
        stDir    = trendUp < close ?  1 : (trendDown > close ? -1 : nz(stDir[1]))
        st       = stDir > 0      ? trendUp : trendDown
    """
    high = high.astype(float)
    low = low.astype(float)
    close = close.astype(float)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    a = tr.ewm(alpha=1.0 / atr_period, adjust=False).mean()

    hl2 = (high + low) / 2.0
    up_band = hl2 - multiplier * a  # lower / "upperBand" in TradingView
    dn_band = hl2 + multiplier * a  # upper / "lowerBand" in TradingView

    n = len(close)
    trend_up = np.full(n, np.nan)
    trend_dn = np.full(n, np.nan)
    st_dir = np.full(n, np.nan)
    st = np.full(n, np.nan)

    close_vals = close.values
    up_vals = up_band.values
    dn_vals = dn_band.values

    for i in range(n):
        if i == 0:
            trend_up[i] = up_vals[i]
            trend_dn[i] = dn_vals[i]
            # Pine's nz() defaults to +1 when undefined; matches the source.
            st_dir[i] = 1
            st[i] = trend_up[i] if st_dir[i] > 0 else trend_dn[i]
            continue

        # Ratchet trend bands toward price when trend persists
        if close_vals[i - 1] > trend_up[i - 1]:
            trend_up[i] = max(up_vals[i], trend_up[i - 1])
        else:
            trend_up[i] = up_vals[i]

        if close_vals[i - 1] < trend_dn[i - 1]:
            trend_dn[i] = min(dn_vals[i], trend_dn[i - 1])
        else:
            trend_dn[i] = dn_vals[i]

        # Direction — Pine:
        #   stDir = trendUp < close ? 1 : (trendDown > close ? -1 : nz(stDir[1]))
        if trend_up[i] < close_vals[i]:
            st_dir[i] = 1   # uptrend
        elif trend_dn[i] > close_vals[i]:
            st_dir[i] = -1  # downtrend
        else:
            prev = st_dir[i - 1]
            st_dir[i] = prev if not np.isnan(prev) else 1

        st[i] = trend_up[i] if st_dir[i] > 0 else trend_dn[i]

    # Flip convention to match Paul's Pine Script: -1 = uptrend, +1 = downtrend.
    # The loop above computed the canonical TradingView convention
    # (1 = up, -1 = down); we negate the series to align with the Pine source.
    st_dir_signed = (-st_dir).astype(int)
    # The Supertrend line itself: in canonical TradingView, st = trendUp when
    # stDir > 0 (uptrend) and trendDown when stDir < 0 (downtrend). After
    # flipping the direction sign we must mirror that mapping:
    #   direction == -1 (uptrend)   -> trendUp
    #   direction == +1 (downtrend) -> trendDown
    st_signed = np.where(st_dir_signed < 0, trend_up, trend_dn)

    st_line = pd.Series(st_signed, index=close.index, name=f"supertrend_{atr_period}_{multiplier}")
    direction = pd.Series(
        st_dir_signed, index=close.index, name=f"supertrend_dir_{atr_period}_{multiplier}"
    )  # plain int (nullable comparison-safe)
    return st_line, direction


# ---------------------------------------------------------------------------
# RSI — Wilder
# ---------------------------------------------------------------------------
def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder RSI matching `ta.rsi(length)`.

    Uses the canonical Wilder smoothing: ``EWM alpha=1/length``.
    """
    close = close.astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1.0 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss == 0 -> RSI = 100
    out = out.where(avg_loss != 0, 100.0)
    out.name = f"rsi_{length}"
    return out


# ---------------------------------------------------------------------------
# EMA / SMA
# ---------------------------------------------------------------------------
def ema(close: pd.Series, length: int) -> pd.Series:
    """Standard EMA matching `ta.ema(length)` (Pine default uses adjust=False)."""
    out = close.astype(float).ewm(span=length, adjust=False).mean()
    out.name = f"ema_{length}"
    return out


def sma(close: pd.Series, length: int) -> pd.Series:
    """Simple MA matching `ta.sma(length)`."""
    out = close.astype(float).rolling(length, min_periods=1).mean()
    out.name = f"sma_{length}"
    return out


# ---------------------------------------------------------------------------
# Slope — percent change over `lookback` bars
# ---------------------------------------------------------------------------
def slope(series: pd.Series, lookback: int) -> pd.Series:
    """``(series - series.shift(lookback)) / series.shift(lookback) * 100``.

    Matches the Pine form::

        slope = (src - src[lookback]) / src[lookback] * 100
    """
    prev = series.astype(float).shift(lookback)
    out = (series.astype(float) - prev) / prev.replace(0, np.nan) * 100.0
    out.name = f"slope_{lookback}"
    return out


# ---------------------------------------------------------------------------
# BBW percentrank — `ta.percentrank(bbw, lookback)`
# ---------------------------------------------------------------------------
def bbw_pct(close: pd.Series, length: int = 20, stddev: float = 2.0, lookback: int = 100) -> pd.Series:
    """Bollinger Band width percentrank matching Pine `ta.percentrank`.

    ``bbw`` = (upper - lower) / middle.
    ``bbw_pct`` = % of past ``lookback`` values of ``bbw`` that are < current.
    """
    close = close.astype(float)
    mid = sma(close, length)
    sd = close.rolling(length, min_periods=1).std(ddof=0)  # TradingView default uses population
    upper = mid + stddev * sd
    lower = mid - stddev * sd
    bbw = (upper - lower) / mid.replace(0, np.nan)

    out = bbw.rolling(lookback, min_periods=lookback).apply(
        lambda w: (w.iloc[:-1] < w.iloc[-1]).sum() / len(w.iloc[:-1]) if len(w) > 1 else np.nan,
        raw=False,
    )
    out.name = f"bbw_pct_{length}_{stddev}_{lookback}"
    return out


if __name__ == "__main__":
    # Quick smoke check
    n = 50
    np.random.seed(0)
    df = pd.DataFrame(
        {
            "open": 100 + np.cumsum(np.random.randn(n)),
            "high": 100 + np.cumsum(np.random.randn(n)) + 1,
            "low":  100 + np.cumsum(np.random.randn(n)) - 1,
            "close": 100 + np.cumsum(np.random.randn(n)),
        }
    )
    print("ATR last:", atr(df, 14).iloc[-1])
    p, m, a = dmi(df, 14)
    print("ADX last:", a.iloc[-1])
    st, d = supertrend(df["high"], df["low"], df["close"], 3.0, 10)
    print("Supertrend last:", st.iloc[-1], "dir:", d.iloc[-1])
    print("RSI last:", rsi(df["close"], 14).iloc[-1])
    print("EMA last:", ema(df["close"], 10).iloc[-1])
    print("SMA last:", sma(df["close"], 10).iloc[-1])
    print("Slope last:", slope(df["close"], 5).iloc[-1])
    print("BBWpct last:", bbw_pct(df["close"]).iloc[-1])

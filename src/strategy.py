"""ATT Regime Switch strategy — Pine-to-Python port.

**Design**
The Pine Script generates signals via bar-by-bar persistent state (`var`
declarations). To reproduce Pine's exact semantics we implement signal
generation as an iterative loop over the dataframe. The indicators module is
purely vectorised; the engine owns the persistent state.

`ATTStrategy` is a dataclass holding all the strategy parameters. A signal
pass returns a *signed* column set. `simulate()` is the end-to-end driver
that produces a `BacktestResult` (trades, equity curve, daily returns).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .indicators import (
    atr,
    bbw_pct,
    dmi,
    ema,
    rsi,
    slope,
    sma,
    supertrend,
)


@dataclass
class ATTStrategy:
    """ATT Regime Switch strategy parameters — defaults match the Pine port.

    Pine parameter `process_orders_on_close=false` is handled by the engine
    by applying entries at the *next* bar's open.
    """

    # Indicator lengths
    adx_len: int = 14          # `adxLen`
    atr_len: int = 14          # `atrLen`  (used for trailing-stop ATR)
    ema_len: int = 50          # `emaLen`  (50 EMA for regime filter)
    rsi_len: int = 2           # `rsiLen`  (Connors-style short RSI)
    st_mult: float = 3.0       # `stMult`   Supertrend multiplier
    st_atr_len: int = 10       # `stAtrLen` Supertrend ATR period
    dmi_len: int = 14          # `dmiLen`   re-used for DMI

    # ADX threshold to define a "trending" vs "ranging" market
    adx_trend: float = 20.0    # `adxTrend`
    adx_range: float = 18.0    # `adxRange`

    # Volatility filter — only trade when BBW pctrank > this
    bbwpct_min: float = 0.10   # `bbwpctMin` (10th percentile minimum width)

    # Regime flips: detect by ADX & DI+/DI- sign relative to close vs EMA
    regime_mode: str = "adx_di"  # 'adx_di' | 'supertrend' | 'hybrid'

    # Mean-reversion arm (Paul's Pine `mrXxx` params).  Used when ADX is
    # below ``adx_range`` (ranging regime) — RSI 2 over-extension in the
    # direction of the structural 200-SMA trend is the MR entry trigger.
    rsi_oversold: int = 10       # `rsiOversold`   (long MR trigger)
    rsi_overbought: int = 90     # `rsiOverbought` (short MR trigger)
    sma_trend_len: int = 200     # `smaTrendLen`   (structural trend filter)
    mr_trail_mult: float = 1.5   # `mrTrailMult`   (tighter trail for MR)

    # Order / risk
    initial_capital: float = 10_000.0
    risk_pct: float = 1.0      # % of equity to risk per trade
    trail_mult: float = 2.0    # ATR multiplier for the ratcheting trail
    max_bars_in_trade: int = 20
    commission_pct: float = 0.05  # % per side (Pine `commission_value=0.05`)
    slippage: float = 3.0      # absolute price points to subtract/add
    dead_money_pct: float = 0.5  # |PnL%| below this -> "dead money" exit

    # Misc
    seed: int | None = None


@dataclass
class Trade:
    entry_bar: int
    exit_bar: int
    side: str  # "long" | "short"
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float  # pnl as percentage of equity at entry
    exit_reason: str
    entry_time: pd.Timestamp | None = None
    exit_time: pd.Timestamp | None = None


@dataclass
class BacktestResult:
    symbol: str
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series | None = None
    daily_returns: pd.Series | None = None
    final_equity: float = 0.0
    initial_capital: float = 0.0
    params: dict | None = None

    def n_trades(self) -> int:
        return len(self.trades)


# ---------------------------------------------------------------------------
# Signal generation — iterative, matches Pine's bar-by-bar persistent state.
# ---------------------------------------------------------------------------
# Regime semantics:
#   regime ==  1  -> bullish (long-only territory)
#   regime == -1  -> bearish (short-only territory)
#   regime ==  0  -> neutral / no trades
#
# Entry/exit are emitted as one-shot booleans per bar.  Position state lives
# in the engine; this routine returns what the *strategy* says, given the
# indicator values present at each bar.


def _regime_adx_di(row: dict, params: ATTStrategy) -> int:
    """Bull when close > EMA(50) and ADX > trend; bear when close < EMA(50) and ADX > trend; flat otherwise.

    Pine forms the regime as a combination of ADX rising, DI+/DI- signs, and
    close vs EMA. The intent is "trend up / trend down / range".
    """
    close = row["close"]
    ema50 = row["ema50"]
    adx = row["adx"]
    plus_di = row["plus_di"]
    minus_di = row["minus_di"]

    if any(pd.isna(v) for v in (close, ema50, adx, plus_di, minus_di)):
        return 0

    if adx > params.adx_trend and close > ema50 and plus_di > minus_di:
        return 1
    if adx > params.adx_trend and close < ema50 and minus_di > plus_di:
        return -1
    if adx < params.adx_range:
        return 0
    # mid-trend continuation -> maintain current sign (caller passes previous)
    return 0


def add_indicators(df: pd.DataFrame, params: ATTStrategy) -> pd.DataFrame:
    """Compute the indicator columns used by the strategy and return a new df.

    Required input columns: ``open``, ``high``, ``low``, ``close``.
    Optional: ``volume``.

    New columns appended:
        ``atr_a``        — ATR used for trail sizing
        ``atr_st``       — ATR used internally by Supertrend
        ``adx``          — ADX
        ``plus_di``, ``minus_di``
        ``supertrend``   — Supertrend line
        ``supertrend_dir`` — +1/-1 (-1 = uptrend, +1 = downtrend; Paul's Pine convention)
        ``ema50``        — EMA(50)
        ``rsi``          — RSI(14)
        ``bbwpct``       — BBW percentrank
        ``adx_prev1``    — `adx.shift(1)` helper
        ``adx_pivot``    — `nz(adx_prev1)` (handled by caller)
    """
    out = df.copy()

    # Make sure we have the standard columns
    out.columns = [str(c).lower() for c in out.columns]

    a = atr(out, params.atr_len)
    out["atr_a"] = a.values

    plus, minus, adx = dmi(out, params.dmi_len)
    out["plus_di"] = plus.values
    out["minus_di"] = minus.values
    out["adx"] = adx.values

    st_line, st_dir = supertrend(out["high"], out["low"], out["close"],
                                 multiplier=params.st_mult, atr_period=params.st_atr_len)
    out["supertrend"] = st_line.values
    out["supertrend_dir"] = st_dir.values  # -1=uptrend, +1=downtrend (Paul's Pine)

    out["ema50"] = ema(out["close"], params.ema_len).values
    out["rsi"] = rsi(out["close"], params.rsi_len).values
    out["bbwpct"] = bbw_pct(out["close"], length=20, stddev=2.0, lookback=100).values

    # Structural-trend filter for the mean-reversion arm.
    # Paul's Pine `smaTrendLen` (default 200) — used by the MR entry to
    # ensure MR-long entries only fire above structural uptrend and MR-short
    # entries only fire below structural downtrend.
    out["sma_trend"] = sma(out["close"], params.sma_trend_len).values

    # ATR used by Supertrend internally — needed for sizing
    a_st = atr(out, params.st_atr_len)
    out["atr_st"] = a_st.values

    # Strategy helpers — shifted equivalents of Pine `[i]`
    out["adx_prev1"] = out["adx"].shift(1)
    out["plus_di_prev1"] = out["plus_di"].shift(1)
    out["minus_di_prev1"] = out["minus_di"].shift(1)

    return out


def generate_signals(df_with_indicators: pd.DataFrame, params: ATTStrategy) -> pd.DataFrame:
    """Add ``regime``, ``enter_long``, ``enter_short``, ``exit_long``, ``exit_short`` columns.

    Iterative; persistent state is kept in ``var``-style locals:
        prev_regime   — last non-zero regime
    """
    out = df_with_indicators.copy()

    n = len(out)
    regime = np.zeros(n, dtype=np.int8)
    enter_long = np.zeros(n, dtype=bool)
    enter_short = np.zeros(n, dtype=bool)
    exit_long = np.zeros(n, dtype=bool)
    exit_short = np.zeros(n, dtype=bool)
    # ``is_mr`` flags whether an enter_long/enter_short signal was generated
    # by the mean-reversion arm (so the engine can pick the appropriate
    # tighter trail-mult for new MR entries).  Paul used different
    # `mrTrailMult` for ranging MR entries vs trending entries.
    is_mr = np.zeros(n, dtype=bool)

    prev_regime = 0

    cols = out.columns
    # Pull rows as tuples-of-dicts for speed
    close_vals = out["close"].values
    adx_vals = out["adx"].values
    adx_p = out["adx_prev1"].values
    plus_di_vals = out["plus_di"].values
    plus_di_p = out["plus_di_prev1"].values
    minus_di_vals = out["minus_di"].values
    minus_di_p = out["minus_di_prev1"].values
    ema50_vals = out["ema50"].values
    rsi_vals = out["rsi"].values
    bbw_vals = out["bbwpct"].values
    st_dir_vals = out["supertrend_dir"].astype(float).values
    atr_vals = out["atr_a"].values
    sma_trend_vals = out["sma_trend"].values if "sma_trend" in out.columns else np.full(n, np.nan)

    for i in range(n):
        c = close_vals[i]
        a = adx_vals[i]
        a_p = adx_p[i]
        p_di = plus_di_vals[i]
        p_di_p = plus_di_p[i]
        m_di = minus_di_vals[i]
        m_di_p = minus_di_p[i]
        em = ema50_vals[i]
        rv = rsi_vals[i]
        bw = bbw_vals[i]
        sd = st_dir_vals[i]
        at = atr_vals[i]

        if any(np.isnan(v) for v in (c, a, p_di, m_di, em, rv)):
            regime[i] = 0
            continue

        # --- Regime classification ---
        # Bull regime: ADX trend strength AND close > EMA50 AND DI+ > DI-
        # Bear regime: ADX trend strength AND close < EMA50 AND DI- > DI+
        # Range:       ADX < adx_range
        # Use `nz(adx_prev1)` semantics — i.e. when prev ADX is NaN use current
        a_p_eff = a_p if not np.isnan(a_p) else a
        p_di_p_eff = p_di_p if not np.isnan(p_di_p) else p_di
        m_di_p_eff = m_di_p if not np.isnan(m_di_p) else m_di

        cur_regime = 0
        if a > params.adx_trend and c > em and p_di > m_di:
            cur_regime = 1
        elif a > params.adx_trend and c < em and m_di > p_di:
            cur_regime = -1
        elif a < params.adx_range:
            cur_regime = 0

        # Persistent carry-over: if ADX remains > trend between bars and DI
        # dominance persists from previous bar, hold regime.
        if (
            cur_regime == 0
            and prev_regime != 0
            and not np.isnan(a_p)
            and a > params.adx_trend
            and a_p > params.adx_trend
        ):
            if prev_regime == 1 and (p_di > m_di) and (p_di_p_eff >= m_di_p_eff):
                cur_regime = 1
            elif prev_regime == -1 and (m_di > p_di) and (m_di_p_eff >= p_di_p_eff):
                cur_regime = -1

        regime[i] = cur_regime

        # --- Entries (only when flat, evaluated by engine; signal is hint) ---
        # Use 1-bar ago cross of regime as a "fresh flip" entry trigger.
        prev_regime_eff = prev_regime if not np.isnan(prev_regime) else 0

        # Volatility confirmation — same BBW minimum gate used by both arms
        vol_confirm = (np.isnan(bw) or bw > params.bbwpct_min)

        # --- TREND arm (long/short) ---
        # Long: prev regime != 1, cur regime == 1, BBW OK, RSI not overbought,
        #       supertrend uptrend (direction == -1 in Paul's Pine convention).
        if (
            prev_regime_eff != 1
            and cur_regime == 1
            and vol_confirm
            and (np.isnan(rv) or rv < 75.0)
            and (np.isnan(sd) or sd < 0)  # supertrend uptrend (Paul's Pine convention)
        ):
            enter_long[i] = True
            is_mr[i] = False

        if (
            prev_regime_eff != -1
            and cur_regime == -1
            and vol_confirm
            and (np.isnan(rv) or rv > 25.0)
            and (np.isnan(sd) or sd > 0)  # supertrend downtrend (Paul's Pine convention)
        ):
            enter_short[i] = True
            is_mr[i] = False

        # --- MEAN-REVERSION arm (Paul's Pine rsiOversold/rsiOverbought) ---
        # Only fires when ADX is in range (cur_regime == 0 with ADX < range),
        # which is exactly the "ranging" market.  We additionally require:
        #   * structural trend filter (close vs SMA(sma_trend_len))
        #   * RSI 2 over-extension in the direction of the structural trend
        sma_v = sma_trend_vals[i] if i < len(sma_trend_vals) else np.nan

        cur_adx = a
        ranging = (not np.isnan(cur_adx)) and cur_adx < params.adx_range

        # Long MR: close > SMA(structural uptrend), RSI < oversold,
        #          ADX in range, BBW OK
        if (
            not enter_long[i]
            and ranging
            and vol_confirm
            and (np.isnan(rv) or rv < params.rsi_oversold)
            and (np.isnan(sma_v) or c > sma_v)  # structural uptrend
        ):
            enter_long[i] = True
            is_mr[i] = True

        # Short MR: close < SMA(structural downtrend), RSI > overbought,
        #           ADX in range, BBW OK
        if (
            not enter_short[i]
            and ranging
            and vol_confirm
            and (np.isnan(rv) or rv > params.rsi_overbought)
            and (np.isnan(sma_v) or c < sma_v)  # structural downtrend
        ):
            enter_short[i] = True
            is_mr[i] = True

        prev_regime = int(cur_regime)

    out["regime"] = regime
    out["enter_long"] = enter_long
    out["enter_short"] = enter_short
    out["exit_long"] = exit_long  # engine uses its own priority
    out["exit_short"] = exit_short
    out["is_mr"] = is_mr
    return out


# ---------------------------------------------------------------------------
# End-to-end simulator: combines strategy + engine (single function for ergonomics).
# Implemented in engine.py — see simulate() there.
# ---------------------------------------------------------------------------

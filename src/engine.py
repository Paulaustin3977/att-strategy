"""Backtester engine — bar-by-bar simulation with persistent state.

Mirrors TradingView's `process_orders_on_close=false`:

* Entry signals detected at bar ``t`` are filled at bar ``t+1`` open.
* Slippage is applied as ``+/- 3`` *points* (price-level units), matching
  Pine ``slippage=3``.
* Commission ``0.05%`` per side, matching ``commission_value=0.05``.

State is updated bar-by-bar so trail ratchets, dead-money exits, and
regime-flip exits reproduce Pine's behavior exactly.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .strategy import ATTStrategy, BacktestResult, Trade, add_indicators, generate_signals


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _nz(x: float, default: float = 0.0) -> float:
    """Pine's `nz(x)` — replace NaN with the supplied default."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return default
    return x


def _floor(x: float) -> int:
    """Integer floor — Pine's sizing uses integer qty."""
    return int(math.floor(x)) if x is not None else 0


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------
def simulate(df: pd.DataFrame, params: ATTStrategy,
             *, symbol: str = "") -> BacktestResult:
    """Run the ATT Regime Switch backtest on a single OHLCV frame.

    Args:
        df: DataFrame with columns ``open``, ``high``, ``low``, ``close``
            (and optionally ``volume``), indexed by date.
        params: strategy parameters.
        symbol: ticker (for reporting only).

    Returns:
        :class:`BacktestResult` with trades, equity curve, and daily returns.
    """
    if df is None or len(df) < 60:
        raise ValueError("need at least 60 bars to compute indicators meaningfully")

    # 1. Ensure lowercase columns
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]

    # 2. Indicators + signals
    df = add_indicators(df, params)
    df = generate_signals(df, params)

    n = len(df)
    open_v = df["open"].to_numpy(dtype=float, copy=True)
    high_v = df["high"].to_numpy(dtype=float, copy=True)
    low_v  = df["low"].to_numpy(dtype=float, copy=True)
    close_v = df["close"].to_numpy(dtype=float, copy=True)
    atr_v   = df["atr_a"].to_numpy(dtype=float, copy=True)
    ema_v   = df["ema50"].to_numpy(dtype=float, copy=True)
    adx_v   = df["adx"].to_numpy(dtype=float, copy=True)
    di_p_v  = df["plus_di"].to_numpy(dtype=float, copy=True)
    di_m_v  = df["minus_di"].to_numpy(dtype=float, copy=True)
    regime_v = df["regime"].to_numpy(dtype=int, copy=True)
    enter_l = df["enter_long"].to_numpy(dtype=bool, copy=True)
    enter_s = df["enter_short"].to_numpy(dtype=bool, copy=True)

    # 3. Persistent state (matches Pine `var`)
    equity = float(params.initial_capital)
    pos_side = 0          # +1 long, -1 short, 0 flat
    pos_qty = 0.0
    entry_price = 0.0
    entry_bar_idx = -1
    bars_in_trade = 0

    long_trail = np.nan   # ratcheting long stop (lowest of close-ATR*mult)
    short_trail = np.nan  # ratcheting short stop (highest of close+ATR*mult)

    # 4. Output arrays
    trades: list[Trade] = []
    equity_curve = np.full(n, np.nan)

    slip = float(params.slippage)
    comm_rate = float(params.commission_pct) / 100.0

    # 5. Bar loop
    for i in range(n):
        c = close_v[i]
        o = open_v[i]
        a = atr_v[i]
        ema50_v = ema_v[i]
        adx = adx_v[i]
        di_p = di_p_v[i]
        di_m = di_m_v[i]
        rg = int(regime_v[i])
        sig_l = bool(enter_l[i])
        sig_s = bool(enter_s[i])

        # ---- If we have a position, evaluate exits BEFORE the entry check ----
        exited = False
        exit_reason: Optional[str] = None
        exit_price = 0.0
        exit_bar = i

        if pos_side != 0:
            bars_in_trade += 1

            # Maintain the ratcheting trail each bar.
            # Pine:
            #   newTrail = close - atr * trail_mult
            #   longTrail = math.max(nz(longTrail), newTrail)
            if pos_side == 1:
                if not np.isnan(a):
                    new_trail = c - params.trail_mult * a
                    long_trail = max(_nz(long_trail, new_trail), new_trail)
                # Exit 1: trail stop hit
                if not np.isnan(long_trail) and c < long_trail:
                    exit_price = max(long_trail, low_v[i] if not np.isnan(low_v[i]) else c)
                    exit_reason = "trail_stop"
                    exit_bar = i
                    exited = True
            else:  # short
                if not np.isnan(a):
                    new_trail = c + params.trail_mult * a
                    short_trail = min(_nz(short_trail, new_trail), new_trail)
                if not np.isnan(short_trail) and c > short_trail:
                    exit_price = min(short_trail, low_v[i] if not np.isnan(low_v[i]) else c)
                    exit_reason = "trail_stop"
                    exit_bar = i
                    exited = True

            # Exit 2: dead money
            if not exited and bars_in_trade >= params.max_bars_in_trade:
                unreal_pct = (c - entry_price) / entry_price * 100.0 if pos_side == 1 else (entry_price - c) / entry_price * 100.0
                if abs(unreal_pct) < params.dead_money_pct:
                    exit_price = c
                    exit_reason = f"dead_money({unreal_pct:.2f}%)"
                    exit_bar = i
                    exited = True

            # Exit 3: regime flip
            if not exited:
                if (
                    pos_side == 1
                    and rg == -1
                    and not np.isnan(adx)
                    and not np.isnan(ema50_v)
                    and adx < params.adx_range
                    and c < ema50_v
                ):
                    exit_price = c
                    exit_reason = "regime_flip"
                    exit_bar = i
                    exited = True
                if (
                    pos_side == -1
                    and rg == 1
                    and not np.isnan(adx)
                    and not np.isnan(ema50_v)
                    and adx < params.adx_range
                    and c > ema50_v
                ):
                    exit_price = c
                    exit_reason = "regime_flip"
                    exit_bar = i
                    exited = True

        # ---- Apply exit (fills at close-of-bar — we mark-to-market then settle) ----
        if exited and pos_side != 0:
            # Apply slippage against the trader
            if pos_side == 1:
                fill = exit_price - slip
            else:
                fill = exit_price + slip
            pnl = (fill - entry_price) * pos_qty * pos_side  # gross
            commission = abs(fill * pos_qty * comm_rate)
            pnl -= commission
            equity += pnl
            trades.append(
                Trade(
                    entry_bar=entry_bar_idx,
                    exit_bar=exit_bar,
                    side=("long" if pos_side == 1 else "short"),
                    entry_price=float(entry_price),
                    exit_price=float(fill),
                    qty=float(pos_qty),
                    pnl=float(pnl),
                    pnl_pct=(pnl / max(equity - pnl, 1e-9) * 100.0) if (equity - pnl) > 0 else 0.0,
                    exit_reason=exit_reason or "unknown",
                    entry_time=df.index[entry_bar_idx] if entry_bar_idx >= 0 else None,
                    exit_time=df.index[exit_bar],
                )
            )
            # Reset state
            pos_side = 0
            pos_qty = 0.0
            entry_price = 0.0
            entry_bar_idx = -1
            bars_in_trade = 0
            long_trail = np.nan
            short_trail = np.nan

        # ---- Mark to market at close (used for equity curve only) ----
        mtm = equity
        if pos_side != 0:
            unreal = (close_v[i] - entry_price) * pos_qty * pos_side
            mtm = equity + unreal

        # ---- Entries: signal at bar `i` -> fill at next bar open ----
        # We mustn't write today's open (we already started iterating); apply
        # this conservatively by limiting entries to those whose next-bar
        # exists.  If i+1 >= n, we cannot enter today.
        if pos_side == 0 and i + 1 < n:
            next_open = open_v[i + 1]
            # Long signal
            if sig_l:
                if not np.isnan(a) and a > 0 and not np.isnan(next_open):
                    risk_dollars = equity * params.risk_pct / 100.0
                    if risk_dollars > 0:
                        raw_qty = risk_dollars / (a * params.trail_mult)
                        qty = _floor(raw_qty)
                        if qty >= 1:
                            entry_price = next_open + slip  # buy one tick + slippage up
                            commission = entry_price * qty * comm_rate
                            if equity > commission:
                                pos_qty = float(qty)
                                pos_side = 1
                                entry_bar_idx = i + 1
                                bars_in_trade = 0
                                long_trail = entry_price - params.trail_mult * a  # initial stop at fill
                                # deduct commission from equity to keep things consistent
                                equity -= commission
            elif sig_s:
                if not np.isnan(a) and a > 0 and not np.isnan(next_open):
                    risk_dollars = equity * params.risk_pct / 100.0
                    if risk_dollars > 0:
                        raw_qty = risk_dollars / (a * params.trail_mult)
                        qty = _floor(raw_qty)
                        if qty >= 1:
                            entry_price = next_open - slip  # sell one tick - slippage down
                            commission = entry_price * qty * comm_rate
                            if equity > commission:
                                pos_qty = float(qty)
                                pos_side = -1
                                entry_bar_idx = i + 1
                                bars_in_trade = 0
                                short_trail = entry_price + params.trail_mult * a
                                equity -= commission

        equity_curve[i] = mtm

    # Close out any open position at the final bar's close (mark-to-market)
    if pos_side != 0:
        final_price = close_v[-1]
        if pos_side == 1:
            fill = final_price - slip
        else:
            fill = final_price + slip
        pnl = (fill - entry_price) * pos_qty * pos_side
        commission = abs(fill * pos_qty * comm_rate)
        pnl -= commission
        equity += pnl
        trades.append(
            Trade(
                entry_bar=entry_bar_idx,
                exit_bar=n - 1,
                side=("long" if pos_side == 1 else "short"),
                entry_price=float(entry_price),
                exit_price=float(fill),
                qty=float(pos_qty),
                pnl=float(pnl),
                pnl_pct=pnl / max(entry_price * abs(pos_qty), 1e-9) * 100.0,
                exit_reason="end_of_data",
                entry_time=df.index[entry_bar_idx] if entry_bar_idx >= 0 else None,
                exit_time=df.index[-1],
            )
        )

    eq = pd.Series(equity_curve, index=df.index, name=f"equity_{symbol or 'X'}")
    dr = eq.pct_change().fillna(0.0)
    dr.name = "daily_return"

    return BacktestResult(
        symbol=symbol,
        trades=trades,
        equity_curve=eq,
        daily_returns=dr,
        final_equity=float(eq.iloc[-1]) if len(eq) else float(params.initial_capital),
        initial_capital=float(params.initial_capital),
        params=ATTStrategy(**asdict_dict(params)).__dict__,
    )


def asdict_dict(obj):
    """Workaround: asdict() requires dataclass instances."""
    try:
        from dataclasses import asdict as _asdict
        return _asdict(obj)
    except Exception:
        return dict(obj.__dict__) if hasattr(obj, "__dict__") else {}

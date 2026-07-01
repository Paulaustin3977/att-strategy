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


def _trail_mult_for_entry(params: ATTStrategy, *, long_mr: bool, regime: int) -> float:
    """Pick the appropriate trail-mult for a NEW entry.

    For mean-reversion (ranging) entries (``long_mr`` is True) we use
    ``mr_trail_mult`` (tighter) instead of ``trail_mult``. For trend
    entries we use ``trail_mult``. Regime parameter is currently unused
    but kept for forward-compatibility (e.g. future hybrid mode).
    """
    if long_mr:
        return float(getattr(params, "mr_trail_mult", params.trail_mult))
    return float(params.trail_mult)


def _entry_atr(df: pd.DataFrame, i: int, params: ATTStrategy, *, use_st: bool = False) -> float:
    """ATR for sizing a new entry.

    Falls back gracefully through:
      1. ``atr_a`` column (set by ``add_indicators`` — matches engine's
         default column name)
      2. ``atr_st`` column (Supertrend-period ATR; sometimes useful for
         momentum entries)
      3. NaN (caller will fall back to its own copy)
    """
    try:
        col = "atr_st" if use_st else "atr_a"
        if col in df.columns:
            v = df[col].iloc[i]
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                return float(v)
    except Exception:
        pass
    return float("nan")


def _slippage_in_price(price: float, slip_value: float) -> float:
    """Convert ``params.slippage`` to a *price-normalised* slippage.

    Paul's Pine ``slippage=3`` was calibrated for stocks/commodities in
    absolute price points.  Applied to a 1.05 EURUSD that makes a SHORT
    entry fill at ``1.05 - 3 = -1.95`` — wildly nonsensical and the
    source of the catastrophic FX results in Phase A2.

    Here we interpret ``slippage`` as **basis points of price** (1 unit =
    0.01% of price = 1 bp).  A ``slippage=3`` value therefore becomes
    3 bps of price:

        EURUSD  1.0500 → 0.000315 slip (3 bps)
        GC=F    2000    → 0.60      slip
        ES=F    4500    → 1.35      slip

    These match the order of magnitude of the Pine model on each
    instrument (a few ticks) while remaining safe across asset classes.
    Negative or zero ``price`` falls back to zero slippage.
    """
    if price is None or price <= 0 or np.isnan(price) or slip_value <= 0:
        return 0.0
    # ``slippage`` is now interpreted as bps (1 unit = 0.0001 of price)
    return float(price) * float(slip_value) * 1e-4


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
    is_mr_v = (
        df["is_mr"].to_numpy(dtype=bool, copy=True)
        if "is_mr" in df.columns else np.zeros(n, dtype=bool)
    )

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
            # Apply slippage against the trader (price-normalised: scaled to
            # the fill currency instead of the legacy absolute "3 price
            # points" of Pine).
            if pos_side == 1:
                fill = exit_price - _slippage_in_price(exit_price, slip)
            else:
                fill = exit_price + _slippage_in_price(exit_price, slip)
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
                if not np.isnan(a) and a > 0 and not np.isnan(next_open) and next_open > 0:
                    risk_dollars = equity * params.risk_pct / 100.0
                    if risk_dollars > 0:
                        # ---- PRICE-NORMALISED SIZING (Bug 1 fix) ----
                        # Use the trail stop distance as a fraction of price
                        # so 1% risk-per-trade means 1% of position value at
                        # risk regardless of the asset's price scale (e.g.
                        # EURUSD=1.10 vs GC=2000).
                        cur_trail_mult = _trail_mult_for_entry(
                            params, long_mr=bool(is_mr_v[i]), regime=int(regime_v[i])
                        )
                        cur_atr = _entry_atr(df, i, params, use_st=False)
                        if np.isnan(cur_atr) or cur_atr <= 0:
                            cur_atr = a
                        stop_pct = (cur_atr * cur_trail_mult) / next_open
                        if 0 < stop_pct < 0.20:
                            raw_qty = risk_dollars / (next_open * stop_pct)
                            qty = _floor(raw_qty)
                        else:
                            qty = 0  # ATR sanity check failed -> skip entry
                        if qty >= 1:
                            entry_slip = _slippage_in_price(next_open, slip)
                            entry_price = next_open + entry_slip  # buy one tick + slippage up
                            commission = entry_price * qty * comm_rate
                            if equity > commission:
                                pos_qty = float(qty)
                                pos_side = 1
                                entry_bar_idx = i + 1
                                bars_in_trade = 0
                                long_trail = entry_price - cur_trail_mult * cur_atr  # initial stop at fill
                                # deduct commission from equity to keep things consistent
                                equity -= commission
            elif sig_s:
                if not np.isnan(a) and a > 0 and not np.isnan(next_open) and next_open > 0:
                    risk_dollars = equity * params.risk_pct / 100.0
                    if risk_dollars > 0:
                        # ---- PRICE-NORMALISED SIZING (Bug 1 fix) ----
                        cur_trail_mult = _trail_mult_for_entry(
                            params, long_mr=bool(is_mr_v[i]), regime=int(regime_v[i])
                        )
                        cur_atr = _entry_atr(df, i, params, use_st=False)
                        if np.isnan(cur_atr) or cur_atr <= 0:
                            cur_atr = a
                        stop_pct = (cur_atr * cur_trail_mult) / next_open
                        if 0 < stop_pct < 0.20:
                            raw_qty = risk_dollars / (next_open * stop_pct)
                            qty = _floor(raw_qty)
                        else:
                            qty = 0
                        if qty >= 1:
                            entry_slip = _slippage_in_price(next_open, slip)
                            entry_price = next_open - entry_slip  # sell one tick - slippage down
                            commission = entry_price * qty * comm_rate
                            if equity > commission:
                                pos_qty = float(qty)
                                pos_side = -1
                                entry_bar_idx = i + 1
                                bars_in_trade = 0
                                short_trail = entry_price + cur_trail_mult * cur_atr
                                equity -= commission

        equity_curve[i] = mtm

    # Close out any open position at the final bar's close (mark-to-market)
    if pos_side != 0:
        final_price = close_v[-1]
        if pos_side == 1:
            fill = final_price - _slippage_in_price(final_price, slip)
        else:
            fill = final_price + _slippage_in_price(final_price, slip)
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

# ATT Strategy — Chart Visualisation Guide

_Companion to `pine/ATT_paper_trade_4h.pine`._

This document explains every indicator plotted on the chart, how to read the visual output, and what each colour/style means. The strategy is loaded as an indicator overlay, so all of this renders on top of your candlestick chart.

---

## 1. Regime background (the most important visual)

The chart background is tinted by the current market regime, classified bar-by-bar:

| Background colour | Meaning | Trigger |
|---|---|---|
| **Green** (90% transparent) | TRENDING regime | ADX > trending threshold AND ADX slope positive |
| **Orange** (90% transparent) | RANGING regime | ADX < ranging threshold AND Bollinger Band Width < 40th percentile |
| **Yellow** (90% transparent) | TRANSITION regime | Neither trending nor ranging criteria satisfied |

**How to read it:** green sections are where the trend-following arm is most likely to fire. Orange sections are where mean-reversion entries (RSI(2) oversold/overbought) are most likely. Yellow sections are dead zones — no new entries expected.

The regime is computed bar-by-bar from ADX + Bollinger Band Width percentile, not from a smoothed lookback. Expect frequent regime flips near the boundaries.

---

## 2. Overlaid line plots

Six line plots are drawn on the price chart:

| Plot name | Colour | Line style | What it is |
|---|---|---|---|
| **Supertrend** | Green (uptrend) / Red (downtrend) | Solid, 2px, line-break style | The trailing stop level. Longs are taken when supertrend flips up; shorts when it flips down. The colour of the line tells you the current supertrend direction. |
| **Trend EMA 50** | Blue | Solid, 2px | 50-period EMA (input: `ema_len=100` for the 4h winner). Acts as the macro trend filter — entries require price to be on the correct side of this line. |
| **Long-term SMA 200** | Grey | Solid, 1px | 200-period SMA. Used by the mean-reversion arm: only MR longs above this line, only MR shorts below. Often a slow-moving reference for the overall trend. |
| **BB Upper** | Purple (50% transparent) | Solid, 1px | Upper Bollinger Band (length=20, std=2.0). |
| **BB Basis** | Purple (50% transparent) | Solid, 1px | Bollinger Band middle line (20-period SMA). |
| **BB Lower** | Purple (50% transparent) | Solid, 1px | Lower Bollinger Band. |

**How to read them together:** when price is between the BB Upper and BB Lower, the market is "in band" — typical ranging behaviour. When price closes outside a band, it's an expansion move, often associated with strong directional momentum. The squeeze (BB Width below 25th percentile) is an input condition for the squeeze-breakout entry arm.

---

## 3. Trailing stop lines

| Plot name | Colour | Line style | When shown |
|---|---|---|---|
| **Long Trail Stop** | Lime green | Cross markers, 2px | Only when you have an open long position. Ratchets UP only — never retreats. |
| **Short Trail Stop** | Red | Cross markers, 2px | Only when you have an open short position. Ratchets DOWN only. |

**How to read them:** the trail is the price level that, if touched on a bar close, will close your position. Watch the lime line during a long trade — if price approaches it, your exit is imminent. The trail is computed as `close - ATR(14) * 1.5` (the 4h winner's `trailMult=1.5`).

---

## 4. Entry and exit labels (small text labels on the chart)

When `Show Entry/Exit Labels` is enabled, four label types appear at the relevant bar:

| Label | Position | Colour | Meaning |
|---|---|---|---|
| **L** | Below bar (low) | Green background, white text | Long entry signal fired on this bar |
| **S** | Above bar (high) | Red background, white text | Short entry signal fired on this bar |
| **XL** | Above bar (high) | Lime background, black text | Long exit executed on this bar (trail, dead-money, or regime-flip) |
| **XS** | Below bar (low) | Maroon background, white text | Short exit executed on this bar |

These labels are most useful when validating live behaviour against backtest — they show you exactly where the strategy would have entered and exited. If you see an L on a bar but no trade was actually opened, something is wrong with your broker/execution pipeline.

---

## 5. Info table (top-right of chart)

A two-column table is rendered in the top-right corner. It updates on the most recent bar. Columns are: label (left) and value (right).

| Row | What it shows |
|---|---|
| **Regime** | TRENDING / RANGING / TRANSITION (coloured green/orange/yellow) |
| **ADX** | Current ADX value, 1 decimal place |
| **Supertrend** | UP or DOWN (coloured green or red) |
| **RSI2** | Current 2-period RSI, integer |
| **BBW%** | Bollinger Band Width percentile, 0-100 |
| **Trail Stop** | Current long or short trailing stop price level, or "—" if flat |
| **Bars In Trade** | How many bars the current position has been open |
| **TF** | Current chart timeframe (yellow text) — should read `240` on 4H. If it doesn't, the strategy is not on the calibrated timeframe. |

**How to use it:** glance at the top-right to check the strategy's current state. The Regime + Supertrend + RSI2 trio tells you whether the strategy is likely to take a new position soon. BBW% near 0 = squeeze imminent (breakout arm may fire). Bars In Trade > 30 = approaching the dead-money exit.

---

## 6. 4H safety warning (bottom-left of chart)

A red warning panel appears at the bottom-left of the chart **only** if the current chart timeframe is NOT 4 hours (240 minutes). The text reads:

> "WARNING: This strategy is calibrated for 4H (240m). Current TF: [your TF]. Edges will not match backtest."

**How to use it:** if you accidentally load the strategy on a 1D or 1H chart, this warning fires immediately. The strategy will still run (Pine doesn't prevent it), but the parameter set won't be calibrated for that timeframe and the backtest edges won't apply. Switch the chart to 4H (240m) to clear the warning.

---

## 7. The 4 alert conditions (sidebar / alert dialog)

The strategy exposes four `alertcondition()` calls that TradingView's alert dialog can subscribe to. When configuring an alert in the chart, set:

- **Condition:** `ATT Paper-Trade 4H`
- **Choose one of:**
  - `ATT Long Entry` — fires on every bar where `enterLong` is true (long entry signal)
  - `ATT Short Entry` — fires on every bar where `enterShort` is true
  - `ATT Long Exit` — fires on every bar where `exitLong` is true (trail/dead-money/regime-flip)
  - `ATT Short Exit` — fires on every bar where `exitShort` is true

The alert message text is intentionally short and includes the ticker + price. Connect the alert webhook URL to your broker for semi-automated execution (3Commas, PineConnector, Alertatron, or your broker's native webhook like IBKR/TDA).

**Important note:** entry alerts fire on the bar that produces the signal, but the strategy uses `process_orders_on_close=false`, so the actual fill happens at the next bar's open. Your broker's webhook should respect this delay (most auto-execution tools do).

---

## 8. What you should NOT see

| If you see... | It means... | Fix |
|---|---|---|
| Red `!` next to the strategy name on the chart title bar | Compile error in the Pine Script | Check the Pine Editor error message; the most common cause is loading an older version of the script |
| A "WARNING: This strategy is calibrated for 4H" panel at the bottom | The chart isn't on 4H | Click the timeframe selector at the top of the chart and pick `4h` |
| No regime background colour | The "Show Regime Background" input is set to false | Open strategy settings → "Visual" group → toggle `Show Regime Background` to true |
| No L/S/XL/XS labels | The "Show Entry/Exit Labels" input is set to false | Open strategy settings → "Visual" group → toggle `Show Entry/Exit Labels` to true |

---

## 9. Quick reference: what to watch during paper-trading

**Per bar (every 4 hours):**
- Regime colour (green = trending regime, orange = ranging, yellow = quiet)
- Info table (regime + ADX + supertrend direction + RSI2 + BBW%)
- Any new L or S label

**Daily:**
- Trail stop lines (lime or red, only when in position)
- Equity vs your expected backtest target
- Any XL or XS label

**Weekly:**
- Compare live equity curve vs the basket equity chart in `results/multi_tf/paper_trade_basket_equity.png`
- Net return per symbol should be in the +0.5% to +3% per trade range
- Win rate per symbol should be 50-63%

**Monthly:**
- Compute live Sharpe, Sortino, MaxDD
- Decision criteria (from `results/multi_tf/paper_trade_setup.md`):
  - 3-month Sharpe > 0 AND MaxDD < 5%: continue, consider scaling
  - Sharpe between -0.5 and 0: continue, monitor
  - Sharpe < -0.5 OR MaxDD > 10%: stop, re-examine

---

## 10. Where to find the backtest reference

For comparison while paper-trading, the original backtest equity chart is at:

```
/Users/paul/att-strategy/results/multi_tf/paper_trade_basket_equity.png
```

This shows what the strategy's equity curve looked like over the 2-year backtest on the same basket. Your live equity should loosely follow this curve. Divergences are normal (live ≠ backtest), but the overall shape should be similar.

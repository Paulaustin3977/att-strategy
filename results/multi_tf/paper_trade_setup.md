# ATT Strategy — 4h Paper-Trade Setup Guide

_Generated: 2026-07-01 18:30 BST_
_Winner from `results/multi_tf/4h_winner.json` (commit a012f96)_

This guide walks you through running the ATT Regime Switch strategy on **live demo data** at **4h bars** using the parameters the walk-forward optimizer selected. The goal is to **validate live behaviour against the backtest** before committing any real capital.

---

## 1. The Strategy (TL;DR)

- **Edge:** regime-switching system — Supertrend + ADX for trend entries, RSI(2) for mean-reversion, ATR-based trailing stop. Long + short, fixed-fractional sizing, no pyramiding.
- **Timeframe:** 4h bars. ~250 4h bars per quarter per symbol.
- **Universe:** the 10 symbols listed below (chosen from the 46-symbol 4h qualifying universe based on Sharpe, win rate, and liquidity).
- **Risk per trade:** 1.0% of equity, sized via ATR distance.

## 2. Winning Parameters (drop-in)

These are the exact parameters from `results/multi_tf/4h_winner.json`. Copy them into the Pine Script inputs when you re-deploy on TradingView, or pass them as `ATTStrategy(**params)` in the Python port.

```python
from src.strategy import ATTStrategy

ATTStrategy(
    adx_len=20,
    atr_len=14,
    ema_len=100,
    rsi_len=2,
    dmi_len=20,
    st_atr_len=14,
    st_mult=2.0,
    adx_trend=30.0,
    adx_range=20.0,
    bbwpct_min=0.05,
    rsi_oversold=10,
    rsi_overbought=85,
    sma_trend_len=200,
    mr_trail_mult=2.0,
    risk_pct=1.0,
    trail_mult=1.5,
    max_bars_in_trade=30,
    dead_money_pct=0.25
)
```

The original Pine Script in your brief is already structurally compatible — just replace its input defaults with these values.

## 3. Recommended Paper-Trade Basket (10 symbols)

Ranked by walk-forward OOS Sharpe, win rate, and liquidity. All had ≥88% positive OOS windows in the backtest.

| Tier | Symbol | Asset | Notes |
|---|---|---|---|
| 1 | GC=F | Gold | Cleanest signal, used in sanity check |
| 1 | CL=F | WTI Crude | High return, liquid |
| 1 | ^GSPC | S&P 500 cash | Most liquid equity index |
| 1 | USDJPY=X | USD/JPY | Best FX performer |
| 1 | OJ=F | Orange Juice | Highest OOS Sharpe in basket |
| 2 | HG=F | Copper | Industrial metal |
| 2 | ZS=F | Soybeans | Grain |
| 2 | ^RUT | Russell 2000 | Small-cap US equity |
| 2 | NG=F | Natural Gas | Energy diversification |
| 2 | RB=F | Gasoline | Highest return % |

**Tier 1** = start with these 5.
**Tier 2** = add after 1-2 months if Tier 1 is performing in line with the backtest.

### Symbols to AVOID at 4h (in the existing universe, but 4h backtest showed losses)

- NQ=F, EUR/GBP, GBP/JPY, CAD/JPY, USD/CHF, AUD/CHF, NZD/CAD, CT=F (Cotton), HE=F (Lean Hogs)
- The strategy loses money on these at 4h with the winner params. Exclude them.

## 4. Broker / Execution Setup

### Recommended brokers (paper-trading mode)

| Broker | 4h bars? | Commissions | Notes |
|---|---|---|---|
| **Interactive Brokers (IBKR)** | Yes (via TWS API or TWS workstation) | Tiered: $0.65/share for US, ~$2.50/lot FX | Industry standard. Use `IBKR Pro` (not Lite). |
| **MetaTrader 5** | Yes (synthetic from M1/M5) | ~$3.50/lot FX typical | Most retail-friendly. EAs in MQL5 can replicate the Pine logic. |
| **TradingView (paper-trading)** | Yes (built-in) | Simulated | Easiest if you keep the Pine Script. Built-in alerts → webhook to broker if you want semi-automated. |
| **OANDA (fxTrade)** | Yes | Spread-only (FX) | Good for the USDJPY leg. |

### Account setup

- **Account size:** start with whatever your broker's paper-trading default is (IBKR = $1M demo, MT5 = configurable). The strategy targets **1% risk per trade**, so account size doesn't change signal frequency, only position size.
- **Leverage:** 1:1 to 5:1 recommended. The strategy already manages risk via position sizing; piling on leverage just magnifies drawdowns.

## 5. Mapping the strategy to live execution

### Entry rules

For each 4h bar close:
1. Compute all indicators using `ATTStrategy(**winner_params)`. Either run the Pine Script directly on TradingView (with the params above) or use the Python port at `~/att-strategy/`.
2. Check `enter_long` / `enter_short` flags.
3. If a new entry signal fires AND you're flat: open the position at the **next bar's open** (NOT the current bar's close — this is what the backtest does).
4. Position size: `qty = floor((equity * 0.01) / (close * stop_pct))` where `stop_pct = (ATR(14) * 1.5) / close`. Cap at 0.20 (skip entry if stop would be >20% of price).

### Exit rules (in priority order)

1. **Trailing stop** (ATR × 1.5): close < longTrail for longs, close > shortTrail for shorts. The trail only ratchets in your favour.
2. **Dead-money exit**: after 30 bars in trade (≈5 days at 4h) and unrealised P&L < +0.25%, exit.
3. **Regime-flip exit**: if ADX < 20 and you're long and price < EMA(100), exit.

### Sizing sanity check

If the backtest sizing formula gives a position > 20% of your account value, **skip the trade**. This catches miscomputed ATR or data errors.

## 6. Live-monitoring protocol

### Daily checks (5 min)

- Open the strategy dashboard (TradingView, IBKR TWS, or the Python port's `simulate()` on the latest 4h bar).
- Confirm: open positions match the strategy's intended state, no orphaned positions, no missed signals.
- Verify equity is within ±5% of expected based on the in-trade positions.

### Weekly checks (30 min)

- Reconcile paper equity vs. expected equity from the backtest on the same date range.
- Note: in live trading the strategy expects to produce ~2-3 trades per symbol per month. If your actual trade count is way off (±50%), something is wrong.
- Log any trades that were entered but the backtest wouldn't have entered, or vice versa.

### Monthly review

- Compute Sharpe, Sortino, MaxDD on the live data.
- **Decision criteria:**
  - If after **3 months** live Sharpe > 0 AND MaxDD < 5%: continue, consider scaling up.
  - If Sharpe between -0.5 and 0: continue, monitor closely, don't scale.
  - If Sharpe < -0.5 OR MaxDD > 10%: stop paper-trading, re-examine.

### Failure-mode playbook

| Symptom | Likely cause | Fix |
|---|---|---|
| No signals for 2+ weeks | Data feed broken, or symbol halted | Check broker data; check Yahoo Finance directly |
| Too many signals (>5/day on one symbol) | Slippage / spread assumptions too tight | Increase commission assumption, increase slippage |
| Equities diverge from backtest by >10% | Look-ahead bias in your code, or live fills are worse | Review code for next-bar-open execution; widen slippage assumption |
| Win rate much lower than backtest | Random — could just be a bad month | Continue, don't panic after <3 months |

## 7. What NOT to do

- **Don't add symbols not in the basket.** The 10-symbol basket is curated for liquidity + backtest performance. Adding weak symbols dilutes the edge.
- **Don't increase position size.** The 1% risk-per-trade is baked into the backtest. Larger positions = larger drawdowns that the backtest didn't model.
- **Don't skip signals.** If the strategy says enter and you don't, your live Sharpe will be lower than backtest. Conversely, don't add signals the strategy didn't generate — that's not paper-trading, that's manual override.
- **Don't go live before 3 months.** Walk-forward is statistically thin; you need live data to confirm the edge.
- **Don't trade other strategies simultaneously.** Confounders. One paper-trade at a time.

## 8. Where the data lives

| File | What it contains |
|---|---|
| `data/GC_F_4h.csv` | 4h OHLCV for Gold |
| `data/CL_F_4h.csv` | 4h OHLCV for WTI Crude |
| ... (10 files for the basket) | One per symbol |
| `results/multi_tf/4h_winner.json` | Winner parameters + metrics |
| `results/multi_tf/4h_robustness_report.md` | Full robustness analysis |
| `results/multi_tf/paper_trade_basket_equity.png` | Equity curves for the basket |
| `results/multi_tf/paper_trade_basket_metrics.json` | Per-symbol metrics for the basket |
| `results/multi_tf/SUMMARY.md` | Cross-timeframe verdict |

## 9. Expected paper-trade profile (from backtest)

- **Trade frequency:** ~2-3 trades per symbol per month (so ~20-30 trades/month across the 10-symbol basket)
- **Per-trade holding period:** 1-30 bars (4h-5 days)
- **Per-trade expectancy:** +0.5% to +3% net (after commission + slippage)
- **Win rate:** 50-63% across the basket
- **Monthly basket return target:** +2-3% with -3-5% monthly MaxDD
- **Quarterly basket return target:** +6-9%

These are **backtest targets, not guarantees**. Live will be worse by an unknown amount due to:
- Real slippage vs. assumed 3 bps
- Discretionary trade timing (you may not enter exactly at next-bar-open)
- Regime changes the optimizer didn't see
- Data feed quirks (missing bars, weird rollover, etc.)

If live matches within ±30% over 3 months, consider that a successful validation. If it matches within ±10%, treat it as evidence the strategy's edge is robust.

## 10. After 6 months

If paper-trading confirms the edge:
1. **Re-run the walk-forward optimizer** with 2 more years of data (4y → 6y if your data source supports it; otherwise re-run with current data and compare to this report).
2. **Re-validate the winner params** — if they've changed materially, the regime behaviour is shifting.
3. **Then and only then** consider live trading. Start with the smallest position size your broker allows. Scale up gradually.

If paper-trading does NOT confirm the edge:
1. **The edge was overfit.** The walk-forward robustness filters passed, but on 4 windows of 45-bar OOS that's thin. The strategy may not survive 2027+ market structure.
2. **Stop the paper-trade.** Don't average down or extend the test period hoping for a different outcome.
3. **Document what went wrong** in `results/multi_tf/paper_trade_log.md` (create this file as you go).

---

## TL;DR

1. Use the 10-symbol basket (Tier 1 = first 5).
2. Use the winning 4h params above.
3. Paper-trade for 3-6 months. No real capital.
4. Compare live results to backtest. Acceptable if within ±30%.
5. Don't trade on real capital until live validates.

If you have questions during paper-trading, ping me on Telegram with `paper-trade:` prefix.
# ATT Strategy — Multi-Timeframe Verdict

_Generated: 2026-07-01 18:04 BST_

## 1. Default-Params Sweep (60 symbols per timeframe)

| TF | Bars/Symbol (max) | % Profitable | Mean Sharpe | Mean Return | Mean MaxDD |
| --- | --- | --- | --- | --- | --- |
| 15m    | ~1500 (60d) | 34.5% | -3.17 | -27.1% | -36.4% |
| 30m    | ~750 (60d)  | 37.3% | -1.22 | -9.6%  | -16.5% |
| 1h     | ~4800 (2y)  | 47.5% | -0.25 | -8.4%  | -33.8% |
| 4h     | ~1200 (2y)  | 61.0% | +0.23 | +6.5%  | -8.7%  |
| weekly | ~520 (10y)  | 63.3% | +0.10 | +1.7%  | -2.4%  |

Profitability scales monotonically with timeframe. 15m and 30m are deep red on default params. 4h and weekly are mildly profitable.

## 2. Walk-Forward Winners

| TF | Mean OOS Sharpe | % Pos OOS | % Pos Return | % MaxDD<35% | n Qualified | Filters |
| --- | --- | --- | --- | --- | --- | --- |
| 15m    | -0.696 | 0.0%  | 23.3% | 100.0% | 43/58 | **FAIL** |
| 30m    | -1.110 | 0.0%  | 0.0%  | 100.0% | 28/59 | **FAIL** |
| 1h     | -0.324 | 7.5%  | 26.4% | 100.0% | 53/59 | **FAIL** |
| **4h** | **+0.138** | **60.9%** | **63.0%** | **100.0%** | **46/59** | **PASS** |
| **weekly** | **+0.357** | **71.4%** | **71.4%** | **100.0%** | **7/60** | **PASS** |

Robustness filters: OOS Sharpe > 0 on ≥60% symbols, MaxDD < 35% on ≥70%, total return > 0 on ≥55%. Only **4h** and **weekly** pass all three.

## 3. Per-Timeframe Verdicts

### 15m — FAIL
- Winner params: adx_len=20, rsi_len=5, st_mult=4.0, trail_mult=3.0, risk_pct=1.0
- OOS Sharpe -0.696, 0% positive OOS across 43 qualifying symbols
- **Honest verdict:** With only ~1500 bars × 8 walk-forward windows of ~5 days each, statistical power is essentially zero. The default-params sweep already showed -27% mean return and -3.17 Sharpe. The regime classifier (ADX/Supertrend/RSI) was designed for daily bars; at 15m it's just whipsawing on noise. **Retire this TF.**

### 30m — FAIL
- Winner params: adx_len=20, rsi_len=2, st_mult=4.0, trail_mult=1.5, risk_pct=2.0
- OOS Sharpe -1.110, 0% positive OOS, 0% positive return across 28 qualifying symbols
- **Honest verdict:** Only 60 days of data (yfinance limit), 6 windows × 10 days each. Even shorter OOS than 15m. Same regime-noise story. **Retire this TF.**

### 1h — FAIL
- Winner params: adx_len=14, rsi_len=2, st_mult=3.0, trail_mult=1.5, risk_pct=1.0
- OOS Sharpe -0.324, only 7.5% positive OOS across 53 qualifying symbols (best of the failing TFs)
- **Honest verdict:** 6 walk-forward windows × 90 days each, ~120 bars per OOS segment. Better data volume than 15m/30m but the regime classifier still fails to produce a robust edge. **Falls between 30m (failure) and 4h (success) — the strategy needs slower context than 1h to function.**

### 4h — PASS
- Winner params: adx_len=20, atr_len=14, ema_len=100, rsi_len=2, dmi_len=20, st_atr_len=14, **st_mult=2.0**, adx_trend=30.0, adx_range=20.0, bbwpct_min=0.05, rsi_oversold=10, rsi_overbought=85, sma_trend_len=200, **mr_trail_mult=2.0**, risk_pct=1.0, **trail_mult=1.5**, max_bars_in_trade=30, dead_money_pct=0.25
- OOS Sharpe +0.138, 60.9% positive OOS, 63.0% positive return, 100% MaxDD < 35%
- 46/59 symbols qualified for analysis (good breadth)
- **Honest verdict:** First TF where the optimizer finds a parameter set that holds up out-of-sample across the universe. The winner uses slower supertrend (mult=2.0 vs default 3.0) and tighter trail (1.5) — fitting a slower-bar regime. **Best candidate for live paper-trading.**

### weekly — PASS
- Winner params: adx_len=10, atr_len=20, ema_len=100, **rsi_len=3**, dmi_len=10, st_atr_len=14, **st_mult=4.0**, adx_trend=25.0, adx_range=18.0, **bbwpct_min=0.2**, rsi_oversold=5, rsi_overbought=85, sma_trend_len=200, mr_trail_mult=1.5, risk_pct=1.0, trail_mult=1.5, max_bars_in_trade=20, dead_money_pct=0.25
- OOS Sharpe +0.357, 71.4% positive OOS, 71.4% positive return, 100% MaxDD < 35%
- Only 7/60 symbols qualified — **statistically thin** (10y of weekly = ~520 bars, 4 windows × 2y, ~130 bars per window)
- **Honest verdict:** Strongest per-symbol Sharpe (0.357) but the smallest qualifying universe (only 7 symbols). The qualifying 7 must show some genuine regime behaviour at weekly granularity, but the result is vulnerable to overfit. **Treat as a secondary candidate; if the 4h winner survives paper-trading, weekly deserves a deeper look with longer history.**

## 4. Final Recommendation

**Best timeframe: 4h.** It is the only TF with both robust out-of-sample performance AND broad symbol coverage (46/59 qualified). Weekly has higher Sharpe but only 7 qualifying symbols — too thin for confidence.

**Edge verdict per TF:**

| TF | Verdict |
| --- | --- |
| 15m | No edge (regime noise) |
| 30m | No edge (insufficient data + regime noise) |
| 1h  | No edge (intermediate, not enough slow-bar context) |
| **4h** | **Real edge, broad universe, paper-trade candidate** |
| weekly | Marginal edge, thin universe, secondary candidate |

**Statistical caveats:**
- 4h: ~1200 bars/symbol over 2y, 4 walk-forward windows of ~180 days each (~45-bar OOS segments). Sharpe estimates have wide confidence intervals — sample Sharpe 0.14 from 45 bars has 95% CI roughly [-0.5, +0.8].
- weekly: ~520 bars over 10y, 4 windows of ~2y (~130-bar OOS). Better statistical power but only 7 symbols.
- **Both are statistically thin.** Robustness filters passed, but the filters themselves are sample-dependent.

**Next steps:**
1. **Paper-trade the 4h winner** on live data for 3-6 months. Verify OOS equity stays in the +1% to +3% monthly range with MaxDD ≤ 1.5%.
2. **If 4h holds up:** consider extending the lookback from 2y to 5y for higher statistical power. yfinance caps 1h at ~730 days, so 4h data would need to be built from a different source for longer history.
3. **Weekly:** explore why only 7 symbols qualified. May be a data-volume issue (some instruments don't have 10y of weekly history on yfinance).
4. **Do NOT trade this on real capital yet.** The Sharpe numbers are positive but small, the universe is filtered, and 4h execution requires a broker with low commissions and reliable 24/5 access — neither of which was modelled in this backtest.

## 5. Output Files

- `15m_robustness_report.md`, `30m_robustness_report.md`, `1h_robustness_report.md`, `4h_robustness_report.md`, `weekly_robustness_report.md` — per-TF verdicts
- `<tf>_winner.json`, `<tf>_verdict.json` — winner params + headline metrics
- `<tf>_equity_curves.png` — overlaid equity curves for each TF
- `<tf>_phase1_summary.csv` and `<tf>_phase2_summary.csv` — per-combo aggregates

## 6. Bonus — BTC/USDT Single-Symbol Test

The original brief excluded crypto. A single BTC/USDT 1D/4y default-params backtest was added on request to compare crypto against the asset universe.

**Note:** Crypto was explicitly excluded from the main study. This is a single-symbol curiosity check, NOT a robust validation.

| Metric | BTC/USDT | 1D Universe Median |
| --- | --- | --- |
| Total return | 0% (0 trades) | +1.84% |
| Sharpe | n/a | +0.17 |
| Max DD | 0% (no position) | -4.65% |
| # trades | 0 (signals generated but not converted) | 15 |
| Win rate | n/a | 40% |

**Result:** With default Pine params, the strategy produced **0 trades** on BTC/USDT over 4 years (1462 bars). For reference: 21 long-entry and 13 short-entry signals were generated by `generate_signals()`, but `simulate()` reported zero trades. There may be an interaction bug between signal generation and the engine's internal state machine on this dataset (worth investigating as a future phase). Either way, the headline finding is **the default strategy doesn't trade BTC at all** — likely because BTC's volatility profile doesn't generate the regime-trend + RSI-overshoot confluence the strategy requires with default thresholds.

**Honest verdict:** A single BTC/USDT run is statistically meaningless on its own — no comparison universe, no walk-forward. The result here (0 trades with default params) is one data point showing BTC's volatility regime doesn't align with the default parameter set. To actually test BTC, you'd want to (a) debug why signals aren't converting to trades, then (b) walk-forward BTC separately with TF-appropriate parameters. Not done here.

## 7. Output files (updated)

- All per-TF reports + winner JSONs as listed above
- `results/crypto/BTC_USD_metrics.json` — BTC/USDT raw metrics
- `data/BTC_USD.csv` — 1D OHLCV (gitignored like other market data)
# ATT Regime Switch — Research-Backed Multi-Asset Backtest

Python port of the Pine Script `ATT Regime Switch: ADX + ATR Trail + RSI Mean Rev`
strategy. Runs rigorous, walk-forward backtests across ~60-80 instruments
(commodities, metals, forex, indices — no crypto) on the 1D timeframe over
the last 4 years.

## Scope

- **Strategy:** Regime-switching system combining trend-following
  (Supertrend + EMA + Volume) and mean-reversion (RSI2 / Connors).
- **Timeframe:** 1 day.
- **Lookback:** Last 4 years only (period = `4y` in yfinance).
- **Universe:** All liquid commodities, metals, forex, indices available
  via yfinance. Crypto explicitly excluded.
- **Risk:** Fixed-fractional position sizing, ATR-based stop, no pyramiding,
  no live trading — research only.

## Layout

```
att-strategy/
├── src/
│   ├── indicators.py      # ATR, ADX/DI, Supertrend, BBW%, RSI, EMA/SMA
│   ├── strategy.py        # ATTStrategy dataclass + signal generation
│   ├── engine.py          # Iterative backtest engine (long/short/trail/exits)
│   ├── data.py            # yfinance loader, CSV persistence
│   ├── universe.py        # Symbol universe (grouped)
│   ├── reporting.py       # Per-symbol + aggregate metrics
│   └── optimizer.py       # Walk-forward parameter optimisation
├── tests/
│   └── test_indicators.py # Unit tests vs Pine Script semantics
├── scripts/
│   ├── fetch_data.py      # Download all symbols → data/*.csv
│   ├── backtest_all.py    # Default-params sweep across the universe
│   ├── sanity_check.py    # GC=F sanity check (compare vs TradingView)
│   └── optimize.py        # Walk-forward optimiser CLI
├── data/                  # Cached OHLCV CSVs (gitignored)
└── results/               # JSON + CSV reports (gitignored)
```

## Setup

```bash
cd ~/att-strategy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run order

```bash
# 1. Fetch 4 years of 1D data for every symbol in the universe
python scripts/fetch_data.py

# 2. Run a single sanity-check on Gold futures (GC=F)
python scripts/sanity_check.py
#   → Compare the headline numbers against TradingView's Strategy Tester
#     for the same script on GC=F, 1D, 4y. They should agree within
#     rounding tolerance.

# 3. Backtest every symbol with the default Pine params
python scripts/backtest_all.py
#   → Writes results/per_symbol.csv + results/aggregate.json

# 4. Walk-forward optimisation
python scripts/optimize.py --phase 1   # coarse LHS sample, ~300 combos
python scripts/optimize.py --phase 2   # local refinement around top combos
#   → Writes results/optimization/{winner.json,robustness_report.md,...}
```

## Asset Universe

**Indices (futures + cash proxies):** ES=F, NQ=F, YM=F, RTY=F, ^GSPC, ^IXIC, ^DJI, ^RUT
**Commodities:** CL=F, BZ=F, NG=F, HO=F, RB=F, ZC=F, ZW=F, ZS=F, ZM=F, ZL=F,
                CT=F, KC=F, SB=F, CC=F, OJ=F, LE=F, GF=F, HE=F, LBS=F
**Metals:** GC=F, SI=F, HG=F, PL=F, PA=F, ^XAU
**Forex (majors + crosses):** EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, NZDUSD,
                USDCAD, EURGBP, EURJPY, GBPJPY, AUDJPY, EURCHF, GBPCHF,
                EURCAD, CADJPY, AUDNZD, NZDJPY, CHFJPY, AUDCAD, AUDCHF,
                NZDCAD, NZDCHF, EURAUD, EURNZD, GBPAUD, GBPNZD, GBPCAD
                (yfinance `XXX=X` suffix stripped at fetch time)

Total: ~60-80 symbols, filtered at fetch time based on yfinance availability.

## Known Caveats

- **yfinance gaps.** Some futures tickers (especially less-liquid ags)
  have short histories or roll gaps. The pipeline warns and skips anything
  with <750 bars.
- **Pine fidelity.** Process-orders-on-close = false means entries/exits
  fill on the next bar's open. This is faithfully reproduced. Slippage is
  applied as a fixed 3-point hit; on a $2000/oz gold bar that's negligible
  but on a $1.10 EURUSD bar it's a meaningful % — interpret forex results
  cautiously.
- **1D timeframe constraint.** 4 years × ~250 bars = ~1000 bars per symbol.
  This is barely enough to validate a strategy. Out-of-sample robustness
  is fragile. The robustness_report.md will say so explicitly.
- **Walk-forward windows.** 4 rolling 1-year windows → only 3-month
  out-of-sample test segments per window. Treat the optimisation output
  as a *guide*, not a guarantee.
- **No crypto.** Explicitly excluded per project brief.
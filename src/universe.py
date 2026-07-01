"""Asset universe for the ATT Regime Switch backtest.

NO crypto. Grouped: indices, commodities, metals, forex.
Aim ~60-80 symbols.
"""

# Indices — equity futures first, then cash-index proxies
INDICES_FUTURES = [
    "ES=F",  # S&P 500 e-mini
    "NQ=F",  # Nasdaq 100 e-mini
    "YM=F",  # Dow e-mini
    "RTY=F", # Russell 2000 e-mini
]

INDICES_CASH = [
    "^GSPC", # S&P 500
    "^IXIC", # Nasdaq Composite
    "^DJI",  # Dow Jones Industrial
    "^RUT",  # Russell 2000
]

INDICES = INDICES_FUTURES + INDICES_CASH

# Commodities — energy, grains, softs, meats
COMMODITIES = [
    # Energy
    "CL=F",  # WTI Crude
    "BZ=F",  # Brent Crude
    "NG=F",  # Natural Gas
    "HO=F",  # Heating Oil
    "RB=F",  # RBOB Gasoline
    # Grains
    "ZC=F",  # Corn
    "ZW=F",  # Wheat
    "ZS=F",  # Soybeans
    "ZM=F",  # Soybean Meal
    "ZL=F",  # Soybean Oil
    # Softs
    "CT=F",  # Cotton
    "KC=F",  # Coffee
    "SB=F",  # Sugar
    "CC=F",  # Cocoa
    "OJ=F",  # Orange Juice
    # Meats
    "LE=F",  # Live Cattle
    "GF=F",  # Feeder Cattle
    "HE=F",  # Lean Hogs
    "LBS=F", # Lumber
]

# Metals — futures + spot proxies
METALS = [
    "GC=F",  # Gold
    "SI=F",  # Silver
    "HG=F",  # Copper
    "PL=F",  # Platinum
    "PA=F",  # Palladium
    "^XAU",  # Gold spot proxy (Philadelphia Gold Index) - not always available
]

# Forex — majors + crosses (yfinance format: XXX=X)
FOREX_MAJORS = [
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "USDCHF=X",
    "AUDUSD=X",
    "NZDUSD=X",
    "USDCAD=X",
]

FOREX_CROSSES = [
    "EURJPY=X",
    "GBPJPY=X",
    "AUDJPY=X",
    "CADJPY=X",
    "NZDJPY=X",
    "CHFJPY=X",
    "EURGBP=X",
    "EURCHF=X",
    "GBPCHF=X",
    "EURCAD=X",
    "EURAUD=X",
    "EURNZD=X",
    "GBPAUD=X",
    "GBPNZD=X",
    "GBPCAD=X",
    "AUDNZD=X",
    "AUDCAD=X",
    "AUDCHF=X",
    "NZDCAD=X",
    "NZDCHF=X",
]

FOREX = FOREX_MAJORS + FOREX_CROSSES


def all_symbols():
    """Return the full ordered list of symbols (no crypto)."""
    return INDICES_FUTURES + COMMODITIES + METALS + FOREX_MAJORS + FOREX_CROSSES + INDICES_CASH


# Convenience groupings
GROUPS = {
    "indices": INDICES,
    "commodities": COMMODITIES,
    "metals": METALS,
    "forex": FOREX,
}


if __name__ == "__main__":
    syms = all_symbols()
    print(f"Total symbols: {len(syms)}")
    for g, lst in GROUPS.items():
        print(f"  {g}: {len(lst)}")

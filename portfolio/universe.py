"""
Universe Selection — Equity Universe for Portfolio Allocation
═════════════════════════════════════════════════════════════
Top 100 S&P500 stocks + major sector/index ETFs.
"""

from __future__ import annotations
from typing import List, Dict

# ── Major ETFs (core allocation candidates) ──
ETFS = [
    "SPY",   # S&P 500
    "QQQ",   # Nasdaq 100
    "IWM",   # Russell 2000
    "DIA",   # Dow Jones
    "XLK",   # Technology
    "XLF",   # Financials
    "XLV",   # Healthcare
    "XLE",   # Energy
    "XLI",   # Industrials
    "XLP",   # Consumer Staples
    "XLY",   # Consumer Discretionary
    "XLU",   # Utilities
    "XLB",   # Materials
    "XLRE",  # Real Estate
    "XLC",   # Communication Services
    "VTI",   # Total US Market
    "VEA",   # Developed International
    "VWO",   # Emerging Markets
    "BND",   # US Aggregate Bond
    "TLT",   # 20+ Year Treasury
    "GLD",   # Gold
    "SLV",   # Silver
    "VNQ",   # Real Estate
    "HYG",   # High Yield Corporate Bond
]

# ── Top ~60 S&P500 by market cap (stable, liquid) ──
SP500_TOP = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "BRK-B", "LLY",
    "AVGO", "JPM", "TSLA", "UNH", "XOM", "V", "MA", "PG",
    "COST", "JNJ", "HD", "ABBV", "WMT", "NFLX", "BAC", "CRM",
    "CVX", "MRK", "KO", "AMD", "PEP", "TMO", "LIN", "CSCO",
    "ACN", "MCD", "ABT", "ADBE", "DHR", "WFC", "TXN", "PM",
    "NEE", "QCOM", "AMGN", "INTU", "CMCSA", "HON", "IBM", "AMAT",
    "UNP", "CAT", "GS", "LOW", "DE", "RTX", "BLK", "ISRG",
    "SPGI", "AXP", "SYK", "PFE",
]

# ── Complete universe ──
FULL_UNIVERSE: List[str] = ETFS + SP500_TOP

# ── Subsets for faster testing ──
CORE_ETFS = ["SPY", "QQQ", "XLK", "XLF", "XLV", "XLE", "XLI", "XLP",
             "XLY", "XLU", "GLD", "TLT", "VEA", "VWO", "BND"]

CORE_STOCKS = ["AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "JPM",
               "UNH", "V", "JNJ", "PG", "XOM", "HD", "MA", "LLY"]

# ── Default universe (balanced: 15 ETFs + 15 stocks = 30 tickers) ──
DEFAULT_UNIVERSE: List[str] = CORE_ETFS + CORE_STOCKS

# ── Asset metadata ──
ASSET_TYPE: Dict[str, str] = {}
for t in ETFS:
    ASSET_TYPE[t] = "ETF"
for t in SP500_TOP:
    ASSET_TYPE[t] = "STOCK"
# Override specific
ASSET_TYPE["GLD"] = "COMMODITY"
ASSET_TYPE["SLV"] = "COMMODITY"
ASSET_TYPE["BND"] = "BOND"
ASSET_TYPE["TLT"] = "BOND"
ASSET_TYPE["HYG"] = "BOND"

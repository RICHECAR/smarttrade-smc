"""
Data Loader — Fetch historical equity data via yfinance
════════════════════════════════════════════════════════
Downloads daily OHLCV for a universe of tickers.
Caches to local CSV to avoid repeated API calls.
"""

from __future__ import annotations

import os
import logging
import pandas as pd
import numpy as np
from typing import List, Optional, Dict
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "cache"


def fetch_prices(
    tickers: List[str],
    period: str = "5y",
    cache: bool = True,
) -> pd.DataFrame:
    """
    Fetch adjusted close prices for a list of tickers.

    Returns:
        DataFrame with DatetimeIndex, columns = tickers, values = adj close.
        Tickers that fail to download are silently dropped.
    """
    import yfinance as yf

    cache_file = CACHE_DIR / f"prices_{period}_{len(tickers)}.csv"

    if cache and cache_file.exists():
        age_hours = (pd.Timestamp.now() - pd.Timestamp(cache_file.stat().st_mtime, unit="s")).total_seconds() / 3600
        if age_hours < 12:
            logger.info(f"Loading cached prices ({cache_file.name})")
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            # Check if all requested tickers are present
            missing = set(tickers) - set(df.columns)
            if not missing:
                return df[tickers]

    logger.info(f"Downloading {len(tickers)} tickers, period={period}...")
    raw = yf.download(tickers, period=period, auto_adjust=True, progress=False, threads=True)

    if raw.empty:
        logger.error("No data downloaded")
        return pd.DataFrame()

    # Extract Close prices
    if isinstance(raw.columns, pd.MultiIndex):
        prices = raw["Close"].copy()
    else:
        prices = raw[["Close"]].copy()
        prices.columns = tickers[:1]

    # Drop tickers with >30% missing
    threshold = len(prices) * 0.3
    valid_cols = [c for c in prices.columns if prices[c].notna().sum() > threshold]
    prices = prices[valid_cols]

    # Forward-fill gaps (weekends/holidays already excluded by yfinance)
    prices = prices.ffill().dropna(how="all")

    # Ensure index is timezone-naive datetime
    if prices.index.tz is not None:
        prices.index = prices.index.tz_localize(None)

    logger.info(f"Got {len(prices)} days for {len(prices.columns)} tickers "
                f"({len(tickers) - len(prices.columns)} dropped)")

    # Cache
    if cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        prices.to_csv(cache_file)

    return prices


def fetch_total_return(
    tickers: List[str],
    period: str = "5y",
) -> pd.DataFrame:
    """Fetch daily returns (pct change of adjusted close)."""
    prices = fetch_prices(tickers, period=period)
    if prices.empty:
        return pd.DataFrame()
    return prices.pct_change().iloc[1:]

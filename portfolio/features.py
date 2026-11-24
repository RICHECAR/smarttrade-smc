"""
Feature Engine — Per-asset feature computation
═══════════════════════════════════════════════
Computes: momentum (1M, 3M, 6M), volatility, drawdown,
trend strength, relative strength vs benchmark.

All features are computed on a rolling basis over a DataFrame
of daily prices [DatetimeIndex × tickers].
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional


@dataclass
class FeatureConfig:
    """Configuration for feature calculations."""
    momentum_windows: tuple = (21, 63, 126)   # 1M, 3M, 6M trading days
    volatility_window: int = 63               # 3M annualized volatility
    drawdown_window: int = 252                # 1Y rolling max drawdown
    trend_window: int = 63                    # 3M trend (slope of log-price)
    rs_window: int = 63                       # Relative strength period
    benchmark: str = "SPY"                    # RS benchmark


def compute_momentum(prices: pd.DataFrame, windows: tuple = (21, 63, 126)) -> dict[str, pd.DataFrame]:
    """
    Compute momentum as total return over lookback windows.
    Returns dict: {"mom_21": DataFrame, "mom_63": DataFrame, ...}
    """
    result = {}
    for w in windows:
        result[f"mom_{w}"] = prices.pct_change(w)
    return result


def compute_volatility(prices: pd.DataFrame, window: int = 63) -> pd.DataFrame:
    """
    Annualized rolling volatility of daily returns.
    Lower is better for allocation (stability).
    """
    returns = prices.pct_change()
    return returns.rolling(window).std() * np.sqrt(252)


def compute_drawdown(prices: pd.DataFrame, window: int = 252) -> pd.DataFrame:
    """
    Rolling max drawdown over window.
    Returns negative values (e.g., -0.15 = -15% drawdown).
    """
    rolling_max = prices.rolling(window, min_periods=1).max()
    drawdown = (prices - rolling_max) / rolling_max
    return drawdown


def compute_trend_strength(prices: pd.DataFrame, window: int = 63) -> pd.DataFrame:
    """
    Trend strength = R² of linear regression on log-prices.
    1.0 = perfect trend, 0.0 = no trend.
    Captures how linear/smooth the price trajectory is.
    """
    log_prices = np.log(prices)
    result = pd.DataFrame(index=prices.index, columns=prices.columns, dtype=float)

    for col in prices.columns:
        series = log_prices[col].dropna()
        r2_vals = pd.Series(index=series.index, dtype=float)
        x = np.arange(window, dtype=float)
        x_mean = x.mean()
        ss_xx = ((x - x_mean) ** 2).sum()

        for i in range(window - 1, len(series)):
            y = series.iloc[i - window + 1:i + 1].values
            if len(y) < window:
                continue
            y_mean = y.mean()
            ss_yy = ((y - y_mean) ** 2).sum()
            ss_xy = ((x - x_mean) * (y - y_mean)).sum()
            if ss_yy > 0:
                r2_vals.iloc[i] = (ss_xy ** 2) / (ss_xx * ss_yy)
            else:
                r2_vals.iloc[i] = 0.0
        result[col] = r2_vals

    return result


def compute_relative_strength(
    prices: pd.DataFrame,
    benchmark: str = "SPY",
    window: int = 63,
) -> pd.DataFrame:
    """
    Relative strength = asset_return / benchmark_return over window.
    > 1.0 means outperforming benchmark.
    """
    if benchmark not in prices.columns:
        return pd.DataFrame(index=prices.index, columns=prices.columns, dtype=float)

    bench_ret = prices[benchmark].pct_change(window)
    rs = pd.DataFrame(index=prices.index, columns=prices.columns, dtype=float)
    for col in prices.columns:
        asset_ret = prices[col].pct_change(window)
        # RS ratio: (1 + asset_ret) / (1 + bench_ret)
        rs[col] = (1 + asset_ret) / (1 + bench_ret)
    return rs


def compute_all_features(
    prices: pd.DataFrame,
    config: FeatureConfig = None,
) -> dict[str, pd.DataFrame]:
    """
    Compute all features for the universe.

    Returns dict of feature DataFrames, each with same index/columns as prices.
    """
    if config is None:
        config = FeatureConfig()

    features = {}

    # Momentum
    mom = compute_momentum(prices, config.momentum_windows)
    features.update(mom)

    # Volatility
    features["volatility"] = compute_volatility(prices, config.volatility_window)

    # Drawdown
    features["drawdown"] = compute_drawdown(prices, config.drawdown_window)

    # Trend strength
    features["trend_r2"] = compute_trend_strength(prices, config.trend_window)

    # Relative strength
    features["rel_strength"] = compute_relative_strength(
        prices, config.benchmark, config.rs_window,
    )

    return features

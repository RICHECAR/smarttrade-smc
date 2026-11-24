"""
Risk Management — Portfolio-level risk controls
════════════════════════════════════════════════
Volatility targeting, max drawdown stops, cash overlay.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass


@dataclass
class RiskConfig:
    vol_target: float = 0.12         # 12% annualized portfolio vol target
    max_drawdown: float = 0.20       # 20% max drawdown → go to cash
    drawdown_recovery: float = 0.10  # Resume investing when DD recovers to -10%
    vol_lookback: int = 63           # 3M window for realized vol estimation


def vol_scaling_factor(
    portfolio_returns: pd.Series,
    config: RiskConfig,
) -> float:
    """
    Returns a scalar in (0, 1] to scale allocation by.
    If realized vol > target, reduce exposure proportionally.
    """
    if len(portfolio_returns) < 21:
        return 1.0

    recent = portfolio_returns.iloc[-config.vol_lookback:]
    realized_vol = recent.std() * np.sqrt(252)

    if realized_vol <= 0 or np.isnan(realized_vol):
        return 1.0

    ratio = config.vol_target / realized_vol
    return float(np.clip(ratio, 0.0, 1.0))


def check_drawdown_stop(
    equity_curve: pd.Series,
    config: RiskConfig,
) -> bool:
    """
    Returns True if portfolio should be in cash (drawdown > threshold).
    """
    if len(equity_curve) < 2:
        return False

    peak = equity_curve.cummax()
    dd = (equity_curve - peak) / peak
    current_dd = dd.iloc[-1]

    return current_dd < -config.max_drawdown


def check_drawdown_recovery(
    equity_curve: pd.Series,
    config: RiskConfig,
) -> bool:
    """
    Returns True if portfolio has recovered enough to re-enter.
    """
    if len(equity_curve) < 2:
        return True

    peak = equity_curve.cummax()
    dd = (equity_curve - peak) / peak
    current_dd = dd.iloc[-1]

    return current_dd > -config.drawdown_recovery


def apply_risk_overlay(
    weights: dict[str, float],
    portfolio_returns: pd.Series,
    equity_curve: pd.Series,
    in_cash: bool,
    config: RiskConfig = None,
) -> tuple[dict[str, float], bool]:
    """
    Apply risk overlay to portfolio weights.

    Returns:
        (adjusted_weights, new_in_cash_state)
    """
    if config is None:
        config = RiskConfig()

    # Check max drawdown stop
    if not in_cash and check_drawdown_stop(equity_curve, config):
        # Go to cash
        return {}, True

    if in_cash:
        # Only re-enter if DD has recovered
        if check_drawdown_recovery(equity_curve, config):
            in_cash = False
        else:
            return {}, True

    # Vol scaling
    scale = vol_scaling_factor(portfolio_returns, config)
    if scale < 1.0:
        weights = {t: w * scale for t, w in weights.items()}
        # Remainder is cash (implicit)

    return weights, in_cash

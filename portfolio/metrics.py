"""
Metrics — Portfolio performance measurement
════════════════════════════════════════════
CAGR, Sharpe, Sortino, Max Drawdown, Calmar, Turnover.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass


@dataclass
class MetricResult:
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float
    annual_vol: float
    total_return: float
    avg_turnover: float  # Average one-way turnover per rebalance


def compute_cagr(equity_curve: pd.Series) -> float:
    """Compound Annual Growth Rate."""
    if len(equity_curve) < 2:
        return 0.0
    total_return = equity_curve.iloc[-1] / equity_curve.iloc[0]
    if total_return <= 0:
        return -1.0
    n_days = (equity_curve.index[-1] - equity_curve.index[0]).days
    if n_days <= 0:
        return 0.0
    years = n_days / 365.25
    return total_return ** (1.0 / years) - 1.0


def compute_sharpe(returns: pd.Series, rf_annual: float = 0.04) -> float:
    """Annualized Sharpe ratio (daily returns → annual)."""
    if len(returns) < 30:
        return 0.0
    mean_daily = returns.mean()
    std_daily = returns.std()
    if std_daily <= 0:
        return 0.0
    rf_daily = (1 + rf_annual) ** (1 / 252) - 1
    return (mean_daily - rf_daily) / std_daily * np.sqrt(252)


def compute_sortino(returns: pd.Series, rf_annual: float = 0.04) -> float:
    """Sortino ratio: penalizes only downside deviation."""
    if len(returns) < 30:
        return 0.0
    rf_daily = (1 + rf_annual) ** (1 / 252) - 1
    excess = returns - rf_daily
    downside = excess[excess < 0]
    if len(downside) == 0:
        return float("inf")
    downside_std = np.sqrt((downside ** 2).mean()) * np.sqrt(252)
    if downside_std <= 0:
        return 0.0
    annual_excess = (returns.mean() - rf_daily) * 252
    return annual_excess / downside_std


def compute_max_drawdown(equity_curve: pd.Series) -> float:
    """Max drawdown as a positive number (e.g., 0.25 = 25%)."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve.cummax()
    dd = (equity_curve - peak) / peak
    return abs(dd.min())


def compute_annual_vol(returns: pd.Series) -> float:
    """Annualized volatility."""
    if len(returns) < 2:
        return 0.0
    return returns.std() * np.sqrt(252)


def compute_all_metrics(
    equity_curve: pd.Series,
    returns: pd.Series,
    turnover_series: pd.Series | None = None,
    rf_annual: float = 0.04,
) -> MetricResult:
    """Compute all portfolio metrics."""
    cagr = compute_cagr(equity_curve)
    sharpe = compute_sharpe(returns, rf_annual)
    sortino = compute_sortino(returns, rf_annual)
    max_dd = compute_max_drawdown(equity_curve)
    calmar = cagr / max_dd if max_dd > 0 else 0.0
    ann_vol = compute_annual_vol(returns)
    total_ret = equity_curve.iloc[-1] / equity_curve.iloc[0] - 1 if len(equity_curve) >= 2 else 0.0
    avg_turnover = turnover_series.mean() if turnover_series is not None and len(turnover_series) > 0 else 0.0

    return MetricResult(
        cagr=cagr,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        calmar=calmar,
        annual_vol=ann_vol,
        total_return=total_ret,
        avg_turnover=avg_turnover,
    )


def format_metrics(m: MetricResult, label: str = "Strategy") -> str:
    """Pretty-print metrics."""
    lines = [
        f"{'=' * 50}",
        f"  {label}",
        f"{'=' * 50}",
        f"  CAGR           : {m.cagr:>8.2%}",
        f"  Sharpe          : {m.sharpe:>8.2f}",
        f"  Sortino         : {m.sortino:>8.2f}",
        f"  Max Drawdown    : {m.max_drawdown:>8.2%}",
        f"  Calmar          : {m.calmar:>8.2f}",
        f"  Annual Vol      : {m.annual_vol:>8.2%}",
        f"  Total Return    : {m.total_return:>8.2%}",
        f"  Avg Turnover    : {m.avg_turnover:>8.2%}",
        f"{'=' * 50}",
    ]
    return "\n".join(lines)

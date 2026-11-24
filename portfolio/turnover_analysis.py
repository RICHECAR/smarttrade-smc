"""
Turnover & Cost Analysis — Detailed cost impact measurement
════════════════════════════════════════════════════════════
Monthly/annual turnover, cost drag, position stability, fiscal impact.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from portfolio.backtest import BacktestResult
from portfolio.metrics import MetricResult


def compute_turnover_stats(result: BacktestResult) -> dict:
    """Compute detailed turnover statistics from backtest result."""
    ts = result.turnover_series
    rebal_turnover = ts[ts > 0]

    if len(rebal_turnover) == 0:
        return {"monthly_avg": 0, "annual_est": 0, "max_single": 0, "min_single": 0,
                "cost_per_rebal_bps": 0, "total_cost_drag_pct": 0, "n_rebalances": 0}

    # Time span
    days = (ts.index[-1] - ts.index[0]).days
    years = max(days / 365.25, 0.01)
    months = years * 12

    n_rebal = len(rebal_turnover)
    monthly_avg = rebal_turnover.sum() / months
    annual_est = rebal_turnover.sum() / years

    return {
        "n_rebalances": n_rebal,
        "monthly_avg_turnover": monthly_avg,
        "annual_turnover": annual_est,
        "max_single_turnover": rebal_turnover.max(),
        "min_single_turnover": rebal_turnover.min(),
        "median_turnover": rebal_turnover.median(),
        "total_cost_drag": result.cost_drag,
        "cost_drag_annual_pct": result.cost_drag / years,
    }


def compute_position_stability(result: BacktestResult) -> dict:
    """Measure how stable the portfolio positions are over time."""
    pos = result.positions_history
    if pos.empty:
        return {}

    # Find tickers that were ever held
    held = pos.columns[(pos > 0.01).any()]
    if len(held) == 0:
        return {"unique_tickers_held": 0}

    # Average holding duration per ticker
    in_portfolio = (pos[held] > 0.01).astype(int)
    avg_holding_days = in_portfolio.sum() / in_portfolio.shape[0]

    # How many unique tickers were held
    unique_held = len(held)

    # Average position count per date
    avg_positions = (pos > 0.01).sum(axis=1).mean()

    # Concentration: average max weight
    max_weights = pos.max(axis=1)
    avg_max_weight = max_weights[max_weights > 0].mean()

    return {
        "unique_tickers_held": unique_held,
        "avg_positions_per_day": avg_positions,
        "avg_max_weight": avg_max_weight,
        "avg_holding_pct": avg_holding_days.mean(),
        "most_held": avg_holding_days.sort_values(ascending=False).head(5).to_dict(),
    }


def estimate_tax_impact(
    result: BacktestResult,
    short_term_rate: float = 0.30,  # 30% short-term cap gains
    long_term_rate: float = 0.15,   # 15% long-term
) -> dict:
    """
    Rough tax impact estimate.
    Assumes: high turnover → short-term, low turnover → long-term.
    This is an approximation, not tax advice.
    """
    ts = compute_turnover_stats(result)
    annual_turnover = ts.get("annual_turnover", 0)

    # If annual turnover > 100%, most gains are short-term
    # If < 30%, most are long-term
    short_pct = min(1.0, annual_turnover / 1.5)
    long_pct = 1.0 - short_pct

    cagr = result.strategy_metrics.cagr
    blended_tax = short_pct * short_term_rate + long_pct * long_term_rate
    after_tax_cagr = cagr * (1 - blended_tax)

    return {
        "blended_tax_rate": blended_tax,
        "short_term_pct": short_pct,
        "pre_tax_cagr": cagr,
        "estimated_after_tax_cagr": after_tax_cagr,
        "tax_drag_annual": cagr - after_tax_cagr,
    }


def format_turnover_analysis(result: BacktestResult) -> str:
    """Format complete turnover and cost analysis."""
    ts = compute_turnover_stats(result)
    ps = compute_position_stability(result)
    tax = estimate_tax_impact(result)

    lines = [
        "",
        "╔══════════════════════════════════════════════════════════╗",
        "║           TURNOVER & COST ANALYSIS                      ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Rebalances          : {ts['n_rebalances']:>6}                            ║",
        f"║  Monthly Avg Turnover: {ts['monthly_avg_turnover']:>6.1%}                            ║",
        f"║  Annual Turnover     : {ts['annual_turnover']:>6.1%}                            ║",
        f"║  Max Single Turnover : {ts['max_single_turnover']:>6.1%}                            ║",
        f"║  Median Turnover     : {ts['median_turnover']:>6.1%}                            ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Total Cost Drag     : {ts['total_cost_drag']:>8.4f}                          ║",
        f"║  Annual Cost Drag    : {ts['cost_drag_annual_pct']:>6.2%}                            ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Unique Tickers Held : {ps.get('unique_tickers_held', 0):>6}                            ║",
        f"║  Avg Positions/Day   : {ps.get('avg_positions_per_day', 0):>6.1f}                            ║",
        f"║  Avg Max Weight      : {ps.get('avg_max_weight', 0):>6.1%}                            ║",
        f"║  Avg Holding Pct     : {ps.get('avg_holding_pct', 0):>6.1%}                            ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Pre-Tax CAGR        : {tax['pre_tax_cagr']:>6.2%}                            ║",
        f"║  Blended Tax Rate    : {tax['blended_tax_rate']:>6.1%}                            ║",
        f"║  Estimated After-Tax : {tax['estimated_after_tax_cagr']:>6.2%}                            ║",
        f"║  Tax Drag            : {tax['tax_drag_annual']:>6.2%}                            ║",
        "╚══════════════════════════════════════════════════════════╝",
    ]

    # Most held tickers
    if "most_held" in ps:
        lines.append("\n  Most-held tickers:")
        for t, pct in ps["most_held"].items():
            lines.append(f"    {t}: in portfolio {pct:.0%} of time")

    return "\n".join(lines)

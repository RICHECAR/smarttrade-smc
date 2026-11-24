"""
Institutional Reporting — Comprehensive per-run analysis report
═══════════════════════════════════════════════════════════════
Portfolio composition, factor contribution, rolling metrics, attribution.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from portfolio.backtest import BacktestResult
from portfolio.metrics import compute_cagr, compute_sharpe, compute_max_drawdown


def rolling_sharpe(returns: pd.Series, window: int = 126, rf_annual: float = 0.04) -> pd.Series:
    """Rolling annualized Sharpe ratio."""
    rf_daily = (1 + rf_annual) ** (1 / 252) - 1
    excess = returns - rf_daily
    rolling_mean = excess.rolling(window).mean()
    rolling_std = returns.rolling(window).std()
    return (rolling_mean / rolling_std * np.sqrt(252)).dropna()


def rolling_cagr(equity_curve: pd.Series, window: int = 252) -> pd.Series:
    """Rolling 1-year CAGR."""
    result = pd.Series(index=equity_curve.index, dtype=float)
    for i in range(window, len(equity_curve)):
        start_val = equity_curve.iloc[i - window]
        end_val = equity_curve.iloc[i]
        if start_val > 0:
            result.iloc[i] = (end_val / start_val) - 1.0
    return result.dropna()


def monthly_returns(returns: pd.Series) -> pd.Series:
    """Aggregate daily returns into monthly returns."""
    monthly = (1 + returns).resample("ME").prod() - 1
    return monthly


def hit_ratio_monthly(returns: pd.Series) -> float:
    """Fraction of months with positive returns."""
    mr = monthly_returns(returns)
    if len(mr) == 0:
        return 0.0
    return (mr > 0).mean()


def worst_quarter(returns: pd.Series) -> float:
    """Worst quarterly return."""
    quarterly = (1 + returns).resample("QE").prod() - 1
    if len(quarterly) == 0:
        return 0.0
    return quarterly.min()


def best_quarter(returns: pd.Series) -> float:
    """Best quarterly return."""
    quarterly = (1 + returns).resample("QE").prod() - 1
    if len(quarterly) == 0:
        return 0.0
    return quarterly.max()


def correlation_to_benchmark(strat_returns: pd.Series, bench_returns: pd.Series) -> float:
    """Correlation of strategy to benchmark returns."""
    aligned = pd.DataFrame({"s": strat_returns, "b": bench_returns}).dropna()
    if len(aligned) < 30:
        return 0.0
    return aligned["s"].corr(aligned["b"])


def drawdown_path(equity_curve: pd.Series) -> pd.Series:
    """Full drawdown time series."""
    peak = equity_curve.cummax()
    return (equity_curve - peak) / peak


def contribution_by_position(
    positions_history: pd.DataFrame,
    daily_returns: pd.DataFrame,
) -> pd.Series:
    """
    Average contribution of each ticker to portfolio return.
    Returns Series: ticker → avg daily contribution.
    """
    # Align
    common_dates = positions_history.index.intersection(daily_returns.index)
    common_cols = positions_history.columns.intersection(daily_returns.columns)
    if len(common_dates) == 0 or len(common_cols) == 0:
        return pd.Series(dtype=float)

    pos = positions_history.loc[common_dates, common_cols]
    rets = daily_returns.loc[common_dates, common_cols]

    contrib = (pos * rets).mean()
    return contrib.sort_values(ascending=False)


def generate_institutional_report(
    result: BacktestResult,
    prices: pd.DataFrame,
) -> str:
    """Generate comprehensive institutional-grade report."""
    s = result.strategy_metrics
    b = result.benchmark_metrics

    lines = [
        "",
        "╔══════════════════════════════════════════════════════════════════════╗",
        "║              INSTITUTIONAL PORTFOLIO REPORT                         ║",
        "╠══════════════════════════════════════════════════════════════════════╣",
    ]

    # ── 1. Performance summary ──
    lines.append("║  1. PERFORMANCE SUMMARY")
    lines.append(f"║     CAGR              : {s.cagr:>8.2%}  (bench: {b.cagr:.2%})")
    lines.append(f"║     Sharpe            : {s.sharpe:>8.2f}  (bench: {b.sharpe:.2f})")
    lines.append(f"║     Sortino           : {s.sortino:>8.2f}  (bench: {b.sortino:.2f})")
    lines.append(f"║     Max Drawdown      : {s.max_drawdown:>8.2%}  (bench: {b.max_drawdown:.2%})")
    lines.append(f"║     Calmar            : {s.calmar:>8.2f}  (bench: {b.calmar:.2f})")
    lines.append(f"║     Annual Vol        : {s.annual_vol:>8.2%}  (bench: {b.annual_vol:.2%})")
    lines.append(f"║     Total Return      : {s.total_return:>8.2%}  (bench: {b.total_return:.2%})")

    # ── 2. Monthly hit ratio ──
    hr = hit_ratio_monthly(result.returns)
    wq = worst_quarter(result.returns)
    bq = best_quarter(result.returns)
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("║  2. MONTHLY ANALYSIS")
    lines.append(f"║     Monthly Hit Ratio : {hr:>8.1%}")
    lines.append(f"║     Worst Quarter     : {wq:>8.2%}")
    lines.append(f"║     Best Quarter      : {bq:>8.2%}")

    # ── 3. Correlation ──
    corr = correlation_to_benchmark(result.returns, result.benchmark_returns)
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("║  3. BENCHMARK CORRELATION")
    lines.append(f"║     Corr to SPY       : {corr:>8.3f}")

    # ── 4. Rolling metrics ──
    rs = rolling_sharpe(result.returns, window=126)
    rc = rolling_cagr(result.equity_curve, window=252)
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("║  4. ROLLING METRICS (6M Sharpe / 1Y CAGR)")
    if len(rs) > 0:
        lines.append(f"║     Rolling Sharpe    : mean={rs.mean():.2f}  min={rs.min():.2f}  max={rs.max():.2f}")
        pct_positive = (rs > 0).mean()
        lines.append(f"║     % positive Sharpe : {pct_positive:.1%}")
    if len(rc) > 0:
        lines.append(f"║     Rolling CAGR      : mean={rc.mean():.2%}  min={rc.min():.2%}  max={rc.max():.2%}")

    # ── 5. Drawdown path ──
    dd = drawdown_path(result.equity_curve)
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("║  5. DRAWDOWN ANALYSIS")
    lines.append(f"║     Current DD        : {dd.iloc[-1]:>8.2%}")
    lines.append(f"║     Max DD            : {dd.min():>8.2%}")
    # Time underwater
    underwater = (dd < -0.05).mean()
    lines.append(f"║     % time DD > 5%    : {underwater:>8.1%}")

    # ── 6. Position contribution ──
    daily_rets = prices.pct_change().fillna(0.0)
    contrib = contribution_by_position(result.positions_history, daily_rets)
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("║  6. TOP CONTRIBUTORS (avg daily contribution)")
    for t, c in contrib.head(5).items():
        lines.append(f"║     {t:<8}: {c:>+10.5f} ({c*252:>+.2%} annualized)")
    if len(contrib) > 5:
        lines.append("║     ... (bottom)")
        for t, c in contrib.tail(3).items():
            lines.append(f"║     {t:<8}: {c:>+10.5f} ({c*252:>+.2%} annualized)")

    # ── 7. Portfolio composition snapshot (last date) ──
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("║  7. FINAL PORTFOLIO COMPOSITION")
    if not result.positions_history.empty:
        last_pos = result.positions_history.iloc[-1]
        held = last_pos[last_pos > 0.01].sort_values(ascending=False)
        for t, w in held.items():
            lines.append(f"║     {t:<8}: {w:>6.1%}")
        if len(held) == 0:
            lines.append("║     (all cash)")

    lines.append("╚══════════════════════════════════════════════════════════════════════╝")
    return "\n".join(lines)

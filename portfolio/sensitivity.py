"""
Sensitivity Analysis — Parameter grid search with heatmap output
════════════════════════════════════════════════════════════════
Systematically varies key parameters to identify stable vs fragile zones.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import replace
from itertools import product

from portfolio.backtest import run_backtest, BacktestConfig
from portfolio.allocator import AllocatorConfig, WeightMethod
from portfolio.signals import SignalConfig
from portfolio.features import FeatureConfig


def _safe_run(prices, config):
    """Run backtest, return (sharpe, cagr) or (NaN, NaN)."""
    try:
        r = run_backtest(prices, config)
        return r.strategy_metrics.sharpe, r.strategy_metrics.cagr
    except Exception:
        return float("nan"), float("nan")


# ══════════════════════════════════════════════════════════════════
# 1. MOMENTUM WEIGHT SENSITIVITY
# ══════════════════════════════════════════════════════════════════

def sensitivity_momentum_weights(
    prices: pd.DataFrame,
    base_config: BacktestConfig = None,
) -> pd.DataFrame:
    """
    Vary mom_63 weight (0.05 to 0.45) vs mom_126 weight (0.05 to 0.35).
    Returns DataFrame with Sharpe for each combo.
    """
    if base_config is None:
        base_config = BacktestConfig(
            allocator_config=AllocatorConfig(top_n=5, max_weight=0.30, method=WeightMethod.SCORE_WEIGHTED),
            rebal_freq="M", cost_bps=5.0,
        )

    mom63_range = [0.05, 0.15, 0.25, 0.35, 0.45]
    mom126_range = [0.05, 0.15, 0.25, 0.35]

    results = []
    for m63, m126 in product(mom63_range, mom126_range):
        remaining = max(0.0, 1.0 - m63 - m126 - 0.05)
        weights = {
            "mom_21": 0.05,
            "mom_63": m63,
            "mom_126": m126,
            "volatility": -remaining * 0.4,
            "drawdown": -remaining * 0.3,
            "trend_r2": remaining * 0.2,
            "rel_strength": remaining * 0.1,
        }
        cfg = replace(base_config, signal_config=SignalConfig(weights=weights))
        sharpe, cagr = _safe_run(prices, cfg)
        results.append({"mom_63": m63, "mom_126": m126, "sharpe": sharpe, "cagr": cagr})

    df = pd.DataFrame(results)
    return df


# ══════════════════════════════════════════════════════════════════
# 2. TOP-N vs MAX WEIGHT
# ══════════════════════════════════════════════════════════════════

def sensitivity_topn_maxweight(
    prices: pd.DataFrame,
    base_config: BacktestConfig = None,
) -> pd.DataFrame:
    """Vary top_n (3-15) vs max_weight (0.10-0.50)."""
    if base_config is None:
        base_config = BacktestConfig(
            allocator_config=AllocatorConfig(top_n=5, max_weight=0.30, method=WeightMethod.SCORE_WEIGHTED),
            rebal_freq="M", cost_bps=5.0,
        )

    top_n_range = [3, 5, 7, 10, 15]
    max_w_range = [0.10, 0.20, 0.30, 0.40, 0.50]

    results = []
    for n, w in product(top_n_range, max_w_range):
        if w < 1.0 / n * 0.8:
            continue  # Impossible to allocate
        cfg = replace(base_config, allocator_config=AllocatorConfig(
            top_n=n, max_weight=w, method=WeightMethod.SCORE_WEIGHTED,
        ))
        sharpe, cagr = _safe_run(prices, cfg)
        results.append({"top_n": n, "max_weight": w, "sharpe": sharpe, "cagr": cagr})

    return pd.DataFrame(results)


# ══════════════════════════════════════════════════════════════════
# 3. VOLATILITY WEIGHT vs DRAWDOWN WEIGHT
# ══════════════════════════════════════════════════════════════════

def sensitivity_vol_dd_weights(
    prices: pd.DataFrame,
    base_config: BacktestConfig = None,
) -> pd.DataFrame:
    """Vary volatility weight and drawdown weight in the signal."""
    if base_config is None:
        base_config = BacktestConfig(
            allocator_config=AllocatorConfig(top_n=5, max_weight=0.30, method=WeightMethod.SCORE_WEIGHTED),
            rebal_freq="M", cost_bps=5.0,
        )

    vol_range = [0.0, 0.05, 0.10, 0.20, 0.30, 0.40]
    dd_range = [0.0, 0.05, 0.10, 0.20, 0.30]

    results = []
    for vol_w, dd_w in product(vol_range, dd_range):
        remaining = max(0.0, 1.0 - vol_w - dd_w)
        weights = {
            "mom_21": remaining * 0.12,
            "mom_63": remaining * 0.35,
            "mom_126": remaining * 0.25,
            "volatility": -vol_w,
            "drawdown": -dd_w,
            "trend_r2": remaining * 0.15,
            "rel_strength": remaining * 0.13,
        }
        cfg = replace(base_config, signal_config=SignalConfig(weights=weights))
        sharpe, cagr = _safe_run(prices, cfg)
        results.append({"vol_weight": vol_w, "dd_weight": dd_w, "sharpe": sharpe, "cagr": cagr})

    return pd.DataFrame(results)


# ══════════════════════════════════════════════════════════════════
# TEXT HEATMAP
# ══════════════════════════════════════════════════════════════════

def text_heatmap(df: pd.DataFrame, row_col: str, col_col: str, val_col: str, title: str = "") -> str:
    """Generate a text-based heatmap from long-form DataFrame."""
    pivot = df.pivot_table(index=row_col, columns=col_col, values=val_col, aggfunc="first")
    pivot = pivot.sort_index(ascending=False)

    lines = [f"\n  {title}", "  " + "=" * 60]

    # Header
    col_vals = pivot.columns.tolist()
    header = f"  {row_col:<10}" + "".join(f"{c:>10}" for c in col_vals)
    lines.append(header)
    lines.append("  " + "-" * len(header))

    for idx in pivot.index:
        row = f"  {idx:<10}"
        for c in col_vals:
            v = pivot.loc[idx, c]
            if pd.isna(v):
                row += f"{'---':>10}"
            else:
                row += f"{v:>10.2f}"
        lines.append(row)

    # Identify best and stable zone
    valid = pivot.values[~np.isnan(pivot.values)] if pivot.values.size > 0 else np.array([])
    if len(valid) > 0:
        best = np.nanmax(pivot.values)
        median = np.nanmedian(pivot.values)
        worst = np.nanmin(pivot.values)
        lines.append(f"\n  Best={best:.2f}  Median={median:.2f}  Worst={worst:.2f}  Range={best-worst:.2f}")
        # Stable zone: cells within 80% of best
        threshold = best * 0.8 if best > 0 else best * 1.2
        n_stable = np.sum(pivot.values >= threshold)
        n_total = np.sum(~np.isnan(pivot.values))
        lines.append(f"  Stable zone (≥80% of best): {n_stable}/{n_total} = {n_stable/max(n_total,1):.0%}")

    return "\n".join(lines)


def run_sensitivity_analysis(prices: pd.DataFrame) -> str:
    """Run complete sensitivity analysis and return formatted report."""
    lines = [
        "",
        "╔══════════════════════════════════════════════════════════╗",
        "║            SENSITIVITY ANALYSIS                         ║",
        "╚══════════════════════════════════════════════════════════╝",
    ]

    print("  Momentum weights grid...")
    mom_df = sensitivity_momentum_weights(prices)
    lines.append(text_heatmap(mom_df, "mom_63", "mom_126", "sharpe", "Sharpe: mom_63 vs mom_126"))
    lines.append(text_heatmap(mom_df, "mom_63", "mom_126", "cagr", "CAGR: mom_63 vs mom_126"))

    print("  Top-N vs Max Weight grid...")
    tn_df = sensitivity_topn_maxweight(prices)
    lines.append(text_heatmap(tn_df, "top_n", "max_weight", "sharpe", "Sharpe: top_n vs max_weight"))

    print("  Vol vs DD weights grid...")
    vd_df = sensitivity_vol_dd_weights(prices)
    lines.append(text_heatmap(vd_df, "vol_weight", "dd_weight", "sharpe", "Sharpe: vol_weight vs dd_weight"))

    # Dominant parameter analysis
    lines.append("\n  PARAMETER DOMINANCE:")
    lines.append("  " + "=" * 40)

    # Momentum grid variance
    if not mom_df.empty:
        by_m63 = mom_df.groupby("mom_63")["sharpe"].mean()
        by_m126 = mom_df.groupby("mom_126")["sharpe"].mean()
        lines.append(f"  mom_63 range of mean Sharpe: {by_m63.max()-by_m63.min():.3f}")
        lines.append(f"  mom_126 range of mean Sharpe: {by_m126.max()-by_m126.min():.3f}")

    if not tn_df.empty:
        by_n = tn_df.groupby("top_n")["sharpe"].mean()
        by_w = tn_df.groupby("max_weight")["sharpe"].mean()
        lines.append(f"  top_n range of mean Sharpe: {by_n.max()-by_n.min():.3f}")
        lines.append(f"  max_weight range of mean Sharpe: {by_w.max()-by_w.min():.3f}")

    if not vd_df.empty:
        by_vol = vd_df.groupby("vol_weight")["sharpe"].mean()
        by_dd = vd_df.groupby("dd_weight")["sharpe"].mean()
        lines.append(f"  vol_weight range of mean Sharpe: {by_vol.max()-by_vol.min():.3f}")
        lines.append(f"  dd_weight range of mean Sharpe: {by_dd.max()-by_dd.min():.3f}")

    return "\n".join(lines)

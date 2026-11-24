"""
Walk-Forward Out-of-Sample Engine
═════════════════════════════════
Calibrate on past window, apply on future window, no information leakage.
Proves the engine works on data it has never seen during parameter selection.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import replace
from typing import Optional

from portfolio.backtest import run_backtest, BacktestConfig, BacktestResult
from portfolio.allocator import AllocatorConfig, WeightMethod
from portfolio.signals import SignalConfig
from portfolio.metrics import compute_all_metrics, MetricResult


def _best_config_on_window(
    prices: pd.DataFrame,
    warmup: int = 126,
) -> BacktestConfig:
    """
    Simple calibration: test a few top_n values, pick the one with best Sharpe.
    Deliberately minimal to avoid overfitting the calibration.
    """
    best_sharpe = -999.0
    best_cfg = None

    for n in [3, 5, 7, 10]:
        cfg = BacktestConfig(
            allocator_config=AllocatorConfig(
                top_n=n,
                max_weight=min(0.40, 1.0 / n + 0.05),
                method=WeightMethod.SCORE_WEIGHTED,
            ),
            rebal_freq="M",
            cost_bps=5.0,
            warmup=warmup,
        )
        try:
            result = run_backtest(prices, cfg)
            if result.strategy_metrics.sharpe > best_sharpe:
                best_sharpe = result.strategy_metrics.sharpe
                best_cfg = cfg
        except Exception:
            continue

    if best_cfg is None:
        best_cfg = BacktestConfig(
            allocator_config=AllocatorConfig(top_n=5, max_weight=0.30, method=WeightMethod.SCORE_WEIGHTED),
            rebal_freq="M", cost_bps=5.0, warmup=warmup,
        )

    return best_cfg


def run_walk_forward(
    prices: pd.DataFrame,
    train_days: int = 504,    # ~2 years training
    test_days: int = 252,     # ~1 year testing
    step_days: int = 126,     # ~6 months step
    warmup: int = 126,
) -> dict:
    """
    Walk-forward validation:
    1. For each window: calibrate on [t, t+train_days], apply on [t+train_days, t+train_days+test_days]
    2. Stitch together OOS equity curves
    3. Compare to in-sample performance

    Returns dict with OOS metrics, IS metrics, and per-fold details.
    """
    dates = prices.index
    n_dates = len(dates)

    folds = []
    oos_returns = []
    is_sharpes = []
    oos_sharpes = []

    fold_idx = 0
    start = 0

    while start + train_days + test_days <= n_dates:
        train_end = start + train_days
        test_end = min(train_end + test_days, n_dates)

        train_prices = prices.iloc[start:train_end]
        test_prices = prices.iloc[start:test_end]  # Include train for warmup features

        # Calibrate on training window
        best_cfg = _best_config_on_window(train_prices, warmup=warmup)

        # Run on training (IS)
        try:
            is_result = run_backtest(train_prices, best_cfg)
            is_sharpe = is_result.strategy_metrics.sharpe
        except Exception:
            is_sharpe = 0.0

        # Run on full window (OOS portion = after train_end)
        oos_cfg = replace(best_cfg, warmup=warmup)
        try:
            full_result = run_backtest(test_prices, oos_cfg)
            # Extract OOS portion of returns
            oos_dates = dates[train_end:test_end]
            full_rets = full_result.returns
            oos_rets = full_rets[full_rets.index.isin(oos_dates)]
            if len(oos_rets) > 20:
                oos_sharpe = oos_rets.mean() / oos_rets.std() * np.sqrt(252) if oos_rets.std() > 0 else 0.0
            else:
                oos_sharpe = 0.0
            oos_returns.append(oos_rets)
        except Exception:
            oos_sharpe = 0.0

        is_sharpes.append(is_sharpe)
        oos_sharpes.append(oos_sharpe)

        folds.append({
            "fold": fold_idx,
            "train_start": dates[start],
            "train_end": dates[train_end - 1],
            "test_start": dates[train_end],
            "test_end": dates[test_end - 1],
            "is_sharpe": is_sharpe,
            "oos_sharpe": oos_sharpe,
            "top_n": best_cfg.allocator_config.top_n,
        })

        fold_idx += 1
        start += step_days

    # Stitch OOS returns
    if oos_returns:
        combined_oos = pd.concat(oos_returns).sort_index()
        # Remove duplicate dates (overlapping test windows)
        combined_oos = combined_oos[~combined_oos.index.duplicated(keep="first")]
        oos_equity = (1 + combined_oos).cumprod()
        oos_metrics = compute_all_metrics(oos_equity, combined_oos)
    else:
        oos_metrics = None

    return {
        "folds": folds,
        "oos_metrics": oos_metrics,
        "is_sharpes": is_sharpes,
        "oos_sharpes": oos_sharpes,
        "avg_is_sharpe": np.mean(is_sharpes) if is_sharpes else 0.0,
        "avg_oos_sharpe": np.mean(oos_sharpes) if oos_sharpes else 0.0,
    }


def format_walk_forward(wf: dict) -> str:
    """Format walk-forward results."""
    lines = [
        "",
        "╔══════════════════════════════════════════════════════════╗",
        "║            WALK-FORWARD OUT-OF-SAMPLE RESULTS           ║",
        "╠══════════════════════════════════════════════════════════╣",
    ]

    for f in wf["folds"]:
        lines.append(
            f"║  Fold {f['fold']}: Train {f['train_start'].strftime('%Y-%m')}→{f['train_end'].strftime('%Y-%m')} "
            f"Test {f['test_start'].strftime('%Y-%m')}→{f['test_end'].strftime('%Y-%m')} "
            f"IS={f['is_sharpe']:.2f} OOS={f['oos_sharpe']:.2f} N={f['top_n']}"
        )

    lines.append("╠══════════════════════════════════════════════════════════╣")
    lines.append(f"║  Avg IS Sharpe  : {wf['avg_is_sharpe']:.2f}")
    lines.append(f"║  Avg OOS Sharpe : {wf['avg_oos_sharpe']:.2f}")

    decay = wf["avg_is_sharpe"] - wf["avg_oos_sharpe"]
    lines.append(f"║  IS→OOS Decay   : {decay:+.2f}")

    if wf["oos_metrics"]:
        m = wf["oos_metrics"]
        lines.append(f"║  OOS CAGR       : {m.cagr:.2%}")
        lines.append(f"║  OOS Max DD     : {m.max_drawdown:.2%}")

    # Verdict
    oos_positive = sum(1 for s in wf["oos_sharpes"] if s > 0)
    total = len(wf["oos_sharpes"])
    lines.append(f"║  OOS positive   : {oos_positive}/{total}")

    if wf["avg_oos_sharpe"] > 0.3 and oos_positive >= total * 0.6:
        lines.append("║  WF VERDICT     : SURVIVES OUT-OF-SAMPLE ✓")
    else:
        lines.append("║  WF VERDICT     : FAILS OUT-OF-SAMPLE ✗")

    lines.append("╚══════════════════════════════════════════════════════════╝")
    return "\n".join(lines)

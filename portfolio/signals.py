"""
Signal Engine — Composite scoring and ranking
══════════════════════════════════════════════
Converts raw features into a single composite score per asset per date.
Ranking, not timing: every asset gets a score, top-N are selected.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field


@dataclass
class SignalConfig:
    """Weights for the composite score (must sum to ~1.0)."""
    weights: dict[str, float] = field(default_factory=lambda: {
        "mom_21":       0.10,   # Short momentum
        "mom_63":       0.25,   # Medium momentum (primary)
        "mom_126":      0.20,   # Long momentum
        "volatility":  -0.15,   # Lower vol → higher score (negative weight)
        "drawdown":    -0.10,   # Less drawdown → higher score (negative = penalty inverted)
        "trend_r2":     0.10,   # Smooth trends rewarded
        "rel_strength": 0.10,   # Outperformance vs SPY
    })


def _cross_sectional_rank(df: pd.DataFrame) -> pd.DataFrame:
    """
    Cross-sectional percentile rank each row (date).
    Output in [0, 1], higher = better.
    NaN propagated.
    """
    return df.rank(axis=1, pct=True)


def compute_composite_score(
    features: dict[str, pd.DataFrame],
    config: SignalConfig = None,
) -> pd.DataFrame:
    """
    Compute composite score from features.

    Steps:
    1. Cross-sectional rank each feature (percentile, 0-1)
    2. Weighted sum of ranked features → composite score
    3. Final ranking for selection

    Returns DataFrame (dates × tickers) of composite scores in [0, 1].
    """
    if config is None:
        config = SignalConfig()

    # Align on common index/columns across all feature frames
    all_keys = [k for k in config.weights if k in features]
    if not all_keys:
        raise ValueError("No matching features for configured weights")

    ref = features[all_keys[0]]
    composite = pd.DataFrame(0.0, index=ref.index, columns=ref.columns)

    total_weight = 0.0
    for key in all_keys:
        w = config.weights[key]
        feat = features[key].reindex_like(ref)

        # For negative-weight features (volatility, drawdown),
        # we flip the sign so higher rank = better:
        # low vol → ranked high after negation
        if w < 0:
            ranked = _cross_sectional_rank(-feat)
            w = abs(w)
        else:
            ranked = _cross_sectional_rank(feat)

        composite += w * ranked
        total_weight += w

    # Normalize to [0, 1]
    if total_weight > 0:
        composite /= total_weight

    return composite


def rank_assets(composite: pd.DataFrame) -> pd.DataFrame:
    """
    Rank assets by composite score each date.
    Rank 1 = best asset, N = worst.
    """
    return composite.rank(axis=1, ascending=False, method="min")


def select_top_n(composite: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """
    Boolean mask: True if asset is in top-N on that date.
    """
    ranks = rank_assets(composite)
    return ranks <= n

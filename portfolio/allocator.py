"""
Portfolio Allocator — Weight assignment with constraints
════════════════════════════════════════════════════════
Converts composite scores into portfolio weights.
Methods: score-weighted, inverse-vol, equal-weight.
Constraints: max weight per position, min diversification.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass
from enum import Enum


class WeightMethod(Enum):
    EQUAL = "equal"
    SCORE_WEIGHTED = "score_weighted"
    INVERSE_VOL = "inverse_vol"


@dataclass
class AllocatorConfig:
    top_n: int = 10                   # Number of positions
    max_weight: float = 0.20          # Max 20% per position
    method: WeightMethod = WeightMethod.SCORE_WEIGHTED
    min_weight: float = 0.02          # Min 2% per position (avoid dust)


def allocate_equal(selected: list[str], config: AllocatorConfig) -> dict[str, float]:
    """Equal weight across selected assets."""
    if not selected:
        return {}
    n = len(selected)
    w = min(1.0 / n, config.max_weight)
    weights = {t: w for t in selected}
    # Renormalize to sum to 1
    total = sum(weights.values())
    if total > 0:
        weights = {t: v / total for t, v in weights.items()}
    return weights


def allocate_score_weighted(
    selected: list[str],
    scores: pd.Series,
    config: AllocatorConfig,
) -> dict[str, float]:
    """Weight proportional to composite score, capped at max_weight."""
    if not selected:
        return {}

    raw = {t: max(scores.get(t, 0.0), 0.0) for t in selected}
    total = sum(raw.values())
    if total <= 0:
        return allocate_equal(selected, config)

    # Normalize
    weights = {t: v / total for t, v in raw.items()}

    # Iterative capping: cap overweight positions, redistribute excess
    for _ in range(10):
        excess = 0.0
        uncapped = []
        for t in weights:
            if weights[t] > config.max_weight:
                excess += weights[t] - config.max_weight
                weights[t] = config.max_weight
            else:
                uncapped.append(t)
        if excess <= 0 or not uncapped:
            break
        uncapped_total = sum(weights[t] for t in uncapped)
        if uncapped_total > 0:
            for t in uncapped:
                weights[t] += excess * (weights[t] / uncapped_total)

    return weights


def allocate_inverse_vol(
    selected: list[str],
    volatility: pd.Series,
    config: AllocatorConfig,
) -> dict[str, float]:
    """Weight inversely proportional to volatility (risk parity lite)."""
    if not selected:
        return {}

    inv_vol = {}
    for t in selected:
        vol = volatility.get(t, np.nan)
        if np.isnan(vol) or vol <= 0:
            inv_vol[t] = 0.0
        else:
            inv_vol[t] = 1.0 / vol

    total = sum(inv_vol.values())
    if total <= 0:
        return allocate_equal(selected, config)

    weights = {t: v / total for t, v in inv_vol.items()}

    # Apply max_weight cap
    for _ in range(10):
        excess = 0.0
        uncapped = []
        for t in weights:
            if weights[t] > config.max_weight:
                excess += weights[t] - config.max_weight
                weights[t] = config.max_weight
            else:
                uncapped.append(t)
        if excess <= 0 or not uncapped:
            break
        uncapped_total = sum(weights[t] for t in uncapped)
        if uncapped_total > 0:
            for t in uncapped:
                weights[t] += excess * (weights[t] / uncapped_total)

    return weights


def compute_allocation(
    composite_scores: pd.Series,
    volatility: pd.Series | None = None,
    config: AllocatorConfig = None,
) -> dict[str, float]:
    """
    Main allocation entry point for a single date.

    Parameters:
        composite_scores: Series (ticker → score) for one date
        volatility: Series (ticker → vol) for one date (needed for inverse_vol)
        config: AllocatorConfig

    Returns:
        Dict[ticker, weight] summing to ~1.0
    """
    if config is None:
        config = AllocatorConfig()

    # Select top N by score
    valid = composite_scores.dropna().sort_values(ascending=False)
    selected = valid.index[:config.top_n].tolist()

    if not selected:
        return {}

    if config.method == WeightMethod.EQUAL:
        return allocate_equal(selected, config)
    elif config.method == WeightMethod.INVERSE_VOL:
        if volatility is None:
            return allocate_equal(selected, config)
        return allocate_inverse_vol(selected, volatility, config)
    else:
        return allocate_score_weighted(selected, valid, config)

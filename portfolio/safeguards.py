"""
Safeguards — Fail-closed safety controls
═════════════════════════════════════════
Blocks rebalancing on stale/partial data, NaN scores, excessive concentration.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass


@dataclass
class SafeguardConfig:
    max_data_age_days: int = 3          # Block if latest price > 3 days old
    min_valid_tickers: int = 10         # Block if < 10 tickers have valid data
    max_nan_pct: float = 0.20           # Block if > 20% of scores are NaN
    max_single_weight: float = 0.35     # Hard cap on any single position
    max_turnover_per_rebal: float = 0.60  # Block if turnover > 60% one-way
    min_score_range: float = 0.05       # Block if all scores within 5% (no signal)
    max_concentration_top3: float = 0.70  # Top 3 weights must be < 70%


@dataclass
class SafeguardResult:
    passed: bool
    checks: dict[str, bool]
    warnings: list[str]
    blockers: list[str]


def check_data_freshness(
    prices: pd.DataFrame,
    reference_date: pd.Timestamp | None = None,
    config: SafeguardConfig = None,
) -> tuple[bool, str]:
    """Check that price data is fresh enough."""
    if config is None:
        config = SafeguardConfig()

    if prices.empty:
        return False, "BLOCKER: No price data"

    latest = prices.index.max()
    if reference_date is None:
        reference_date = pd.Timestamp.now().normalize()

    age_days = (reference_date - latest).days
    if age_days > config.max_data_age_days:
        return False, f"BLOCKER: Data is {age_days} days old (max {config.max_data_age_days})"

    return True, f"OK: Data age {age_days} days"


def check_valid_tickers(
    prices: pd.DataFrame,
    config: SafeguardConfig = None,
) -> tuple[bool, str]:
    """Check minimum number of valid tickers."""
    if config is None:
        config = SafeguardConfig()

    # Count tickers with recent valid data (last row not NaN)
    last_row = prices.iloc[-1]
    valid = last_row.notna().sum()

    if valid < config.min_valid_tickers:
        return False, f"BLOCKER: Only {valid} valid tickers (min {config.min_valid_tickers})"

    return True, f"OK: {valid} valid tickers"


def check_scores(
    scores: pd.Series,
    config: SafeguardConfig = None,
) -> tuple[bool, str]:
    """Check composite scores for NaN saturation and signal quality."""
    if config is None:
        config = SafeguardConfig()

    if scores is None or len(scores) == 0:
        return False, "BLOCKER: No scores available"

    nan_pct = scores.isna().mean()
    if nan_pct > config.max_nan_pct:
        return False, f"BLOCKER: {nan_pct:.0%} NaN scores (max {config.max_nan_pct:.0%})"

    valid = scores.dropna()
    if len(valid) < config.min_valid_tickers:
        return False, f"BLOCKER: Only {len(valid)} non-NaN scores"

    score_range = valid.max() - valid.min()
    if score_range < config.min_score_range:
        return False, f"BLOCKER: Score range {score_range:.3f} too tight (no signal)"

    return True, f"OK: {len(valid)} valid scores, range={score_range:.3f}"


def check_weights(
    weights: dict[str, float],
    config: SafeguardConfig = None,
) -> tuple[bool, str]:
    """Check proposed weights for concentration and validity."""
    if config is None:
        config = SafeguardConfig()

    if not weights:
        return True, "OK: Empty weights (cash)"

    # Single position cap
    max_w = max(weights.values())
    if max_w > config.max_single_weight:
        return False, f"BLOCKER: Max weight {max_w:.1%} exceeds {config.max_single_weight:.1%}"

    # Top-3 concentration
    sorted_w = sorted(weights.values(), reverse=True)
    top3 = sum(sorted_w[:3])
    if top3 > config.max_concentration_top3:
        return False, f"BLOCKER: Top 3 concentration {top3:.1%} exceeds {config.max_concentration_top3:.1%}"

    # NaN or negative weights
    for t, w in weights.items():
        if np.isnan(w) or w < 0:
            return False, f"BLOCKER: Invalid weight for {t}: {w}"

    total = sum(weights.values())
    if total > 1.05:
        return False, f"BLOCKER: Weights sum {total:.3f} > 1.05"

    return True, f"OK: {len(weights)} positions, max={max_w:.1%}, top3={top3:.1%}"


def check_turnover(
    old_weights: dict[str, float],
    new_weights: dict[str, float],
    config: SafeguardConfig = None,
) -> tuple[bool, str]:
    """Check that turnover is within limits."""
    if config is None:
        config = SafeguardConfig()

    all_tickers = set(old_weights.keys()) | set(new_weights.keys())
    turnover = sum(
        abs(new_weights.get(t, 0.0) - old_weights.get(t, 0.0))
        for t in all_tickers
    ) / 2.0

    if turnover > config.max_turnover_per_rebal:
        return False, f"BLOCKER: Turnover {turnover:.1%} exceeds {config.max_turnover_per_rebal:.1%}"

    return True, f"OK: Turnover {turnover:.1%}"


def run_all_safeguards(
    prices: pd.DataFrame,
    scores: pd.Series,
    weights: dict[str, float],
    old_weights: dict[str, float] | None = None,
    reference_date: pd.Timestamp | None = None,
    config: SafeguardConfig = None,
) -> SafeguardResult:
    """Run all safeguard checks. Returns SafeguardResult."""
    if config is None:
        config = SafeguardConfig()
    if old_weights is None:
        old_weights = {}

    checks = {}
    warnings = []
    blockers = []

    for name, (passed, msg) in [
        ("data_freshness", check_data_freshness(prices, reference_date, config)),
        ("valid_tickers", check_valid_tickers(prices, config)),
        ("scores", check_scores(scores, config)),
        ("weights", check_weights(weights, config)),
        ("turnover", check_turnover(old_weights, weights, config)),
    ]:
        checks[name] = passed
        if not passed and "BLOCKER" in msg:
            blockers.append(msg)
        elif "WARNING" in msg:
            warnings.append(msg)

    all_passed = all(checks.values())
    return SafeguardResult(passed=all_passed, checks=checks, warnings=warnings, blockers=blockers)

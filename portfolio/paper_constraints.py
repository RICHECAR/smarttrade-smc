"""
Paper Constraints — Fail-closed verrou for paper trading compliance.
════════════════════════════════════════════════════════════════════
Enforces validated constraints from Chantier 16:
  - monthly rebalance ONLY (weekly forbidden)
  - vol_weight ≤ 0.15
  - turnover cap per rebalance
  - minimum universe size
  - data freshness
  - score validity

Every violation produces an explicit reason code and blocks execution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ── Reason Codes ─────────────────────────────────────────────────

PAPER_DATA_STALE = "PAPER_DATA_STALE"
PAPER_DATA_PARTIAL = "PAPER_DATA_PARTIAL"
PAPER_TURNOVER_EXCEEDED = "PAPER_TURNOVER_EXCEEDED"
PAPER_VOL_WEIGHT_INVALID = "PAPER_VOL_WEIGHT_INVALID"
PAPER_WEEKLY_FORBIDDEN = "PAPER_WEEKLY_FORBIDDEN"
PAPER_UNIVERSE_TOO_SMALL = "PAPER_UNIVERSE_TOO_SMALL"
PAPER_SCORE_NAN = "PAPER_SCORE_NAN"
PAPER_REBALANCE_BLOCKED = "PAPER_REBALANCE_BLOCKED"

ALL_REASON_CODES = (
    PAPER_DATA_STALE,
    PAPER_DATA_PARTIAL,
    PAPER_TURNOVER_EXCEEDED,
    PAPER_VOL_WEIGHT_INVALID,
    PAPER_WEEKLY_FORBIDDEN,
    PAPER_UNIVERSE_TOO_SMALL,
    PAPER_SCORE_NAN,
    PAPER_REBALANCE_BLOCKED,
)


@dataclass
class PaperConstraints:
    """Immutable parameter lock validated by Chantier 16."""
    rebal_freq: str = "M"                     # monthly ONLY
    strategy: str = "score_top5_monthly"
    max_vol_weight: float = 0.15              # vol_weight ≤ 0.15
    max_turnover_per_rebal: float = 0.50      # 50% one-way cap
    min_universe_tickers: int = 10
    max_data_age_days: int = 5                # business days
    min_non_nan_score_pct: float = 0.70       # ≥ 70% valid scores
    top_n: int = 5
    max_single_weight: float = 0.30
    cost_bps: float = 5.0


@dataclass
class ConstraintCheckResult:
    passed: bool
    reason_code: str       # empty if passed
    detail: str


@dataclass
class ConstraintGateResult:
    """Aggregate of all constraint checks."""
    passed: bool
    checks: List[ConstraintCheckResult] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)       # reason codes
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.passed:
            return "ALL_CONSTRAINTS_PASSED"
        return " | ".join(self.blockers)


# ── Individual checks ────────────────────────────────────────────

def check_rebal_freq(freq: str, _cfg: PaperConstraints) -> ConstraintCheckResult:
    """Weekly is forbidden."""
    if freq.upper() in ("W", "WEEKLY"):
        return ConstraintCheckResult(False, PAPER_WEEKLY_FORBIDDEN,
                                     f"Rebalance freq '{freq}' forbidden; only monthly allowed")
    if freq.upper() not in ("M", "MONTHLY"):
        return ConstraintCheckResult(False, PAPER_REBALANCE_BLOCKED,
                                     f"Unknown rebalance freq '{freq}'")
    return ConstraintCheckResult(True, "", "Monthly rebalance OK")


def check_vol_weight(signal_weights: Dict[str, float], cfg: PaperConstraints) -> ConstraintCheckResult:
    """Absolute vol weight must be ≤ max_vol_weight."""
    vol_w = abs(signal_weights.get("volatility", 0.0))
    if vol_w > cfg.max_vol_weight + 1e-9:
        return ConstraintCheckResult(False, PAPER_VOL_WEIGHT_INVALID,
                                     f"vol_weight={vol_w:.2f} > max {cfg.max_vol_weight}")
    return ConstraintCheckResult(True, "", f"vol_weight={vol_w:.2f} OK")


def check_data_freshness(
    prices: pd.DataFrame,
    reference_date: pd.Timestamp,
    cfg: PaperConstraints,
) -> ConstraintCheckResult:
    """Block if data is too old."""
    if prices.empty:
        return ConstraintCheckResult(False, PAPER_DATA_STALE, "No price data")
    latest = prices.index.max()
    age = (reference_date - latest).days
    if age > cfg.max_data_age_days:
        return ConstraintCheckResult(False, PAPER_DATA_STALE,
                                     f"Data {age}d old > max {cfg.max_data_age_days}d")
    return ConstraintCheckResult(True, "", f"Data age {age}d OK")


def check_universe_size(
    valid_tickers: int,
    cfg: PaperConstraints,
) -> ConstraintCheckResult:
    if valid_tickers < cfg.min_universe_tickers:
        return ConstraintCheckResult(False, PAPER_UNIVERSE_TOO_SMALL,
                                     f"Only {valid_tickers} valid tickers < min {cfg.min_universe_tickers}")
    return ConstraintCheckResult(True, "", f"{valid_tickers} tickers OK")


def check_data_completeness(
    prices: pd.DataFrame,
    cfg: PaperConstraints,
) -> ConstraintCheckResult:
    """Block on partial data (too many NaN columns)."""
    if prices.empty:
        return ConstraintCheckResult(False, PAPER_DATA_PARTIAL, "Empty price DataFrame")
    nan_pct = prices.iloc[-1].isna().mean()
    if nan_pct > 0.30:
        return ConstraintCheckResult(False, PAPER_DATA_PARTIAL,
                                     f"{nan_pct:.0%} tickers have NaN on last row")
    return ConstraintCheckResult(True, "", f"Data completeness OK ({nan_pct:.0%} NaN)")


def check_scores(
    scores: pd.Series,
    cfg: PaperConstraints,
) -> ConstraintCheckResult:
    """Block if too many NaN scores or all identical."""
    if scores is None or scores.empty:
        return ConstraintCheckResult(False, PAPER_SCORE_NAN, "No scores available")
    valid_pct = scores.notna().mean()
    if valid_pct < cfg.min_non_nan_score_pct:
        return ConstraintCheckResult(False, PAPER_SCORE_NAN,
                                     f"Only {valid_pct:.0%} valid scores < min {cfg.min_non_nan_score_pct:.0%}")
    score_range = scores.dropna().max() - scores.dropna().min()
    if score_range < 0.01:
        return ConstraintCheckResult(False, PAPER_SCORE_NAN,
                                     f"Score range {score_range:.4f} too narrow — no signal")
    return ConstraintCheckResult(True, "", f"Scores OK ({valid_pct:.0%} valid, range={score_range:.4f})")


def check_turnover(
    old_weights: Dict[str, float],
    new_weights: Dict[str, float],
    cfg: PaperConstraints,
) -> ConstraintCheckResult:
    """Block if one-way turnover exceeds cap."""
    all_t = set(old_weights) | set(new_weights)
    turnover = sum(abs(new_weights.get(t, 0.0) - old_weights.get(t, 0.0)) for t in all_t) / 2.0
    if turnover > cfg.max_turnover_per_rebal + 1e-9:
        return ConstraintCheckResult(False, PAPER_TURNOVER_EXCEEDED,
                                     f"Turnover {turnover:.1%} > cap {cfg.max_turnover_per_rebal:.0%}")
    return ConstraintCheckResult(True, "", f"Turnover {turnover:.1%} OK")


# ── Aggregate gate ───────────────────────────────────────────────

def cap_turnover_weights(
    old_weights: Dict[str, float],
    new_weights: Dict[str, float],
    max_turnover: float,
) -> Dict[str, float]:
    """Scale the move toward target so one-way turnover ≤ max_turnover.

    Returns adjusted weights that move proportionally toward new_weights
    but respect the turnover budget. Sum is preserved.
    """
    all_t = set(old_weights) | set(new_weights)
    raw_turnover = sum(abs(new_weights.get(t, 0.0) - old_weights.get(t, 0.0)) for t in all_t) / 2.0

    if raw_turnover <= max_turnover + 1e-9:
        return dict(new_weights)  # no capping needed

    # Scale the delta proportionally
    scale = max_turnover / raw_turnover if raw_turnover > 0 else 0.0
    capped: Dict[str, float] = {}
    for t in all_t:
        old = old_weights.get(t, 0.0)
        new = new_weights.get(t, 0.0)
        capped[t] = old + (new - old) * scale

    # Renormalize to sum=1
    total = sum(capped.values())
    if total > 0:
        capped = {t: w / total for t, w in capped.items()}

    # Drop dust
    capped = {t: w for t, w in capped.items() if w > 0.001}
    return capped


def run_constraint_gate(
    *,
    rebal_freq: str,
    signal_weights: Dict[str, float],
    prices: pd.DataFrame,
    reference_date: pd.Timestamp,
    scores: pd.Series,
    old_weights: Dict[str, float],
    new_weights: Dict[str, float],
    cfg: PaperConstraints = None,
) -> ConstraintGateResult:
    """Run all constraint checks. Fail-closed: any failure blocks rebalance.

    NOTE: turnover is checked but reported as a WARNING, not a blocker.
    The engine applies turnover capping via cap_turnover_weights().
    """
    if cfg is None:
        cfg = PaperConstraints()

    valid_tickers = int(prices.iloc[-1].notna().sum()) if not prices.empty else 0

    # Hard checks (block rebalance)
    hard_checks = [
        check_rebal_freq(rebal_freq, cfg),
        check_vol_weight(signal_weights, cfg),
        check_data_freshness(prices, reference_date, cfg),
        check_universe_size(valid_tickers, cfg),
        check_data_completeness(prices, cfg),
        check_scores(scores, cfg),
    ]

    # Soft check (warning only — capped by engine)
    turnover_check = check_turnover(old_weights, new_weights, cfg)

    all_checks = hard_checks + [turnover_check]
    blockers = [c.reason_code for c in hard_checks if not c.passed]
    warnings = []
    if not turnover_check.passed:
        warnings.append(f"{PAPER_TURNOVER_EXCEEDED}: {turnover_check.detail}")

    passed = len(blockers) == 0

    return ConstraintGateResult(
        passed=passed,
        checks=all_checks,
        blockers=blockers,
        warnings=warnings,
    )

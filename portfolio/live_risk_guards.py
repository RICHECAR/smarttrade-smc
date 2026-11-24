"""
Live Risk Guards — Pre-trade and post-trade safety checks.
═══════════════════════════════════════════════════════════
Every check is HARD: fail → block order.
Never override a guard programmatically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from portfolio.live_config import (
    LiveSafeConfig,
    LIVE_POSITION_SIZE_EXCEEDED,
    LIVE_EXPOSURE_CAP_EXCEEDED,
    LIVE_CAPITAL_LIMIT_EXCEEDED,
    LIVE_DD_HALT,
    LIVE_DATA_INCOHERENT,
    LIVE_TRACKING_ERROR,
    LIVE_WEIGHT_DIVERGENCE,
)

logger = logging.getLogger(__name__)


@dataclass
class GuardResult:
    passed: bool
    reason_code: str = ""
    detail: str = ""


@dataclass
class RiskGuardReport:
    """Aggregate of all pre-trade risk checks."""
    passed: bool = True
    checks: List[GuardResult] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.passed:
            return "ALL_RISK_GUARDS_PASSED"
        return " | ".join(self.blockers)


# ── Individual guards ────────────────────────────────────────────

def check_position_size(
    ticker: str,
    order_dollar: float,
    account_value: float,
    cfg: LiveSafeConfig,
) -> GuardResult:
    """Block if single order exceeds capital_limit_pct or max_position_size."""
    if order_dollar > cfg.max_position_size_usd:
        return GuardResult(
            False, LIVE_POSITION_SIZE_EXCEEDED,
            f"{ticker}: ${order_dollar:,.0f} > max ${cfg.max_position_size_usd:,.0f}",
        )
    max_by_pct = account_value * cfg.capital_limit_pct
    if order_dollar > max_by_pct:
        return GuardResult(
            False, LIVE_CAPITAL_LIMIT_EXCEEDED,
            f"{ticker}: ${order_dollar:,.0f} > {cfg.capital_limit_pct:.0%} of ${account_value:,.0f}",
        )
    return GuardResult(True, "", f"{ticker}: ${order_dollar:,.0f} OK")


def check_global_exposure(
    current_invested: float,
    pending_buy_total: float,
    account_value: float,
    cfg: LiveSafeConfig,
) -> GuardResult:
    """Block if total exposure after trades exceeds cap."""
    projected = current_invested + pending_buy_total
    if projected > cfg.global_exposure_cap_usd:
        return GuardResult(
            False, LIVE_EXPOSURE_CAP_EXCEEDED,
            f"Projected ${projected:,.0f} > cap ${cfg.global_exposure_cap_usd:,.0f}",
        )
    cap_by_pct = account_value * cfg.global_exposure_cap_pct
    if projected > cap_by_pct:
        return GuardResult(
            False, LIVE_EXPOSURE_CAP_EXCEEDED,
            f"Projected ${projected:,.0f} > {cfg.global_exposure_cap_pct:.0%} of ${account_value:,.0f}",
        )
    return GuardResult(True, "", f"Exposure ${projected:,.0f} OK")


def check_drawdown(
    current_value: float,
    peak_value: float,
    cfg: LiveSafeConfig,
) -> GuardResult:
    """Block all trading if drawdown exceeds max_dd_pct."""
    if peak_value <= 0:
        return GuardResult(True, "", "No peak recorded yet")
    dd = (peak_value - current_value) / peak_value
    if dd > cfg.max_dd_pct:
        return GuardResult(
            False, LIVE_DD_HALT,
            f"Drawdown {dd:.1%} > max {cfg.max_dd_pct:.0%} — HALT",
        )
    return GuardResult(True, "", f"Drawdown {dd:.1%} OK")


def check_data_coherence(
    prices: Dict[str, float],
    target_tickers: List[str],
) -> GuardResult:
    """Block if prices missing or zero for target tickers."""
    missing = [t for t in target_tickers if t not in prices or prices[t] <= 0]
    if missing:
        return GuardResult(
            False, LIVE_DATA_INCOHERENT,
            f"Missing/zero prices: {missing}",
        )
    return GuardResult(True, "", "All target prices available")


def check_paper_live_divergence(
    paper_weights: Dict[str, float],
    live_weights: Dict[str, float],
    cfg: LiveSafeConfig,
) -> GuardResult:
    """Block if paper vs live weights diverge too much."""
    all_t = set(paper_weights) | set(live_weights)
    if not all_t:
        return GuardResult(True, "", "No positions to compare")
    max_diff = max(
        abs(paper_weights.get(t, 0.0) - live_weights.get(t, 0.0))
        for t in all_t
    )
    if max_diff > cfg.max_weight_divergence_pct:
        return GuardResult(
            False, LIVE_WEIGHT_DIVERGENCE,
            f"Max weight diff {max_diff:.1%} > {cfg.max_weight_divergence_pct:.0%}",
        )
    return GuardResult(True, "", f"Max weight diff {max_diff:.1%} OK")


def check_tracking_error(
    paper_return: float,
    live_return: float,
    cfg: LiveSafeConfig,
) -> GuardResult:
    """Warn if cumulative return difference exceeds threshold."""
    diff = abs(paper_return - live_return)
    if diff > cfg.max_tracking_error_pct:
        return GuardResult(
            False, LIVE_TRACKING_ERROR,
            f"Tracking error {diff:.1%} > max {cfg.max_tracking_error_pct:.0%}",
        )
    return GuardResult(True, "", f"Tracking error {diff:.1%} OK")


# ── Aggregate pre-trade gate ─────────────────────────────────────

def run_pre_trade_guards(
    *,
    target_weights: Dict[str, float],
    prices: Dict[str, float],
    account_value: float,
    current_invested: float,
    peak_value: float,
    paper_weights: Dict[str, float],
    live_weights: Dict[str, float],
    paper_return: float = 0.0,
    live_return: float = 0.0,
    cfg: LiveSafeConfig,
) -> RiskGuardReport:
    """Run all pre-trade risk checks. Any failure blocks rebalance."""
    report = RiskGuardReport()

    # 1. Drawdown halt
    dd = check_drawdown(account_value, peak_value, cfg)
    report.checks.append(dd)
    if not dd.passed:
        report.blockers.append(f"{dd.reason_code}: {dd.detail}")

    # 2. Data coherence
    target_tickers = [t for t, w in target_weights.items() if w > 0.001]
    dc = check_data_coherence(prices, target_tickers)
    report.checks.append(dc)
    if not dc.passed:
        report.blockers.append(f"{dc.reason_code}: {dc.detail}")

    # 3. Global exposure
    pending_buys = sum(
        max(0, (target_weights.get(t, 0.0) - live_weights.get(t, 0.0))) * account_value
        for t in set(target_weights) | set(live_weights)
    )
    ge = check_global_exposure(current_invested, pending_buys, account_value, cfg)
    report.checks.append(ge)
    if not ge.passed:
        report.blockers.append(f"{ge.reason_code}: {ge.detail}")

    # 4. Per-position size
    for ticker, weight in target_weights.items():
        current_w = live_weights.get(ticker, 0.0)
        delta_dollar = abs(weight - current_w) * account_value
        if delta_dollar < 1.0:
            continue
        ps = check_position_size(ticker, delta_dollar, account_value, cfg)
        report.checks.append(ps)
        if not ps.passed:
            report.blockers.append(f"{ps.reason_code}: {ps.detail}")

    # 5. Paper/live divergence (warning, not hard block unless extreme)
    div = check_paper_live_divergence(paper_weights, live_weights, cfg)
    report.checks.append(div)
    if not div.passed:
        report.warnings.append(f"{div.reason_code}: {div.detail}")

    # 6. Tracking error
    te = check_tracking_error(paper_return, live_return, cfg)
    report.checks.append(te)
    if not te.passed:
        report.warnings.append(f"{te.reason_code}: {te.detail}")

    report.passed = len(report.blockers) == 0
    return report

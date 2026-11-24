"""
Robustness Models — Data Structures for the Statistical Robustness Layer
═══════════════════════════════════════════════════════════════════════

Extends the Darwinian layer with confidence, multi-window, fragility,
promotion-gate, and allocation-stability structures.
Never touches existing files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
#  ENUMS
# ═══════════════════════════════════════════════════════════════

class ConfidenceRecommendation(str, Enum):
    PROMOTE = "promote"       # Strong statistical evidence — allow promotion
    HOLD = "hold"             # Insufficient evidence — keep current state
    DELAY = "delay"           # Marginal data — wait for more trades
    BLOCK = "block"           # Statistically unreliable — suppress


class EdgeTrend(str, Enum):
    PERSISTENT = "persistent"       # Edge stable across all windows
    DECAYING = "decaying"           # Edge weakening over time
    UNSTABLE = "unstable"           # High variance across windows
    REGIME_DEPENDENT = "regime_dependent"  # Edge only in specific regimes
    INSUFFICIENT = "insufficient"   # Not enough data to classify


class PromotionDecision(str, Enum):
    APPROVED = "approved"
    DENIED_SAMPLE = "denied_sample"           # Not enough trades
    DENIED_ROBUSTNESS = "denied_robustness"   # Score too low
    DENIED_SPIKE = "denied_spike"             # Recent outlier spike
    FAST_DOWNGRADE = "fast_downgrade"         # Immediate demotion warranted


# ═══════════════════════════════════════════════════════════════
#  STATISTICAL CONFIDENCE
# ═══════════════════════════════════════════════════════════════

@dataclass
class ConfidenceVerdict:
    """Output of StatisticalConfidenceEngine."""
    confidence_score: float = 0.0       # 0–1
    recommendation: ConfidenceRecommendation = ConfidenceRecommendation.HOLD
    sample_size: int = 0
    wr_ci_lower: float = 0.0           # Wilson CI lower bound
    wr_ci_upper: float = 1.0           # Wilson CI upper bound
    expectancy_stability: float = 0.0  # 0–1 (1 = perfectly stable)
    pf_stability: float = 0.0         # 0–1
    window_stability: float = 0.0     # 0–1 (rolling window consistency)
    components: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "confidence_score": round(self.confidence_score, 4),
            "recommendation": self.recommendation.value,
            "sample_size": self.sample_size,
            "wr_ci_lower": round(self.wr_ci_lower, 4),
            "wr_ci_upper": round(self.wr_ci_upper, 4),
            "expectancy_stability": round(self.expectancy_stability, 4),
            "pf_stability": round(self.pf_stability, 4),
            "window_stability": round(self.window_stability, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
        }


# ═══════════════════════════════════════════════════════════════
#  MULTI-WINDOW VALIDATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class WindowStats:
    """Stats for one time window."""
    window_name: str = ""
    window_size: int = 0
    trade_count: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    sharpe_approx: float = 0.0     # simple approximation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_name": self.window_name,
            "window_size": self.window_size,
            "trade_count": self.trade_count,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 2),
            "expectancy": round(self.expectancy, 2),
            "sharpe_approx": round(self.sharpe_approx, 3),
        }


@dataclass
class MultiWindowVerdict:
    """Output of MultiWindowValidator."""
    edge_trend: EdgeTrend = EdgeTrend.INSUFFICIENT
    windows: List[WindowStats] = field(default_factory=list)
    trend_score: float = 0.5          # 0=decaying, 0.5=stable, 1=strengthening
    consistency: float = 0.0          # 0–1 cross-window agreement
    regime_dependency: float = 0.0    # 0–1 (0 = regime-independent)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_trend": self.edge_trend.value,
            "trend_score": round(self.trend_score, 4),
            "consistency": round(self.consistency, 4),
            "regime_dependency": round(self.regime_dependency, 4),
            "windows": [w.to_dict() for w in self.windows],
        }


# ═══════════════════════════════════════════════════════════════
#  EDGE FRAGILITY
# ═══════════════════════════════════════════════════════════════

@dataclass
class FragilityReport:
    """Output of EdgeFragilityAnalyzer."""
    fragility_score: float = 0.0     # 0=robust, 1=extremely fragile
    variance_penalty: float = 0.0
    drawdown_instability: float = 0.0
    win_concentration: float = 0.0   # dependence on few large wins
    regime_inconsistency: float = 0.0
    components: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fragility_score": round(self.fragility_score, 4),
            "variance_penalty": round(self.variance_penalty, 4),
            "drawdown_instability": round(self.drawdown_instability, 4),
            "win_concentration": round(self.win_concentration, 4),
            "regime_inconsistency": round(self.regime_inconsistency, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
        }


# ═══════════════════════════════════════════════════════════════
#  PROMOTION GATE
# ═══════════════════════════════════════════════════════════════

@dataclass
class PromotionVerdict:
    """Output of PromotionGate."""
    decision: PromotionDecision = PromotionDecision.DENIED_SAMPLE
    robustness_score: float = 0.0    # composite of confidence + fragility + windows
    required_trades: int = 0
    actual_trades: int = 0
    spike_detected: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "robustness_score": round(self.robustness_score, 4),
            "required_trades": self.required_trades,
            "actual_trades": self.actual_trades,
            "spike_detected": self.spike_detected,
            "reason": self.reason,
        }


# ═══════════════════════════════════════════════════════════════
#  ALLOCATION STABILITY
# ═══════════════════════════════════════════════════════════════

@dataclass
class StabilizedAllocation:
    """Output of AllocationStabilityGuard."""
    setup_key: str = ""
    raw_allocation: float = 1.0         # from EdgeAllocator
    stabilized_allocation: float = 1.0  # after smoothing
    previous_allocation: float = 1.0
    step_applied: float = 0.0           # actual delta
    capped: bool = False
    in_cooldown: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "raw_allocation": round(self.raw_allocation, 4),
            "stabilized_allocation": round(self.stabilized_allocation, 4),
            "previous_allocation": round(self.previous_allocation, 4),
            "step_applied": round(self.step_applied, 4),
            "capped": self.capped,
            "in_cooldown": self.in_cooldown,
            "reason": self.reason,
        }


# ═══════════════════════════════════════════════════════════════
#  RESEARCH SNAPSHOT
# ═══════════════════════════════════════════════════════════════

@dataclass
class ResearchSnapshot:
    """Periodic snapshot of the full robustness state of a setup."""
    setup_key: str = ""
    timestamp: float = field(default_factory=time.time)
    lifecycle_state: str = ""
    survival_score: float = 0.0
    confidence_score: float = 0.0
    fragility_score: float = 0.0
    edge_trend: str = ""
    robustness_score: float = 0.0
    allocation_pct: float = 1.0
    regime_fit: float = 0.5
    total_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "timestamp": self.timestamp,
            "lifecycle_state": self.lifecycle_state,
            "survival_score": round(self.survival_score, 4),
            "confidence_score": round(self.confidence_score, 4),
            "fragility_score": round(self.fragility_score, 4),
            "edge_trend": self.edge_trend,
            "robustness_score": round(self.robustness_score, 4),
            "allocation_pct": round(self.allocation_pct, 4),
            "regime_fit": round(self.regime_fit, 4),
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 2),
            "expectancy": round(self.expectancy, 2),
        }

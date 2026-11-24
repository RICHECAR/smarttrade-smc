"""
Portfolio Models — Data Structures for the Portfolio Intelligence Layer
═══════════════════════════════════════════════════════════════════════

Shared enums and dataclasses consumed by:
  PortfolioAllocator, CorrelationRiskEngine, PortfolioDrawdownGovernor,
  CapitalEfficiencyScorer, ConflictResolutionEngine, PortfolioStressTester.

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

class DrawdownPhase(str, Enum):
    NORMAL = "normal"           # DD < 5 %
    CAUTION = "caution"         # 5–10 %
    STRESSED = "stressed"       # 10–20 %
    CRITICAL = "critical"       # > 20 %
    RECOVERY = "recovery"       # rebounding after stress


class CorrelationLevel(str, Enum):
    LOW = "low"                 # < 0.30
    MODERATE = "moderate"       # 0.30 – 0.60
    HIGH = "high"               # 0.60 – 0.85
    EXTREME = "extreme"         # > 0.85


class StressScenario(str, Enum):
    VOLATILITY_SPIKE = "volatility_spike"
    CORRELATED_DRAWDOWN = "correlated_drawdown"
    SLIPPAGE_EXPANSION = "slippage_expansion"
    LIQUIDITY_DETERIORATION = "liquidity_deterioration"


class ConflictOutcome(str, Enum):
    WINNER = "winner"
    REDUCED = "reduced"
    REJECTED = "rejected"


# ═══════════════════════════════════════════════════════════════
#  PORTFOLIO ALLOCATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class SetupOpportunity:
    """Input to the allocator: one candidate setup."""
    setup_key: str = ""
    symbol: str = ""
    direction: str = ""                 # long / short
    adjusted_edge: float = 0.0          # from ExecutionAdjustedEdgeEngine
    robustness_score: float = 0.0       # from PromotionGate
    regime_fit: float = 0.0             # from RegimeSetupMatrix
    execution_quality: float = 1.0      # from ExecutionQualityAnalyzer
    requested_allocation_pct: float = 0.0  # from EdgeAllocator
    capital_required: float = 0.0       # notional USD

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "symbol": self.symbol,
            "direction": self.direction,
            "adjusted_edge": round(self.adjusted_edge, 4),
            "robustness_score": round(self.robustness_score, 4),
            "regime_fit": round(self.regime_fit, 4),
            "execution_quality": round(self.execution_quality, 4),
            "requested_allocation_pct": round(self.requested_allocation_pct, 4),
            "capital_required": round(self.capital_required, 2),
        }


@dataclass
class PortfolioAllocation:
    """Output of PortfolioAllocator: final allocation for one setup."""
    setup_key: str = ""
    symbol: str = ""
    allocated_pct: float = 0.0          # final % of portfolio
    allocated_capital: float = 0.0      # USD
    priority_rank: int = 0
    adjustments: Dict[str, float] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "symbol": self.symbol,
            "allocated_pct": round(self.allocated_pct, 4),
            "allocated_capital": round(self.allocated_capital, 2),
            "priority_rank": self.priority_rank,
            "adjustments": {k: round(v, 4) for k, v in self.adjustments.items()},
            "reason": self.reason,
        }


@dataclass
class PortfolioSnapshot:
    """Full allocation snapshot across all setups."""
    total_capital: float = 0.0
    allocated_capital: float = 0.0
    free_capital: float = 0.0
    utilization: float = 0.0
    allocations: List[PortfolioAllocation] = field(default_factory=list)
    drawdown_phase: str = "normal"
    correlation_haircut: float = 0.0    # total pct removed by correlation
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_capital": round(self.total_capital, 2),
            "allocated_capital": round(self.allocated_capital, 2),
            "free_capital": round(self.free_capital, 2),
            "utilization": round(self.utilization, 4),
            "allocations": [a.to_dict() for a in self.allocations],
            "drawdown_phase": self.drawdown_phase,
            "correlation_haircut": round(self.correlation_haircut, 4),
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  CORRELATION RISK
# ═══════════════════════════════════════════════════════════════

@dataclass
class CorrelationPair:
    """Measured correlation between two positions/symbols."""
    key_a: str = ""
    key_b: str = ""
    correlation: float = 0.0
    level: CorrelationLevel = CorrelationLevel.LOW
    shared_risk_factor: str = ""        # e.g. "crypto_beta", "usd_strength"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key_a": self.key_a,
            "key_b": self.key_b,
            "correlation": round(self.correlation, 4),
            "level": self.level.value,
            "shared_risk_factor": self.shared_risk_factor,
        }


@dataclass
class CorrelationRiskReport:
    """Output of CorrelationRiskEngine."""
    pairs: List[CorrelationPair] = field(default_factory=list)
    concentration_clusters: List[List[str]] = field(default_factory=list)
    max_cluster_exposure: float = 0.0   # highest combined alloc in one cluster
    haircut_map: Dict[str, float] = field(default_factory=dict)  # key → reduction mult
    portfolio_diversification_score: float = 1.0  # 0–1 (1=well diversified)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pairs": [p.to_dict() for p in self.pairs],
            "concentration_clusters": self.concentration_clusters,
            "max_cluster_exposure": round(self.max_cluster_exposure, 4),
            "haircut_map": {k: round(v, 4) for k, v in self.haircut_map.items()},
            "portfolio_diversification_score": round(self.portfolio_diversification_score, 4),
        }


# ═══════════════════════════════════════════════════════════════
#  DRAWDOWN GOVERNOR
# ═══════════════════════════════════════════════════════════════

@dataclass
class DrawdownState:
    """Current drawdown state of the portfolio."""
    peak_equity: float = 0.0
    current_equity: float = 0.0
    drawdown_pct: float = 0.0           # 0–1
    phase: DrawdownPhase = DrawdownPhase.NORMAL
    allocation_multiplier: float = 1.0  # applied to all allocations
    max_concurrent_positions: int = 10
    aggression_level: float = 1.0       # 0–1
    recovery_progress: float = 0.0      # 0–1 (how far back to peak)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "peak_equity": round(self.peak_equity, 2),
            "current_equity": round(self.current_equity, 2),
            "drawdown_pct": round(self.drawdown_pct, 4),
            "phase": self.phase.value,
            "allocation_multiplier": round(self.allocation_multiplier, 4),
            "max_concurrent_positions": self.max_concurrent_positions,
            "aggression_level": round(self.aggression_level, 4),
            "recovery_progress": round(self.recovery_progress, 4),
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  CAPITAL EFFICIENCY
# ═══════════════════════════════════════════════════════════════

@dataclass
class EfficiencyScore:
    """Output of CapitalEfficiencyScorer for one setup."""
    setup_key: str = ""
    risk_adjusted_return: float = 0.0   # edge / risk proxy
    capital_turnover: float = 0.0       # trades per unit capital per day
    sharpe_proxy: float = 0.0           # mean return / volatility
    efficiency_score: float = 0.0       # composite 0–1
    rank: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "risk_adjusted_return": round(self.risk_adjusted_return, 4),
            "capital_turnover": round(self.capital_turnover, 4),
            "sharpe_proxy": round(self.sharpe_proxy, 4),
            "efficiency_score": round(self.efficiency_score, 4),
            "rank": self.rank,
        }


# ═══════════════════════════════════════════════════════════════
#  CONFLICT RESOLUTION
# ═══════════════════════════════════════════════════════════════

@dataclass
class ConflictDecision:
    """Outcome for one setup in a conflict."""
    setup_key: str = ""
    outcome: ConflictOutcome = ConflictOutcome.WINNER
    composite_score: float = 0.0
    original_alloc_pct: float = 0.0
    final_alloc_pct: float = 0.0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "outcome": self.outcome.value,
            "composite_score": round(self.composite_score, 4),
            "original_alloc_pct": round(self.original_alloc_pct, 4),
            "final_alloc_pct": round(self.final_alloc_pct, 4),
            "reason": self.reason,
        }


@dataclass
class ConflictReport:
    """Full conflict resolution output."""
    competing_setups: int = 0
    available_budget_pct: float = 0.0
    decisions: List[ConflictDecision] = field(default_factory=list)
    total_requested: float = 0.0
    total_allocated: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "competing_setups": self.competing_setups,
            "available_budget_pct": round(self.available_budget_pct, 4),
            "decisions": [d.to_dict() for d in self.decisions],
            "total_requested": round(self.total_requested, 4),
            "total_allocated": round(self.total_allocated, 4),
        }


# ═══════════════════════════════════════════════════════════════
#  STRESS TESTING
# ═══════════════════════════════════════════════════════════════

@dataclass
class StressResult:
    """Outcome of one stress scenario on one setup."""
    setup_key: str = ""
    scenario: StressScenario = StressScenario.VOLATILITY_SPIKE
    original_edge: float = 0.0
    stressed_edge: float = 0.0
    edge_survival_pct: float = 1.0      # stressed / original
    recommended_alloc_mult: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "scenario": self.scenario.value,
            "original_edge": round(self.original_edge, 4),
            "stressed_edge": round(self.stressed_edge, 4),
            "edge_survival_pct": round(self.edge_survival_pct, 4),
            "recommended_alloc_mult": round(self.recommended_alloc_mult, 4),
        }


@dataclass
class StressReport:
    """Full stress test output across all scenarios."""
    setup_key: str = ""
    results: List[StressResult] = field(default_factory=list)
    worst_scenario: str = ""
    worst_survival: float = 1.0
    composite_resilience: float = 1.0   # avg survival across scenarios

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "results": [r.to_dict() for r in self.results],
            "worst_scenario": self.worst_scenario,
            "worst_survival": round(self.worst_survival, 4),
            "composite_resilience": round(self.composite_resilience, 4),
        }

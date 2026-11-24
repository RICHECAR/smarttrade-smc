"""
Self-Evolving Trading Intelligence — Data Models
═════════════════════════════════════════════════

Shared enums and dataclasses for the self-evolution pipeline:

  StrategyEvolutionEngine, MetaLearningEngine, ParameterAdaptationEngine,
  FailurePatternDetector, DiscoveryEngine, ContinuousImprovementLoop.

Never modifies existing files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


# ═══════════════════════════════════════════════════════════════
#  ENUMS
# ═══════════════════════════════════════════════════════════════

class StrategyHealth(str, Enum):
    THRIVING = "thriving"           # improving performance
    STABLE = "stable"               # consistent
    DEGRADING = "degrading"         # declining performance
    CRITICAL = "critical"           # severe underperformance
    DORMANT = "dormant"             # insufficient data


class AdaptationAction(str, Enum):
    INCREASE_WEIGHT = "increase_weight"
    DECREASE_WEIGHT = "decrease_weight"
    TIGHTEN_THRESHOLD = "tighten_threshold"
    LOOSEN_THRESHOLD = "loosen_threshold"
    DISABLE_TEMPORARILY = "disable_temporarily"
    RE_ENABLE = "re_enable"
    NO_CHANGE = "no_change"


class FailureType(str, Enum):
    BAD_TIMING = "bad_timing"
    OVER_AGGRESSION = "over_aggression"
    REGIME_MISMATCH = "regime_mismatch"
    SIGNAL_DECAY = "signal_decay"
    CORRELATION_TRAP = "correlation_trap"
    LIQUIDITY_MISJUDGE = "liquidity_misjudge"
    FALSE_BREAKOUT = "false_breakout"


class LearningSource(str, Enum):
    WINNING_TRADE = "winning_trade"
    LOSING_TRADE = "losing_trade"
    MISSED_OPPORTUNITY = "missed_opportunity"
    FALSE_SIGNAL = "false_signal"
    REGIME_SHIFT = "regime_shift"
    PARAMETER_OUTCOME = "parameter_outcome"


class DiscoveryType(str, Enum):
    EMERGING_PATTERN = "emerging_pattern"
    NEW_CORRELATION = "new_correlation"
    REGIME_BEHAVIOR = "regime_behavior"
    TIMING_EDGE = "timing_edge"
    STRUCTURAL_ANOMALY = "structural_anomaly"


class EvolutionPhase(str, Enum):
    OBSERVATION = "observation"         # gathering data
    ANALYSIS = "analysis"               # analysing patterns
    ADAPTATION = "adaptation"           # applying changes
    VALIDATION = "validation"           # verifying improvements
    DEPLOYMENT = "deployment"           # changes active


# ═══════════════════════════════════════════════════════════════
#  STRATEGY EVOLUTION
# ═══════════════════════════════════════════════════════════════

@dataclass
class StrategyPerformance:
    """Performance snapshot for a single strategy/setup."""
    strategy_name: str = ""
    setup_family: str = ""
    health: StrategyHealth = StrategyHealth.DORMANT
    win_rate_recent: float = 0.0            # last N trades
    win_rate_baseline: float = 0.0          # historical average
    avg_pnl_recent: float = 0.0
    avg_pnl_baseline: float = 0.0
    sharpe_recent: float = 0.0
    sharpe_baseline: float = 0.0
    trade_count_recent: int = 0
    trend_direction: float = 0.0            # -1 to +1 (degrading to improving)
    confidence: float = 0.0                 # 0–1 data reliability
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "setup_family": self.setup_family,
            "health": self.health.value,
            "win_rate_recent": round(self.win_rate_recent, 4),
            "win_rate_baseline": round(self.win_rate_baseline, 4),
            "avg_pnl_recent": round(self.avg_pnl_recent, 4),
            "avg_pnl_baseline": round(self.avg_pnl_baseline, 4),
            "sharpe_recent": round(self.sharpe_recent, 4),
            "sharpe_baseline": round(self.sharpe_baseline, 4),
            "trade_count_recent": self.trade_count_recent,
            "trend_direction": round(self.trend_direction, 4),
            "confidence": round(self.confidence, 4),
            "ts": self.timestamp,
        }


@dataclass
class EvolutionDecision:
    """A single weight/threshold adjustment."""
    strategy_name: str = ""
    action: AdaptationAction = AdaptationAction.NO_CHANGE
    parameter_name: str = ""
    old_value: float = 0.0
    new_value: float = 0.0
    reason: str = ""
    confidence: float = 0.0
    reversible: bool = True
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "action": self.action.value,
            "parameter_name": self.parameter_name,
            "old_value": round(self.old_value, 6),
            "new_value": round(self.new_value, 6),
            "reason": self.reason,
            "confidence": round(self.confidence, 4),
            "reversible": self.reversible,
            "ts": self.timestamp,
        }


@dataclass
class EvolutionReport:
    """Aggregate output of StrategyEvolutionEngine."""
    performances: List[StrategyPerformance] = field(default_factory=list)
    decisions: List[EvolutionDecision] = field(default_factory=list)
    thriving_count: int = 0
    degrading_count: int = 0
    total_adaptations: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "performances": [p.to_dict() for p in self.performances],
            "decisions": [d.to_dict() for d in self.decisions],
            "thriving_count": self.thriving_count,
            "degrading_count": self.degrading_count,
            "total_adaptations": self.total_adaptations,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  META-LEARNING
# ═══════════════════════════════════════════════════════════════

@dataclass
class LearnedPattern:
    """A pattern discovered from past outcomes."""
    pattern_id: str = ""
    source: LearningSource = LearningSource.LOSING_TRADE
    description: str = ""
    conditions: Dict[str, Any] = field(default_factory=dict)
    outcome_correlation: float = 0.0        # -1 to +1
    sample_size: int = 0
    confidence: float = 0.0
    actionable: bool = False
    recommended_action: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "source": self.source.value,
            "description": self.description,
            "conditions": self.conditions,
            "outcome_correlation": round(self.outcome_correlation, 4),
            "sample_size": self.sample_size,
            "confidence": round(self.confidence, 4),
            "actionable": self.actionable,
            "recommended_action": self.recommended_action,
            "ts": self.timestamp,
        }


@dataclass
class MetaLearningReport:
    """Output of the MetaLearningEngine."""
    patterns_discovered: List[LearnedPattern] = field(default_factory=list)
    total_patterns: int = 0
    actionable_count: int = 0
    high_confidence_count: int = 0
    newest_pattern: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "patterns_discovered": [p.to_dict() for p in self.patterns_discovered],
            "total_patterns": self.total_patterns,
            "actionable_count": self.actionable_count,
            "high_confidence_count": self.high_confidence_count,
            "newest_pattern": self.newest_pattern,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  PARAMETER ADAPTATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class ParameterState:
    """Current state of a dynamically managed parameter."""
    param_name: str = ""
    module_name: str = ""
    current_value: float = 0.0
    default_value: float = 0.0
    min_bound: float = 0.0
    max_bound: float = 1.0
    last_change: float = 0.0                # delta of last adjustment
    change_count: int = 0
    effectiveness_score: float = 0.0        # 0–1 how well the change worked
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "param_name": self.param_name,
            "module_name": self.module_name,
            "current_value": round(self.current_value, 6),
            "default_value": round(self.default_value, 6),
            "min_bound": round(self.min_bound, 6),
            "max_bound": round(self.max_bound, 6),
            "last_change": round(self.last_change, 6),
            "change_count": self.change_count,
            "effectiveness_score": round(self.effectiveness_score, 4),
            "ts": self.timestamp,
        }


@dataclass
class AdaptationPlan:
    """Batch of parameter adjustments to apply."""
    adaptations: List[ParameterState] = field(default_factory=list)
    total_changes: int = 0
    avg_effectiveness: float = 0.0
    rollback_available: bool = True
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adaptations": [a.to_dict() for a in self.adaptations],
            "total_changes": self.total_changes,
            "avg_effectiveness": round(self.avg_effectiveness, 4),
            "rollback_available": self.rollback_available,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  FAILURE PATTERN DETECTION
# ═══════════════════════════════════════════════════════════════

@dataclass
class FailurePattern:
    """A recurring failure pattern identified from trade history."""
    pattern_id: str = ""
    failure_type: FailureType = FailureType.BAD_TIMING
    description: str = ""
    occurrence_count: int = 0
    avg_loss_pct: float = 0.0
    conditions: Dict[str, Any] = field(default_factory=dict)
    exposure_reduction: float = 0.0         # 0–1 recommended reduction
    suppression_active: bool = False
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "failure_type": self.failure_type.value,
            "description": self.description,
            "occurrence_count": self.occurrence_count,
            "avg_loss_pct": round(self.avg_loss_pct, 4),
            "conditions": self.conditions,
            "exposure_reduction": round(self.exposure_reduction, 4),
            "suppression_active": self.suppression_active,
            "confidence": round(self.confidence, 4),
            "ts": self.timestamp,
        }


@dataclass
class FailureReport:
    """Output of the FailurePatternDetector."""
    patterns: List[FailurePattern] = field(default_factory=list)
    total_patterns: int = 0
    active_suppressions: int = 0
    total_exposure_reduction: float = 0.0
    worst_pattern: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "patterns": [p.to_dict() for p in self.patterns],
            "total_patterns": self.total_patterns,
            "active_suppressions": self.active_suppressions,
            "total_exposure_reduction": round(self.total_exposure_reduction, 4),
            "worst_pattern": self.worst_pattern,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  DISCOVERY ENGINE
# ═══════════════════════════════════════════════════════════════

@dataclass
class DiscoveredEdge:
    """A newly identified potential edge."""
    edge_id: str = ""
    discovery_type: DiscoveryType = DiscoveryType.EMERGING_PATTERN
    description: str = ""
    evidence_strength: float = 0.0          # 0–1
    sample_size: int = 0
    expected_edge_pct: float = 0.0          # expected improvement
    symbols_affected: List[str] = field(default_factory=list)
    conditions: Dict[str, Any] = field(default_factory=dict)
    validated: bool = False
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "discovery_type": self.discovery_type.value,
            "description": self.description,
            "evidence_strength": round(self.evidence_strength, 4),
            "sample_size": self.sample_size,
            "expected_edge_pct": round(self.expected_edge_pct, 4),
            "symbols_affected": self.symbols_affected,
            "conditions": self.conditions,
            "validated": self.validated,
            "ts": self.timestamp,
        }


@dataclass
class DiscoveryReport:
    """Output of the DiscoveryEngine."""
    edges: List[DiscoveredEdge] = field(default_factory=list)
    total_edges: int = 0
    validated_count: int = 0
    strongest_edge: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edges": [e.to_dict() for e in self.edges],
            "total_edges": self.total_edges,
            "validated_count": self.validated_count,
            "strongest_edge": self.strongest_edge,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  CONTINUOUS IMPROVEMENT LOOP
# ═══════════════════════════════════════════════════════════════

@dataclass
class ImprovementCycle:
    """One complete execution→result→analysis→adaptation→deploy cycle."""
    cycle_id: int = 0
    phase: EvolutionPhase = EvolutionPhase.OBSERVATION
    evolution_summary: str = ""
    learning_summary: str = ""
    adaptation_summary: str = ""
    failure_summary: str = ""
    discovery_summary: str = ""
    total_adaptations_applied: int = 0
    total_failures_suppressed: int = 0
    total_edges_discovered: int = 0
    net_improvement_score: float = 0.0      # -1 to +1
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "phase": self.phase.value,
            "evolution_summary": self.evolution_summary,
            "learning_summary": self.learning_summary,
            "adaptation_summary": self.adaptation_summary,
            "failure_summary": self.failure_summary,
            "discovery_summary": self.discovery_summary,
            "total_adaptations_applied": self.total_adaptations_applied,
            "total_failures_suppressed": self.total_failures_suppressed,
            "total_edges_discovered": self.total_edges_discovered,
            "net_improvement_score": round(self.net_improvement_score, 4),
            "ts": self.timestamp,
        }


@dataclass
class EvolutionState:
    """Global state of the self-evolving system."""
    total_cycles: int = 0
    current_phase: EvolutionPhase = EvolutionPhase.OBSERVATION
    strategies_monitored: int = 0
    patterns_learned: int = 0
    parameters_adapted: int = 0
    failures_detected: int = 0
    edges_discovered: int = 0
    cumulative_improvement: float = 0.0
    last_cycle: ImprovementCycle | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_cycles": self.total_cycles,
            "current_phase": self.current_phase.value,
            "strategies_monitored": self.strategies_monitored,
            "patterns_learned": self.patterns_learned,
            "parameters_adapted": self.parameters_adapted,
            "failures_detected": self.failures_detected,
            "edges_discovered": self.edges_discovered,
            "cumulative_improvement": round(self.cumulative_improvement, 4),
            "last_cycle": self.last_cycle.to_dict() if self.last_cycle else None,
            "ts": self.timestamp,
        }

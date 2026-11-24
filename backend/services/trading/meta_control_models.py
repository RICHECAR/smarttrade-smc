"""
Meta-Control Models — Data Structures for the Meta-Control Layer
═══════════════════════════════════════════════════════════════

Shared enums and dataclasses consumed by:
  MetaControlEngine, GlobalRiskBudgetController, OpportunityUniverseFilter,
  RecoveryProtocolEngine, StrategicRotationEngine, MetaHealthMonitor.

Never touches existing files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


# ═══════════════════════════════════════════════════════════════
#  ENUMS
# ═══════════════════════════════════════════════════════════════

class SystemMode(str, Enum):
    EXPANSION = "expansion"       # Full aggression, max capital deployment
    OFFENSIVE = "offensive"       # Strong but not reckless
    SELECTIVE = "selective"       # Cherry-picking best setups only
    DEFENSIVE = "defensive"       # Capital preservation priority
    SURVIVAL = "survival"         # Minimal exposure, protect capital


class RecoveryPhase(str, Enum):
    NONE = "none"                 # No recovery needed
    STABILISATION = "stabilisation"  # Just exited crisis — do not expand
    REBUILDING = "rebuilding"     # Gradually restoring positions
    VERIFICATION = "verification" # Verifying edge before full return
    RESTORED = "restored"         # Back to normal operations


class HealthDomain(str, Enum):
    EXECUTION = "execution"
    SIGNAL_QUALITY = "signal_quality"
    PORTFOLIO_STRESS = "portfolio_stress"
    POLICY_DEGRADATION = "policy_degradation"
    DATA_FRESHNESS = "data_freshness"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    DEGRADED = "degraded"
    CRITICAL = "critical"


class BlockReason(str, Enum):
    LOW_EDGE = "low_edge"
    POOR_EXECUTION = "poor_execution"
    BAD_REGIME = "bad_regime"
    CORRELATION_OVERLOAD = "correlation_overload"
    SESSION_BANNED = "session_banned"
    RECOVERY_RESTRICTION = "recovery_restriction"
    HEALTH_DEGRADED = "health_degraded"


# ═══════════════════════════════════════════════════════════════
#  META-CONTROL ENGINE
# ═══════════════════════════════════════════════════════════════

@dataclass
class ModeInputs:
    """Aggregated inputs for mode determination."""
    drawdown_pct: float = 0.0           # 0–1 from DrawdownGovernor
    drawdown_phase: str = "normal"
    avg_edge_quality: float = 1.0       # 0–1 avg adjusted edge across active setups
    avg_execution_quality: float = 1.0  # 0–1 avg execution quality
    avg_reliability: float = 1.0        # 0–1 avg fill reliability
    regime_quality: float = 1.0         # 0–1 from regime detection
    active_setup_count: int = 0
    avg_robustness: float = 1.0         # 0–1 from promotion gate
    stack_health: float = 1.0           # 0–1 from MetaHealthMonitor


@dataclass
class ModeDecision:
    """Output of MetaControlEngine."""
    mode: SystemMode = SystemMode.SELECTIVE
    previous_mode: SystemMode = SystemMode.SELECTIVE
    mode_changed: bool = False
    confidence: float = 0.0             # 0–1 confidence in mode selection
    allocation_multiplier: float = 1.0
    max_concurrent: int = 10
    aggression: float = 1.0             # 0–1
    min_edge_threshold: float = 0.0     # min adjusted edge to accept a setup
    min_robustness_threshold: float = 0.0
    reason: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "previous_mode": self.previous_mode.value,
            "mode_changed": self.mode_changed,
            "confidence": round(self.confidence, 4),
            "allocation_multiplier": round(self.allocation_multiplier, 4),
            "max_concurrent": self.max_concurrent,
            "aggression": round(self.aggression, 4),
            "min_edge_threshold": round(self.min_edge_threshold, 4),
            "min_robustness_threshold": round(self.min_robustness_threshold, 4),
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  GLOBAL RISK BUDGET
# ═══════════════════════════════════════════════════════════════

@dataclass
class RiskBudget:
    """Current risk budget parameters."""
    mode: str = "selective"
    max_total_exposure: float = 0.90
    max_concurrent_positions: int = 10
    max_per_symbol_cluster: float = 0.30
    max_per_setup_family: float = 0.25
    min_cash_reserve: float = 0.10
    max_single_position: float = 0.20
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "max_total_exposure": round(self.max_total_exposure, 4),
            "max_concurrent_positions": self.max_concurrent_positions,
            "max_per_symbol_cluster": round(self.max_per_symbol_cluster, 4),
            "max_per_setup_family": round(self.max_per_setup_family, 4),
            "min_cash_reserve": round(self.min_cash_reserve, 4),
            "max_single_position": round(self.max_single_position, 4),
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  OPPORTUNITY UNIVERSE FILTER
# ═══════════════════════════════════════════════════════════════

@dataclass
class UniverseState:
    """Current allowed/blocked universe."""
    blocked_symbols: Set[str] = field(default_factory=set)
    blocked_families: Set[str] = field(default_factory=set)
    blocked_regimes: Set[str] = field(default_factory=set)
    blocked_hours: Set[int] = field(default_factory=set)
    block_reasons: Dict[str, str] = field(default_factory=dict)  # item → reason
    allowed_count: int = 0
    blocked_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "blocked_symbols": sorted(self.blocked_symbols),
            "blocked_families": sorted(self.blocked_families),
            "blocked_regimes": sorted(self.blocked_regimes),
            "blocked_hours": sorted(self.blocked_hours),
            "block_reasons": self.block_reasons,
            "allowed_count": self.allowed_count,
            "blocked_count": self.blocked_count,
        }


@dataclass
class FilterVerdict:
    """Verdict for a single opportunity."""
    setup_key: str = ""
    allowed: bool = True
    block_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "allowed": self.allowed,
            "block_reason": self.block_reason,
        }


# ═══════════════════════════════════════════════════════════════
#  RECOVERY PROTOCOL
# ═══════════════════════════════════════════════════════════════

@dataclass
class RecoveryState:
    """Current recovery protocol state."""
    phase: RecoveryPhase = RecoveryPhase.NONE
    trigger_drawdown: float = 0.0       # DD that triggered recovery
    current_drawdown: float = 0.0
    cycles_in_phase: int = 0
    allocation_cap: float = 1.0         # 0–1 cap on allocations
    min_evidence_trades: int = 0        # trades needed before re-expansion
    expansion_allowed: bool = True
    progress: float = 0.0               # 0–1 toward full restoration
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase.value,
            "trigger_drawdown": round(self.trigger_drawdown, 4),
            "current_drawdown": round(self.current_drawdown, 4),
            "cycles_in_phase": self.cycles_in_phase,
            "allocation_cap": round(self.allocation_cap, 4),
            "min_evidence_trades": self.min_evidence_trades,
            "expansion_allowed": self.expansion_allowed,
            "progress": round(self.progress, 4),
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  STRATEGIC ROTATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class RotationCandidate:
    """Input: one candidate for strategic rotation."""
    setup_key: str = ""
    symbol: str = ""
    adjusted_edge: float = 0.0
    robustness_score: float = 0.0
    execution_quality: float = 1.0
    diversification_value: float = 0.5
    capital_efficiency: float = 0.5
    regime_fit: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "symbol": self.symbol,
            "adjusted_edge": round(self.adjusted_edge, 4),
            "robustness_score": round(self.robustness_score, 4),
            "execution_quality": round(self.execution_quality, 4),
            "diversification_value": round(self.diversification_value, 4),
            "capital_efficiency": round(self.capital_efficiency, 4),
            "regime_fit": round(self.regime_fit, 4),
        }


@dataclass
class RotationDecision:
    """Output: ranked and filtered rotation result."""
    setup_key: str = ""
    composite_score: float = 0.0
    rank: int = 0
    favored: bool = True
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "composite_score": round(self.composite_score, 4),
            "rank": self.rank,
            "favored": self.favored,
            "reason": self.reason,
        }


# ═══════════════════════════════════════════════════════════════
#  META HEALTH MONITOR
# ═══════════════════════════════════════════════════════════════

@dataclass
class DomainHealth:
    """Health of one subsystem domain."""
    domain: HealthDomain = HealthDomain.EXECUTION
    score: float = 1.0                  # 0–1
    status: HealthStatus = HealthStatus.HEALTHY
    details: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain.value,
            "score": round(self.score, 4),
            "status": self.status.value,
            "details": self.details,
        }


@dataclass
class StackHealthReport:
    """Aggregated health of the full trading stack."""
    domains: List[DomainHealth] = field(default_factory=list)
    overall_score: float = 1.0
    overall_status: HealthStatus = HealthStatus.HEALTHY
    degradation_active: bool = False
    degradation_mult: float = 1.0       # multiplier applied to allocations
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domains": [d.to_dict() for d in self.domains],
            "overall_score": round(self.overall_score, 4),
            "overall_status": self.overall_status.value,
            "degradation_active": self.degradation_active,
            "degradation_mult": round(self.degradation_mult, 4),
            "timestamp": self.timestamp,
        }

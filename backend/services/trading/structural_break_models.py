"""
Structural Break Intelligence Layer — Data Models
══════════════════════════════════════════════════

Shared enums and dataclasses for:
  StructuralBreakDetector, RegimeTransitionMonitor, DoctrineValidityEngine,
  ShockPropagationAnalyzer, PreemptiveThrottleEngine, FalseRecoveryGuard.

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

class BreakType(str, Enum):
    VOLATILITY_SHOCK = "volatility_shock"
    LIQUIDITY_DETERIORATION = "liquidity_deterioration"
    SPREAD_EXPANSION = "spread_expansion"
    EXECUTION_BREAK = "execution_break"
    CORRELATION_BREAK = "correlation_break"
    OPPORTUNITY_COLLAPSE = "opportunity_collapse"


class BreakSeverity(str, Enum):
    NONE = "none"
    MINOR = "minor"             # early signal, watch closely
    MODERATE = "moderate"       # tighten parameters
    SEVERE = "severe"           # pre-emptive throttle
    CRITICAL = "critical"       # immediate defensive posture


class TransitionType(str, Enum):
    TREND_EXHAUSTION = "trend_exhaustion"
    RANGE_TO_BREAKOUT = "range_to_breakout"
    BREAKOUT_FAILURE = "breakout_failure"
    COMPRESSION_TO_EXPANSION = "compression_to_expansion"
    UNSTABLE_MIXED = "unstable_mixed"


class DoctrineAction(str, Enum):
    VALID = "valid"
    DEGRADE = "degrade"
    FREEZE = "freeze"
    ROTATE = "rotate"


class ShockScope(str, Enum):
    LOCAL = "local"             # single symbol/setup
    CLUSTER = "cluster"         # correlated group
    SYSTEMIC = "systemic"       # system-wide


class ThrottleLevel(str, Enum):
    NONE = "none"
    LIGHT = "light"             # 80% capacity
    MODERATE = "moderate"       # 50% capacity
    HEAVY = "heavy"             # 25% capacity
    HALT = "halt"               # 0% new positions


class RecoveryValidity(str, Enum):
    INVALID = "invalid"
    PARTIAL = "partial"
    VALID = "valid"


# ═══════════════════════════════════════════════════════════════
#  STRUCTURAL BREAK DETECTOR
# ═══════════════════════════════════════════════════════════════

@dataclass
class BreakSignal:
    """A single detected structural break."""
    break_type: BreakType = BreakType.VOLATILITY_SHOCK
    severity: BreakSeverity = BreakSeverity.NONE
    confidence: float = 0.0
    z_score: float = 0.0
    affected_symbols: List[str] = field(default_factory=list)
    affected_setups: List[str] = field(default_factory=list)
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "break_type": self.break_type.value,
            "severity": self.severity.value,
            "confidence": round(self.confidence, 4),
            "z_score": round(self.z_score, 4),
            "affected_symbols": self.affected_symbols,
            "affected_setups": self.affected_setups,
            "detail": self.detail,
            "ts": self.timestamp,
        }


@dataclass
class BreakReport:
    """Aggregate structural break assessment."""
    breaks: List[BreakSignal] = field(default_factory=list)
    max_severity: BreakSeverity = BreakSeverity.NONE
    composite_stress: float = 0.0       # 0–1
    active_break_count: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "breaks": [b.to_dict() for b in self.breaks],
            "max_severity": self.max_severity.value,
            "composite_stress": round(self.composite_stress, 4),
            "active_break_count": self.active_break_count,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  REGIME TRANSITION MONITOR
# ═══════════════════════════════════════════════════════════════

@dataclass
class TransitionSignal:
    """A detected regime transition."""
    transition_type: TransitionType = TransitionType.UNSTABLE_MIXED
    confidence: float = 0.0
    fragility_impact: float = 0.0       # 0–1 expected impact on setups
    source_regime: str = ""
    target_regime: str = ""
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "transition_type": self.transition_type.value,
            "confidence": round(self.confidence, 4),
            "fragility_impact": round(self.fragility_impact, 4),
            "source_regime": self.source_regime,
            "target_regime": self.target_regime,
            "detail": self.detail,
            "ts": self.timestamp,
        }


@dataclass
class TransitionReport:
    """Aggregate transition assessment."""
    transitions: List[TransitionSignal] = field(default_factory=list)
    is_unstable: bool = False
    max_fragility: float = 0.0
    transition_count: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "transitions": [t.to_dict() for t in self.transitions],
            "is_unstable": self.is_unstable,
            "max_fragility": round(self.max_fragility, 4),
            "transition_count": self.transition_count,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  DOCTRINE VALIDITY ENGINE
# ═══════════════════════════════════════════════════════════════

@dataclass
class DoctrineMismatch:
    """A single doctrine mismatch dimension."""
    dimension: str = ""         # e.g. "mode_vs_regime", "setups_vs_execution"
    expected: str = ""
    actual: str = ""
    severity: float = 0.0      # 0–1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension,
            "expected": self.expected,
            "actual": self.actual,
            "severity": round(self.severity, 4),
        }


@dataclass
class DoctrineVerdict:
    """Output of DoctrineValidityEngine."""
    validity_score: float = 1.0         # 0–1, 1=fully valid
    action: DoctrineAction = DoctrineAction.VALID
    mismatches: List[DoctrineMismatch] = field(default_factory=list)
    mismatch_count: int = 0
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "validity_score": round(self.validity_score, 4),
            "action": self.action.value,
            "mismatches": [m.to_dict() for m in self.mismatches],
            "mismatch_count": self.mismatch_count,
            "detail": self.detail,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  SHOCK PROPAGATION ANALYZER
# ═══════════════════════════════════════════════════════════════

@dataclass
class PropagationResult:
    """Output of ShockPropagationAnalyzer."""
    scope: ShockScope = ShockScope.LOCAL
    propagation_score: float = 0.0      # 0–1
    affected_symbols: List[str] = field(default_factory=list)
    affected_families: List[str] = field(default_factory=list)
    affected_sessions: List[str] = field(default_factory=list)
    containment: str = "none"           # none / partial / contained
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope.value,
            "propagation_score": round(self.propagation_score, 4),
            "affected_symbols": self.affected_symbols,
            "affected_families": self.affected_families,
            "affected_sessions": self.affected_sessions,
            "containment": self.containment,
            "detail": self.detail,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  PREEMPTIVE THROTTLE ENGINE
# ═══════════════════════════════════════════════════════════════

@dataclass
class ThrottleState:
    """Current throttle configuration."""
    level: ThrottleLevel = ThrottleLevel.NONE
    exposure_multiplier: float = 1.0    # 0–1
    size_multiplier: float = 1.0        # 0–1
    max_concurrent_override: int = 0    # 0 = no override
    blocked_setups: List[str] = field(default_factory=list)
    reason: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "exposure_multiplier": round(self.exposure_multiplier, 4),
            "size_multiplier": round(self.size_multiplier, 4),
            "max_concurrent_override": self.max_concurrent_override,
            "blocked_setups": self.blocked_setups,
            "reason": self.reason,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  FALSE RECOVERY GUARD
# ═══════════════════════════════════════════════════════════════

@dataclass
class RecoveryCheckResult:
    """Output of FalseRecoveryGuard."""
    validity: RecoveryValidity = RecoveryValidity.INVALID
    validity_score: float = 0.0         # 0–1
    execution_normalized: bool = False
    opportunity_recovered: bool = False
    regime_stabilized: bool = False
    doctrine_resolved: bool = False
    recommendation: str = "hold"        # hold / partial_restore / restore
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "validity": self.validity.value,
            "validity_score": round(self.validity_score, 4),
            "execution_normalized": self.execution_normalized,
            "opportunity_recovered": self.opportunity_recovered,
            "regime_stabilized": self.regime_stabilized,
            "doctrine_resolved": self.doctrine_resolved,
            "recommendation": self.recommendation,
            "detail": self.detail,
            "ts": self.timestamp,
        }

"""
Validation & Governance Layer — Data Models
════════════════════════════════════════════

Shared enums and dataclasses for the governance pipeline:

  EvolutionValidationEngine, CandidateDeploymentManager,
  ABComparisonEngine, RollbackGovernor, CapitalScalingGovernor,
  LearningPaceController, GovernanceStore.

Never modifies existing files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
#  ENUMS
# ═══════════════════════════════════════════════════════════════

class ValidationVerdict(str, Enum):
    """Outcome of evolution validation."""
    APPROVE = "approve"
    HOLD = "hold"
    REJECT = "reject"


class DeploymentStage(str, Enum):
    """Progressive deployment lifecycle."""
    CANDIDATE = "candidate"
    SHADOW = "shadow"
    PAPER = "paper"
    LIMITED_LIVE = "limited_live"
    ACTIVE = "active"
    ROLLED_BACK = "rolled_back"


class ABOutcome(str, Enum):
    """A/B comparison result."""
    CANDIDATE_SUPERIOR = "candidate_superior"
    BASELINE_SUPERIOR = "baseline_superior"
    INCONCLUSIVE = "inconclusive"
    CANDIDATE_HARMFUL = "candidate_harmful"


class RollbackTrigger(str, Enum):
    """Why a rollback was triggered."""
    LIVE_DEGRADATION = "live_degradation"
    EXECUTION_FRAGILITY = "execution_fragility"
    STRUCTURAL_MISMATCH = "structural_mismatch"
    PORTFOLIO_DAMAGE = "portfolio_damage"
    MANUAL = "manual"
    AB_TEST_FAILURE = "ab_test_failure"
    CAPITAL_BREACH = "capital_breach"


class CapitalStage(str, Enum):
    """Capital scaling tiers."""
    SANDBOX = "sandbox"
    MICRO = "micro"
    SMALL = "small"
    MEDIUM = "medium"
    FULL = "full"


class LearningPace(str, Enum):
    """Adaptation speed control."""
    NORMAL = "normal"
    SLOW = "slow"
    FROZEN = "frozen"


class LearningBrake(str, Enum):
    """Reason for slowing / freezing learning."""
    NONE = "none"
    UNSTABLE_REGIME = "unstable_regime"
    WEAK_EVIDENCE = "weak_evidence"
    NOISY_EXECUTION = "noisy_execution"
    HIGH_PORTFOLIO_STRESS = "high_portfolio_stress"
    MULTIPLE = "multiple"


class GovernanceEventType(str, Enum):
    """Types of audit-trail events."""
    VALIDATION = "validation"
    DEPLOYMENT_TRANSITION = "deployment_transition"
    AB_COMPARISON = "ab_comparison"
    ROLLBACK = "rollback"
    CAPITAL_SCALING = "capital_scaling"
    LEARNING_PACE_CHANGE = "learning_pace_change"


# ═══════════════════════════════════════════════════════════════
#  EVOLUTION VALIDATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class EvidenceQuality:
    """Assessment of the evidence behind a proposed adaptation."""
    sample_size: int = 0
    min_required: int = 30
    win_rate_ci_lower: float = 0.0
    expectancy_stability: float = 0.0      # 0-1
    profit_factor_stability: float = 0.0   # 0-1
    is_sufficient: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "min_required": self.min_required,
            "win_rate_ci_lower": round(self.win_rate_ci_lower, 4),
            "expectancy_stability": round(self.expectancy_stability, 4),
            "profit_factor_stability": round(self.profit_factor_stability, 4),
            "is_sufficient": self.is_sufficient,
        }


@dataclass
class RobustnessCheck:
    """Cross-window and regime robustness assessment."""
    windows_tested: int = 0
    windows_positive: int = 0
    consistency_score: float = 0.0         # 0-1
    regime_dependency: float = 0.0         # 0-1 (0=independent)
    worst_window_expectancy: float = 0.0
    is_robust: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "windows_tested": self.windows_tested,
            "windows_positive": self.windows_positive,
            "consistency_score": round(self.consistency_score, 4),
            "regime_dependency": round(self.regime_dependency, 4),
            "worst_window_expectancy": round(self.worst_window_expectancy, 4),
            "is_robust": self.is_robust,
        }


@dataclass
class ExecutionAdjustedCheck:
    """Whether the improvement survives execution costs."""
    raw_improvement: float = 0.0           # before costs
    execution_adjusted_improvement: float = 0.0  # after costs
    execution_cost_ratio: float = 0.0      # cost / raw improvement
    survives_execution: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_improvement": round(self.raw_improvement, 4),
            "execution_adjusted_improvement": round(self.execution_adjusted_improvement, 4),
            "execution_cost_ratio": round(self.execution_cost_ratio, 4),
            "survives_execution": self.survives_execution,
        }


@dataclass
class FragilityCheck:
    """Whether the adaptation increases hidden fragility."""
    fragility_before: float = 0.0
    fragility_after: float = 0.0
    fragility_delta: float = 0.0
    max_acceptable_delta: float = 0.10
    is_safe: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fragility_before": round(self.fragility_before, 4),
            "fragility_after": round(self.fragility_after, 4),
            "fragility_delta": round(self.fragility_delta, 4),
            "max_acceptable_delta": round(self.max_acceptable_delta, 4),
            "is_safe": self.is_safe,
        }


@dataclass
class DrawdownCheck:
    """Whether the adaptation worsens drawdown profile."""
    max_dd_before: float = 0.0
    max_dd_after: float = 0.0
    dd_contribution: float = 0.0
    max_acceptable_dd: float = 0.15
    is_acceptable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_dd_before": round(self.max_dd_before, 4),
            "max_dd_after": round(self.max_dd_after, 4),
            "dd_contribution": round(self.dd_contribution, 4),
            "max_acceptable_dd": round(self.max_acceptable_dd, 4),
            "is_acceptable": self.is_acceptable,
        }


@dataclass
class ValidationResult:
    """Full output of the EvolutionValidationEngine."""
    adaptation_id: str = ""
    verdict: ValidationVerdict = ValidationVerdict.HOLD
    confidence: float = 0.0
    rejection_reasons: List[str] = field(default_factory=list)
    evidence: EvidenceQuality = field(default_factory=EvidenceQuality)
    robustness: RobustnessCheck = field(default_factory=RobustnessCheck)
    execution_adjusted: ExecutionAdjustedCheck = field(default_factory=ExecutionAdjustedCheck)
    fragility: FragilityCheck = field(default_factory=FragilityCheck)
    drawdown: DrawdownCheck = field(default_factory=DrawdownCheck)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adaptation_id": self.adaptation_id,
            "verdict": self.verdict.value,
            "confidence": round(self.confidence, 4),
            "rejection_reasons": self.rejection_reasons,
            "evidence": self.evidence.to_dict(),
            "robustness": self.robustness.to_dict(),
            "execution_adjusted": self.execution_adjusted.to_dict(),
            "fragility": self.fragility.to_dict(),
            "drawdown": self.drawdown.to_dict(),
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  CANDIDATE DEPLOYMENT
# ═══════════════════════════════════════════════════════════════

@dataclass
class StageRequirement:
    """Evidence threshold for a deployment stage promotion."""
    min_trades: int = 0
    min_win_rate: float = 0.0
    min_expectancy: float = 0.0
    min_sharpe: float = 0.0
    max_fragility: float = 1.0
    max_drawdown: float = 1.0
    min_ab_confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_trades": self.min_trades,
            "min_win_rate": round(self.min_win_rate, 4),
            "min_expectancy": round(self.min_expectancy, 4),
            "min_sharpe": round(self.min_sharpe, 4),
            "max_fragility": round(self.max_fragility, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "min_ab_confidence": round(self.min_ab_confidence, 4),
        }


@dataclass
class CandidateRecord:
    """A tracked adaptation candidate with its deployment state."""
    candidate_id: str = ""
    adaptation_id: str = ""
    description: str = ""
    stage: DeploymentStage = DeploymentStage.CANDIDATE
    stage_history: List[Dict[str, Any]] = field(default_factory=list)
    validation_result: Optional[ValidationResult] = None
    ab_result: Optional["ABComparisonResult"] = None
    trades_in_stage: int = 0
    stage_entered_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "adaptation_id": self.adaptation_id,
            "description": self.description,
            "stage": self.stage.value,
            "stage_history": self.stage_history,
            "validation_result": self.validation_result.to_dict() if self.validation_result else None,
            "ab_result": self.ab_result.to_dict() if self.ab_result else None,
            "trades_in_stage": self.trades_in_stage,
            "stage_entered_at": self.stage_entered_at,
            "created_at": self.created_at,
        }


# ═══════════════════════════════════════════════════════════════
#  A/B COMPARISON
# ═══════════════════════════════════════════════════════════════

@dataclass
class ABMetrics:
    """Metrics for one side of an A/B comparison."""
    trade_count: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    execution_quality: float = 0.0         # 0-1
    avg_recovery_time: float = 0.0         # bars to recover from DD
    portfolio_impact: float = 0.0          # correlation/diversification effect

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_count": self.trade_count,
            "win_rate": round(self.win_rate, 4),
            "expectancy": round(self.expectancy, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "sharpe": round(self.sharpe, 4),
            "execution_quality": round(self.execution_quality, 4),
            "avg_recovery_time": round(self.avg_recovery_time, 2),
            "portfolio_impact": round(self.portfolio_impact, 4),
        }


@dataclass
class ABComparisonResult:
    """Full output of the A/B comparison engine."""
    comparison_id: str = ""
    candidate_id: str = ""
    baseline: ABMetrics = field(default_factory=ABMetrics)
    candidate: ABMetrics = field(default_factory=ABMetrics)
    outcome: ABOutcome = ABOutcome.INCONCLUSIVE
    confidence: float = 0.0
    expectancy_delta: float = 0.0
    drawdown_delta: float = 0.0
    execution_delta: float = 0.0
    recovery_delta: float = 0.0
    portfolio_delta: float = 0.0
    is_truly_superior: bool = False
    reason: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "candidate_id": self.candidate_id,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "outcome": self.outcome.value,
            "confidence": round(self.confidence, 4),
            "expectancy_delta": round(self.expectancy_delta, 4),
            "drawdown_delta": round(self.drawdown_delta, 4),
            "execution_delta": round(self.execution_delta, 4),
            "recovery_delta": round(self.recovery_delta, 4),
            "portfolio_delta": round(self.portfolio_delta, 4),
            "is_truly_superior": self.is_truly_superior,
            "reason": self.reason,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  ROLLBACK
# ═══════════════════════════════════════════════════════════════

@dataclass
class RollbackEvent:
    """Record of a rollback action."""
    rollback_id: str = ""
    candidate_id: str = ""
    trigger: RollbackTrigger = RollbackTrigger.LIVE_DEGRADATION
    previous_stage: DeploymentStage = DeploymentStage.ACTIVE
    severity: float = 0.0                  # 0-1
    metrics_at_rollback: Dict[str, float] = field(default_factory=dict)
    reason: str = ""
    automatic: bool = True
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rollback_id": self.rollback_id,
            "candidate_id": self.candidate_id,
            "trigger": self.trigger.value,
            "previous_stage": self.previous_stage.value,
            "severity": round(self.severity, 4),
            "metrics_at_rollback": {k: round(v, 4) for k, v in self.metrics_at_rollback.items()},
            "reason": self.reason,
            "automatic": self.automatic,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  CAPITAL SCALING
# ═══════════════════════════════════════════════════════════════

@dataclass
class CapitalAllocation:
    """Capital allowed for a candidate at its current stage."""
    candidate_id: str = ""
    stage: CapitalStage = CapitalStage.SANDBOX
    capital_fraction: float = 0.0          # 0-1 of total deployable
    max_position_size: float = 0.0
    stage_history: List[Dict[str, Any]] = field(default_factory=list)
    last_evaluation: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "stage": self.stage.value,
            "capital_fraction": round(self.capital_fraction, 6),
            "max_position_size": round(self.max_position_size, 4),
            "stage_history": self.stage_history,
            "last_evaluation": self.last_evaluation,
        }


@dataclass
class ScalingDecision:
    """Output of a capital scaling evaluation."""
    candidate_id: str = ""
    previous_stage: CapitalStage = CapitalStage.SANDBOX
    new_stage: CapitalStage = CapitalStage.SANDBOX
    previous_fraction: float = 0.0
    new_fraction: float = 0.0
    reason: str = ""
    evidence_met: bool = False
    stability_met: bool = False
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "previous_stage": self.previous_stage.value,
            "new_stage": self.new_stage.value,
            "previous_fraction": round(self.previous_fraction, 6),
            "new_fraction": round(self.new_fraction, 6),
            "reason": self.reason,
            "evidence_met": self.evidence_met,
            "stability_met": self.stability_met,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  LEARNING PACE
# ═══════════════════════════════════════════════════════════════

@dataclass
class PaceAssessment:
    """Output of the LearningPaceController."""
    pace: LearningPace = LearningPace.NORMAL
    brake: LearningBrake = LearningBrake.NONE
    adaptation_multiplier: float = 1.0     # 0=frozen, 0.5=slow, 1=normal
    regime_stability: float = 1.0          # 0-1
    evidence_strength: float = 1.0         # 0-1
    execution_noise: float = 0.0           # 0-1 (1=very noisy)
    portfolio_stress: float = 0.0          # 0-1 (1=extreme stress)
    active_brakes: List[str] = field(default_factory=list)
    reason: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pace": self.pace.value,
            "brake": self.brake.value,
            "adaptation_multiplier": round(self.adaptation_multiplier, 4),
            "regime_stability": round(self.regime_stability, 4),
            "evidence_strength": round(self.evidence_strength, 4),
            "execution_noise": round(self.execution_noise, 4),
            "portfolio_stress": round(self.portfolio_stress, 4),
            "active_brakes": self.active_brakes,
            "reason": self.reason,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  AUDIT TRAIL
# ═══════════════════════════════════════════════════════════════

@dataclass
class GovernanceEvent:
    """Single audit trail entry."""
    event_id: str = ""
    event_type: GovernanceEventType = GovernanceEventType.VALIDATION
    candidate_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "candidate_id": self.candidate_id,
            "payload": self.payload,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  GOVERNANCE STATUS (aggregate)
# ═══════════════════════════════════════════════════════════════

@dataclass
class GovernanceStatus:
    """Aggregate governance layer status."""
    active_candidates: int = 0
    shadow_count: int = 0
    paper_count: int = 0
    limited_live_count: int = 0
    active_count: int = 0
    rolled_back_count: int = 0
    pending_validations: int = 0
    active_ab_tests: int = 0
    learning_pace: LearningPace = LearningPace.NORMAL
    capital_utilisation: float = 0.0       # 0-1
    recent_rollbacks: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_candidates": self.active_candidates,
            "shadow_count": self.shadow_count,
            "paper_count": self.paper_count,
            "limited_live_count": self.limited_live_count,
            "active_count": self.active_count,
            "rolled_back_count": self.rolled_back_count,
            "pending_validations": self.pending_validations,
            "active_ab_tests": self.active_ab_tests,
            "learning_pace": self.learning_pace.value,
            "capital_utilisation": round(self.capital_utilisation, 4),
            "recent_rollbacks": self.recent_rollbacks,
            "ts": self.timestamp,
        }

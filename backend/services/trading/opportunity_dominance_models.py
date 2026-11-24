"""
Opportunity Dominance Layer — Data Models
═════════════════════════════════════════

Shared enums and dataclasses for the offensive opportunity exploitation pipeline:

  OpportunityDetectionEngine, OpportunityRankingEngine, AggressionAmplifier,
  TimingPrecisionEngine, WinnerExploitationEngine, OpportunityDecayDetector,
  OpportunityCapitalAllocator.

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

class OpportunityType(str, Enum):
    LIQUIDITY_SWEEP = "liquidity_sweep"
    VOLATILITY_EXPANSION = "volatility_expansion"
    BREAKOUT_COMPRESSION = "breakout_compression"
    MOMENTUM_IGNITION = "momentum_ignition"
    IMBALANCE_ZONE = "imbalance_zone"


class TimingVerdict(str, Enum):
    ENTER = "enter"
    WAIT = "wait"
    SKIP = "skip"


class WinnerAction(str, Enum):
    SCALE_IN = "scale_in"
    HOLD = "hold"
    REDUCE = "reduce"
    EXIT = "exit"


class DecayStage(str, Enum):
    NONE = "none"
    EARLY = "early"
    PROGRESSING = "progressing"
    TERMINAL = "terminal"


class AllocationTier(str, Enum):
    MAXIMUM = "maximum"         # top asymmetry — full allocation
    ELEVATED = "elevated"       # strong edge — above-normal
    STANDARD = "standard"       # normal allocation
    REDUCED = "reduced"         # low edge — below-normal
    MINIMAL = "minimal"         # crowded / slow — minimal capital


# ═══════════════════════════════════════════════════════════════
#  OPPORTUNITY DETECTION
# ═══════════════════════════════════════════════════════════════

@dataclass
class OpportunitySignal:
    """A single detected high-asymmetry opportunity."""
    symbol: str = ""
    opportunity_type: OpportunityType = OpportunityType.IMBALANCE_ZONE
    opportunity_score: float = 0.0          # 0–1
    expected_asymmetry: float = 0.0         # risk/reward ratio (e.g. 3.0 = 3:1)
    timing_quality: float = 0.0             # 0–1
    confidence: float = 0.0                 # 0–1
    setup_family: str = ""
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "opportunity_type": self.opportunity_type.value,
            "opportunity_score": round(self.opportunity_score, 4),
            "expected_asymmetry": round(self.expected_asymmetry, 4),
            "timing_quality": round(self.timing_quality, 4),
            "confidence": round(self.confidence, 4),
            "setup_family": self.setup_family,
            "detail": self.detail,
            "ts": self.timestamp,
        }


@dataclass
class DetectionReport:
    """Aggregate detection output for one scanning cycle."""
    opportunities: List[OpportunitySignal] = field(default_factory=list)
    total_detected: int = 0
    max_score: float = 0.0
    avg_asymmetry: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "opportunities": [o.to_dict() for o in self.opportunities],
            "total_detected": self.total_detected,
            "max_score": round(self.max_score, 4),
            "avg_asymmetry": round(self.avg_asymmetry, 4),
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  OPPORTUNITY RANKING
# ═══════════════════════════════════════════════════════════════

@dataclass
class RankedOpportunity:
    """A scored and ranked opportunity candidate."""
    symbol: str = ""
    opportunity_type: OpportunityType = OpportunityType.IMBALANCE_ZONE
    composite_rank_score: float = 0.0       # 0–1, higher is better
    opportunity_score: float = 0.0
    execution_edge: float = 0.0             # execution-adjusted edge
    robustness: float = 0.0                 # statistical robustness
    regime_alignment: float = 0.0           # 0–1
    diversification_benefit: float = 0.0    # 0–1
    suppressed: bool = False
    suppression_reason: str = ""
    rank: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "opportunity_type": self.opportunity_type.value,
            "composite_rank_score": round(self.composite_rank_score, 4),
            "opportunity_score": round(self.opportunity_score, 4),
            "execution_edge": round(self.execution_edge, 4),
            "robustness": round(self.robustness, 4),
            "regime_alignment": round(self.regime_alignment, 4),
            "diversification_benefit": round(self.diversification_benefit, 4),
            "suppressed": self.suppressed,
            "suppression_reason": self.suppression_reason,
            "rank": self.rank,
            "ts": self.timestamp,
        }


@dataclass
class RankingReport:
    """Output of the ranking engine."""
    ranked: List[RankedOpportunity] = field(default_factory=list)
    top_candidates: List[RankedOpportunity] = field(default_factory=list)
    suppressed: List[RankedOpportunity] = field(default_factory=list)
    total_ranked: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ranked": [r.to_dict() for r in self.ranked],
            "top_candidates": [r.to_dict() for r in self.top_candidates],
            "suppressed": [r.to_dict() for r in self.suppressed],
            "total_ranked": self.total_ranked,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  AGGRESSION AMPLIFIER
# ═══════════════════════════════════════════════════════════════

@dataclass
class AggressionDecision:
    """Output of the AggressionAmplifier."""
    aggression_multiplier: float = 1.0      # 1.0 = normal, >1 = amplified
    size_multiplier: float = 1.0            # position size scaling
    scaling_speed: float = 1.0              # how fast to scale in (1=normal)
    tp_delay_factor: float = 1.0            # >1 = delay take-profit
    trailing_looseness: float = 1.0         # >1 = wider trailing stop
    risk_adjusted_intensity: float = 0.0    # 0–1 composite
    conditions_met: int = 0                 # how many conditions passed
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "aggression_multiplier": round(self.aggression_multiplier, 4),
            "size_multiplier": round(self.size_multiplier, 4),
            "scaling_speed": round(self.scaling_speed, 4),
            "tp_delay_factor": round(self.tp_delay_factor, 4),
            "trailing_looseness": round(self.trailing_looseness, 4),
            "risk_adjusted_intensity": round(self.risk_adjusted_intensity, 4),
            "conditions_met": self.conditions_met,
            "detail": self.detail,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  TIMING PRECISION
# ═══════════════════════════════════════════════════════════════

@dataclass
class TimingResult:
    """Output of the TimingPrecisionEngine."""
    timing_score: float = 0.0               # 0–1
    verdict: TimingVerdict = TimingVerdict.WAIT
    optimal_window: bool = False
    late_entry_risk: float = 0.0            # 0–1
    early_entry_risk: float = 0.0           # 0–1
    confirmation_strength: float = 0.0      # 0–1
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timing_score": round(self.timing_score, 4),
            "verdict": self.verdict.value,
            "optimal_window": self.optimal_window,
            "late_entry_risk": round(self.late_entry_risk, 4),
            "early_entry_risk": round(self.early_entry_risk, 4),
            "confirmation_strength": round(self.confirmation_strength, 4),
            "detail": self.detail,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  WINNER EXPLOITATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class WinnerDecision:
    """Output of the WinnerExploitationEngine."""
    action: WinnerAction = WinnerAction.HOLD
    scale_in_size: float = 0.0              # additional % to add
    trailing_stop_adjust: float = 1.0       # multiplier on current stop width
    continuation_strength: float = 0.0      # 0–1
    exhaustion_risk: float = 0.0            # 0–1
    unrealized_pnl_pct: float = 0.0
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "scale_in_size": round(self.scale_in_size, 4),
            "trailing_stop_adjust": round(self.trailing_stop_adjust, 4),
            "continuation_strength": round(self.continuation_strength, 4),
            "exhaustion_risk": round(self.exhaustion_risk, 4),
            "unrealized_pnl_pct": round(self.unrealized_pnl_pct, 4),
            "detail": self.detail,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  OPPORTUNITY DECAY
# ═══════════════════════════════════════════════════════════════

@dataclass
class DecayResult:
    """Output of the OpportunityDecayDetector."""
    decay_score: float = 0.0                # 0–1 (0=fresh, 1=dead)
    stage: DecayStage = DecayStage.NONE
    early_exit_signal: bool = False
    momentum_fading: bool = False
    volume_dropping: bool = False
    continuation_failed: bool = False
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decay_score": round(self.decay_score, 4),
            "stage": self.stage.value,
            "early_exit_signal": self.early_exit_signal,
            "momentum_fading": self.momentum_fading,
            "volume_dropping": self.volume_dropping,
            "continuation_failed": self.continuation_failed,
            "detail": self.detail,
            "ts": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  CAPITAL ALLOCATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class AllocationDecision:
    """Per-opportunity capital allocation."""
    symbol: str = ""
    tier: AllocationTier = AllocationTier.STANDARD
    capital_pct: float = 0.0                # % of available capital
    priority_rank: int = 0
    asymmetry_score: float = 0.0
    timing_score: float = 0.0
    speed_score: float = 0.0                # fastest-moving setups
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "tier": self.tier.value,
            "capital_pct": round(self.capital_pct, 4),
            "priority_rank": self.priority_rank,
            "asymmetry_score": round(self.asymmetry_score, 4),
            "timing_score": round(self.timing_score, 4),
            "speed_score": round(self.speed_score, 4),
            "detail": self.detail,
            "ts": self.timestamp,
        }


@dataclass
class CapitalPlan:
    """Aggregate capital allocation plan."""
    allocations: List[AllocationDecision] = field(default_factory=list)
    total_allocated_pct: float = 0.0
    concentrated: bool = False              # True if top-heavy allocation
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allocations": [a.to_dict() for a in self.allocations],
            "total_allocated_pct": round(self.total_allocated_pct, 4),
            "concentrated": self.concentrated,
            "detail": self.detail,
            "ts": self.timestamp,
        }

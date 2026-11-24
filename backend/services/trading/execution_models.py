"""
Execution Models — Data Structures for the Execution Reality Layer
═══════════════════════════════════════════════════════════════

Extends the trading engine with execution-quality, slippage-profile,
capacity-stress, adjusted-edge, and fill-reliability structures.
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

class FillStatus(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    MISSED = "missed"
    REJECTED = "rejected"


class ReliabilityLevel(str, Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ExecutionGateDecision(str, Enum):
    PASS = "pass"                       # Execution quality acceptable
    WARN = "warn"                       # Edge eroded but not fatal
    DOWNGRADE = "downgrade"             # Demote setup due to execution costs
    BLOCK = "block"                     # Execution kills the edge entirely


# ═══════════════════════════════════════════════════════════════
#  EXECUTION QUALITY
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExecutionRecord:
    """Single trade execution measurement."""
    setup_key: str = ""
    symbol: str = ""
    regime: str = "unknown"
    session_hour: int = 0               # 0-23 UTC
    # Price measurements
    expected_entry: float = 0.0
    realized_entry: float = 0.0
    expected_exit: float = 0.0
    realized_exit: float = 0.0
    spread_paid: float = 0.0            # total spread cost
    # Fill measurements
    intended_size: float = 0.0
    filled_size: float = 0.0
    fill_status: FillStatus = FillStatus.FULL
    # Timing
    order_latency_ms: float = 0.0       # submission → fill
    # Computed (set by analyzer)
    entry_slippage: float = 0.0
    exit_slippage: float = 0.0
    total_execution_cost: float = 0.0   # slippage + spread
    raw_pnl: float = 0.0               # before execution costs
    realized_pnl: float = 0.0          # after execution costs
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "symbol": self.symbol,
            "regime": self.regime,
            "session_hour": self.session_hour,
            "expected_entry": self.expected_entry,
            "realized_entry": self.realized_entry,
            "expected_exit": self.expected_exit,
            "realized_exit": self.realized_exit,
            "spread_paid": round(self.spread_paid, 6),
            "intended_size": self.intended_size,
            "filled_size": self.filled_size,
            "fill_status": self.fill_status.value,
            "order_latency_ms": round(self.order_latency_ms, 1),
            "entry_slippage": round(self.entry_slippage, 6),
            "exit_slippage": round(self.exit_slippage, 6),
            "total_execution_cost": round(self.total_execution_cost, 6),
            "raw_pnl": round(self.raw_pnl, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "timestamp": self.timestamp,
        }


@dataclass
class ExecutionQualityScore:
    """Per-trade or per-setup execution quality assessment."""
    setup_key: str = ""
    quality_score: float = 1.0          # 0–1 (1=perfect execution)
    entry_slip_score: float = 1.0
    exit_slip_score: float = 1.0
    spread_score: float = 1.0
    fill_score: float = 1.0
    latency_score: float = 1.0
    trade_count: int = 0
    components: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "quality_score": round(self.quality_score, 4),
            "entry_slip_score": round(self.entry_slip_score, 4),
            "exit_slip_score": round(self.exit_slip_score, 4),
            "spread_score": round(self.spread_score, 4),
            "fill_score": round(self.fill_score, 4),
            "latency_score": round(self.latency_score, 4),
            "trade_count": self.trade_count,
            "components": {k: round(v, 4) for k, v in self.components.items()},
        }


# ═══════════════════════════════════════════════════════════════
#  SLIPPAGE PROFILE
# ═══════════════════════════════════════════════════════════════

@dataclass
class SlippageBucket:
    """Aggregated slippage stats for one dimension bucket."""
    bucket_key: str = ""                # e.g. "BTC/USD", "trending_up", "hour_14"
    dimension: str = ""                 # "symbol", "setup", "regime", "hour", "volatility"
    trade_count: int = 0
    avg_entry_slip: float = 0.0
    avg_exit_slip: float = 0.0
    avg_spread: float = 0.0
    avg_total_cost: float = 0.0
    max_slip: float = 0.0
    cost_as_pct_of_edge: float = 0.0   # execution cost / raw expectancy

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bucket_key": self.bucket_key,
            "dimension": self.dimension,
            "trade_count": self.trade_count,
            "avg_entry_slip": round(self.avg_entry_slip, 6),
            "avg_exit_slip": round(self.avg_exit_slip, 6),
            "avg_spread": round(self.avg_spread, 6),
            "avg_total_cost": round(self.avg_total_cost, 6),
            "max_slip": round(self.max_slip, 6),
            "cost_as_pct_of_edge": round(self.cost_as_pct_of_edge, 4),
        }


@dataclass
class SlippageProfile:
    """Full slippage profile for a setup across all dimensions."""
    setup_key: str = ""
    by_symbol: Dict[str, SlippageBucket] = field(default_factory=dict)
    by_regime: Dict[str, SlippageBucket] = field(default_factory=dict)
    by_hour: Dict[str, SlippageBucket] = field(default_factory=dict)
    by_volatility: Dict[str, SlippageBucket] = field(default_factory=dict)
    overall: Optional[SlippageBucket] = None
    execution_kills_edge: bool = False  # True if avg cost > raw expectancy

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "overall": self.overall.to_dict() if self.overall else {},
            "by_symbol": {k: v.to_dict() for k, v in self.by_symbol.items()},
            "by_regime": {k: v.to_dict() for k, v in self.by_regime.items()},
            "by_hour": {k: v.to_dict() for k, v in self.by_hour.items()},
            "by_volatility": {k: v.to_dict() for k, v in self.by_volatility.items()},
            "execution_kills_edge": self.execution_kills_edge,
        }


# ═══════════════════════════════════════════════════════════════
#  CAPACITY STRESS
# ═══════════════════════════════════════════════════════════════

@dataclass
class CapacityBucket:
    """Performance at a given size bucket."""
    size_label: str = ""                # "xs", "s", "m", "l", "xl"
    size_min: float = 0.0
    size_max: float = 0.0
    trade_count: int = 0
    avg_pnl: float = 0.0
    avg_slippage: float = 0.0
    avg_fill_ratio: float = 1.0         # filled / intended
    edge_retained_pct: float = 1.0      # pnl_at_size / pnl_at_base_size

    def to_dict(self) -> Dict[str, Any]:
        return {
            "size_label": self.size_label,
            "size_range": [self.size_min, self.size_max],
            "trade_count": self.trade_count,
            "avg_pnl": round(self.avg_pnl, 4),
            "avg_slippage": round(self.avg_slippage, 6),
            "avg_fill_ratio": round(self.avg_fill_ratio, 4),
            "edge_retained_pct": round(self.edge_retained_pct, 4),
        }


@dataclass
class CapacityReport:
    """Output of CapacityStressAnalyzer."""
    setup_key: str = ""
    buckets: List[CapacityBucket] = field(default_factory=list)
    recommended_max_size: float = 0.0
    capacity_score: float = 1.0         # 0–1 (1=scales perfectly)
    degradation_onset: str = ""         # size label where degradation starts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "buckets": [b.to_dict() for b in self.buckets],
            "recommended_max_size": round(self.recommended_max_size, 4),
            "capacity_score": round(self.capacity_score, 4),
            "degradation_onset": self.degradation_onset,
        }


# ═══════════════════════════════════════════════════════════════
#  EXECUTION-ADJUSTED EDGE
# ═══════════════════════════════════════════════════════════════

@dataclass
class AdjustedEdgeReport:
    """Output of ExecutionAdjustedEdgeEngine."""
    setup_key: str = ""
    raw_expectancy: float = 0.0
    avg_execution_cost: float = 0.0
    execution_failure_rate: float = 0.0  # missed + rejected / total
    adjusted_expectancy: float = 0.0
    edge_degradation: float = 0.0        # 0=no degradation, 1=edge fully consumed
    edge_positive: bool = True           # is adjusted edge still > 0?
    trade_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "raw_expectancy": round(self.raw_expectancy, 4),
            "avg_execution_cost": round(self.avg_execution_cost, 6),
            "execution_failure_rate": round(self.execution_failure_rate, 4),
            "adjusted_expectancy": round(self.adjusted_expectancy, 4),
            "edge_degradation": round(self.edge_degradation, 4),
            "edge_positive": self.edge_positive,
            "trade_count": self.trade_count,
        }


# ═══════════════════════════════════════════════════════════════
#  FILL RELIABILITY
# ═══════════════════════════════════════════════════════════════

@dataclass
class ReliabilityReport:
    """Output of FillReliabilityMonitor."""
    key: str = ""                       # setup_key or symbol
    level: ReliabilityLevel = ReliabilityLevel.HEALTHY
    total_orders: int = 0
    full_fills: int = 0
    partial_fills: int = 0
    missed_fills: int = 0
    rejected_orders: int = 0
    avg_latency_ms: float = 0.0
    partial_rate: float = 0.0
    failure_rate: float = 0.0
    latency_p95: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "level": self.level.value,
            "total_orders": self.total_orders,
            "full_fills": self.full_fills,
            "partial_fills": self.partial_fills,
            "missed_fills": self.missed_fills,
            "rejected_orders": self.rejected_orders,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "partial_rate": round(self.partial_rate, 4),
            "failure_rate": round(self.failure_rate, 4),
            "latency_p95": round(self.latency_p95, 1),
        }


# ═══════════════════════════════════════════════════════════════
#  EXECUTION REALITY GATE
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExecutionGateVerdict:
    """Output of ExecutionRealityGate."""
    setup_key: str = ""
    decision: ExecutionGateDecision = ExecutionGateDecision.PASS
    execution_quality: float = 1.0
    edge_degradation: float = 0.0
    reliability_level: str = "healthy"
    capacity_ok: bool = True
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "decision": self.decision.value,
            "execution_quality": round(self.execution_quality, 4),
            "edge_degradation": round(self.edge_degradation, 4),
            "reliability_level": self.reliability_level,
            "capacity_ok": self.capacity_ok,
            "reason": self.reason,
        }

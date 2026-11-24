from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, Optional
import time

class AllocationAction(Enum):
    UP = "up"
    DOWN = "down"
    HOLD = "hold"
    ROLLBACK = "rollback"

class AllocationRegime(Enum):
    NORMAL = "NORMAL"
    HIGH_VOL = "HIGH_VOL"
    STRESS = "STRESS"
    FROZEN = "FROZEN"

@dataclass
class ScaleReadinessSnapshot:
    symbol: str
    status: str  # GREEN, AMBER, RED
    stale_rate: float
    sigma: float
    orphan_total: int
    late_fill_total: int
    rest_rtt_p95: float
    ack_to_ws_p95: float
    end_to_end_p95: float
    rest_rtt_p99: float = 0.0
    ack_to_ws_p99: float = 0.0
    end_to_end_p99: float = 0.0
    pearson: float = 0.0
    spearman: float = 0.0
    huber_slope: float = 0.0
    huber_r2: float = 0.0
    timestamp: float = field(default_factory=time.time)

@dataclass
class AllocationDecision:
    target_capital_eur: float
    current_capital_eur: float
    action: AllocationAction
    reason: str
    regime: AllocationRegime
    cooldown_until: float = 0.0
    snapshot_hash: str = ""
    timestamp: float = field(default_factory=time.time)

@dataclass
class RiskBudgetModel:
    base_capital: float
    max_capital: float
    max_drawdown_pct: float = 10.0
    current_drawdown_pct: float = 0.0
    
    def is_scaling_allowed(self) -> bool:
        return self.current_drawdown_pct < self.max_drawdown_pct

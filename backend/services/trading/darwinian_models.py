"""
Darwinian Models — Data structures for the Setup Selection Layer
═══════════════════════════════════════════════════════════════

Extends the Adaptive Alpha Layer with lifecycle, survival, allocation,
and regime–setup matrix structures.  Never touches existing files.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
#  ENUMS
# ═══════════════════════════════════════════════════════════════

class SetupState(str, Enum):
    EMERGING = "emerging"       # New or recently reset — being evaluated
    ACTIVE = "active"           # Performing within expectations
    DEGRADED = "degraded"       # Performance declining — throttled
    BLOCKED = "blocked"         # Statistical evidence of no edge — no trades


class SurvivalRecommendation(str, Enum):
    BOOST = "boost"             # Increase allocation
    KEEP = "keep"               # Maintain current allocation
    REDUCE = "reduce"           # Decrease allocation
    BLOCK = "block"             # Stop trading this setup


# ═══════════════════════════════════════════════════════════════
#  SETUP IDENTITY — canonical key for a setup
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SetupKey:
    """Uniquely identifies a setup type (direction + strength + symbol)."""
    direction: str          # "long" / "short"
    strength: str           # "strong" / "moderate" / "weak"
    symbol: str = "*"       # "*" = all symbols

    def __str__(self) -> str:
        return f"{self.direction}_{self.strength}_{self.symbol}"

    def to_dict(self) -> Dict[str, str]:
        return {"direction": self.direction, "strength": self.strength, "symbol": self.symbol}


# ═══════════════════════════════════════════════════════════════
#  SETUP LIFECYCLE
# ═══════════════════════════════════════════════════════════════

@dataclass
class SetupProfile:
    """Full profile of a setup tracked over its lifecycle."""
    key: SetupKey = field(default_factory=lambda: SetupKey("", ""))
    state: SetupState = SetupState.EMERGING
    created_at: float = field(default_factory=time.time)
    last_trade_at: float = 0.0
    last_transition_at: float = field(default_factory=time.time)

    # Rolling statistics
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0

    # Computed (refreshed on each update)
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0          # avg pnl per trade
    drawdown_contribution: float = 0.0  # how much this setup adds to DD

    # Lifecycle tracking
    state_history: List[Dict[str, Any]] = field(default_factory=list)
    consecutive_state_bars: int = 0   # how long in current state

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": str(self.key),
            "state": self.state.value,
            "created_at": self.created_at,
            "last_trade_at": self.last_trade_at,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 2),
            "expectancy": round(self.expectancy, 2),
            "net_pnl": round(self.net_pnl, 2),
            "drawdown_contribution": round(self.drawdown_contribution, 2),
            "consecutive_state_bars": self.consecutive_state_bars,
            "state_history": self.state_history[-10:],
        }


# ═══════════════════════════════════════════════════════════════
#  SURVIVAL SCORING
# ═══════════════════════════════════════════════════════════════

@dataclass
class SurvivalVerdict:
    """Output of SetupSurvivalScorer."""
    score: float = 0.5               # 0–1
    recommendation: SurvivalRecommendation = SurvivalRecommendation.KEEP
    components: Dict[str, float] = field(default_factory=dict)
    reason: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "recommendation": self.recommendation.value,
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════
#  EDGE ALLOCATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class AllocationDecision:
    """Output of EdgeAllocator — per-setup capital allocation."""
    setup_key: str = ""
    allocation_pct: float = 1.0      # multiplier on base size (0–2.0)
    max_concurrent: int = 3
    reason: str = ""
    survival_score: float = 0.0
    setup_state: str = "active"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_key": self.setup_key,
            "allocation_pct": round(self.allocation_pct, 3),
            "max_concurrent": self.max_concurrent,
            "reason": self.reason,
            "survival_score": round(self.survival_score, 3),
            "setup_state": self.setup_state,
        }


# ═══════════════════════════════════════════════════════════════
#  REGIME × SETUP MATRIX
# ═══════════════════════════════════════════════════════════════

@dataclass
class RegimeSetupCell:
    """Performance record for one (regime, setup) pair."""
    regime: str = ""
    setup_key: str = ""
    trade_count: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    net_pnl: float = 0.0
    expectancy: float = 0.0
    allowed: bool = True             # False = hard blocked

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime,
            "setup_key": self.setup_key,
            "trade_count": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 2),
            "net_pnl": round(self.net_pnl, 2),
            "allowed": self.allowed,
        }


# ═══════════════════════════════════════════════════════════════
#  POLICY DECAY
# ═══════════════════════════════════════════════════════════════

@dataclass
class DecayReport:
    """Output of PolicyDecayGuard — degradation analysis."""
    decay_severity: int = 0           # 0–3
    override_size_mult: float = 1.0
    override_conf_threshold: float = 0.0
    override_max_concurrent: int = 0  # delta (0 = no change, -1 = reduce by 1)
    hard_block: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decay_severity": self.decay_severity,
            "override_size_mult": round(self.override_size_mult, 3),
            "override_conf_threshold": round(self.override_conf_threshold, 3),
            "override_max_concurrent": self.override_max_concurrent,
            "hard_block": self.hard_block,
            "reason": self.reason,
        }


# ═══════════════════════════════════════════════════════════════
#  PERSISTENCE
# ═══════════════════════════════════════════════════════════════

@dataclass
class StorageRecord:
    """Generic wrapper for persistable events."""
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    record_type: str = ""            # "lifecycle", "survival", "allocation", "matrix", "decay"
    key: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "record_type": self.record_type,
            "key": self.key,
            "data": self.data,
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
        }
